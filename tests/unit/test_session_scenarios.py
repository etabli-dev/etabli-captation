"""Scenario fixtures: probe timelines through ``classify`` and the session state machine.

Each ``tests/fixtures/scenarios/*.yaml`` is a timeline of probe snapshots and events with the
exact commands expected at every step (format: header comment of ``zoom_meeting.yaml``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from captation.models import AppRef, ProbeSnapshot
from captation.rules import classify
from captation.rulesfile import load_rules
from captation.session import (
    Arm,
    AutoDisarmDue,
    BeginSession,
    Cancel,
    CountdownElapsed,
    Disarm,
    Event,
    LowDisk,
    ManualStop,
    ObsBack,
    ObsLost,
    PostprocessDone,
    SegmentStopped,
    SessionParams,
    State,
    Tick,
)
from tests.unit.session_support import PARAMS, Driver, check_invariants, detection, log_steps

SCENARIOS = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios"
RULES = load_rules()

EVENTS: dict[str, Callable[[dict[str, Any]], Event]] = {
    "arm": lambda s: Arm(s.get("consent_ref"), tuple(s.get("blockers", ()))),
    "disarm": lambda s: Disarm(),
    "auto_disarm_due": lambda s: AutoDisarmDue(),
    "cancel": lambda s: Cancel(),
    "manual_stop": lambda s: ManualStop(),
    "obs_lost": lambda s: ObsLost(),
    "obs_back": lambda s: ObsBack(),
    "low_disk": lambda s: LowDisk(),
    "countdown_elapsed": lambda s: CountdownElapsed(),
    "segment_stopped": lambda s: SegmentStopped(s.get("ok", True)),
    "postprocess_done": lambda s: PostprocessDone(s.get("ok", True)),
}


def _load(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc


def _scenarios() -> list[object]:
    out = []
    for path in sorted(SCENARIOS.glob("*.yaml")):
        reqs = _load(path).get("reqs", [])
        out.append(pytest.param(path, id=path.stem, marks=pytest.mark.req(*reqs)))
    return out


def test_all_required_scenarios_exist() -> None:
    assert {p.stem for p in SCENARIOS.glob("*.yaml")} >= {
        "zoom_meeting",
        "signal_voice_to_video",
        "mic_blip_below_debounce",
        "cancel_during_countdown",
        "disarm_mid_call",
        "obs_crash_mid_call",
        "two_apps_tie_break",
        "signal_then_whatsapp_within_stop_debounce",
        "browser_without_titles_with_camera",
    }


@pytest.mark.parametrize("path", _scenarios())
def test_scenario(path: Path) -> None:
    doc = _load(path)
    params = SessionParams(**{**PARAMS.__dict__, **doc.get("params", {})})
    cfg = detection(
        start_debounce_s=params.start_debounce_s,
        stop_debounce_s=params.stop_debounce_s,
        **doc.get("detection", {}),
    )
    apps = {name: AppRef(**spec) for name, spec in doc["apps"].items()}
    exclude = frozenset(doc.get("exclude_pids", []))
    d = Driver(params=params)
    for i, step in enumerate(doc["steps"]):
        where = f"{path.name} step {i} (t={step['t']})"
        assert step["t"] >= d.clock.t, f"{where}: time goes backwards"
        d.clock.t = float(step["t"])
        if "event" in step:
            event = EVENTS[step["event"]](step)
        else:
            snap = ProbeSnapshot(
                ts_monotonic=d.clock.t,
                mic_users=tuple(apps[n] for n in step.get("mic", [])),
                camera_active=bool(step.get("camera", False)),
                camera_users=tuple(apps[n] for n in step.get("camera_users", [])),
                window_titles=(
                    {apps[n].pid: tuple(t) for n, t in step["titles"].items()}
                    if step.get("titles")
                    else None
                ),
            )
            context = classify(snap, RULES, cfg, d.m.current_app_id, exclude_pids=exclude)
            if "context" in step:
                got = None if context is None else f"{context.app_id}/{context.mode}"
                assert got == step["context"], f"{where}: classifier verdict"
            event = Tick(context)
        assert d.send_s(event) == step["expect"], f"{where}: commands"
        if "state" in step:
            assert d.state.name == step["state"], f"{where}: state"
    assert d.state is State[doc["final_state"]]
    begins = {c.session_id for c in d.commands() if isinstance(c, BeginSession)}
    assert len(begins) == doc["sessions"]
    check_invariants(log_steps(d))
