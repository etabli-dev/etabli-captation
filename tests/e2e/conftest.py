"""E2E fixtures: virtual audio nodes of the integration tests, and an isolated state directory.

The end-to-end suite drives the Linux desktop (PipeWire, D-Bus stub desktop); on other systems
it is not collected at all, because its imports need Linux-only dependencies.

The agent under test refuses to arm until DISCLAIMER.md is accepted (R-59). The suite never
touches the user's own acceptance: it points ``XDG_STATE_HOME`` at its own directory and writes a
simulated terminal-checkbox record there (user ``e2e-test``).
"""

import sys

if sys.platform != "linux":
    collect_ignore_glob = ["*.py"]
else:
    import json
    import os
    import shutil
    from collections.abc import Iterator
    from datetime import datetime
    from pathlib import Path

    import pytest
    from platformdirs import user_state_dir

    from captation import terms
    from captation.config import APP_AUTHOR, APP_NAME, ensure_private_dir
    from captation.version import __version__
    from tests.integration.conftest import virtual_audio, work_dir

    __all__ = ["e2e_state_home", "virtual_audio", "work_dir"]

    @pytest.fixture(scope="session", autouse=True)
    def e2e_state_home() -> Iterator[Path]:
        real_state = Path(user_state_dir(APP_NAME, APP_AUTHOR))
        home = Path.home() / ".cache" / "captation-e2e-state"
        shutil.rmtree(home, ignore_errors=True)
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(home)
        try:
            state = ensure_private_dir(Path(user_state_dir(APP_NAME, APP_AUTHOR)))
            token = real_state / "screencast-token"
            if token.exists():  # screen-capture approval is machine state, not consent
                shutil.copy2(token, state / token.name)
            text = terms.disclaimer_text()
            (state / terms.RECORD_NAME).write_text(
                json.dumps(
                    {
                        "version": terms.text_version(text),
                        "sha256": terms.text_digest(text),
                        "accepted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "user": "e2e-test",
                        "method": "terminal-checkbox",
                        "app_version": __version__,
                    }
                ),
                encoding="utf-8",
            )
            yield home
        finally:
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous
            shutil.rmtree(home, ignore_errors=True)
