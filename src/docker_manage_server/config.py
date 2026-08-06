from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("DATA_DIR", "/app/data"))
    )
    docker_host: str | None = field(default_factory=lambda: os.getenv("DOCKER_HOST"))
    compose_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("COMPOSE_TIMEOUT_SECONDS", "1800"))
    )


def get_settings() -> Settings:
    return Settings()
