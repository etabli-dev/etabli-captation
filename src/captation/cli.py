"""Command-line interface (Typer)."""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from captation import ipc
from captation.config import Config, ConfigError, load_config
from captation.version import __version__

EXIT_OK = 0
EXIT_WARN = 1
EXIT_ERROR = 2
EXIT_NOT_IMPLEMENTED = 3

app = typer.Typer(
    name="captation",
    help="Établi Captation: records calls and meetings through OBS, only while armed.",
    no_args_is_help=True,
    add_completion=False,
)
obs_app = typer.Typer(help="OBS Studio setup and checks.", no_args_is_help=True)
app.add_typer(obs_app, name="obs")

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Config file (default: user config dir)")
]


def not_implemented(stage: str) -> NoReturn:
    typer.echo(f"not implemented yet (stage {stage})", err=True)
    raise typer.Exit(EXIT_NOT_IMPLEMENTED)


def fail(message: str, code: int = EXIT_ERROR) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _send(msg: dict[str, Any], timeout_s: float = 30) -> dict[str, Any]:
    try:
        reply = ipc.request(msg, timeout_s=timeout_s)
    except ipc.AgentNotRunning:
        fail("agent is not running (start it with `captation run` or at login)")
    except ipc.IpcError as exc:
        fail(f"IPC error: {exc}")
    if not isinstance(reply, dict):
        fail("unexpected reply from the agent")
    return reply


def _print_reply(reply: dict[str, Any]) -> None:
    typer.echo(json.dumps(reply, ensure_ascii=False, indent=2))
    if not reply.get("ok", False):
        raise typer.Exit(EXIT_ERROR)


# ---- agent control ----------------------------------------------------------------------------


@app.command()
def run(config: ConfigOpt = None) -> None:
    """Run the agent (tray + detection). Starts disarmed."""
    from captation import service
    from captation.obs.launcher import ObsNotFound

    try:
        cfg = load_config(config)
        service.run(cfg, str(config) if config is not None else None)
    except ConfigError as exc:
        fail(str(exc))
    except service.AlreadyRunning as exc:
        # Exit 0: launchd (KeepAlive SuccessfulExit=false) and systemd (Restart=on-failure)
        # must not restart a second instance in a loop.
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_OK) from exc
    except ObsNotFound as exc:
        fail(str(exc))


def open_app() -> None:
    """The app was opened without arguments (Finder, Spotlight, Launchpad): the running agent
    opens its menu-bar menu; if none runs, the agent starts here, in the background."""
    try:
        reply = ipc.request({"type": "show"}, timeout_s=5)
    except ipc.IpcError:
        reply = None
    if isinstance(reply, dict) and reply.get("ok"):
        return
    try:
        from captation.platform import get_platform

        start_now = getattr(get_platform().autostart(), "start_now", None)
        if start_now is not None and start_now():
            return  # launchd starts the agent (and restarts it after a crash)
    except Exception:  # no autostart on this platform, or launchctl failed: start here
        pass
    run(None)


@app.command()
def arm(
    ref: Annotated[str | None, typer.Option("--ref", help="Consent reference")] = None,
) -> None:
    """Arm the running agent (study mode)."""
    msg: dict[str, Any] = {"type": "arm"}
    if ref is not None:
        msg["consent_ref"] = ref
    _print_reply(_send(msg, timeout_s=90))  # may launch OBS first


@app.command()
def disarm() -> None:
    """Disarm the running agent. A running recording is stopped and finalised."""
    _print_reply(_send({"type": "disarm"}, timeout_s=180))  # finalises a running session


@app.command()
def status() -> None:
    """Show the agent state."""
    _print_reply(_send({"type": "status"}))


@app.command()
def stop() -> None:
    """Stop the current recording (the agent stays armed)."""
    _print_reply(_send({"type": "stop"}))


@app.command()
def cancel() -> None:
    """Cancel a pending start during the countdown."""
    _print_reply(_send({"type": "cancel"}))


@app.command(hidden=True)
def watchdog(config: ConfigOpt = None) -> None:
    """Internal: started by the agent; stops a Captation recording if the agent dies."""
    from captation import watchdog as wd
    from captation.logs import setup_logging

    try:
        cfg = load_config(config, create_data_root=False)
    except ConfigError as exc:
        fail(str(exc))
    setup_logging(cfg.logging.level, cfg.logging.dir, cfg.data_root_path)
    wd.watch(sys.stdin, lambda: wd.stop_captation_recording(cfg))


