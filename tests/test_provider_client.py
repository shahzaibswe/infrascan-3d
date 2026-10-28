"""Meta Model API adapter tests: documented payload shape, retry policy and error taxonomy.

Every test here uses a mocked transport; the live smoke test is skipped with a stated
reason when no MODEL_API_KEY is configured, so nothing in this file can claim a
connectivity result the machine did not actually obtain.
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from infrascan.config import settings
from infrascan.errors import ProviderError, classify, redact
from infrascan.meta_client import MetaClient, backoff_delay


def _client(handler) -> tuple[MetaClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    client = MetaClient()
    client._client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(_record(handler, seen)),
        headers={"Authorization": "Bearer test-key"},
    )
    return client, seen


def _record(handler, seen: list[httpx.Request]):
    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return wrapped


def test_classify_matches_the_documented_error_codes() -> None:
    assert classify(401, "", "invalid_api_key") == ("authentication", False)
    assert classify(403, "", "permission_error") == ("permission", False)
    assert classify(404, "The model 'muse-x' does not exist", "model_not_found") == ("unknown_model", False)
    assert classify(429, "slow down", "rate_limit_exceeded") == ("rate_limited", True)
    assert classify(400, "bad request") == ("invalid_request", False)
    assert classify(503, "busy") == ("unavailable", True)
    assert classify(None, "") == ("unknown", False)


def test_backoff_is_bounded_and_jittered() -> None:
    delays = {backoff_delay(attempt) for attempt in range(6)}
    assert len(delays) > 1, "full jitter must vary"
    assert max(delays) <= 8.0
    assert min(delays) >= 0.0


def test_401_is_not_retried_and_surfaces_a_typed_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API Key",
                                                   "type": "authentication_error",
                                                   "code": "invalid_api_key"}})

    client, seen = _client(handler)
    with pytest.raises(ProviderError) as captured:
        asyncio.run(client.responses_text(model="muse-spark-1.3",
                                          content=[{"type": "input_text", "text": "hi"}]))
    error = captured.value
    assert error.kind == "authentication" and error.retryable is False
    assert error.status == 401
    assert len(seen) == 1, "an authentication failure must not be retried"
    assert "Invalid API Key" in error.message


def test_5xx_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    client, _seen = _client(handler)
    text = asyncio.run(client.responses_text(model="muse-spark-1.3",
                                            content=[{"type": "input_text", "text": "hi"}]))
    assert text == "ok" and calls["n"] == 3


def test_segment_image_uses_the_documented_request_shape() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=9;h=9|>"
                                              "<|mask;x=0;y=0;data=2,2,~aa|>"})

    client, seen = _client(handler)
    lane = asyncio.run(client.segment_image("road pothole", "data:image/jpeg;base64,AAA"))
    body = json.loads(seen[0].content)
    assert seen[0].url.path == f"{httpx.URL(settings.base_url).path}/responses"
    assert body["model"] == settings.sam_model
    assert body["input"][0]["content"][0] == {"type": "input_text", "text": "road pothole"}
    assert body["input"][0]["content"][1]["type"] == "input_image"
    assert body["metadata"] == {"mask_encoding": "one_bit", "include_confidence": "true"}
    assert lane.startswith("<0f>")


def test_reason_rejects_a_non_json_answer_instead_of_inventing_one() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": "I could not analyse that."})

    client, _seen = _client(handler)
    with pytest.raises(ProviderError) as captured:
        asyncio.run(client.reason("analyse"))
    assert captured.value.kind == "invalid_response"


def test_reason_parses_a_fenced_json_block() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": "```json\n{\"defects\": []}\n```"})

    client, _seen = _client(handler)
    assert asyncio.run(client.reason("analyse")) == {"defects": []}


def test_transport_timeout_is_classified_as_a_retryable_provider_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client, _seen = _client(handler)
    with pytest.raises(ProviderError) as captured:
        asyncio.run(client.list_models())
    assert captured.value.kind == "timeout" and captured.value.retryable


def test_error_messages_never_carry_the_key() -> None:
    leaked = redact("Authorization: Bearer sk-SUPERSECRET123 and MODEL_API_KEY=sk-SUPERSECRET123")
    assert "SUPERSECRET" not in leaked and "sk-SUPERSECRET123" not in leaked
    assert "SUPERSECRET" not in redact("request failed, key=SUPERSECRET123 value dropped")
    # the variable *name* in honest setup prose must survive, or the UI cannot explain how to go live
    assert redact("no MODEL_API_KEY on the server, so no request was sent") == (
        "no MODEL_API_KEY on the server, so no request was sent")


def test_files_upload_posts_the_documented_purpose() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{httpx.URL(settings.base_url).path}/files"
        assert b'user_data' in request.read()
        return httpx.Response(200, json={"id": "file-42"})

    client, _seen = _client(handler)
    from pathlib import Path

    path = Path("tests/conftest.py")
    assert asyncio.run(client.upload_file(path)) == "file-42"


@pytest.mark.skipif(not os.getenv("MODEL_API_KEY"),
                    reason="no MODEL_API_KEY in the environment, so real connectivity cannot be verified here")
def test_live_smoke_when_a_key_is_present() -> None:  # pragma: no cover - needs credentials
    client = MetaClient()
    report = asyncio.run(client.check_connectivity())
    assert report["ok"], report["message"]
    assert settings.sam_model in report["models"] or report["models"], report
