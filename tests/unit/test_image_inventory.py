from copy import deepcopy

import pytest

from docker_manage_server.docker_runtime import (
    DockerRuntimeError,
    ImageNotFoundError,
)
from docker_manage_server.image_inventory import (
    ImageBatchPreviewResult,
    ImageInUseError,
    ImageInventoryService,
    InvalidImagePageError,
)


USED_ID = "sha256:" + "1" * 64
FREE_ID = "sha256:" + "2" * 64
DANGLING_ID = "sha256:" + "3" * 64
MISSING_ID = "sha256:" + "4" * 64


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
        "image_reference": "demo/app:1",
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


def test_batch_preview_groups_free_dangling_used_and_missing_images():
    runtime = FakeRuntime(
        [
            image(USED_ID, ("demo/used:1",)),
            image(FREE_ID, ("demo/free:1",)),
            image(DANGLING_ID),
        ],
        [
            container("running", USED_ID, name="z-running", running=True),
            container("stopped", USED_ID, name="a-stopped", running=False),
        ],
    )

    result = ImageInventoryService(runtime).preview_batch_removal(
        (USED_ID, FREE_ID, DANGLING_ID, MISSING_ID)
    )

    assert isinstance(result, ImageBatchPreviewResult)
    assert [item.id for item in result.deletable] == [FREE_ID, DANGLING_ID]
    assert [item.id for item in result.in_use] == [USED_ID]
    assert [item.name for item in result.in_use[0].containers] == [
        "a-stopped",
        "z-running",
    ]
    assert [item.running for item in result.in_use[0].containers] == [False, True]
    assert result.missing == (MISSING_ID,)
    assert result.deletable[1].tags == ()


def test_batch_remove_deletes_whole_images_and_continues_after_failure():
    failing_id = "sha256:" + "5" * 64
    runtime = FakeRuntime(
        [
            image(FREE_ID, ("demo/free:1", "demo/free:2")),
            image(DANGLING_ID),
            image(failing_id, ("demo/fail:1",)),
            image(USED_ID, ("demo/used:1",)),
        ],
        [container("stopped", USED_ID, name="consumer", running=False)],
    )
    runtime.fail_reference = failing_id

    result = ImageInventoryService(runtime).remove_unused_images(
        (FREE_ID, failing_id, DANGLING_ID, USED_ID, MISSING_ID),
        query="demo",
        page=2,
    )

    assert runtime.remove_calls == [FREE_ID, failing_id, DANGLING_ID]
    assert [item.id for item in result.deleted] == [FREE_ID, DANGLING_ID]
    assert [item.id for item in result.in_use] == [USED_ID]
    assert result.in_use[0].containers[0].name == "consumer"
    assert result.missing == (MISSING_ID,)
    assert [item.id for item in result.failed] == [failing_id]
    assert result.failed[0].error == "remove failed"
    assert result.suggested_page == 1


def test_batch_remove_rechecks_containers_after_preview():
    runtime = FakeRuntime([image(FREE_ID, ("demo/free:1",))])
    service = ImageInventoryService(runtime)
    assert [item.id for item in service.preview_batch_removal((FREE_ID,)).deletable] == [
        FREE_ID
    ]
    runtime.containers.append(
        container("new-container", FREE_ID, name="new-consumer", running=True)
    )

    result = service.remove_unused_images((FREE_ID,), query="", page=1)

    assert runtime.remove_calls == []
    assert [item.id for item in result.in_use] == [FREE_ID]
    assert result.in_use[0].containers[0].name == "new-consumer"


def test_batch_remove_requires_successful_initial_docker_snapshot():
    runtime = FakeRuntime([image(FREE_ID, ("demo/free:1",))])

    def fail_containers():
        raise DockerRuntimeError("daemon offline")

    runtime.list_containers = fail_containers

    with pytest.raises(DockerRuntimeError, match="daemon offline"):
        ImageInventoryService(runtime).remove_unused_images(
            (FREE_ID,), query="", page=1
        )

    assert runtime.remove_calls == []


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