# ---- disclaimer acceptance (R-59) ----------------------------------------------------------------


@app.command()
def accept(
    terminal: Annotated[
        bool, typer.Option("--terminal", help="Checkbox prompt in this terminal, no window")
    ] = False,
    show_status: Annotated[
        bool, typer.Option("--status", help="Only show whether the current text is accepted")
    ] = False,
    revoke: Annotated[bool, typer.Option("--revoke", help="Withdraw the acceptance")] = False,
    config: ConfigOpt = None,
) -> None:
    """Read DISCLAIMER.md and accept it with a checkbox (required before the first use)."""
    from captation import terms
    from captation.ui.i18n import Strings
    from captation.ui.terms_dialog import NoDisplay, show_dialog, terminal_checkbox

    language: Any = "de"
    with contextlib.suppress(ConfigError):
        language = load_config(config, create_data_root=False).language
    strings = Strings(language)

    def report(acc: terms.Acceptance) -> None:
        typer.echo(
            strings("terms.accepted", version=acc.version, at=acc.accepted_at, method=acc.method)
        )

    if revoke:
        terms.revoke()
        typer.echo(strings("terms.revoked"))
        return
    if show_status:
        acc = terms.current()
        if acc is None:
            fail(strings("terms.missing"))
        report(acc)
        return
    text = terms.disclaimer_text()
    method = "dialog-checkbox"
    ok: bool | None = None
    if not terminal:
        try:
            ok = show_dialog(text, strings)
        except NoDisplay:
            ok = None
    if ok is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            fail(strings("terms.no_tty"))
        method = "terminal-checkbox"
        ok = terminal_checkbox(text, strings, show=typer.echo, ask=input)
    if not ok:
        fail(strings("terms.declined"))
    report(terms.record(method))


# ---- checks and setup -------------------------------------------------------------------------


def _stream_probe(cfg: Config, count: int | None) -> None:
    """Print live probe snapshots as JSON lines (one per poll interval)."""
    import time

    from captation.platform import get_platform

    probe = get_platform().probe(exclude_pids=frozenset(), store_titles=False)
    interval = cfg.detection.poll_interval_ms / 1000
    n = 0
    try:
        while count is None or n < count:
            snap = probe.snapshot()
            line = {
                "t": round(snap.ts_monotonic, 2),
                "mic": [[r.pid, r.process_name, r.parent_app] for r in snap.mic_users],
                "camera_active": snap.camera_active,
                "camera": [[r.pid, r.process_name] for r in snap.camera_users],
                "confidence": snap.confidence,
            }
            typer.echo(json.dumps(line, ensure_ascii=False))
            n += 1
            if count is None or n < count:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        probe.close()


@app.command()
def doctor(
    config: ConfigOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    probe: Annotated[bool, typer.Option("--probe", help="Stream live probe snapshots")] = False,
    count: Annotated[int | None, typer.Option("--count", help="Stop after N snapshots")] = None,
) -> None:
    """Check the installation. Exit code 0 = ok, 1 = warnings, 2 = errors."""
    cfg = _load(config)
    if probe:
        _stream_probe(cfg, count)
        return
    from captation import doctor as doc

    checks = doc.run_checks(cfg)
    typer.echo(doc.render(checks, as_json))
    raise typer.Exit(doc.exit_code(checks))


def _load(config: Path | None) -> Config:
    try:
        return load_config(config)
    except ConfigError as exc:
        fail(str(exc))


