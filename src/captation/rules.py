"""Detection and mode rules: data model, merging, compiling and classification.

Pure module: no file, process or clock access . Reading rules files lives in
``captation.rulesfile``.

A user rules document is merged into the default one by app ``id``: a user entry replaces the
default entry with the same id in place, new ids are appended, ``ignore`` lists are unioned and
the user's ``modes`` replace the defaults.

``classify`` implements architecture §5. It is a pure function of
``(snapshot, rules, detection config, current_app_id, exclude_pids)``.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from captation.errors import ConfigError
from captation.models import CATEGORY_PRIORITY, AppRef, CallContext, Category, Mode, ProbeSnapshot

if TYPE_CHECKING:
    from captation.config import DetectionConfig

UNKNOWN_APP_ID = "unknown"
WEB_MEETING_APP_ID = "web_meeting"  # detection.extra_meeting_titles

# Window-title normalisation before title rules (browser chrome is not part of the page title).
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_BROWSER_SUFFIX = re.compile(
    r"\s[-–—]\s(?:[^-–—]+\s[-–—]\s)?"
    r"(?:Google Chrome|Chromium|Mozilla Firefox|Firefox(?: Developer Edition)?|Nightly"
    r"|Microsoft Edge|Brave|Vivaldi|Opera|LibreWolf|Zen Browser|Zen|Floorp|Waterfox|Safari"
    r"|Arc|Web)$"
)


def normalise_title(title: str) -> str:
    """Drop invisible characters, turn no-break spaces into spaces and cut the browser's own
    suffix (" - Google Chrome", " — Mozilla Firefox", " - Profile - Microsoft Edge")."""
    text = _INVISIBLE.sub("", title).replace("\u00a0", " ").strip()
    return _BROWSER_SUFFIX.sub("", text).strip()


@functools.lru_cache(maxsize=32)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


FALLBACK_MODE: Mode = "audio"
FALLBACK_REASON = "default"


@dataclass(frozen=True)
class TitleRule:
    pattern: re.Pattern[str]
    category: Category
    app_id: str | None


@dataclass(frozen=True)
class AppRule:
    id: str
    category: Category
    process: tuple[re.Pattern[str], ...] = ()
    bundle_id: tuple[re.Pattern[str], ...] = ()
    package: tuple[re.Pattern[str], ...] = ()
    title_rules: tuple[TitleRule, ...] = ()
    verify: bool = False
    order: int = 0


@dataclass(frozen=True)
class ModeRule:
    category: Category | None
    camera_active: bool | None
    mode: Mode
    reason: str


@dataclass(frozen=True)
class Rules:
    apps: tuple[AppRule, ...]
    modes: tuple[ModeRule, ...]
    ignore_process: tuple[re.Pattern[str], ...] = ()
    ignore_bundle_id: tuple[re.Pattern[str], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def app(self, app_id: str) -> AppRule | None:
        return next((a for a in self.apps if a.id == app_id), None)


# ---- merging and compiling --------------------------------------------------------------------


def _compile_list(values: list[str] | None, where: str) -> tuple[re.Pattern[str], ...]:
    out = []
    for i, pattern in enumerate(values or []):
        try:
            out.append(re.compile(pattern))
        except re.error as exc:
            raise ConfigError(f"{where}[{i}]: regex does not compile: {exc}") from exc
    return tuple(out)


def merge_rules(default: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Merge a user rules document into the default one (see module docstring)."""
    apps: list[dict[str, Any]] = [dict(a) for a in default["apps"]]
    index = {a["id"]: i for i, a in enumerate(apps)}
    for entry in user["apps"]:
        if entry["id"] in index:
            apps[index[entry["id"]]] = dict(entry)
        else:
            index[entry["id"]] = len(apps)
            apps.append(dict(entry))
    ignore: dict[str, list[str]] = {}
    for key in ("process", "bundle_id"):
        merged = list(default.get("ignore", {}).get(key, []))
        merged += [p for p in user.get("ignore", {}).get(key, []) if p not in merged]
        if merged:
            ignore[key] = merged
    return {"version": 1, "apps": apps, "modes": list(user["modes"]), "ignore": ignore}


