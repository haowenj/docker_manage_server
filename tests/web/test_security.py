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


def test_cross_origin_terminal_is_rejected(web_context):
    client, _store, _runtime = web_context
    with pytest.raises(WebSocketDisconnect) as captured:
        with client.websocket_connect(
            "/api/containers/abc/terminal",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    assert captured.value.code == 1008
