from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(str, Enum):
    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    PENDING_REVIEW = "pending_review"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    DISCARDED = "discarded"
    FAILED = "failed"


class DeploymentTask(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    task_id: str
    status: TaskStatus
    original_filename: str
    package_dir: Path
    extracted_dir: Path
    deployment_dir: Path | None = None
    app_name: str | None = None
    error: str | None = None
    command_output: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class FileEntry(BaseModel):
    path: str
    kind: str
    size: int = Field(ge=0)
