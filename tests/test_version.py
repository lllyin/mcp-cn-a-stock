"""Application version tests."""

import importlib
import logging
import tomllib
from pathlib import Path

from qtf_mcp import __version__


def test_runtime_version_matches_project_metadata():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)

    assert __version__ == "1.1.0"
    assert project["project"]["version"] == __version__


def test_application_version_is_logged(caplog):
    app_main = importlib.import_module("main")
    caplog.set_level(logging.INFO, logger="qtf_mcp")

    app_main.log_application_version()

    assert "cn-stock-mcp version=1.1.0" in caplog.text
