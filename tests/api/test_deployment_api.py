from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from docker_manage_server.api import create_app
from docker_manage_server.config import Settings
from docker_manage_server.docker_runtime import ContainerNotFoundError
from docker_manage_server.models import FailurePhase, TaskStatus
from docker_manage_server.storage import TaskStore


class ApiFakeRuntime:
    def ping(self):
        return True

    def list_containers(self):
        return []

    def get_container(self, container_id):
        raise ContainerNotFoundError(container_id)

    def compose_config(self, project_dir, compose_file, env_file):
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def compose_up(self, cwd):
        return SimpleNamespace(returncode=0, stdout=b"started\n", stderr=b"")


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


def test_update_configuration_and_review_metadata(client, valid_archive):
    task_id = upload(client, valid_archive)["task_id"]
    response = client.put(
        f"/api/deployment-tasks/{task_id}/configuration",
        json={
            "env": "SECRET=changed\n",
            "compose": "services:\n  web:\n    image: changed:latest\n",
            "directories": [{"path": "data/mysql", "mode": "0770"}],
        },
    )
    assert response.status_code == 200
    review = client.get(f"/api/deployment-tasks/{task_id}/review").json()
    assert review["env"] == "SECRET=changed\n"
    assert review["directories"] == [
        {"path": "data/mysql", "mode": "0770", "exists": False}
    ]
    assert review["editable"] is True
    assert review["retryable"] is True
    assert review["edited_at"] is not None


def test_update_configuration_maps_errors(client, valid_archive):
    task_id = upload(client, valid_archive)["task_id"]
    unsafe = client.put(
        f"/api/deployment-tasks/{task_id}/configuration",
        json={
            "env": "A=1\n",
            "compose": "services: {}\n",
            "directories": [{"path": "../bad", "mode": "0770"}],
        },
    )
    assert unsafe.status_code == 422
    oversized = client.put(
        f"/api/deployment-tasks/{task_id}/configuration",
        json={
            "env": "A" * (1024 * 1024 + 1),
            "compose": "services: {}\n",
            "directories": [],
        },
    )
    assert oversized.status_code == 413


def test_api_allows_retryable_failed_task_and_rejects_upload_failure(
    client, valid_archive
):
    task_id = upload(client, valid_archive)["task_id"]
    store = client.app.state.store
    task = store.get(task_id)
    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.DEPLOY
    store.save(task)

    retry = client.post(f"/api/deployment-tasks/{task_id}/deploy")
    assert retry.status_code == 202

    task = store.get(task_id)
    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.UPLOAD
    store.save(task)
    blocked = client.post(f"/api/deployment-tasks/{task_id}/deploy")
    assert blocked.status_code == 409
