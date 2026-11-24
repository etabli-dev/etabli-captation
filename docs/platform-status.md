# Platform status

State on 2026-11-23. Linux is built and tested end to end; macOS and Windows are prepared on
Linux (ADR-015) and need their final steps on their own machines.

| Area | Linux | macOS | Windows |
|---|---|---|---|
| Specification, requirements, decisions | done | – | – |
| Scaffold, configuration, CLI, IPC, tooling, CI | done | – | – |
| OBS setup and platform facts (bootstrap, facts, firewall) | done | prepared → `runbook-macos.md` §6 | prepared → `runbook-windows.md` §3 |
| OBS control layer | done (integration tests on Linux) | run integration tests | run integration tests |
| Call and camera detection | done | prepared (Swift probe not yet built) → §7 | prepared → §4 |
| Classifier and session state machine | done | – | – |
| Consent guard, tray, notifications | done (smoke test with a stub tray host; the screen was locked) | smoke test → §11 | smoke test → §5 |
| Post-processing, QC, storage | done | – | – |
| Autostart, doctor, packaging | done | → §11 | → §6 |
| End-to-end checks | automated E1–E13 done; real-app checklist open | → §12 | → §7 |
| Mobile import | done | – | – |
| Release review | review done, findings fixed; decisions recorded (ADR-016, ADR-017) | – | – |

**Pending facts** (`python tools/check_facts.py --os <os>` lists them): macOS PF-101…128,
Windows PF-201…270. Each row in `docs/platform-facts.md` names the command that verifies it.
`check_facts.py --release --os <os>` (release workflow) blocks that OS's build until its pending
facts are verified; Linux is released independently.

## On the Mac or the Windows PC

1. `git clone` / `git pull` this repository (`etabli-dev/etabli-captation`).
2. Follow `docs/runbook-macos.md` or `docs/runbook-windows.md` from the top.
3. Record verified facts in `docs/platform-facts.md` and the end-to-end results in the release
   notes.

## Decisions (2026-11-23, ADR-016)

Proprietary licence · recordings only on encrypted storage (`block`) · OBS start-up egress
accepted · Windows fully supported in v1 · video = meeting window · Linux OBS = Flatpak ·
**v0.1.0 = Linux after the real-call checklist**, macOS/Windows as v0.2.0 · no preconfigured
hook · tray loss mid-call: continue with a warning · auto-disarm 240 min · macOS target 26 (macOS: superseded by ADR-021: no time limit with a 4-hour reminder, macOS 27 reference, macOS first).

ADR-017: the repository is public (source available, no licence granted); `DISCLAIMER.md` must be
accepted with a checkbox before the first use on every machine and for every user
(`captation accept`; the macOS and Windows runbooks verify the dialog there).
