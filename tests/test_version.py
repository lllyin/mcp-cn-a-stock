"""Application version tests."""

import importlib
import logging
import tomllib
from pathlib import Path

from finmcp import __version__


def test_runtime_version_matches_project_metadata():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)

    assert __version__ == project["project"]["version"]


def test_mcp_handshake_reports_the_service_version():
    """握手回给客户端的 serverInfo.version 是本服务的版本，不是 mcp SDK 的。

    FastMCP 不往底层 Server 传 version，底层就报 SDK 包自己的版本号；靠的是
    QtfMCP 构造时补上的那一行，SDK 改了内部结构时这里会先红。
    """
    from finmcp import mcp_app

    options = mcp_app._mcp_server.create_initialization_options()

    assert options.server_version == __version__


def test_application_version_is_logged(caplog):
    app_main = importlib.import_module("main")
    caplog.set_level(logging.INFO, logger="finmcp")

    app_main.log_application_version()

    assert f"cn-stock-mcp version={__version__}" in caplog.text


def test_http_channel_is_logged_at_startup(caplog):
    app_main = importlib.import_module("main")
    caplog.set_level(logging.INFO, logger="finmcp")

    app_main.log_http_channel()

    assert "HTTP channel mode=" in caplog.text
    assert "reason=" in caplog.text