@obs_app.command("bootstrap")
def obs_bootstrap(config: ConfigOpt = None) -> None:
    """Create the Captation profiles and scene collection and configure obs-websocket."""
    from captation.obs import bootstrap, facts, launcher, manage
    from captation.obs.client import ObsError
    from captation.platform import current_os

    cfg = _load(config)
    try:
        inst = manage.locate(cfg)
    except launcher.ObsNotFound as exc:
        fail(str(exc))
    typer.echo(f"OBS: {inst.kind} {inst.version or '?'}")
    state = bootstrap.ensure_websocket_config(inst, cfg, obs_running=manage.obs_running(cfg))
    if state == "needs_password":
        fail(
            "obs-websocket is already set up with a password Captation does not know.\n"
            "Store it with `captation obs set-password`, then run the bootstrap again."
        )
    if state == "obs_running":
        fail("OBS is running: close OBS once so the websocket config can be written, then rerun.")
    typer.echo(f"websocket config: {state}")
    report = bootstrap.BootstrapReport(websocket=state)
    try:
        manage.ensure_running(cfg, inst)
        ws = manage.connect(cfg)
    except (ObsError, OSError) as exc:
        fail(f"cannot reach OBS: {exc}")
    try:
        enc = facts.available_encoders(Path(inst.config_dir))
        bootstrap.Bootstrapper(
            cfg,
            ws,
            available_encoders=enc["audio"] | enc["video"],
            os_name=current_os(),
            report=report,
        ).run()
    except (ObsError, bootstrap.BootstrapError) as exc:
        fail(f"bootstrap failed: {exc}")
    finally:
        ws.close()
    typer.echo(f"OBS {report.obs_version}, obs-websocket {report.ws_version}")
    for line in report.changes or ["no changes (already bootstrapped)"]:
        typer.echo(f"  {line}")
    for line in report.warnings:
        typer.echo(f"  warning: {line}")


@obs_app.command("facts")
def obs_facts(
    config: ConfigOpt = None,
    out: Annotated[Path, typer.Option("--out")] = Path("docs/platform-facts.md"),
) -> None:
    """Query OBS and write this OS's section of docs/platform-facts.md."""
    from captation.obs import facts, firewall, launcher, manage
    from captation.obs.client import ObsError
    from captation.platform import current_os

    cfg = _load(config)
    try:
        inst = manage.locate(cfg)
        manage.ensure_running(cfg, inst)
        ws = manage.connect(cfg)
    except (launcher.ObsNotFound, ObsError, OSError) as exc:
        fail(str(exc))
    try:
        data = facts.collect(ws, Path(inst.config_dir), cfg.obs.scene_collection)
    except ObsError as exc:
        fail(str(exc))
    finally:
        ws.close()
    port = cfg.obs.websocket.port
    extra = {
        "OBS installation": f"{inst.kind} ({inst.command[-1]}), config dir "
        + inst.config_dir.replace(str(Path.home()), "~"),
        "Websocket bind": "all interfaces (not loopback-only)"
        if firewall.listening_publicly(port)
        else "loopback only",
        "Launch flags used": " ".join(launcher.launch_args(None, None, [])),
    }
    section = facts.render_section(current_os(), data, extra)
    if not out.exists():
        fail(f"{out} not found (run from the repository root or pass --out)")
    facts.write_section(out, current_os(), section)
    typer.echo(f"wrote the {current_os()} section of {out}")


@obs_app.command("approve-screen")
def obs_approve_screen(config: ConfigOpt = None) -> None:
    """One-time screen-capture approval (Wayland portal); stores the restore token."""
    from captation.obs import launcher, manage
    from captation.obs.client import ObsError

    cfg = _load(config)
    try:
        ctl = manage.make_controller(cfg)
        ctl.ensure_running()
        typer.echo("OBS shows the screen-sharing dialog now: choose the screen, tick")
        typer.echo("'Remember this selection' (if offered) and click 'Share'.")
        ok = ctl.approve_screen()
    except (launcher.ObsNotFound, ObsError) as exc:
        fail(str(exc))
    finally:
        with contextlib.suppress(Exception):
            ctl.close()
    if not ok:
        fail("no approval received (portal dialog cancelled or timed out)")
    typer.echo("screen capture approved; video segments will capture the screen")


@obs_app.command("set-password")
def obs_set_password() -> None:
    """Store an existing obs-websocket password in the OS keyring."""
    from captation.obs import credentials

    password = typer.prompt("obs-websocket password", hide_input=True)
    try:
        credentials.store_password(password)
    except ValueError as exc:
        fail(str(exc))
    typer.echo("stored in the OS keyring")


