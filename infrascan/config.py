"""Runtime configuration for InfraScan 3D."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

BASE_DIR = Path(os.getenv("INFRASCAN_DATA_DIR", ROOT / "data")).resolve()
KNOWLEDGE_DIR = Path(os.getenv("INFRASCAN_KNOWLEDGE_DIR", ROOT / "knowledge")).resolve()
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


@dataclass(slots=True)
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv("MODEL_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("INFRASCAN_BASE_URL", "https://api.meta.ai/v1"))
    sam_model: str = field(default_factory=lambda: os.getenv("INFRASCAN_SAM_MODEL", "sam-3.1"))
    spark_model: str = field(default_factory=lambda: os.getenv("INFRASCAN_SPARK_MODEL", "muse-spark-1.3"))
    force_mock: bool = field(default_factory=lambda: os.getenv("INFRASCAN_MODE", "").lower() == "mock")
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("INFRASCAN_MAX_UPLOAD_MB", "20")))
    max_frames: int = field(default_factory=lambda: int(os.getenv("INFRASCAN_MAX_FRAMES", "16")))
    max_concurrency: int = field(default_factory=lambda: int(os.getenv("INFRASCAN_MAX_CONCURRENCY", "4")))
    retention_days: int = field(default_factory=lambda: int(os.getenv("INFRASCAN_RETENTION_DAYS", "14")))
    request_timeout: float = field(default_factory=lambda: float(os.getenv("INFRASCAN_TIMEOUT", "180")))

    @property
    def live(self) -> bool:
        return bool(self.api_key) and not self.force_mock

    @property
    def mode(self) -> str:
        return "live" if self.live else "mock"


settings = Settings()
