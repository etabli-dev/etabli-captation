# Platform status

State on 2026-11-23. Linux is built and tested end to end; macOS and Windows are prepared on
Linux (ADR-015) and need their final steps on their own machines.

| Stage | Linux | macOS | Windows |
|---|---|---|---|
| 01 Spec, requirements, decisions ⛳ | done | – | – |
| 02 Scaffold, config, CLI, IPC, tooling, CI | done | – | – |
| 03 OBS bootstrap, facts, firewall ⛳ (per OS) | done | prepared → `runbook-macos.md` §6 | prepared → `runbook-windows.md` §3 |
| 04 OBS control layer | done (integration on Linux) | run integration tests | run integration tests |
| 05/06/07 Detection | 05 done | 06 prepared (Swift probe not yet built) → §7 | 07 prepared → §4 |
| 08 Classifier and state machine | done | – | – |
| 09 Consent guard, tray, notifications | done (smoke test with a stub tray host; screen was locked) | smoke test → §11 | smoke test → §5 |
| 10 Post-processing, QC, storage | done | – | – |
| 11 Autostart, doctor, packaging (per OS) | done | → §11 | → §6 |
| 12 Tests and E2E ⛳ (per OS) | automated E1–E13 done; real-app checklist open | → §12 | → §7 |
| 13 Mobile import | done | – | – |
| 14 Release audit ⛳ | review done, findings fixed; owner decisions recorded (ADR-016, ADR-017) | – | – |

**Pending facts** (`python tools/check_facts.py --os <os>` lists them): macOS PF-101…128,
Windows PF-201…270. Each row in `docs/platform-facts.md` names the command that verifies it.
`check_facts.py --release` (release workflow) fails until every pending fact is verified.

## Before the session on the Mac or the Windows PC

1. `git clone` / `git pull` this repository (`etabli-dev/etabli-captation`).
2. Copy your local, untracked build notes from the Linux clone if you use them (`STATE.md`,
   `reports/`: git-ignored; the runbooks name the report files they expect).
3. Follow `docs/runbook-macos.md` or `docs/runbook-windows.md` from the top.

## Owner decisions (2026-11-23, ADR-016)

Proprietary licence · recordings only on encrypted storage (`block`) · OBS start-up egress
accepted · Windows fully supported in v1 · video = meeting window · Linux OBS = Flatpak ·
**v0.1.0 = Linux after the real-call checklist**, macOS/Windows as v0.2.0 · no preconfigured
hook · tray loss mid-call: continue with a warning · auto-disarm 240 min · macOS target 26.

ADR-017: the repository is public (source available, no licence granted); `DISCLAIMER.md` must be
accepted with a checkbox before the first use on every machine and for every user
(`captation accept`; the macOS and Windows runbooks verify the dialog there).
