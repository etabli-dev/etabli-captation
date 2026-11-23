# Établi Captation

> [!CAUTION]
> **Approval required before any use and before every recording.**
>
> 1. **Using this software requires the prior written approval of the copyright holder.** The
>    source code is published for transparency only; publication grants no right of use
>    ([LICENSE](LICENSE)). Approvals and enquiries: etabli.dev@outlook.com.
> 2. **Every recording requires prior approval by everyone recorded:** their informed, documented
>    consent, given before the recording, and, for research, the approval of the responsible
>    ethics committee and data protection officer. Recording people without their consent is a
>    criminal offence in many countries (in Germany § 201 StGB).
> 3. **Before the first use, every user must accept [DISCLAIMER.md](DISCLAIMER.md) by ticking a
>    checkbox** (`captation accept`). Until then the software does not arm and imports nothing.
>
> **Genehmigung vor jeder Nutzung und vor jeder Aufnahme erforderlich.** Jede Nutzung bedarf der
> vorherigen schriftlichen Genehmigung des Rechteinhabers; jede Aufnahme der vorherigen,
> dokumentierten Einwilligung aller aufgenommenen Personen (in der Forschung zusätzlich Votum der
> Ethikkommission und Freigabe durch den Datenschutzbeauftragten). Vor der ersten Nutzung ist der
> [Haftungsausschluss](DISCLAIMER.md) per Checkbox zu bestätigen.

> Records calls and online meetings through OBS Studio, only while armed and with prior written consent.

