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
> **Genehmigung vor jeder Nutzung und vor jeder Aufnahme erforderlich.**
>
> 1. **Jede Nutzung dieser Software bedarf der vorherigen schriftlichen Genehmigung des
>    Rechteinhabers.** Der Quellcode wird nur zur Transparenz veröffentlicht; die Veröffentlichung
>    räumt kein Nutzungsrecht ein ([LICENSE](LICENSE)). Genehmigungen und Anfragen:
>    etabli.dev@outlook.com.
> 2. **Jede Aufnahme bedarf der vorherigen Zustimmung aller aufgenommenen Personen:** ihrer
>    informierten, dokumentierten Einwilligung vor der Aufnahme und, in der Forschung, des Votums
>    der zuständigen Ethikkommission und der Freigabe durch den Datenschutzbeauftragten. Wer
>    Personen ohne deren Einwilligung aufzeichnet, macht sich in vielen Ländern strafbar (in
>    Deutschland § 201 StGB).
> 3. **Vor der ersten Nutzung muss jede Nutzerin und jeder Nutzer den
>    [Haftungsausschluss](DISCLAIMER.md) per Checkbox bestätigen** (`captation accept`). Bis dahin
>    lässt sich der Studienmodus nicht aktivieren und es wird nichts importiert.

> Records calls and online meetings through OBS Studio for research with the prior written consent
> of all participants, and only while deliberately armed.

