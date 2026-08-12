from copy import deepcopy

import pytest

from docker_manage_server.docker_runtime import (
    DockerRuntimeError,
    ImageNotFoundError,
)
from docker_manage_server.image_inventory import (
    ImageInUseError,
    ImageInventoryService,
    InvalidImagePageError,
)


def image(image_id, tags=(), created="2026-08-12T00:00:00Z", size=100):
    return {
        "id": image_id,
        "short_id": image_id.removeprefix("sha256:")[:12],
        "tags": list(tags),
        "digests": [],
        "created": created,
        "size": size,
        "architecture": "amd64",
        "os": "linux",
        "entrypoint": None,
        "command": ["serve"],
        "raw_attrs": {
            "Id": image_id,
            "Config": {"Labels": {"unsafe": "<script>"}},
        },
    }


def container(
    container_id,
    image_id,
    *,
    name="container",
    project=None,
    running=False,
):
    labels = {}
    if project:
        labels = {
            "com.docker.compose.project": project,
            "com.docker.compose.service": "web",
        }
    return {
        "id": container_id,
        "name": name,
        "status": "running" if running else "exited",
        "running": running,
        "image_id": image_id,
        "labels": labels,
    }


class FakeRuntime:
    def __init__(self, images=(), containers=()):
        self.images = [deepcopy(item) for item in images]
        self.containers = [deepcopy(item) for item in containers]
        self.remove_calls = []
        self.fail_reference = None

    def list_images(self):
        return deepcopy(self.images)

    def list_containers(self):
        return deepcopy(self.containers)

    def get_serialized_image(self, reference):
        for item in self.images:
            if reference in (item["id"], item["short_id"], *item["tags"]):
                return deepcopy(item)
        raise ImageNotFoundError(reference)

    def remove_image(self, reference):
        self.remove_calls.append(reference)
        if reference == self.fail_reference:
            raise DockerRuntimeError("remove failed")
        for item in list(self.images):
            if reference in item["tags"]:
                item["tags"].remove(reference)
                if not item["tags"]:
                    self.images.remove(item)
                return
            if reference == item["id"]:
                self.images.remove(item)
                return
        raise ImageNotFoundError(reference)


def test_list_aggregates_tags_sorts_searches_and_pages():
    images = [
        image(
            "sha256:old",
            ("demo/应用:旧",),
            "2026-01-01T00:00:00Z",
        ),
        image(
            "sha256:new",
            ("Demo/App:Latest",),
            "2026-08-12T00:00:00Z",
        ),
        image(
            "sha256:new",
            ("demo/app:1",),
            "2026-08-12T00:00:00Z",
        ),
        image("sha256:dangling", (), "2026-08-11T00:00:00Z"),
    ] + [
        image(f"sha256:{index:064x}", (f"bulk/item:{index}",))
        for index in range(25)
    ]
    service = ImageInventoryService(FakeRuntime(images))

    first = service.list(page=1)
    second = service.list(page=2)
    searched = service.list(query="app:latest", page=1)
    unicode_search = service.list(query="应用", page=1)
    id_search = service.list(query="dangling", page=1)

    assert len(first.items) == 20
    assert first.page_size == 20
    assert first.total_items == 28
    assert first.total_pages == 2
    assert len(second.items) == 8
    assert searched.items[0].tags == (
        "demo/app:1",
        "Demo/App:Latest",
    )
    assert unicode_search.items[0].id == "sha256:old"
    assert id_search.items[0].id == "sha256:dangling"
    assert any(not item.tags for item in first.items + second.items)
    assert service.list(page=3).items == ()


@pytest.mark.parametrize("page", [0, -1, "x", "1.5"])
def test_list_rejects_invalid_page(page):
    with pytest.raises(InvalidImagePageError):
        ImageInventoryService(FakeRuntime()).list(page=page)


def test_detail_matches_references_by_immutable_image_id():
    runtime = FakeRuntime(
        [image("sha256:image", ("demo/app:1",))],
        [
            container("direct", "sha256:image", name="direct"),
            container(
                "compose",
                "sha256:image",
                name="mall-web",
                project="mall",
                running=True,
            ),
            container("other", "sha256:other", name="other"),
        ],
    )

    detail = ImageInventoryService(runtime).get("demo/app:1")

    assert detail.summary.id == "sha256:image"
    assert [item.id for item in detail.containers] == ["direct", "compose"]
    assert detail.containers[1].compose_project == "mall"
    assert detail.inspect["Id"] == "sha256:image"


@pytest.mark.parametrize("running", [False, True])
def test_remove_rejects_any_container_reference(running):
    runtime = FakeRuntime(
        [image("sha256:image", ("demo/app:1",))],
        [
            container(
                "using",
                "sha256:image",
                name="consumer",
                running=running,
            )
        ],
    )

    with pytest.raises(ImageInUseError, match="consumer"):
        ImageInventoryService(runtime).remove("demo/app:1")

    assert runtime.remove_calls == []


def test_remove_uses_current_tags_then_immutable_id_and_returns_identity():
    runtime = FakeRuntime(
        [image("sha256:image", ("demo/app:2", "demo/app:1"))]
    )

    deleted = ImageInventoryService(runtime).remove("demo/app:1")

    assert runtime.remove_calls == [
        "demo/app:1",
        "demo/app:2",
        "sha256:image",
    ]
    assert deleted == {
        "id": "sha256:image",
        "tags": ["demo/app:1", "demo/app:2"],
    }


def test_remove_stops_after_partial_failure():
    runtime = FakeRuntime(
        [image("sha256:image", ("demo/app:1", "demo/app:2"))]
    )
    runtime.fail_reference = "demo/app:2"

    with pytest.raises(DockerRuntimeError, match="remove failed"):
        ImageInventoryService(runtime).remove("sha256:image")

    assert runtime.remove_calls == ["demo/app:1", "demo/app:2"]


def test_dangling_image_not_found_on_id_delete_is_not_success():
    runtime = FakeRuntime([image("sha256:image")])
    runtime.images = []

    with pytest.raises(ImageNotFoundError):
        ImageInventoryService(runtime).remove("sha256:image")