def compile_rules(raw: dict[str, Any]) -> Rules:
    apps = []
    for order, entry in enumerate(raw["apps"]):
        where = f"apps[{order}] ({entry['id']})"
        match = entry["match"]
        title_rules = tuple(
            TitleRule(
                pattern=_compile_list([t["pattern"]], f"{where}.title_rules")[0],
                category=t["category"],
                app_id=t.get("app_id"),
            )
            for t in entry.get("title_rules", [])
        )
        apps.append(
            AppRule(
                id=entry["id"],
                category=entry["category"],
                process=_compile_list(match.get("process"), f"{where}.match.process"),
                bundle_id=_compile_list(match.get("bundle_id"), f"{where}.match.bundle_id"),
                package=_compile_list(match.get("package"), f"{where}.match.package"),
                title_rules=title_rules,
                verify=bool(entry.get("verify", False)),
                order=order,
            )
        )
    modes = tuple(
        ModeRule(
            category=m["when"].get("category"),
            camera_active=m["when"].get("camera_active"),
            mode=m["mode"],
            reason=m["reason"],
        )
        for m in raw["modes"]
    )
    ignore = raw.get("ignore", {})
    return Rules(
        apps=tuple(apps),
        modes=modes,
        ignore_process=_compile_list(ignore.get("process"), "ignore.process"),
        ignore_bundle_id=_compile_list(ignore.get("bundle_id"), "ignore.bundle_id"),
        raw=raw,
    )


# ---- classification (architecture §5) ---------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    app: AppRef
    app_id: str
    category: Category
    order: int
    title: str | None


def _basename(name: str) -> str:
    """Last path component for both POSIX and Windows separators (no ``os.path``)."""
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _process_names(app: AppRef) -> tuple[str, ...]:
    """Names that ``process`` patterns are tried against: basename, exe basename, parent app."""
    names = [_basename(app.process_name)]
    if app.exe_path:
        names.append(_basename(app.exe_path))
    if app.parent_app:
        names.append(_basename(app.parent_app))
    return tuple(n for n in names if n)


def _any_search(patterns: tuple[re.Pattern[str], ...], values: tuple[str, ...]) -> bool:
    return any(p.search(v) for p in patterns for v in values)


def _is_ignored(app: AppRef, rules: Rules, exclude_pids: frozenset[int]) -> bool:
    if app.pid in exclude_pids:
        return True
    if _any_search(rules.ignore_process, _process_names(app)):
        return True
    return bool(app.bundle_id) and _any_search(rules.ignore_bundle_id, (app.bundle_id or "",))


def _rule_matches(rule: AppRule, app: AppRef) -> bool:
    if _any_search(rule.process, _process_names(app)):
        return True
    if app.bundle_id and _any_search(rule.bundle_id, (app.bundle_id,)):
        return True
    return bool(app.package_id) and _any_search(rule.package, (app.package_id or "",))


def _title_upgrade(rule: AppRule, titles: tuple[str, ...]) -> tuple[Category, str, str | None]:
    """Apply the rule's ``title_rules`` to the window titles of one process.

    The best match wins: highest category priority, then title-rule order, then the
    lexicographically smallest title (independent of the OS's window order). A title rule never
    lowers the category.
    """
    best: tuple[tuple[int, int, str], TitleRule, str] | None = None
    base_priority = CATEGORY_PRIORITY[rule.category]
    normalised = [(title, normalise_title(title)) for title in titles]
    for j, trule in enumerate(rule.title_rules):
        if CATEGORY_PRIORITY[trule.category] < base_priority:
            continue
        for title, clean in normalised:
            if trule.pattern.search(clean):
                key = (-CATEGORY_PRIORITY[trule.category], j, title)
                if best is None or key < best[0]:
                    best = (key, trule, title)
    if best is None:
        return rule.category, rule.id, None
    _, trule, title = best
    return trule.category, trule.app_id or rule.id, title


