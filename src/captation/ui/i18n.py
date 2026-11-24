"""UI string tables. German is the default; every user-facing string lives here."""

from __future__ import annotations

from typing import Literal

Language = Literal["de", "en"]

STRINGS: dict[str, dict[str, str]] = {
    "de": {
        "app.title": "Établi Captation",
        "tray.disarmed": "Captation – nicht aktiv",
        "tray.armed": "Captation – Studienmodus aktiv (noch {minutes} min)",
        "tray.armed_open": "Captation – Studienmodus aktiv (bis Sie ihn beenden)",
        "tray.countdown": "Captation – Aufnahme startet in {seconds} s ({app})",
        "tray.recording": "Captation – AUFNAHME {glyph} {app} – {elapsed}",
        "menu.arm": "Studienmodus aktivieren…",
        "menu.disarm": "Studienmodus beenden",
        "menu.stop": "Aufnahme beenden",
        "menu.cancel": "Start abbrechen",
        "menu.status": "Status: {state}",
        "state.DISARMED": "nicht aktiv",
        "state.ARMED_IDLE": "bereit",
        "state.PENDING_START": "Anruf erkannt",
        "state.COUNTDOWN": "Countdown",
        "state.RECORDING": "Aufnahme läuft",
        "state.PENDING_STOP": "Aufnahme läuft (Anruf beendet?)",
        "state.STOPPING": "wird beendet",
        "state.POSTPROCESSING": "Nachbearbeitung",
        "state.ERROR": "OBS nicht erreichbar",
        "countdown.title": "Aufnahme startet in {seconds} s",
        "countdown.body": "{app} – zum Abbrechen „Abbrechen“ wählen.",
        "countdown.cancel": "Abbrechen",
        "countdown.title_live": "Aufnahme läuft – {seconds} s zum Abbrechen",
        "countdown.body_live": "{app} – „Abbrechen“ beendet die Aufnahme und löscht sie.",
        "reminder.title": "Aufnahme läuft",
        "reminder.default": "Aufnahme läuft – bitte die Einwilligung aller Teilnehmenden zu "
        "Beginn mündlich bestätigen lassen.",
        "armed.title": "Studienmodus aktiv",
        "armed.body": "Aufnahmen sind für {minutes} min möglich (Einwilligung: {ref}).",
        "armed.body_open": "Aufnahmen sind möglich, bis Sie den Studienmodus beenden; "
        "Erinnerung alle {hours} h (Einwilligung: {ref}).",
        "still_armed.title": "Studienmodus ist noch aktiv",
        "still_armed.body": "Anrufe werden nach dem Countdown aufgenommen. Beenden über "
        "„Studienmodus beenden“ oder das Symbol in der Menüleiste.",
        "still_armed.end": "Studienmodus beenden",
        "disarmed.title": "Studienmodus beendet",
        "auto_disarmed.body": "Die Zeit ist abgelaufen; der Studienmodus wurde beendet.",
        "refused.title": "Studienmodus nicht aktiviert",
        "refused.no_tray": "Kein Tray-Symbol verfügbar (GNOME: Erweiterung „AppIndicator“ "
        "aktivieren). Ohne sichtbare Anzeige wird nicht aufgenommen.",
        "refused.exposure_policy_unmet": "Die Firewall-Regel für den OBS-Websocket fehlt "
        "(`captation obs firewall --apply`).",
        "refused.disk_encryption_required": "Der Speicherort ist nicht verschlüsselt.",
        "refused.consent_ref_required": "Bitte eine Einwilligungs-Referenz angeben.",
        "refused.consent_ref_invalid": "Die Einwilligungs-Referenz hat nicht das erwartete Format.",
        "refused.obs_unavailable": "OBS ist nicht erreichbar.",
        "refused.shutting_down": "Captation wird gerade beendet.",
        "refused.terms_not_accepted": "Haftungsausschluss und Nutzungsbedingungen sind noch "
        "nicht bestätigt (`captation accept`).",
        "terms.title": "Établi Captation – Haftungsausschluss und Nutzungsbedingungen",
        "terms.intro": "Vor der ersten Nutzung bitte den vollständigen Text lesen. Jede Nutzung "
        "bedarf der vorherigen schriftlichen Genehmigung des Rechteinhabers; jede Aufnahme der "
        "vorherigen, dokumentierten Einwilligung aller aufgenommenen Personen.",
        "terms.scroll_hint": "Bitte bis zum Ende scrollen, um die Bestätigung freizuschalten.",
        "terms.tick_hint": "Zum Fortfahren das Kästchen ankreuzen und „Akzeptieren“ wählen.",
        "terms.checkbox": "Ich habe den Haftungsausschluss und die Nutzungsbedingungen "
        "vollständig gelesen und akzeptiere sie. Ich bestätige insbesondere, dass ich die "
        "Software nur mit vorheriger schriftlicher Genehmigung des Rechteinhabers verwende und "
        "jede Aufnahme nur mit vorheriger, dokumentierter Einwilligung aller aufgenommenen "
        "Personen anfertige (in der Forschung zusätzlich nach Votum der Ethikkommission und "
        "Freigabe durch den Datenschutzbeauftragten). Für die Rechtmäßigkeit jeder Aufnahme "
        "bin allein ich verantwortlich.",
        "terms.accept": "Akzeptieren",
        "terms.decline": "Ablehnen",
        "terms.terminal_prompt": "Zum Ankreuzen „x“ eingeben und Enter drücken "
        "(jede andere Eingabe lehnt ab): ",
        "terms.accepted": "Bestätigt: Fassung {version} am {at} ({method}).",
        "terms.declined": "Nicht bestätigt. Ohne Bestätigung lässt sich der Studienmodus nicht "
        "aktivieren und nichts importieren.",
        "terms.missing": "Noch nicht bestätigt (oder der Text hat sich geändert).",
        "terms.revoked": "Bestätigung widerrufen.",
        "terms.no_tty": "Die Bestätigung braucht ein Fenster oder ein interaktives Terminal; "
        "eine Bestätigung ohne Ankreuzen ist nicht möglich.",
        "ask_ref.title": "Studienmodus aktivieren",
        "ask_ref.prompt": "Einwilligungs-Referenz (z. B. P-017):",
        "notify.start_cancelled": "Der Start der Aufnahme wurde abgebrochen.",
        "notify.low_disk": "Zu wenig freier Speicher – keine Aufnahme.",
        "notify.obs_lost": "OBS ist abgestürzt oder wurde beendet. Falls OBS nach dem "
        "Neustart nach dem abgesicherten Modus fragt: „Normal ausführen“ wählen.",
        "notify.qc_warning": "Qualitätsprüfung: {warnings}",
        "notify.running_in_background": "Établi Captation läuft im Hintergrund. Das Symbol oben "
        "rechts in der Menüleiste zeigt an, ob aufgenommen wird (rot = Aufnahme).",
        "notify.recording_stopped": "OBS hat die Aufnahme selbst beendet (z. B. Speicher voll "
        "oder Encoder-Fehler). Captation versucht, die Aufnahme fortzusetzen.",
        "notify.start_failed": "Die Aufnahme konnte nicht gestartet werden; dieser Anruf wird "
        "nicht erneut versucht. Details: `captation doctor`.",
        "notify.countdown_failed": "Der Countdown konnte nicht angezeigt werden. Ohne "
        "sichtbaren Countdown wird nicht aufgenommen; der Studienmodus wurde beendet.",
        "notify.stop_failed": "OBS nimmt noch auf, obwohl die Aufnahme beendet werden sollte. "
        "Captation versucht es weiter; notfalls die Aufnahme in OBS stoppen.",
        "notify.disk_unencrypted": "Hinweis: Der Speicherort ist nicht verschlüsselt.",
        "notify.indicator_lost": "Die Tray-Anzeige ist nicht mehr sichtbar.",
        "notify.fallback": "Benachrichtigungen ohne Aktionen – Abbrechen über das Tray-Menü.",
        "session.saved": "Aufnahme gespeichert ({session}).",
        "notify.session_failed": "Die Sitzung konnte nicht angelegt oder gespeichert werden "
        "(Speicherort prüfen: `captation doctor`).",
    },
    "en": {
        "app.title": "Établi Captation",
        "tray.disarmed": "Captation – not active",
        "tray.armed": "Captation – study mode on ({minutes} min left)",
        "tray.armed_open": "Captation – study mode on (until you end it)",
        "tray.countdown": "Captation – recording starts in {seconds} s ({app})",
        "tray.recording": "Captation – RECORDING {glyph} {app} – {elapsed}",
        "menu.arm": "Start study mode…",
        "menu.disarm": "End study mode",
        "menu.stop": "Stop recording",
        "menu.cancel": "Cancel start",
        "menu.status": "Status: {state}",
        "state.DISARMED": "not active",
        "state.ARMED_IDLE": "ready",
        "state.PENDING_START": "call detected",
        "state.COUNTDOWN": "countdown",
        "state.RECORDING": "recording",
        "state.PENDING_STOP": "recording (call ended?)",
        "state.STOPPING": "stopping",
        "state.POSTPROCESSING": "post-processing",
        "state.ERROR": "OBS unreachable",
        "countdown.title": "Recording starts in {seconds} s",
        "countdown.body": "{app} – choose “Cancel” to stop it.",
        "countdown.cancel": "Cancel",
        "countdown.title_live": "Recording – {seconds} s to cancel",
        "countdown.body_live": "{app} – “Cancel” stops the recording and deletes it.",
        "reminder.title": "Recording",
        "reminder.default": "Recording – please have every participant confirm their consent "
        "verbally at the start.",
        "armed.title": "Study mode on",
        "armed.body": "Recordings are possible for {minutes} min (consent: {ref}).",
        "armed.body_open": "Recordings are possible until you end study mode; reminder every "
        "{hours} h (consent: {ref}).",
        "still_armed.title": "Study mode is still on",
        "still_armed.body": "Calls are recorded after the countdown. End it with “End study "
        "mode” or the menu-bar icon.",
        "still_armed.end": "End study mode",
        "disarmed.title": "Study mode ended",
        "auto_disarmed.body": "Time is up; study mode has ended.",
        "refused.title": "Study mode not started",
        "refused.no_tray": "No tray icon available (GNOME: enable the “AppIndicator” "
        "extension). Nothing is recorded without a visible indicator.",
        "refused.exposure_policy_unmet": "The firewall rule for the OBS websocket is missing "
        "(`captation obs firewall --apply`).",
        "refused.disk_encryption_required": "The storage location is not encrypted.",
        "refused.consent_ref_required": "Please enter a consent reference.",
        "refused.consent_ref_invalid": "The consent reference has an unexpected format.",
        "refused.obs_unavailable": "OBS is not reachable.",
        "refused.shutting_down": "Captation is shutting down.",
        "refused.terms_not_accepted": "The disclaimer and conditions of use have not been "
        "accepted yet (`captation accept`).",
        "terms.title": "Établi Captation – disclaimer and conditions of use",
        "terms.intro": "Please read the whole text before the first use. Any use requires the "
        "prior written approval of the copyright holder; every recording requires the prior, "
        "documented consent of all persons recorded.",
        "terms.scroll_hint": "Scroll to the end to unlock the confirmation.",
        "terms.tick_hint": "Tick the box and choose “Accept” to continue.",
        "terms.checkbox": "I have read the disclaimer and conditions of use in full and accept "
        "them. In particular, I confirm that I use the software only with the prior written "
        "approval of the copyright holder, and make every recording only with the prior, "
        "documented consent of all persons recorded (and, for research, after approval by the "
        "ethics committee and the data protection officer). I alone am responsible for the "
        "lawfulness of every recording.",
        "terms.accept": "Accept",
        "terms.decline": "Decline",
        "terms.terminal_prompt": "Type “x” and press Enter to tick the box "
        "(anything else declines): ",
        "terms.accepted": "Accepted: version {version} on {at} ({method}).",
        "terms.declined": "Not accepted. Without acceptance the agent does not arm and nothing "
        "is imported.",
        "terms.missing": "Not accepted yet (or the text has changed).",
        "terms.revoked": "Acceptance withdrawn.",
        "terms.no_tty": "Acceptance needs a window or an interactive terminal; it cannot be "
        "given without ticking the box.",
        "ask_ref.title": "Start study mode",
        "ask_ref.prompt": "Consent reference (e.g. P-017):",
        "notify.start_cancelled": "The recording start was cancelled.",
        "notify.low_disk": "Not enough free disk space – nothing is recorded.",
        "notify.obs_lost": "OBS crashed or was closed. If OBS asks about Safe Mode after the "
        "restart, choose “Run Normally”.",
        "notify.qc_warning": "Quality check: {warnings}",
        "notify.running_in_background": "Établi Captation runs in the background. The icon in "
        "the menu bar shows whether a recording is running (red = recording).",
        "notify.recording_stopped": "OBS stopped the recording on its own (e.g. disk full or "
        "encoder error). Captation tries to continue the recording.",
        "notify.start_failed": "The recording could not be started; this call is not retried. "
        "Details: `captation doctor`.",
        "notify.countdown_failed": "The countdown could not be shown. Nothing is recorded "
        "without a visible countdown; study mode was turned off.",
        "notify.stop_failed": "OBS is still recording although the recording should stop. "
        "Captation keeps trying; if needed, stop the recording in OBS.",
        "notify.disk_unencrypted": "Note: the storage location is not encrypted.",
        "notify.indicator_lost": "The tray indicator is no longer visible.",
        "notify.fallback": "Notifications without actions – cancel from the tray menu.",
        "session.saved": "Recording saved ({session}).",
        "notify.session_failed": "The session could not be created or saved (check the "
        "storage location: `captation doctor`).",
    },
}

GLYPHS = {"audio": "🎙", "video": "🎥"}


class Strings:
    def __init__(self, language: Language = "de") -> None:
        self.language = language
        self._table = STRINGS[language]

    def __call__(self, key: str, **values: object) -> str:
        text = self._table.get(key) or STRINGS["en"][key]
        return text.format(**values) if values else text
