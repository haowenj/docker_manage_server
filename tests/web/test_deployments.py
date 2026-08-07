import json

from docker_manage_server.models import FailurePhase, TaskStatus


def upload_web(client, archive) -> str:
    response = client.post(
        "/deployments",
        files={"file": ("demo.tar.gz", archive.read_bytes(), "application/gzip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def test_upload_redirects_to_server_rendered_review(web_context, valid_archive):
    client, _store, _runtime = web_context
    response = client.post(
        "/deployments",
        files={"file": ("demo.tar.gz", valid_archive.read_bytes(), "application/gzip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "SECRET=value" in detail.text
    assert "services:" in detail.text
    assert "确认部署" in detail.text
    task_id = response.headers["location"].rsplit("/", 1)[1]
    assert f'data-task-poll-url="/api/deployment-tasks/{task_id}"' in detail.text


def test_review_escapes_archive_text(web_context, html_injection_archive):
    client, _store, _runtime = web_context
    response = client.post(
        "/deployments",
        files={
            "file": (
                "demo.tar.gz",
                html_injection_archive.read_bytes(),
                "application/gzip",
            )
        },
        follow_redirects=False,
    )
    detail = client.get(response.headers["location"])
    assert "<script>" not in detail.text
    assert "&lt;script&gt;" in detail.text


def test_invalid_upload_renders_422(web_context):
    client, _store, _runtime = web_context
    response = client.post(
        "/deployments",
        files={"file": ("broken.tar.gz", b"broken", "application/gzip")},
    )
    assert response.status_code == 422
    assert "归档校验失败" in response.text


def test_deploy_and_discard_use_303(web_context, valid_archive):
    client, store, _runtime = web_context
    uploaded = client.post(
        "/deployments",
        files={"file": ("demo.tar.gz", valid_archive.read_bytes(), "application/gzip")},
        follow_redirects=False,
    )
    task_id = uploaded.headers["location"].rsplit("/", 1)[1]
    deploy = client.post(f"/deployments/{task_id}/deploy", follow_redirects=False)
    assert deploy.status_code == 303
    blocked = client.post(f"/deployments/{task_id}/deploy")
    assert blocked.status_code == 409
    assert "任务当前状态不允许部署" in blocked.text

    second = store.create("discard-me", "discard.tar.gz")
    second.status = TaskStatus.PENDING_REVIEW
    store.save(second)
    discard = client.post("/deployments/discard-me/discard", follow_redirects=False)
    assert discard.status_code == 303
    assert discard.headers["location"] == "/deployments"

    missing = client.get("/deployments/not-found")
    assert missing.status_code == 404
    assert "找不到部署任务" in missing.text


def test_edit_page_shows_config_and_manifest_directory_rule(web_context, valid_archive):
    client, _store, _runtime = web_context
    task_id = upload_web(client, valid_archive)
    detail = client.get(f"/deployments/{task_id}")
    assert "编辑配置" in detail.text
    assert "files/sqlite" in detail.text
    assert "0777" in detail.text

    edit = client.get(f"/deployments/{task_id}/edit")
    assert edit.status_code == 200
    assert "SECRET=value" in edit.text
    assert "data-directory-editor" in edit.text
    assert "data-directory-add" in edit.text
    assert "data-directory-mode" in edit.text
    assert 'data-permission-bit="0400"' in edit.text
    assert 'data-permission-bit="0001"' in edit.text
    assert "0700" in edit.text
    assert "0750" in edit.text
    assert "0755" in edit.text
    assert "0770" in edit.text
    assert "0775" in edit.text
    assert "0777" in edit.text
    assert "不递归修改" in edit.text


def test_edit_form_saves_and_redirects_to_detail(web_context, valid_archive):
    client, store, _runtime = web_context
    task_id = upload_web(client, valid_archive)
    response = client.post(
        f"/deployments/{task_id}/edit",
        data={
            "env": "SECRET=changed\n",
            "compose": "services:\n  web:\n    image: changed:latest\n",
            "directories_json": json.dumps(
                [{"path": "data/mysql", "mode": "0770"}]
            ),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/deployments/{task_id}"
    task = store.get(task_id)
    assert task.directory_rules is not None
    assert task.directory_rules[0].path == "data/mysql"


def test_edit_form_error_keeps_submitted_text_escaped(web_context, valid_archive):
    client, _store, runtime = web_context
    task_id = upload_web(client, valid_archive)
    runtime.compose_config_returncode = 1
    runtime.compose_config_stderr = b"invalid compose"
    response = client.post(
        f"/deployments/{task_id}/edit",
        data={
            "env": "VALUE=<script>alert(1)</script>\n",
            "compose": "broken",
            "directories_json": "[]",
        },
    )
    assert response.status_code == 422
    assert "invalid compose" in response.text
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_deploy_failure_detail_allows_edit_and_retry(web_context, valid_archive):
    client, store, _runtime = web_context
    task_id = upload_web(client, valid_archive)
    task = store.get(task_id)
    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.DEPLOY
    task.error = "compose failed"
    store.save(task)
    detail = client.get(f"/deployments/{task_id}")
    assert "编辑并重试" in detail.text
    assert "重新部署" in detail.text
    retry = client.post(f"/deployments/{task_id}/deploy", follow_redirects=False)
    assert retry.status_code == 303


def test_upload_failure_has_no_edit_or_retry_actions(web_context, valid_archive):
    client, store, _runtime = web_context
    task_id = upload_web(client, valid_archive)
    task = store.get(task_id)
    task.status = TaskStatus.FAILED
    task.failure_phase = FailurePhase.UPLOAD
    store.save(task)
    detail = client.get(f"/deployments/{task_id}")
    assert "编辑并重试" not in detail.text
    assert "重新部署" not in detail.text
    assert client.get(f"/deployments/{task_id}/edit").status_code == 409
