"""DISCLAIMER.md acceptance gate (docs/principles.md §2.10, R-59)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from captation import terms
from captation.ui.i18n import STRINGS, Strings
from captation.ui.terms_dialog import _plain_lines, terminal_checkbox

pytestmark = [pytest.mark.terms_gate, pytest.mark.req("R-59")]


@pytest.fixture
def record_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state" / terms.RECORD_NAME
    monkeypatch.setattr(terms, "record_path", lambda: path)
    return path


def test_packaged_text_matches_repository(repo_root: Path) -> None:
    assert terms.disclaimer_text() == (repo_root / "DISCLAIMER.md").read_text(encoding="utf-8")
    assert terms.text_version() != "unknown"


def test_strict_notice_is_in_the_text() -> None:
    lines = terms.disclaimer_text().splitlines()
    text = " ".join(line.removeprefix(">").strip() for line in lines)
    assert "prior written approval of the copyright holder" in text
    assert "vorherigen schriftlichen Genehmigung des Rechteinhabers" in text
    assert "consent of **all**" in text and "Einwilligung **aller**" in text


def test_not_accepted_without_record(record_file: Path) -> None:
    assert not record_file.exists()
    assert terms.current() is None and not terms.accepted()


def test_record_then_accepted_and_private(record_file: Path) -> None:
    acc = terms.record("terminal-checkbox")
    assert terms.accepted() and terms.current() == acc
    stored = json.loads(record_file.read_text(encoding="utf-8"))
    assert stored["sha256"] == terms.text_digest()
    assert stored["method"] == "terminal-checkbox"
    assert stored["version"] == terms.text_version()
    if record_file.stat().st_mode & 0o777 != 0o600:
        pytest.skip("POSIX file modes not enforced here")


def test_changed_text_needs_new_acceptance(
    record_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terms.record("dialog-checkbox")
    original = terms.disclaimer_text()
    monkeypatch.setattr(terms, "disclaimer_text", lambda: original + "\nAmended.\n")
    assert not terms.accepted()


@pytest.mark.parametrize(
    "content",
    ["", "{", "[]", '{"version": "x"}', json.dumps({"method": "flag", "sha256": "0"})],
)
def test_corrupt_or_forged_records_do_not_count(record_file: Path, content: str) -> None:
    record_file.parent.mkdir(parents=True)
    record_file.write_text(content, encoding="utf-8")
    assert not terms.accepted()


def test_unknown_method_is_rejected(record_file: Path) -> None:
    with pytest.raises(ValueError):
        terms.record("command-line-flag")
    assert not record_file.exists()


def test_revoke(record_file: Path) -> None:
    terms.record("dialog-checkbox")
    assert terms.revoke() and not terms.accepted()
    assert not terms.revoke()


@pytest.mark.parametrize(("answer", "ok"), [("x", True), (" X ", True), ("", False), ("y", False)])
def test_terminal_checkbox_needs_an_x(answer: str, ok: bool) -> None:
    shown: list[str] = []
    result = terminal_checkbox("TEXT", Strings("de"), show=shown.append, ask=lambda _p: answer)
    assert result is ok
    assert shown[0] == "TEXT" and shown[-1].startswith("[ ] ")


def test_dialog_text_joins_paragraphs_and_drops_markers() -> None:
    md = (
        "# Title\n\n> ## \u26a0\ufe0f Notice\n>\n"
        "> 1. **Bold** item\n>    continued\n> 2. next\n---\nplain\nwrapped"
    )
    assert _plain_lines(md) == [
        ("heading", "Title"),
        ("plain", ""),
        ("heading", "Notice"),
        ("plain", ""),
        ("plain", "1. Bold item continued"),
        ("plain", "2. next"),
        ("plain", ""),
        ("plain", "plain wrapped"),
    ]


def test_strings_exist_in_both_languages() -> None:
    keys = [k for k in STRINGS["de"] if k.startswith("terms.")] + ["refused.terms_not_accepted"]
    for key in keys:
        assert key in STRINGS["en"], key


def test_cli_accept_without_terminal_or_window_refuses(record_file: Path) -> None:
    from captation import cli

    result = CliRunner().invoke(cli.app, ["accept", "--terminal"], input="x\n")
    # CliRunner's stdin is not a TTY: there is no way to accept non-interactively.
    assert result.exit_code != 0
    assert not record_file.exists()


def test_cli_status_and_revoke(record_file: Path) -> None:
    from captation import cli

    runner = CliRunner()
    assert runner.invoke(cli.app, ["accept", "--status"]).exit_code != 0
    terms.record("dialog-checkbox")
    status = runner.invoke(cli.app, ["accept", "--status"])
    assert status.exit_code == 0 and terms.text_version() in status.output
    assert runner.invoke(cli.app, ["accept", "--revoke"]).exit_code == 0
    assert not record_file.exists()


def test_import_is_refused_without_acceptance(record_file: Path, tmp_path: Path) -> None:
    from captation import importer
    from tests.fixtures.media import make_config

    cfg = make_config(tmp_path)
    with pytest.raises(importer.ImportRefused, match="disclaimer"):
        importer.import_recordings(
            cfg,
            tmp_path,
            device="generic",
            consent_ref="P-017",
            started_at=None,
            app_id=None,
        )
