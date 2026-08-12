from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .docker_runtime import (
    DockerRuntime,
    DockerRuntimeError,
    ImageNotFoundError,
)
from .runtime_inventory import COMPOSE_PROJECT_LABEL, COMPOSE_SERVICE_LABEL


PAGE_SIZE = 20


class InvalidImagePageError(ValueError):
    pass


class ImageInUseError(RuntimeError):
    def __init__(
        self,
        containers: tuple[ImageContainerReference, ...],
    ):
        self.containers = containers
        names = "、".join(item.name for item in containers)
        super().__init__(f"镜像正在被以下容器使用：{names}")


@dataclass(frozen=True)
class ImageSummary:
    id: str
    short_id: str
    tags: tuple[str, ...]
    digests: tuple[str, ...]
    created: str | None
    size: int
    architecture: str | None
    os: str | None
    entrypoint: Any
    command: Any
    container_count: int = 0


@dataclass(frozen=True)
class ImageContainerReference:
    id: str
    name: str
    status: str
    running: bool
    compose_project: str | None
    compose_service: str | None


@dataclass(frozen=True)
class ImagePage:
    items: tuple[ImageSummary, ...]
    query: str
    page: int
    page_size: int
    total_items: int
    total_pages: int


@dataclass(frozen=True)
class ImageDetail:
    summary: ImageSummary
    inspect: dict[str, Any]
    containers: tuple[ImageContainerReference, ...]


class ImageInventoryService:
    def __init__(self, runtime: DockerRuntime):
        self.runtime = runtime

    def list(
        self,
        query: str = "",
        page: int | str = 1,
    ) -> ImagePage:
        page_number = _page_number(page)
        normalized_query = query.strip()
        containers = self.runtime.list_containers()
        reference_counts: dict[str, int] = {}
        for container in containers:
            image_id = str(container.get("image_id") or "")
            if image_id:
                reference_counts[image_id] = (
                    reference_counts.get(image_id, 0) + 1
                )
        summaries = _summaries(
            self.runtime.list_images(),
            reference_counts,
        )
        if normalized_query:
            needle = normalized_query.casefold()
            summaries = tuple(
                item for item in summaries if _matches(item, needle)
            )
        total = len(summaries)
        start = (page_number - 1) * PAGE_SIZE
        return ImagePage(
            items=summaries[start : start + PAGE_SIZE],
            query=normalized_query,
            page=page_number,
            page_size=PAGE_SIZE,
            total_items=total,
            total_pages=(total + PAGE_SIZE - 1) // PAGE_SIZE,
        )

    def get(self, image_id: str) -> ImageDetail:
        item = self.runtime.get_serialized_image(image_id)
        summary = _summary(item)
        return ImageDetail(
            summary=summary,
            inspect=dict(item.get("raw_attrs") or {}),
            containers=self._references(summary.id),
        )

    def remove(self, image_id: str) -> dict[str, Any]:
        detail = self.get(image_id)
        if detail.containers:
            raise ImageInUseError(detail.containers)
        immutable_id = detail.summary.id
        tags = list(detail.summary.tags)
        for tag in tags:
            current = self.runtime.get_serialized_image(tag)
            if str(current.get("id") or "") != immutable_id:
                raise DockerRuntimeError(
                    f"镜像 Tag 已发生变化，已停止删除：{tag}"
                )
            self.runtime.remove_image(tag)
        try:
            self.runtime.remove_image(immutable_id)
        except ImageNotFoundError:
            if not tags:
                raise
        return {"id": immutable_id, "tags": tags}

    def _references(
        self,
        immutable_image_id: str,
    ) -> tuple[ImageContainerReference, ...]:
        references = []
        for item in self.runtime.list_containers():
            if item.get("image_id") != immutable_image_id:
                continue
            labels = _labels(item)
            references.append(
                ImageContainerReference(
                    id=str(item["id"]),
                    name=str(item.get("name") or item["id"]),
                    status=str(item.get("status") or "unknown"),
                    running=bool(item.get("running")),
                    compose_project=_optional_text(
                        labels.get(COMPOSE_PROJECT_LABEL)
                    ),
                    compose_service=_optional_text(
                        labels.get(COMPOSE_SERVICE_LABEL)
                    ),
                )
            )
        return tuple(
            sorted(
                references,
                key=lambda item: (item.name.casefold(), item.id),
            )
        )


def _page_number(value: int | str) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidImagePageError("页码必须是大于等于 1 的整数") from exc
    if isinstance(value, str) and str(page) != value.strip():
        raise InvalidImagePageError("页码必须是大于等于 1 的整数")
    if page < 1:
        raise InvalidImagePageError("页码必须是大于等于 1 的整数")
    return page


def _summaries(
    items: list[dict[str, Any]],
    reference_counts: dict[str, int] | None = None,
) -> tuple[ImageSummary, ...]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        image_id = str(item.get("id") or "")
        if not image_id:
            continue
        if image_id not in grouped:
            grouped[image_id] = dict(item)
            grouped[image_id]["tags"] = set(item.get("tags") or ())
            grouped[image_id]["digests"] = set(
                item.get("digests") or ()
            )
        else:
            grouped[image_id]["tags"].update(item.get("tags") or ())
            grouped[image_id]["digests"].update(
                item.get("digests") or ()
            )
    counts = reference_counts or {}
    summaries = tuple(
        _summary(item, counts.get(str(item.get("id") or ""), 0))
        for item in grouped.values()
    )
    return tuple(
        sorted(
            summaries,
            key=lambda item: (_created_key(item.created), item.id),
            reverse=True,
        )
    )


def _summary(
    item: dict[str, Any],
    container_count: int = 0,
) -> ImageSummary:
    image_id = str(item.get("id") or "")
    short_id = str(item.get("short_id") or "")
    if short_id.startswith("sha256:"):
        short_id = short_id.removeprefix("sha256:")
    if not short_id:
        short_id = image_id.removeprefix("sha256:")[:12]
    return ImageSummary(
        id=image_id,
        short_id=short_id,
        tags=tuple(
            sorted(
                set(item.get("tags") or ()),
                key=lambda value: (str(value).casefold(), str(value)),
            )
        ),
        digests=tuple(
            sorted(
                set(item.get("digests") or ()),
                key=lambda value: (str(value).casefold(), str(value)),
            )
        ),
        created=_optional_text(item.get("created")),
        size=int(item.get("size") or 0),
        architecture=_optional_text(item.get("architecture")),
        os=_optional_text(item.get("os")),
        entrypoint=item.get("entrypoint"),
        command=item.get("command"),
        container_count=container_count,
    )


def _matches(item: ImageSummary, needle: str) -> bool:
    image_id = item.id.casefold()
    stripped_id = item.id.removeprefix("sha256:").casefold()
    values = (image_id, stripped_id, item.short_id.casefold()) + tuple(
        tag.casefold() for tag in item.tags
    )
    return any(needle in value for value in values)


def _labels(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("labels")
    return value if isinstance(value, dict) else {}


def _optional_text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _created_key(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
