"""doctor takes tray and notification state from the running agent (audit 2026-11-24).

On macOS a second menu-bar item cannot appear without the agent's AppKit run loop, so a probe
of its own reported "no tray host" while the agent's icon was visible.
"""

from __future__ import annotations

from typing import Any

import pytest

from captation import doctor, ipc


@pytest.mark.req("R-50")
def test_agent_state_comes_from_the_running_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    def status(msg: dict[str, Any], timeout_s: float = 3) -> dict[str, Any]:
        assert msg == {"type": "status"}
        return {"ok": True, "state": "DISARMED", "tray": True, "notifications": ["actions"]}

    monkeypatch.setattr(ipc, "request", status)
    assert doctor._agent_ui_state() == (True, {"actions"})


def test_without_an_agent_doctor_probes_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    def none_running(msg: dict[str, Any], timeout_s: float = 3) -> dict[str, Any]:
        raise ipc.AgentNotRunning("no agent")

    monkeypatch.setattr(ipc, "request", none_running)
    assert doctor._agent_ui_state() is None
    monkeypatch.setattr(ipc, "request", lambda msg, timeout_s=3: {"ok": True})  # old agent
    assert doctor._agent_ui_state() is None
