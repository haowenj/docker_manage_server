import pytest
from starlette.websockets import WebSocketDisconnect


def test_responses_include_security_headers(web_context):
    client, _store, _runtime = web_context
    response = client.get("/")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"


def test_cross_origin_form_is_rejected_but_missing_origin_is_allowed(web_context):
    client, _store, _runtime = web_context
    rejected = client.post(
        "/deployments",
        headers={"Origin": "https://evil.example"},
        files={"file": ("x.tar.gz", b"broken", "application/gzip")},
    )
    assert rejected.status_code == 403

    compatible = client.post(
        "/api/deployment-tasks",
        files={"file": ("x.tar.gz", b"broken", "application/gzip")},
    )
    assert compatible.status_code == 422

    same_origin = client.post(
        "/deployments",
        headers={"Origin": "http://testserver"},
        files={"file": ("x.tar.gz", b"broken", "application/gzip")},
    )
    assert same_origin.status_code == 422

    rejected_update = client.put(
        "/api/deployment-tasks/missing/configuration",
        headers={"Origin": "https://evil.example"},
        json={"env": "A=1\n", "compose": "services: {}\n", "directories": []},
    )
    assert rejected_update.status_code == 403


def test_cross_origin_terminal_is_rejected(web_context):
    client, _store, _runtime = web_context
    with pytest.raises(WebSocketDisconnect) as captured:
        with client.websocket_connect(
            "/api/containers/abc/terminal",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    assert captured.value.code == 1008


def test_cross_origin_compose_terminal_is_rejected(web_context):
    client, _store, _runtime = web_context
    with pytest.raises(WebSocketDisconnect) as captured:
        with client.websocket_connect(
            "/api/compose-projects/mall/containers/mall-web/terminal",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    assert captured.value.code == 1008


def test_cross_origin_runtime_actions_are_rejected(web_context):
    client, _store, runtime = web_context
    for method, path in (
        ("post", "/containers/abc123/stop"),
        ("post", "/compose-projects/mall/stop"),
        ("delete", "/api/compose-projects/mall"),
    ):
        response = getattr(client, method)(
            path,
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
    assert runtime.lifecycle_calls == []


def test_cross_origin_image_deletes_are_rejected(web_context):
    client, _store, runtime = web_context
    image_id = "sha256:" + "a" * 64
    runtime.images = [
        {
            "id": image_id,
            "short_id": "a" * 12,
            "tags": ["demo/app:1"],
            "raw_attrs": {"Id": image_id},
        }
    ]
    requests = (
        ("post", f"/images/{image_id}/delete", None),
        ("delete", f"/api/images/{image_id}/tags", None),
        (
            "post",
            "/api/images/batch-delete-preview",
            {"image_ids": [image_id]},
        ),
        (
            "post",
            "/api/images/batch-delete",
            {"image_ids": [image_id], "query": "", "page": 1},
        ),
    )
    for method, path, payload in requests:
        kwargs = {"headers": {"Origin": "https://evil.example"}}
        if payload is not None:
            kwargs["json"] = payload
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 403
    assert runtime.image_remove_calls == []