Desktop agent for Linux (macOS and Windows follow) · Source available, all rights reserved · Part of the [Établi Suite](https://github.com/etabli-dev)

*Captation* is French for the recording of a live performance.

## Status

**Linux: built and tested end to end, not yet released.** macOS and Windows are prepared and
need their final steps on those machines (`docs/platform-status.md`, `docs/runbook-macos.md`,
`docs/runbook-windows.md`). Until a platform's facts are verified there, its release is blocked
by `tools/check_facts.py --release`. See `CHANGELOG.md`.

| Situation | Recording |
|---|---|
| Video conference: a meeting app (Zoom, Teams, Webex, Meet/Jitsi/BBB in the browser) **or** any call with the camera on | Video + multitrack audio (mix / remote / local) |
| Any other call on the computer (iPhone call relayed to the Mac, WhatsApp/Signal Desktop voice, …) | Audio only (multitrack) |
| Call taken on the iPhone itself | Recorded on mobile hardware (Plaud Note Pro, viaim RecDot, …), then `captation import` |
| Agent **not armed** | Nothing, ever |

## Contents

| Path | Content |
|---|---|
| `src/captation/` | The agent: probe, classifier, session state machine, OBS control, post-processing, import, tray |
| `specs/` | Architecture, JSON schemas and example documents (source of truth; mirrored into `src/captation/data/`) |
| `DISCLAIMER.md` | Disclaimer and conditions of use (English/German), accepted with a checkbox before the first use |
| `docs/` | Requirements, decisions (ADRs), platform facts, known limitations, security, licensing, data protection (German), consent workflow (German), hooks, import devices |
| `tests/` | Unit, integration (`obs`), end-to-end (`e2e`) tests |
| `tools/` | `sync_specs.py`, `check_facts.py`, `traceability.py`, `licence_gate.py`, `capture_pw_fixture.py` |
| `packaging/linux/` | PyInstaller build and per-user install script |

## Development

```
uv sync
uv run pytest -m "not obs and not e2e"   # unit tests (CI)
uv run pytest -m obs                      # needs a bootstrapped OBS
uv run pytest -m e2e                      # full end-to-end run (≈ 15 min)
uv run python tools/check_facts.py && uv run python tools/licence_gate.py
```

---

## Benutzerhandbuch (Deutsch)

### 1. Installation (Linux)

0. **Voraussetzung:** schriftliche Genehmigung des Rechteinhabers zur Nutzung (siehe oben).
1. **OBS Studio** (nicht enthalten) und das PipeWire-Audio-Plugin installieren:
   ```
   flatpak install --user flathub com.obsproject.Studio com.obsproject.Studio.Plugin.PipeWireAudioCapture
   ```
2. **Captation** installieren: das Archiv `etabli-captation-<version>-linux-x86_64.tar.gz`
   entpacken und `./install.sh` ausführen (legt `~/.local/bin/captation` an). Alternativ aus dem
   Quellcode: `uv sync` und `uv run captation …`.
3. **GNOME:** Die Erweiterung „AppIndicator“ (Ubuntu: *Ubuntu AppIndicators*) muss aktiv sein –
   ohne Tray-Symbol verweigert Captation den Studienmodus.
4. **Einrichtung:**
   ```
   captation accept                 # Haftungsausschluss lesen und per Checkbox bestätigen (Pflicht)
   captation obs bootstrap          # Profile, Szenensammlung, Websocket-Passwort (im Schlüsselbund)
   captation obs firewall --apply   # sperrt die OBS-Steuerschnittstelle fürs Netzwerk (einmal, sudo)
   captation obs approve-screen     # einmalige Freigabe der Bildschirmaufnahme (Wayland-Dialog)
   captation install-autostart      # startet Captation bei der Anmeldung – immer deaktiviert
   captation doctor                 # prüft alles; 0 = ok, 1 = Hinweise, 2 = Fehler
   ```
5. **Konfiguration:** `config/config.example.yaml` nach `~/.config/captation/config.yaml`
   kopieren und anpassen, mindestens `data_root` (auf einem **verschlüsselten** Datenträger,
   z. B. `/data/CaptationData`) und `project_id`.

### 2. Aufnehmen

1. Schriftliche Einwilligung einholen (siehe `docs/einwilligung-workflow.md`).
2. Studienmodus aktivieren: Tray-Menü **„Studienmodus aktivieren…“** und Referenz eingeben, oder
   `captation arm --ref P-017`. Symbol gelb = bereit.
3. Anruf starten. Captation zeigt einen **Countdown** mit **„Abbrechen“**. Danach ist das Symbol
   **rot** – die Aufnahme läuft. Jetzt die Einwilligung mündlich bestätigen lassen.
4. Kamera an während eines Tonanrufs → Captation wechselt automatisch auf Video.
5. Nach dem Auflegen endet die Aufnahme nach wenigen Sekunden; die Dateien werden geprüft.
6. **„Studienmodus beenden“** (oder `captation disarm`). Der Studienmodus endet auch
   automatisch nach der eingestellten Zeit.

Weitere Befehle: `captation status`, `captation stop` (Aufnahme beenden, bereit bleiben),
`captation cancel` (Countdown abbrechen).

### 3. Mobile Aufnahmen importieren

```
captation import <datei-oder-ordner> --device plaud_note_pro --consent-ref P-018 [--dry-run]
```
Geräte: `plaud_note_pro`, `viaim_recdot`, `recordergear_pr200`, `magmo_pro`, `generic`.
Dateinamen und Datei-Tags (oft Telefonnummern) werden nicht übernommen.

### 4. Dateien finden

`<data_root>/<Projekt-ID oder unassigned>/S-JJJJMMTT-HHMMSS-xxxx/`

| Datei | Inhalt |
|---|---|
| `session.json` | Metadaten, Warnungen, Prüfsummen |
| `segNN_mix.flac`, `segNN_remote.flac`, `segNN_local.flac` | Gesamtmix, Gegenseite, eigenes Mikrofon |
| `segNN_video.mp4` | Video (nur Videositzungen) |
| `segNN_audio.mka`, `segNN_container.mkv` | alle Tonspuren zusammen; OBS-Original |
| `SHA256SUMS` | Prüfsummen (`sha256sum -c SHA256SUMS`) |

### 5. Probleme

`captation doctor` nennt zu jedem Problem die Lösung. Bekannte Grenzen:
`docs/known-limitations.md`. Datenschutz: `docs/datenschutz.md`.

## Licence and disclaimer

Source available, **not open source**: all rights reserved, and any use requires the prior written
approval of the copyright holder ([LICENSE](LICENSE)). Approved use is subject to
[DISCLAIMER.md](DISCLAIMER.md), which every user accepts with a checkbox before the first use.
Third-party components keep their own licences ([docs/licensing.md](docs/licensing.md)).
