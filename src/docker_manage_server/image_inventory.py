from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
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


@dataclass(frozen=True)
class ImageTagRemovalPreview:
    id: str
    deletable_tags: tuple[str, ...]
    retained_tags: tuple[str, ...]


@dataclass(frozen=True)
class ImageTagRemovalResult:
    id: str
    deleted_tags: tuple[str, ...]
    retained_tags: tuple[str, ...]
    skipped_tags: tuple[str, ...]
    image_exists: bool


class ImageInventoryService:
    def __init__(self, runtime: DockerRuntime):
        self.runtime = runtime
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()

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

    def preview_tag_removal(self, image_id: str) -> ImageTagRemovalPreview:
        item = self.runtime.get_serialized_image(image_id)
        summary = _summary(item)
        used = _used_tag_references(
            self.runtime.list_containers(),
            summary.tags,
        )
        return ImageTagRemovalPreview(
            id=summary.id,
            deletable_tags=tuple(tag for tag in summary.tags if tag not in used),
            retained_tags=tuple(tag for tag in summary.tags if tag in used),
        )

    def remove_available_tags(self, image_id: str) -> ImageTagRemovalResult:
        initial = self.runtime.get_serialized_image(image_id)
        immutable_id = str(initial.get("id") or "")
        with self._lock_for(immutable_id):
            preview = self.preview_tag_removal(immutable_id)
            if not preview.deletable_tags:
                references = self._references(immutable_id)
                raise ImageInUseError(references)
            deleted = []
            skipped = []
            for tag in preview.deletable_tags:
                try:
                    current = self.runtime.get_serialized_image(tag)
                except ImageNotFoundError:
                    skipped.append(tag)
                    continue
                if str(current.get("id") or "") != immutable_id:
                    skipped.append(tag)
                    continue
                try:
                    self.runtime.remove_image(tag)
                except ImageNotFoundError:
                    skipped.append(tag)
                    continue
                deleted.append(tag)
            try:
                self.runtime.get_serialized_image(immutable_id)
                image_exists = True
            except ImageNotFoundError:
                image_exists = False
            return ImageTagRemovalResult(
                id=immutable_id,
                deleted_tags=tuple(deleted),
                retained_tags=preview.retained_tags,
                skipped_tags=tuple(skipped),
                image_exists=image_exists,
            )

    def _lock_for(self, image_id: str) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(image_id, Lock())

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


def _used_tag_references(
    containers: list[dict[str, Any]],
    tags: tuple[str, ...],
) -> set[str]:
    tag_set = set(tags)
    return {
        str(item.get("image_reference"))
        for item in containers
        if item.get("image_reference") in tag_set
    }


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
