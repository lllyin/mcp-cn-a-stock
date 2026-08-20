"""HTTP request lifecycle logging tests."""

import logging

import pytest

from qtf_mcp.mcp_app import RequestLifecycleLogMiddleware


def _scope(method: str = "POST"):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/cnstock/mcp",
        "raw_path": b"/cnstock/mcp",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8686),
    }


async def _run_disconnect(scope, caplog):
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

    caplog.set_level(logging.DEBUG, logger="qtf_mcp")
    await RequestLifecycleLogMiddleware(app)(scope, receive, send)


@pytest.mark.asyncio
async def test_get_stream_teardown_is_not_a_warning(caplog):
    """GET 是 SSE 通道，客户端断开即正常终结，不应产生 WARNING。

    否则每个正常的客户端生命周期都会留下一条告警，把真正被放弃的 POST 淹没。
    """
    await _run_disconnect(_scope("GET"), caplog)

    disconnect_records = [
        r for r in caplog.records
        if "disconnected before response finished" in r.getMessage()
    ]
    assert disconnect_records, "断开事件仍应被记录，只是不该用 WARNING"
    assert all(r.levelno <= logging.DEBUG for r in disconnect_records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_post_abandoned_midway_still_warns(caplog):
    """POST 被中途放弃是真问题，必须保持 WARNING。"""
    await _run_disconnect(_scope("POST"), caplog)

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "disconnected before response finished" in r.getMessage()
    ]
    assert len(warnings) == 1


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
