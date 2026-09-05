"""Lightweight request context shared by MCP, datasource, and browser logs."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


request_id_var: ContextVar[str] = ContextVar("qtf_mcp_request_id", default="-")
http_trace_id_var: ContextVar[str] = ContextVar("qtf_mcp_http_trace_id", default="-")
tool_var: ContextVar[str] = ContextVar("qtf_mcp_tool", default="-")
symbol_var: ContextVar[str] = ContextVar("qtf_mcp_symbol", default="-")


@contextmanager
def bind_log_context(
    *,
    request_id: str | None = None,
    http_trace_id: str | None = None,
    tool: str | None = None,
    symbol: str | None = None,
) -> Iterator[None]:
    """Bind correlation fields for logs emitted by the current async task."""
    tokens = []
    if request_id is not None:
        tokens.append((request_id_var, request_id_var.set(request_id)))
    if http_trace_id is not None:
        tokens.append((http_trace_id_var, http_trace_id_var.set(http_trace_id)))
    if tool is not None:
        tokens.append((tool_var, tool_var.set(tool)))
    if symbol is not None:
        tokens.append((symbol_var, symbol_var.set(symbol)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def log_context() -> tuple[str, str, str]:
    """Return request_id, tool, and symbol for the current task."""
    return request_id_var.get(), tool_var.get(), symbol_var.get()