Desktop agent for Linux (macOS and Windows follow) · Source available, all rights reserved · Part of the [Établi Suite](https://github.com/etabli-dev)

*Captation* is French for the recording of a live performance.

## Status

**Linux: built; the automated end-to-end checks pass with test apps; the real-app checklist and the release are open.** macOS and Windows are prepared and
need their final steps on those machines (`docs/platform-status.md`, `docs/runbook-macos.md`,
`docs/runbook-windows.md`). Until a platform's facts are verified there, its release is blocked
by `tools/check_facts.py --release --os <os>`. See `CHANGELOG.md`.

| Situation | Recording |
|---|---|
| Meeting app: Zoom, Microsoft Teams, Cisco Webex, Jitsi, GoTo, **or** any call with the camera on | Video + multitrack audio (mix / remote / local) |
| Meeting in a browser: Teams, Zoom, Webex, Google Meet, BigBlueButton, Cisco Meeting Server, Jitsi, … | Video (also when the page cannot be identified; on Linux the approved screen) |
| Call: FaceTime and iPhone calls on the Mac, Windows Phone Link, Cisco Jabber, softphones | Audio only (multitrack); video when the camera is on |
| Messenger: WhatsApp, Signal, Telegram, Discord, Slack, Element (app or web) | Audio only (multitrack); video when the camera is on |
| Any other app that opens the microphone | Audio as `unknown` (`record_unknown_apps`, default on) |
| Call taken on the iPhone itself | The iPhone cannot run Captation: take the call on the Mac, or record on mobile hardware (Plaud Note Pro, viaim RecDot, …) and `captation import` |
| Agent **not armed** | Nothing, ever |

Coverage per OS, native and in the browser, and what is still to be confirmed on macOS and
Windows: [docs/app-coverage.md](docs/app-coverage.md).

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
4. **Haftungsausschluss bestätigen** (Pflicht, vor allem anderen):
   ```
   captation accept                 # Haftungsausschluss lesen und per Checkbox bestätigen
   ```
   `install.sh` ruft den Dialog bereits auf; wurde er dort abgebrochen, hier nachholen.
5. **Konfiguration:** die Beispieldatei kopieren und anpassen, mindestens `data_root` (auf einem
   **verschlüsselten** Datenträger, z. B. einer LUKS-verschlüsselten Datenpartition) und
   `project_id`. Die Beispieldatei liegt im Linux-Archiv als `config.example.yaml` (nach
   `install.sh` auch unter `~/.local/share/etabli-captation/current/`), im Quellcode als
   `config/config.example.yaml`.
   ```
   mkdir -p ~/.config/captation
   cp ~/.local/share/etabli-captation/current/config.example.yaml ~/.config/captation/config.yaml
   captation check-config           # prüft die Datei; „ok <Prüfsumme>“ = gültig
   ```
   Ohne eigene Datei gelten die eingebauten Voreinstellungen (`data_root: ~/CaptationData`;
   liegt `data_root` auf einem unverschlüsselten Datenträger, wird der Studienmodus verweigert).
   Ort der Datei je Betriebssystem:

   | System | Konfigurationsdatei |
   |---|---|
   | Linux | `~/.config/captation/config.yaml` (bzw. `$XDG_CONFIG_HOME/captation/config.yaml`) |
   | macOS | `~/Library/Application Support/captation/config.yaml` |
   | Windows | `%LOCALAPPDATA%\etabli\captation\config.yaml` |
6. **OBS und System einrichten:**
   ```
   captation obs bootstrap          # Profile, Szenensammlung, Websocket-Passwort (im Schlüsselbund)
   captation obs firewall --apply   # sperrt die OBS-Steuerschnittstelle fürs Netzwerk (einmal, sudo)
   captation obs approve-screen     # einmalige Freigabe der Bildschirmaufnahme (Wayland-Dialog)
   captation install-autostart      # startet Captation bei der Anmeldung – immer deaktiviert
   captation doctor                 # prüft alles; 0 = ok, 1 = Hinweise, 2 = Fehler
   ```
7. **Headset:** jedes Headset funktioniert (Kabel, USB, Bluetooth, AirPods). Auf dem Mac nimmt
   Captation die eigene Spur von dem Mikrofon auf, das die Anruf-App gerade verwendet, auch wenn
   während des Gesprächs ein Headset verbunden oder abgenommen wird (`local_input: follow_call`).
   Über Lautsprecher gelangen die Stimmen der Gegenseite in die eigene Spur (Echo);
   Bluetooth-Headsets wechseln bei geöffnetem Mikrofon in ein Profil mit schlechterer
   Tonqualität. Die beste Qualität bringt ein Kabel- oder USB-Headset. Unter Linux und Windows
   `recording.audio.local_input` auf das Mikrofon der Anruf-App setzen.

### 2. Aufnehmen

1. Schriftliche Einwilligung einholen (siehe `docs/einwilligung-workflow.md`).
2. Studienmodus aktivieren: Tray-Menü **„Studienmodus aktivieren…“** und Referenz eingeben, oder
   `captation arm --ref P-017`. Symbol gelb = bereit.
3. Anruf starten. Nach etwa einer Sekunde zeigt Captation „Aufnahme läuft“ mit **„Abbrechen“**;
   das Symbol ist **rot**. „Abbrechen“ innerhalb des Countdowns beendet die Aufnahme und löscht
   sie (ADR-022). Danach die Einwilligung mündlich bestätigen lassen.
4. Kamera an während eines Tonanrufs → Captation wechselt automatisch auf Video.
5. Nach dem Auflegen endet die Aufnahme nach wenigen Sekunden; die Dateien werden geprüft.
6. **„Studienmodus beenden“** (oder `captation disarm`). Ohne Zeitlimit (Standard, ADR-021)
   bleibt der Studienmodus an, bis Sie ihn beenden, sich abmelden oder neu starten; alle 4 Stunden
   erinnert eine Mitteilung daran (mit „Studienmodus beenden“). Mit
   `consent.auto_disarm_after_minutes` (5–720) endet er stattdessen nach der eingestellten Zeit.

Weitere Befehle: `captation status`, `captation stop` (Aufnahme beenden, bereit bleiben),
`captation cancel` (Countdown abbrechen).

**Im Hintergrund:** Captation hat kein Fenster und (macOS) kein Dock-Symbol. Mit
`captation install-autostart` startet es bei jeder Anmeldung, immer mit beendetem Studienmodus,
und nach einem Absturz von selbst neu. Sichtbar ist nur das Symbol in der Menüleiste: grau = aus,
gelb = Studienmodus, rot = Aufnahme. Wer die App erneut öffnet (Finder, Spotlight, Launchpad),
bekommt das Menü des Symbols.

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
| `session.json` | Metadaten, Warnungen (Bedeutung: `specs/architecture.md` §8), Prüfsummen |
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
