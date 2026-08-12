from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .deployment_config import can_edit_task, can_retry_task
from .models import DeploymentTask, TaskStatus
from .runtime_inventory import ComposeProject, RuntimeOverview
from .image_inventory import ImageContainerReference, ImageSummary


STATUS_LABELS = {
    TaskStatus.UPLOADED: "已上传",
    TaskStatus.EXTRACTING: "正在解压",
    TaskStatus.PENDING_REVIEW: "待审核",
    TaskStatus.DEPLOYING: "部署中",
    TaskStatus.DEPLOYED: "已部署",
    TaskStatus.DISCARDED: "已丢弃",
    TaskStatus.FAILED: "失败",
}


def task_view(task: DeploymentTask) -> dict[str, Any]:
    return {
        "task": task,
        "status_value": task.status.value,
        "status_label": STATUS_LABELS[task.status],
        "created_at": _format_time(task.created_at),
        "updated_at": _format_time(task.updated_at),
        "edited_at": _format_time(task.edited_at),
        "editable": can_edit_task(task),
        "retryable": can_retry_task(task),
    }


def container_view(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item": item,
        "name": item.get("name") or item.get("short_id") or "未知容器",
        "image": item.get("image") or "—",
        "running": bool(item.get("running")),
        "status_label": "运行中" if item.get("running") else str(item.get("status") or "已停止"),
        "ports_text": _format_ports(item.get("ports")),
        "compose_service": item.get("compose_service") or "—",
    }


def compose_project_view(project: ComposeProject) -> dict[str, Any]:
    return {
        "project": project,
        "status_label": project.status,
        "running": project.running,
        "container_count": project.container_count,
        "running_containers": project.running_containers,
        "containers": [container_view(item) for item in project.containers],
    }


def image_summary_view(item: ImageSummary) -> dict[str, Any]:
    return {
        "item": item,
        "tags": item.tags or ("未标记",),
        "created": _format_docker_time(item.created),
        "size": _format_bytes(item.size),
        "entrypoint": _format_command(item.entrypoint),
        "command": _format_command(item.command),
    }


def image_reference_view(item: ImageContainerReference) -> dict[str, Any]:
    container_id = quote(item.id, safe="")
    if item.compose_project:
        project = quote(item.compose_project, safe="")
        href = f"/compose-projects/{project}?container={container_id}"
    else:
        href = f"/containers/{container_id}"
    return {"item": item, "href": href}


def runtime_metrics(
    tasks: Sequence[DeploymentTask],
    overview: RuntimeOverview,
) -> dict[str, int]:
    containers = [
        item
        for project in overview.compose_projects
        for item in project.containers
    ] + list(overview.standalone_containers)
    return {
        "compose_projects": len(overview.compose_projects),
        "standalone_containers": len(overview.standalone_containers),
        "containers": len(containers),
        "running": sum(bool(item.get("running")) for item in containers),
        "failed": sum(task.status is TaskStatus.FAILED for task in tasks),
    }


def _format_time(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _format_ports(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "—"
    rendered = []
    for container_port, bindings in sorted(value.items()):
        if not bindings:
            rendered.append(str(container_port))
            continue
        hosts = ", ".join(
            f"{item.get('HostIp') or '0.0.0.0'}:{item.get('HostPort')}"
            for item in bindings
            if isinstance(item, dict)
        )
        rendered.append(f"{hosts} → {container_port}")
    return "; ".join(rendered)


def _format_docker_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_bytes(value: int) -> str:
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            rendered = (
                f"{amount:.1f}" if unit != "B" else str(int(amount))
            )
            return f"{rendered} {unit}"
        amount /= 1024
    return f"{value} B"


def _format_command(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value) or "—"
    return str(value) if value not in (None, "") else "—"