def test_list_counts_running_and_stopped_container_references():
    runtime = FakeRuntime(
        [
            image("sha256:used", ("demo/used:1",)),
            image("sha256:unused", ("demo/unused:1",)),
        ],
        [
            container("running", "sha256:used", running=True),
            container("stopped", "sha256:used", running=False),
            container("other", "sha256:other", running=True),
        ],
    )

    items = ImageInventoryService(runtime).list().items

    counts = {item.id: item.container_count for item in items}
    assert counts == {"sha256:used": 2, "sha256:unused": 0}


def test_list_sorts_mixed_rfc3339_precision_by_actual_time():
    service = ImageInventoryService(
        FakeRuntime(
            [
                image("sha256:whole", created="2026-08-12T00:00:00Z"),
                image(
                    "sha256:fraction",
                    created="2026-08-12T00:00:00.9Z",
                ),
            ]
        )
    )

    assert [item.id for item in service.list().items] == [
        "sha256:fraction",
        "sha256:whole",
    ]


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
def test_preview_retains_exact_tag_used_by_running_or_stopped_container(running):
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

    preview = ImageInventoryService(runtime).preview_tag_removal("demo/app:1")
    assert preview.deletable_tags == ()
    assert preview.retained_tags == ("demo/app:1",)


def test_remove_available_tags_keeps_used_tag_and_deletes_unused_tag():
    runtime = FakeRuntime(
        [image("sha256:image", ("demo/app:2", "demo/app:1"))],
        [container("using", "sha256:image", name="consumer")],
    )
    runtime.containers[0]["image_reference"] = "demo/app:1"

    result = ImageInventoryService(runtime).remove_available_tags("demo/app:1")

    assert runtime.remove_calls == ["demo/app:2"]
    assert result.deleted_tags == ("demo/app:2",)
    assert result.retained_tags == ("demo/app:1",)
    assert result.skipped_tags == ()
    assert result.image_exists is True


def test_remove_stops_after_partial_failure():
    runtime = FakeRuntime(
        [image("sha256:image", ("demo/app:1", "demo/app:2"))]
    )
    runtime.fail_reference = "demo/app:2"

    with pytest.raises(DockerRuntimeError, match="remove failed"):
        ImageInventoryService(runtime).remove_available_tags("sha256:image")

    assert runtime.remove_calls == ["demo/app:1", "demo/app:2"]


def test_remove_skips_tag_if_repointed_before_deletion():
    runtime = FakeRuntime(
        [
            image("sha256:image", ("demo/app:1", "demo/app:2")),
            image("sha256:other", ("demo/other:1",)),
        ]
    )
    original_get = runtime.get_serialized_image
    lookups = 0

    def repoint_on_second_tag(reference):
        nonlocal lookups
        if reference == "demo/app:2":
            lookups += 1
            if lookups == 1:
                for item in runtime.images:
                    if item["id"] == "sha256:image":
                        item["tags"].remove(reference)
                    if item["id"] == "sha256:other":
                        item["tags"].append(reference)
        return original_get(reference)

    runtime.get_serialized_image = repoint_on_second_tag

    result = ImageInventoryService(runtime).remove_available_tags(
        "sha256:image"
    )

    assert runtime.remove_calls == ["demo/app:1"]
    assert result.skipped_tags == ("demo/app:2",)
    assert "demo/app:2" in runtime.images[1]["tags"]


def test_tag_disappearing_during_removal_is_skipped():
    runtime = FakeRuntime([image("sha256:image", ("demo/app:1",))])
    original_remove = runtime.remove_image

    def disappear(reference):
        runtime.images = []
        original_remove(reference)

    runtime.remove_image = disappear

    result = ImageInventoryService(runtime).remove_available_tags("sha256:image")
    assert result.deleted_tags == ()
    assert result.skipped_tags == ("demo/app:1",)
    assert result.image_exists is False


def test_dangling_or_all_used_image_has_no_deletable_tags():
    dangling = ImageInventoryService(
        FakeRuntime([image("sha256:dangling")])
    ).preview_tag_removal("sha256:dangling")
    assert dangling.deletable_tags == ()

    runtime = FakeRuntime(
        [image("sha256:used", ("demo/used:1",))],
        [container("using", "sha256:used")],
    )
    runtime.containers[0]["image_reference"] = "demo/used:1"
    with pytest.raises(ImageInUseError):
        ImageInventoryService(runtime).remove_available_tags("sha256:used")
