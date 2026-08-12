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


def image_fixture():
    return {
        "id": "sha256:image",
        "short_id": "image",
        "tags": ["demo:1"],
        "digests": [],
        "created": "2026-08-12T00:00:00Z",
        "size": 100,
        "architecture": "amd64",
        "os": "linux",
        "entrypoint": None,
        "command": ["serve"],
        "raw_attrs": {"Id": "sha256:image"},
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
                "image_id": "sha256:image",
                "labels": {},
            }
        ]
        self.fail = None

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
    assert payload["items"][0]["id"] == "sha256:image"


def test_image_api_returns_inspect_and_container_references(client):
    response = client.get("/api/images/sha256:image")

    assert response.status_code == 200
    assert response.json()["inspect"]["Id"] == "sha256:image"
    assert response.json()["containers"][0]["id"] == "container-id"


def test_image_api_blocks_used_image_then_deletes_by_immutable_identity(client):
    blocked = client.delete("/api/images/demo:1")
    assert blocked.status_code == 409
    assert "consumer" in blocked.json()["detail"]

    client.app.state.test_runtime.containers = []
    deleted = client.delete("/api/images/demo:1")

    assert deleted.status_code == 200
    assert deleted.json() == {
        "deleted": True,
        "id": "sha256:image",
        "tags": ["demo:1"],
    }


def test_image_api_maps_missing_invalid_page_and_runtime_errors(client):
    assert client.get("/api/images/missing").status_code == 404
    assert client.delete("/api/images/missing").status_code == 404
    assert client.get("/api/images?page=0").status_code == 422
    assert client.get("/api/images?page=x").status_code == 422

    runtime = client.app.state.test_runtime
    runtime.fail = "list"
    assert client.get("/api/images").status_code == 503
    runtime.fail = "get"
    assert client.get("/api/images/sha256:image").status_code == 503
    runtime.fail = "remove"
    runtime.containers = []
    assert client.delete("/api/images/sha256:image").status_code == 503
