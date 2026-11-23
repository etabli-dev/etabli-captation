# Licensing

## Project licence

Proprietary and **source available** (`LICENSE`, ADR-016, ADR-017): the repository is public for
transparency and review, but no licence is granted; any use requires the prior written approval
of the copyright holder, and every user accepts `DISCLAIMER.md` with a checkbox before the first
use (R-59). No licence headers are added to source files.

## OBS Studio (GPL-2.0-or-later)

OBS is used **only as a separate process** through obs-websocket v5 (docs/principles.md §4.1). Captation
does not link, patch, fork or bundle OBS or its plugins; the installer and `doctor` point to the
official download (Flathub `com.obsproject.Studio` on Linux). The obs-websocket client is an
in-house implementation on `websockets` (BSD-3-Clause); `obsws-python` (GPL-3.0-only) is not
used (ADR-002).

## No AGPL code

No code from AGPL projects (for example Cap) was copied into this repository. The only AGPL
candidate considered, Nuitka 4.x (AGPL-3.0-or-later), was rejected for packaging (ADR-005).

## Runtime dependencies (Linux)

`uv run python tools/licence_gate.py` lists the runtime closure with licences; it fails CI on any
GPL, AGPL or unknown licence. State on 2026-11-23: 35 packages in the runtime closure (33 installed on Linux, 2 Windows-only), 0 denied.
Main ones: pydantic (MIT), jsonschema (MIT), PyYAML (MIT), typer/click (MIT/BSD-3-Clause), rich
(MIT), keyring (MIT), SecretStorage (BSD-3-Clause), jeepney (MIT), platformdirs (MIT), websockets
(BSD-3-Clause), dbus-fast (MIT), cryptography (Apache-2.0 OR BSD-3-Clause).

**LGPL components bundled:** none on Linux. On macOS and Windows, `pystray` (LGPL-3.0) is a
runtime dependency again since ADR-015 (prepared on Linux, verified in stages 06/07). PyInstaller
keeps it as a separate, replaceable Python module inside the bundle, so users can relink it; the
macOS/Windows packages must ship its licence text and this note. The licence gate on Linux skips
packages that are only installed on macOS/Windows (pystray, Pillow, desktop-notifier, pyobjc,
winrt, psutil): run `tools/licence_gate.py` on each OS before its first build.

## Build-time tools

PyInstaller (GPL-2.0-or-later **with the bootloader exception**) builds the Linux bundle; the
exception allows distributing the built application under any licence. It is a build dependency
only (`uv sync --group build`).

## External tools at run time

`ffmpeg`/`ffprobe` and, if `storage.age_recipients` is set, `age` (BSD-3-Clause) are called as
separate programs and are not bundled.
