"""Record a sanitised ``pw-dump`` fixture for the Linux probe tests.

Keeps only Node and Client objects and a whitelist of properties the probe reads. Media
names, window titles, user and host names are dropped; device node names are replaced by
``device-N``. Usage: ``python tools/capture_pw_fixture.py <name> [--with-video-users]``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP_TYPES = {"PipeWire:Interface:Node", "PipeWire:Interface:Client"}
KEEP_PROPS = {
    "media.class",
    "client.id",
    "application.process.binary",
    "application.process.id",
    "application.name",
    "pipewire.sec.pid",
    "pipewire.access.portal.app_id",
    "node.name",
}
STREAM_NODE_NAMES = {"fakecall-e2e", "OBS", "Chromium", "Google Chrome", "pw-cat"}


def sanitise(objects: list[dict[str, object]]) -> list[dict[str, object]]:
    """Stream nodes and their own clients only: other apps on the desktop are never recorded."""
    streams = {
        o.get("id")
        for o in objects
        if o.get("type") == "PipeWire:Interface:Node"
        and str(((o.get("info") or {}).get("props") or {}).get("media.class", "")).startswith(
            "Stream/"
        )  # type: ignore[union-attr]
    }
    clients = {
        ((o.get("info") or {}).get("props") or {}).get("client.id")  # type: ignore[union-attr]
        for o in objects
        if o.get("id") in streams
    }
    objects = [o for o in objects if o.get("id") in streams or o.get("id") in clients]
    out: list[dict[str, object]] = []
    devices: dict[str, str] = {}
    for obj in objects:
        if obj.get("type") not in KEEP_TYPES:
            continue
        info = obj.get("info") or {}
        assert isinstance(info, dict)
        props = {k: v for k, v in (info.get("props") or {}).items() if k in KEEP_PROPS}
        name = props.get("node.name")
        if (
            isinstance(name, str)
            and name not in STREAM_NODE_NAMES
            and not name.startswith("captation-test")
        ):
            props["node.name"] = devices.setdefault(name, f"device-{len(devices) + 1}")
        entry: dict[str, object] = {
            "id": obj.get("id"),
            "type": obj.get("type"),
            "info": {"props": props},
        }
        if obj.get("type") == "PipeWire:Interface:Node":
            entry["info"]["state"] = info.get("state")  # type: ignore[index]
        out.append(entry)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--with-video-users", action="store_true")
    args = parser.parse_args()
    raw = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
    doc: dict[str, object] = {"pw_dump": sanitise(raw)}
    if args.with_video_users:
        sys.path.insert(0, str(ROOT / "src"))
        from captation.platform.linux import video_device_users

        doc["video_users"] = [
            {"pid": r.pid, "process_name": r.process_name} for r in video_device_users()
        ]
    target = ROOT / "tests" / "fixtures" / "linux" / f"{args.name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {target.relative_to(ROOT)} ({len(doc['pw_dump'])} objects)")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
