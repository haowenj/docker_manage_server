from copy import deepcopy

import pytest
from starlette.testclient import TestClient

from docker_manage_server.api import create_app
from docker_manage_server.config import Settings
from docker_manage_server.docker_runtime import (
    DockerRuntimeError,
    ImageNotFoundError,
)
from docker_manage_server.storage import TaskStore


IMAGE_ID = "sha256:" + "a" * 64
FREE_ID = "sha256:" + "b" * 64
DANGLING_ID = "sha256:" + "c" * 64
MISSING_ID = "sha256:" + "d" * 64


def image_fixture(image_id=IMAGE_ID, tags=("demo:1",)):
    return {
        "id": image_id,
        "short_id": image_id.removeprefix("sha256:")[:12],
        "tags": list(tags),
        "digests": [],
        "created": "2026-08-12T00:00:00Z",
        "size": 100,
        "architecture": "amd64",
        "os": "linux",
        "entrypoint": None,
        "command": ["serve"],
        "raw_attrs": {"Id": image_id},
    }


class ImageApiRuntime:
    def __init__(self):
        self.images = [image_fixture()]
        self.containers = [
            {
                "id": "container-id",
                "name": "consumer",
                "status": "exited",
                "running": False,
                "image_id": IMAGE_ID,
                "image_reference": "demo:1",
                "labels": {},
            }
        ]
        self.fail = None
        self.fail_reference = None

    def list_images(self):
        if self.fail == "list":
            raise DockerRuntimeError("daemon offline")
        return deepcopy(self.images)

    def list_containers(self):
        return deepcopy(self.containers)

    def get_serialized_image(self, reference):
        if self.fail == "get":
            raise DockerRuntimeError("inspect failed")
        for item in self.images:
            if reference in (item["id"], item["short_id"], *item["tags"]):
                return deepcopy(item)
        raise ImageNotFoundError(reference)

    def remove_image(self, reference):
        if self.fail == "remove":
            raise DockerRuntimeError("remove failed")
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


@pytest.fixture
def client(tmp_path):
    runtime = ImageApiRuntime()
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        store=TaskStore(tmp_path),
        runtime=runtime,
    )
    app.state.test_runtime = runtime
    return TestClient(app)


def test_image_api_lists_searches_and_pages(client):
    response = client.get("/api/images?q=demo&page=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "demo"
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert payload["total_items"] == 1
    assert payload["items"][0]["id"] == IMAGE_ID
    assert payload["items"][0]["container_count"] == 1


def test_image_api_returns_inspect_and_container_references(client):
    response = client.get(f"/api/images/{IMAGE_ID}")

    assert response.status_code == 200
    assert response.json()["inspect"]["Id"] == IMAGE_ID
    assert response.json()["containers"][0]["id"] == "container-id"


def test_image_api_previews_used_tag_then_deletes_available_tags(client):
    preview = client.get(
        "/api/images/demo:1/tag-removal-preview"
    )
    assert preview.status_code == 200
    assert preview.json()["retained_tags"] == ["demo:1"]
    blocked = client.delete("/api/images/demo:1/tags")
    assert blocked.status_code == 409
    assert "consumer" in blocked.json()["detail"]

    client.app.state.test_runtime.containers = []
    deleted = client.delete("/api/images/demo:1/tags")

    assert deleted.status_code == 200
    assert deleted.json() == {
        "id": IMAGE_ID,
        "deleted_tags": ["demo:1"],
        "retained_tags": [],
        "skipped_tags": [],
        "image_exists": False,
    }


def test_image_api_maps_missing_invalid_page_and_runtime_errors(client):
    assert client.get("/api/images/missing").status_code == 404
    assert client.delete("/api/images/missing/tags").status_code == 404
    assert client.get("/api/images?page=0").status_code == 422
    assert client.get("/api/images?page=x").status_code == 422

    runtime = client.app.state.test_runtime
    runtime.fail = "list"
    assert client.get("/api/images").status_code == 503
    runtime.fail = "get"
    assert client.get(f"/api/images/{IMAGE_ID}").status_code == 503
    runtime.fail = "remove"
    runtime.containers = []
    assert client.delete(f"/api/images/{IMAGE_ID}/tags").status_code == 503


def test_image_batch_api_previews_then_deletes_only_unused_images(client):
    runtime = client.app.state.test_runtime
    runtime.images.extend(
        [
            image_fixture(FREE_ID, ("demo/free:1", "demo/free:2")),
            image_fixture(DANGLING_ID, ()),
        ]
    )

    preview = client.post(
        "/api/images/batch-delete-preview",
        json={"image_ids": [IMAGE_ID, FREE_ID, DANGLING_ID, MISSING_ID]},
    )

    assert preview.status_code == 200
    assert [item["id"] for item in preview.json()["deletable"]] == [
        FREE_ID,
        DANGLING_ID,
    ]
    assert preview.json()["in_use"][0]["id"] == IMAGE_ID
    assert preview.json()["in_use"][0]["containers"][0]["name"] == "consumer"
    assert preview.json()["missing"] == [MISSING_ID]

    deleted = client.post(
        "/api/images/batch-delete",
        json={
            "image_ids": [IMAGE_ID, FREE_ID, DANGLING_ID, MISSING_ID],
            "query": "demo",
            "page": 2,
        },
    )

    assert deleted.status_code == 200
    payload = deleted.json()
    assert [item["id"] for item in payload["deleted"]] == [FREE_ID, DANGLING_ID]
    assert [item["id"] for item in payload["in_use"]] == [IMAGE_ID]
    assert payload["missing"] == [MISSING_ID]
    assert payload["failed"] == []
    assert payload["suggested_page"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"image_ids": []},
        {"image_ids": [IMAGE_ID, IMAGE_ID]},
        {"image_ids": ["sha256:not-hex"]},
        {"image_ids": ["a" * 64]},
        {"image_ids": [f"sha256:{index:064x}" for index in range(21)]},
        {"image_ids": [IMAGE_ID], "page": 0},
        {"image_ids": [IMAGE_ID], "unexpected": True},
    ],
)
def test_image_batch_api_rejects_invalid_payloads(client, payload):
    path = (
        "/api/images/batch-delete"
        if "page" in payload or "unexpected" in payload
        else "/api/images/batch-delete-preview"
    )
    assert client.post(path, json=payload).status_code == 422


def test_image_batch_execute_maps_initial_snapshot_failure_to_503(client):
    runtime = client.app.state.test_runtime
    runtime.fail = "list"

    response = client.post(
        "/api/images/batch-delete",
        json={"image_ids": [IMAGE_ID], "query": "", "page": 1},
    )

    assert response.status_code == 503
    assert runtime.images == [image_fixture()]


def test_image_batch_api_returns_item_failure_and_continues(client):
    runtime = client.app.state.test_runtime
    runtime.containers = []
    runtime.images.extend(
        [
            image_fixture(FREE_ID, ("demo/free:1",)),
            image_fixture(DANGLING_ID, ()),
        ]
    )
    runtime.fail_reference = FREE_ID

    response = client.post(
        "/api/images/batch-delete",
        json={
            "image_ids": [FREE_ID, DANGLING_ID],
            "query": "",
            "page": 1,
        },
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["failed"]] == [FREE_ID]
    assert response.json()["failed"][0]["error"] == "remove failed"
    assert [item["id"] for item in response.json()["deleted"]] == [DANGLING_ID]