@obs_app.command("firewall")
def obs_firewall(
    config: ConfigOpt = None,
    check: Annotated[bool, typer.Option("--check")] = False,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    remove: Annotated[bool, typer.Option("--remove")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation")] = False,
) -> None:
    """Check, apply or remove the inbound block rule for the websocket port."""
    from captation.obs import firewall

    if sum([check, apply, remove]) != 1:
        fail("use exactly one of --check, --apply, --remove")
    cfg = _load(config)
    port = cfg.obs.websocket.port
    if apply or remove:
        action = "install" if apply else "remove"
        if not yes and not typer.confirm(
            f"This will {action} a system firewall rule for TCP port {port} "
            f"({'a UAC prompt follows' if sys.platform == 'win32' else 'needs sudo'}). Continue?"
        ):
            raise typer.Exit(EXIT_ERROR)
        try:
            ran = firewall.apply(port) if apply else firewall.remove()
        except RuntimeError as exc:
            fail(str(exc))
        for line in ran:
            typer.echo(f"  {line}")
    status_ = firewall.check(port)
    typer.echo(f"exposure check ({status_.method}): {'ok' if status_.ok else 'NOT MET'}")
    for line in status_.details:
        typer.echo(f"  {line}")
    if check and not status_.ok:
        raise typer.Exit(EXIT_ERROR)


@app.command("import")
def import_cmd(
    source: Annotated[Path, typer.Argument(help="File or directory")],
    device: Annotated[
        str,
        typer.Option(
            "--device",
            help="plaud_note_pro | viaim_recdot | recordergear_pr200 | magmo_pro | generic",
        ),
    ],
    consent_ref: Annotated[
        str | None, typer.Option("--consent-ref", help="Consent reference (required)")
    ] = None,
    started_at: Annotated[
        str | None,
        typer.Option("--started-at", help="ISO-8601 start, used if the file has no timestamp"),
    ] = None,
    app_id: Annotated[
        str | None, typer.Option("--app-id", help="App ID (default: mobile_<device>)")
    ] = None,
    recursive: Annotated[bool, typer.Option("--recursive", help="Include subfolders")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be imported (no file names)")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-import a known source")] = False,
    config: ConfigOpt = None,
) -> None:
    """Import recordings from a mobile recorder. Output never shows file names."""
    from datetime import datetime

    from captation import importer
    from captation.logs import setup_logging

    try:
        cfg = load_config(config)
    except ConfigError as exc:
        fail(str(exc))
    start: datetime | None = None
    if started_at is not None:
        try:
            start = datetime.fromisoformat(started_at)
        except ValueError:
            fail("import refused: --started-at is not an ISO-8601 timestamp")
        if start.tzinfo is None:
            start = start.astimezone()
    setup_logging(cfg.logging.level, cfg.logging.dir, cfg.data_root_path)
    try:
        results = importer.import_recordings(
            cfg,
            source,
            device=device,
            consent_ref=consent_ref,
            started_at=start,
            app_id=app_id,
            recursive=recursive,
            dry_run=dry_run,
            force=force,
        )
    except importer.ImportRefused as exc:
        fail(str(exc))
    except Exception as exc:
        # Tracebacks and OS error messages can contain the source path (file names may hold
        # phone numbers), so only the exception type is reported.
        fail(f"import failed: {type(exc).__name__}")
    for result in results:
        typer.echo(result.describe())
    ok_states = {"would_import"} if dry_run else {"imported"}
    if not results:
        fail("nothing to import", EXIT_WARN)
    if any(r.status not in ok_states for r in results):
        raise typer.Exit(EXIT_ERROR)


@app.command("install-autostart")
def install_autostart(
    systemd: Annotated[bool, typer.Option("--systemd", help="Use a systemd user unit")] = False,
) -> None:
    """Start the agent at login (always disarmed)."""
    from captation.platform import get_platform

    try:
        installed = get_platform().autostart().install(systemd=systemd)
    except (RuntimeError, OSError) as exc:
        fail(f"autostart could not be installed: {exc}")
    for path in installed:
        typer.echo(f"installed {path}")


@app.command("uninstall-autostart")
def uninstall_autostart() -> None:
    """Remove everything install-autostart created."""
    from captation.platform import get_platform

    removed = get_platform().autostart().uninstall()
    for path in removed or []:
        typer.echo(f"removed {path}")
    if not removed:
        typer.echo("nothing to remove")
    typer.echo("note: the firewall rule is separate (`captation obs firewall --remove`)")


@app.command("check-config")
def check_config(config: ConfigOpt = None) -> None:
    """Validate the configuration and print its digest."""
    try:
        cfg = load_config(config, create_data_root=False)
    except ConfigError as exc:
        fail(str(exc))
    typer.echo(f"ok {cfg.digest()}")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)
