"""Meta Model API client for InfraScan 3D.

Talks to ``https://api.meta.ai/v1`` through the documented OpenAI-compatible Responses API
(``POST /responses`` with ``model``, an ``input`` message holding ``input_text`` plus
``input_image``/``input_video``, and ``metadata.mask_encoding`` / ``include_confidence``).

Every failure leaves this module as a classified :class:`infrascan.errors.ProviderError`, so
callers can tell an authentication problem from a rate limit from an outage. When no
``MODEL_API_KEY`` is configured nothing here is called: the pipeline uses
:mod:`infrascan.simulation` and labels that output as simulated.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import random
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .errors import ProviderError, classify, from_transport, redact

# Documented transient statuses only; 409/425 are not part of the published error contract.
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_BASE = 0.6
BACKOFF_CAP = 8.0


def file_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter, so retries do not synchronise into a spike."""
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
    return round(ceiling / 2 + random.uniform(0, ceiling / 2), 3)


class MetaClient:
    """Async wrapper over the Responses API: bounded concurrency, jittered retries, typed errors."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._slots = asyncio.Semaphore(max(1, settings.max_concurrency))

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.base_url,
                timeout=settings.request_timeout,
                headers={"Authorization": f"Bearer {settings.api_key}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _raise(self, response: httpx.Response) -> None:
        detail = _error_envelope(response)
        kind, retryable = classify(response.status_code, detail["message"], detail["code"])
        raise ProviderError(
            kind,
            redact(f"model API returned {response.status_code}: {detail['message']}"),
            status=response.status_code,
            code=detail["code"],
            retryable=retryable,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        http = await self._http()
        last: ProviderError | None = None
        async with self._slots:
            for attempt in range(MAX_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(backoff_delay(attempt - 1))
                try:
                    response = await http.request(method, path, **kwargs)
                except httpx.HTTPError as exc:
                    last = from_transport(exc)
                    continue
                if response.status_code in RETRY_STATUS:
                    detail = _error_envelope(response)
                    last = ProviderError(
                        "unavailable" if response.status_code >= 500 else "rate_limited",
                        redact(f"model API returned {response.status_code}: {detail['message']}"),
                        status=response.status_code,
                        code=detail["code"],
                        retryable=True,
                    )
                    continue
                if response.is_error:
                    self._raise(response)
                return response
        raise last or ProviderError("network", "model API unreachable after retries", retryable=True)

    async def _post_json(self, payload: dict[str, Any]) -> Any:
        return (await self._request("POST", "/responses", json=payload,
                                    headers={"Accept": "application/json"})).json()

    async def _stream_text(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        """Yield ``response.output_text.delta`` payloads; raises ProviderError before streaming."""
        http = await self._http()
        async with self._slots:
            try:
                async with http.stream(
                    "POST", "/responses", json=payload,
                    headers={"Accept": "text/event-stream"},
                    timeout=settings.request_timeout,
                ) as response:
                    if response.is_error:
                        body = (await response.aread()).decode("utf-8", "replace")
                        kind, retryable = classify(response.status_code, body, "")
                        raise ProviderError(kind, redact(f"stream failed: {body[:300]}"),
                                            status=response.status_code, retryable=retryable)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "response.output_text.delta" and event.get("delta"):
                            yield event["delta"]
                        elif event.get("type") == "response.error":
                            detail = (event.get("response") or {}).get("error") or {}
                            raise ProviderError("invalid_response",
                                                redact(str(detail.get("message") or "streamed error")))
            except httpx.HTTPError as exc:
                raise from_transport(exc) from exc

    async def responses_text(
        self,
        *,
        model: str,
        content: list[dict[str, Any]],
        stream: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "input": [{"type": "message", "role": "user", "content": content}],
        }
        if metadata:
            payload["metadata"] = metadata
        if stream:
            return "".join([chunk async for chunk in self._stream_text({**payload, "stream": True})])
        return _extract_output_text(await self._post_json(payload))

    async def segment_image(
        self,
        concept: str,
        image_ref: str,
        *,
        mask_encoding: str = "one_bit",
        include_confidence: bool = True,
    ) -> str:
        """Segment one concept in one still; returns the raw ``output_text`` lane."""
        metadata = {"mask_encoding": mask_encoding}
        if include_confidence:
            metadata["include_confidence"] = "true"
        return await self.responses_text(
            model=settings.sam_model,
            content=[
                {"type": "input_text", "text": concept},
                {"type": "input_image", "image_url": image_ref},
            ],
            metadata=metadata,
        )

    async def segment_video(self, concept: str, file_id: str) -> AsyncIterator[str]:
        """Segment a clip uploaded through the Files API, streaming one line per frame."""
        content = [
            {"type": "input_text", "text": concept},
            {"type": "input_video", "file_id": file_id},
        ]
        payload = {"model": settings.sam_model,
                   "input": [{"type": "message", "role": "user", "content": content}],
                   "stream": True}
        async for chunk in self._stream_text(payload):
            yield chunk

    async def upload_file(self, path: Path) -> str:
        with path.open("rb") as handle:
            response = await self._request(
                "POST", "/files",
                data={"purpose": "user_data"},
                files={"file": (path.name, handle)},
                timeout=settings.request_timeout * 2,
            )
        body = response.json()
        file_id = body.get("id")
        if not file_id:
            raise ProviderError("invalid_response", "files API returned no id")
        return str(file_id)

    async def reason(self, prompt: str) -> dict[str, Any]:
        """Muse Spark contextual reasoning; returns a parsed JSON object."""
        text = await self.responses_text(
            model=settings.spark_model,
            content=[{"type": "input_text", "text": prompt}],
        )
        parsed = _parse_json_block(text)
        if not parsed or "raw" in parsed:
            raise ProviderError("invalid_response",
                                "reasoning model did not return the documented JSON shape")
        return parsed

    async def generate_code(self, prompt: str) -> str:
        """Muse Spark used as the code/artifact generator behind the Action Agent."""
        return await self.responses_text(
            model=settings.spark_model,
            content=[{"type": "input_text", "text": prompt}],
        )

    async def list_models(self) -> list[str]:
        body = (await self._request("GET", "/models", headers={"Accept": "application/json"})).json()
        return sorted(str(item.get("id")) for item in body.get("data", []) if item.get("id"))

    async def check_connectivity(self) -> dict[str, Any]:
        """Tiny, cheap real request used by the provider health panel. Never leaks the key."""
        if not settings.api_key:
            return {
                "ok": False,
                "status_code": 0,
                "latency_ms": 0,
                "kind": "not_configured",
                "message": "no MODEL_API_KEY on the server, so no request was sent — connectivity is unverified",
            }
        started = asyncio.get_running_loop().time()
        try:
            models = await self.list_models()
            return {
                "ok": True,
                "status_code": 200,
                "latency_ms": round((asyncio.get_running_loop().time() - started) * 1000),
                "models": models,
                "message": f"reached {settings.base_url}; {len(models)} model ids available",
            }
        except ProviderError as error:
            return {
                "ok": False,
                "status_code": error.status_source or 0,
                "latency_ms": round((asyncio.get_running_loop().time() - started) * 1000),
                "kind": error.kind,
                "message": error.message,
            }


def _error_envelope(response: httpx.Response) -> dict[str, str]:
    try:
        error = (response.json() or {}).get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    return {
        "message": str(error.get("message") or response.text[:300] or "unknown error"),
        "code": str(error.get("code") or ""),
    }


def _extract_output_text(body: dict[str, Any]) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    chunks: list[str] = []
    for item in body.get("output", []) or []:
        for part in item.get("content", []) or []:
            if part.get("type") in ("output_text", "text"):
                chunks.append(part.get("text", ""))
    return "".join(chunks)


_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)


def _parse_json_block(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*|```$", "", text, flags=re.MULTILINE).strip()
    candidates = [text]
    match = _JSON_RE.search(text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        return parsed if isinstance(parsed, dict) else {"items": parsed}
    return {"raw": text}


client = MetaClient()
