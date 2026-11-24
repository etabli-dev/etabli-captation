# Einwilligungs-Workflow

> **Vorlage.** Von der Ethikkommission bzw. der/dem Datenschutzbeauftragten zu prüfen. Keine
> Rechtsberatung.

## Ablauf einer Studiensitzung

1. **Vorab: schriftliche Einwilligung** aller Teilnehmenden einholen (Studieninformation,
   Zweck, Aufnahmeart Ton/Bild, Speicherung, Rücktrittsrecht). Die Erklärung erhält eine
   Referenz, z. B. `P-017`. Captation speichert nur diese Referenz, nie das Dokument.
2. **Studienmodus aktivieren:** Tray-Menü „Studienmodus aktivieren…“ oder
   `captation arm --ref P-017`. Das Symbol wird gelb. Ohne Zeitlimit (Standard, ADR-021) bleibt er
   an, bis Sie ihn beenden, sich abmelden oder neu starten; alle 4 Stunden erinnert eine
   Mitteilung daran. Mit `consent.auto_disarm_after_minutes` endet er nach der eingestellten Zeit.
3. **Gespräch beginnen.** Captation erkennt die Anruf-App und zeigt „Aufnahme läuft“ mit einem
   **Countdown** und „Abbrechen“; die Aufnahme läuft ab diesem Moment (ADR-022). Wer nicht
   aufgenommen werden soll, bricht innerhalb des Countdowns ab: dann wird das bis dahin
   Aufgenommene gelöscht und es bleibt keine Aufnahme.
4. **Rotes Symbol beachten.** Solange das Symbol rot ist, läuft die Aufnahme.
5. **Mündliche Bestätigung** zu Beginn der Aufnahme (siehe Ansagetext unten). Die Erinnerung
   erscheint automatisch.
6. **Später Hinzukommende** in Gruppengesprächen: brauchen eine eigene Einwilligung – sonst die
   Aufnahme beenden („Aufnahme beenden“) oder die Person bitten, das Gespräch zu verlassen.
7. **Nach dem Gespräch:** Die Aufnahme endet einige Sekunden nach dem Auflegen von selbst. Dann
   den Studienmodus beenden („Studienmodus beenden“ oder `captation disarm`).
8. **Ablage prüfen:** `data_root/<Projekt>/<Sitzung>/` enthält die Spuren, `session.json` und
   `SHA256SUMS`. Warnungen (z. B. stumme Spur) stehen in `session.json` unter `warnings`
   (Bedeutung der Codes: `specs/architecture.md` §8).

## Mobile Aufnahmen (am iPhone geführte Anrufe)

1. Einwilligung wie oben, Referenz notieren.
2. Aufnahme mit dem Hardware-Rekorder; Hersteller-Cloud und KI-Funktionen sind **aus**.
3. Datei lokal exportieren (USB oder App-Export ohne Cloud).
4. `captation import <datei> --device <gerät> --consent-ref P-018`.
5. Die Originaldatei auf dem Gerät nach erfolgreichem Import gemäß Studienprotokoll löschen.

## Ansagetext (Vorlage)

> „Bevor wir beginnen: Dieses Gespräch wird für die Studie *[Titel]* aufgezeichnet, wie in der
> Einwilligungserklärung beschrieben. Die Aufnahme läuft jetzt. Sind alle Anwesenden damit
> einverstanden? Bitte bestätigen Sie das kurz mündlich. Sie können die Aufnahme jederzeit
> beenden lassen.“

Englische Fassung:

> "Before we start: this conversation is being recorded for the study *[title]*, as described in
> the consent form. The recording is running now. Does everyone agree? Please confirm briefly.
> You can ask to stop the recording at any time."
