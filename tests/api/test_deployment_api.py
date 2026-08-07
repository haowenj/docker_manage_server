from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from docker_manage_server.api import create_app
from docker_manage_server.config import Settings
from docker_manage_server.docker_runtime import ContainerNotFoundError
from docker_manage_server.storage import TaskStore


class ApiFakeRuntime:
    def ping(self):
        return True

    def list_containers(self):
        return []

    def get_container(self, container_id):
        raise ContainerNotFoundError(container_id)


@pytest.fixture
def client(tmp_path):
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        store=TaskStore(tmp_path),
        runtime=ApiFakeRuntime(),
    )
    return TestClient(app)


def upload(client: TestClient, archive):
    response = client.post(
        "/api/deployment-tasks",
        files={"file": ("demo.tar.gz", archive.read_bytes(), "application/gzip")},
    )
    assert response.status_code == 201
    return response.json()


def test_upload_returns_pending_review(client, valid_archive):
    response = client.post(
        "/api/deployment-tasks",
        files={"file": ("demo.tar.gz", valid_archive.read_bytes(), "application/gzip")},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "pending_review"
    assert "created_at" not in response.json()
    assert "updated_at" not in response.json()


def test_review_returns_full_env_and_compose(client, valid_archive):
    task_id = upload(client, valid_archive)["task_id"]
    response = client.get(f"/api/deployment-tasks/{task_id}/review")
    assert response.status_code == 200
    assert "SECRET=value" in response.json()["env"]
    assert "services:" in response.json()["compose"]


def test_health_does_not_require_running_application_containers(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["docker_connected"] is True
    assert response.json()["containers"] == 0


def test_unknown_task_returns_not_found(client):
    response = client.get("/api/deployment-tasks/missing")
    assert response.status_code == 404
