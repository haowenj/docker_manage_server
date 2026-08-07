from docker_manage_server.models import TaskStatus


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
