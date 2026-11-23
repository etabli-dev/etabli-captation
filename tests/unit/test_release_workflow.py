"""The CI and release workflows keep their gates (tag = version, tests, per-OS facts, pinning)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.MULTILINE)


def load(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return doc


def runs(job: dict[str, Any]) -> str:
    """All shell commands of a job, one block per step."""
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def step(job: dict[str, Any], **match: str) -> dict[str, Any]:
    for s in job["steps"]:
        if all(text in str(s.get(key, "")) for key, text in match.items()):
            return dict(s)
    raise AssertionError(f"no step with {match}")


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_actions_are_pinned_to_full_commit_shas(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    uses = USES.findall(text)
    assert uses
    for ref, comment in uses:
        action, _, sha = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", sha), ref
        assert re.search(r"#\s*v\d", comment), f"{action}: version comment missing"


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_permissions_are_minimal(name: str) -> None:
    wf = load(name)
    assert wf["permissions"] == {"contents": "read"}
    for job_name, job in wf["jobs"].items():
        perms = job.get("permissions", {})
        assert set(perms) <= {"contents"}, job_name
        if perms.get("contents") == "write":
            # only jobs that upload to the draft release may write
            assert "softprops/action-gh-release" in str(job["steps"]), job_name


def test_release_checks_job_gates_every_build() -> None:
    jobs = load("release.yml")["jobs"]
    assert set(jobs) == {"checks", "linux", "macos", "windows"}
    checks = jobs["checks"]
    assert "permissions" not in checks  # read-only from the workflow default
    tag = step(checks, name="Tag matches")["run"]
    assert "GITHUB_REF_NAME" in tag and "captation.version import __version__" in tag
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" in tag and '"v${version}"' in tag
    commands = runs(checks)
    for command in (
        "tools/sync_specs.py --check",
        "ruff check src tests tools",
        "ruff format --check src tests tools",
        "mypy --strict src",
        'pytest -m "not obs and not e2e"',
        "tools/traceability.py --strict",
        "tools/check_facts.py --release --os linux",
    ):
        assert command in commands, command
    assert "age" in step(checks, name="Install")["run"]  # R-47 tests run, not skip
    platforms = step(checks, id="platforms")
    for os_name in ("macos", "windows"):
        assert checks["outputs"][os_name] == f"${{{{ steps.platforms.outputs.{os_name} }}}}"
        assert os_name in platforms["run"]
    assert "--release --os" in platforms["run"] and "::notice" in platforms["run"]


def test_release_builds_linux_always_and_other_platforms_only_when_verified() -> None:
    jobs = load("release.yml")["jobs"]
    assert jobs["linux"]["needs"] == "checks" and "if" not in jobs["linux"]
    for os_name in ("macos", "windows"):
        job = jobs[os_name]
        assert set(job["needs"]) == {"checks", "linux"}
        assert job["if"] == f"needs.checks.outputs.{os_name} == 'true'"
        assert f"tools/check_facts.py --release --os {os_name}" in runs(job)
        assert 'pytest -m "not obs and not e2e"' in runs(job)
    assert "tools/check_facts.py --release --os linux" in runs(jobs["linux"])
    # plain --release (every OS) would block a Linux-only release
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert not re.search(r"check_facts\.py --release(?! --os)", text)


def test_release_uploads_fail_on_missing_files_and_windows_needs_inno_setup() -> None:
    jobs = load("release.yml")["jobs"]
    for name, job in jobs.items():
        for s in job["steps"]:
            if "softprops/action-gh-release" in str(s.get("uses", "")):
                assert s["with"]["fail_on_unmatched_files"] is True, name
                assert s["with"]["draft"] is True, name
    windows = jobs["windows"]
    assert "innosetup" in step(windows, name="Install")["run"]
    assert "build.ps1 -RequireInstaller" in runs(windows)
    assert "swift test" in runs(jobs["macos"])


def test_ci_runs_age_strict_traceability_and_the_macos_probe() -> None:
    (job,) = load("ci.yml")["jobs"].values()
    install = step(job, name="Install")["run"]
    linux = next(line for line in install.splitlines() if "Linux)" in line)
    macos = next(line for line in install.splitlines() if "macOS)" in line)
    assert " age" in linux and " age" in macos
    strict = step(job, run="tools/traceability.py --strict")
    assert strict["if"] == "runner.os == 'Linux'"
    probe = step(job, run="swift build -c release")
    assert probe["if"] == "runner.os == 'macOS'" and "swift test" in probe["run"]
    assert probe["working-directory"] == "probes/macos/CaptationProbe"
    assert (WORKFLOWS.parents[1] / "probes" / "macos" / "CaptationProbe" / "Tests").is_dir()


def test_dependabot_updates_actions_and_uv_monthly() -> None:
    doc = yaml.safe_load((WORKFLOWS.parent / "dependabot.yml").read_text(encoding="utf-8"))
    assert doc["version"] == 2
    updates = {u["package-ecosystem"]: u for u in doc["updates"]}
    assert set(updates) == {"github-actions", "uv"}
    assert all(u["schedule"]["interval"] == "monthly" for u in updates.values())