def _candidates(
    snap: ProbeSnapshot, rules: Rules, cfg: DetectionConfig, exclude_pids: frozenset[int]
) -> list[_Candidate]:
    out: list[_Candidate] = []
    titles_by_pid = snap.window_titles or {}
    for app in snap.mic_users:
        if _is_ignored(app, rules, exclude_pids):
            continue
        matched = False
        for rule in rules.apps:
            if not _rule_matches(rule, app):
                continue
            matched = True
            titles = titles_by_pid.get(app.pid, ())
            category, app_id, title = _title_upgrade(rule, titles)
            if category == "browser" and titles and cfg.extra_meeting_titles:
                # The user's own meeting pages (a BigBlueButton or Jitsi instance with its own
                # name, a branded Cisco Meeting Server web app).
                extra = _compiled(tuple(cfg.extra_meeting_titles))
                hits = sorted(t for t in titles if any(p.search(normalise_title(t)) for p in extra))
                if hits:
                    category, app_id, title = "meeting", WEB_MEETING_APP_ID, hits[0]
            out.append(_Candidate(app, app_id, category, rule.order, title))
        if not matched and cfg.record_unknown_apps:
            out.append(_Candidate(app, UNKNOWN_APP_ID, "unknown", len(rules.apps), None))
    return out


def _opt_key(value: str | None) -> tuple[bool, str]:
    return (value is None, value or "")


def _select(
    candidates: list[_Candidate], current_app_id: str | None, camera_pids: frozenset[int]
) -> _Candidate:
    """Deterministic choice (architecture §5.2 and §5.4).

    Priority first, then (a) the running session's app, (b) a PID that also uses the camera,
    (c) rule order, (d) the lowest PID. The remaining fields only make the order total, so the
    result never depends on the order of ``mic_users``.
    """

    def key(c: _Candidate) -> tuple[object, ...]:
        a = c.app
        return (
            -CATEGORY_PRIORITY[c.category],
            c.app_id != current_app_id,
            a.pid not in camera_pids,
            c.order,
            a.pid,
            c.app_id,
            a.process_name,
            _opt_key(a.exe_path),
            _opt_key(a.bundle_id),
            _opt_key(a.package_id),
            _opt_key(a.parent_app),
            _opt_key(c.title),
        )

    return min(candidates, key=key)


def _mode_for(category: Category, camera_active: bool, rules: Rules) -> tuple[Mode, str]:
    """First matching ``modes`` entry; ``audio``/``default`` if no entry matches."""
    for m in rules.modes:
        if m.category is not None and m.category != category:
            continue
        if m.camera_active is not None and m.camera_active != camera_active:
            continue
        return m.mode, m.reason
    return FALLBACK_MODE, FALLBACK_REASON


def classify(
    snap: ProbeSnapshot,
    rules: Rules,
    cfg: DetectionConfig,
    current_app_id: str | None,
    *,
    exclude_pids: frozenset[int] = frozenset(),
) -> CallContext | None:
    """Turn one probe snapshot into the call context, or ``None`` if there is no call.

    1. Mic users matching ``ignore`` (process name or bundle id) or listed in ``exclude_pids``
       (OBS, the agent) are dropped.
    2. ``process`` patterns are searched in the basename, the exe basename and ``parent_app``
       (browser helpers); ``bundle_id`` and ``package`` in the respective IDs.
    3. Title rules (only when the snapshot carries titles for that PID) may upgrade the
       category and set the app ID. Unknown apps count only with ``record_unknown_apps``.
    4. Highest priority wins; ties break deterministically (see ``_select``).
    5. Mode and reason come from the first matching ``modes`` entry.

    The window title is returned only if ``store_window_titles`` is set.
    """
    candidates = _candidates(snap, rules, cfg, exclude_pids)
    if not candidates:
        return None
    camera_pids = frozenset(a.pid for a in snap.camera_users)
    best = _select(candidates, current_app_id, camera_pids)
    mode, reason = _mode_for(best.category, snap.camera_active, rules)
    return CallContext(
        app_id=best.app_id,
        category=best.category,
        pid=best.app.pid,
        mode=mode,
        reason=reason,
        capture_target=best.app,
        window_title=best.title if cfg.store_window_titles else None,
    )
