from __future__ import annotations

import os
import runpy
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


OBSERVER_TOOLS = {
    "get_pending_alerts",
    "acknowledge_alert",
    "get_realtime_symbol_context",
    "get_intraday_theme_context",
    "get_intraday_market_context",
    "record_agent_observation",
    "get_agent_observation",
    "list_agent_observations",
    "notify_agent_observation",
}

OBSERVER_PROFILE_TOOLS = OBSERVER_TOOLS | {
    "get_recent_market_events",
    "get_runner_status",
}


def _tool_include(path: Path) -> set[str]:
    payload = yaml.safe_load(path.read_text())
    return set(payload["mcp_servers"]["aegis_alpha"]["tools"]["include"])


def test_observer_tools_are_exposed_to_hermes_mcp_snippet():
    include = _tool_include(ROOT / ".hermes" / "config" / "aegis-alpha-mcp.yaml")
    assert OBSERVER_TOOLS <= include


def test_observer_tools_are_exposed_to_hermes_project_config_template():
    include = _tool_include(ROOT / ".hermes" / "config" / "config.example.yaml")
    assert OBSERVER_TOOLS <= include


def test_lightweight_observer_profile_has_narrow_tool_surface():
    include = _tool_include(ROOT / ".hermes" / "config" / "aegis-observer-profile.yaml")
    assert include == OBSERVER_PROFILE_TOOLS


def test_second_board_skill_documents_observer_contract():
    skill = (ROOT / ".hermes" / "skills" / "second-board-radar" / "SKILL.md").read_text()
    for tool in OBSERVER_TOOLS:
        assert tool in skill
    assert "Agent 市场观察" in skill
    assert "不要自填或口头承诺 notification grade" in skill


def test_mcp_runner_uses_project_data_paths_from_any_working_directory(tmp_path, monkeypatch):
    for key in (
        "AEGIS_ALPHA_PROJECT_ROOT",
        "AEGIS_ALPHA_ENV_FILE",
        "AEGIS_ALPHA_DATA_DIR",
        "AEGIS_ALPHA_DB_PATH",
        "AEGIS_ALPHA_RUNNER_STATUS_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    runpy.run_path(str(ROOT / "scripts" / "run_mcp.py"), run_name="aegis_run_mcp_contract")

    assert os.environ["AEGIS_ALPHA_DATA_DIR"] == str(ROOT / "data")
    assert os.environ["AEGIS_ALPHA_DB_PATH"] == str(ROOT / "data" / "aegis_alpha.db")
    assert os.environ["AEGIS_ALPHA_RUNNER_STATUS_PATH"] == str(ROOT / "data" / "runner_status.json")
