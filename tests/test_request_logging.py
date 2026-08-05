"""HTTP request lifecycle logging tests."""

import logging

import pytest

from qtf_mcp.mcp_app import RequestLifecycleLogMiddleware


def _scope():
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/cnstock/mcp",
        "raw_path": b"/cnstock/mcp",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8686),
    }


@pytest.mark.asyncio
async def test_request_lifecycle_logs_client_disconnect(caplog):
    messages = iter(
        [
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    async def send(message):
        raise AssertionError(f"unexpected response: {message}")

    async def app(scope, receive, send):
        await receive()
        await receive()

    caplog.set_level(logging.INFO, logger="qtf_mcp")
    await RequestLifecycleLogMiddleware(app)(_scope(), receive, send)

    assert "HTTP client disconnected before response finished" in caplog.text
    assert "outcome=client_disconnected" in caplog.text
    assert "response_started=False" in caplog.text


@pytest.mark.asyncio
async def test_request_lifecycle_logs_response_size(caplog):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"abc"})

    caplog.set_level(logging.INFO, logger="qtf_mcp")
    await RequestLifecycleLogMiddleware(app)(_scope(), receive, send)

    assert len(sent) == 2
    assert "status=200" in caplog.text
    assert "response_bytes=3" in caplog.text
    assert "response_finished=True" in caplog.text


@pytest.mark.asyncio
async def test_request_lifecycle_does_not_count_failed_response_send(caplog):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            raise ConnectionError("client closed")

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"abc"})

    caplog.set_level(logging.INFO, logger="qtf_mcp")
    with pytest.raises(ConnectionError, match="client closed"):
        await RequestLifecycleLogMiddleware(app)(_scope(), receive, send)

    assert "outcome=error" in caplog.text
    assert "response_started=True" in caplog.text
    assert "response_bytes=0" in caplog.text
    assert "response_finished=False" in caplog.text
