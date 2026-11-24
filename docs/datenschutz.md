# Datenschutz – Établi Captation

> **Vorlage.** Dieses Dokument beschreibt die technische Umsetzung, um den Ethikantrag und das
> Verzeichnis von Verarbeitungstätigkeiten zu unterstützen. Es ist keine Rechtsberatung und muss
> von der Ethikkommission bzw. der/dem Datenschutzbeauftragten geprüft werden.

## 1. Zweck

Établi Captation zeichnet Forschungsgespräche (Telefonate, Videokonferenzen) auf, für die vorab
eine schriftliche Einwilligung **aller** Teilnehmenden vorliegt. Das Programm nimmt nur im
bewusst aktivierten **Studienmodus** auf; außerhalb davon werden weder Mikrofon noch Kamera
ausgewertet.

## 2. Was erfasst wird

| Daten | Wann | Form |
|---|---|---|
| Ton des Gesprächs | im Studienmodus, wenn eine Anruf-App das Mikrofon nutzt | Mehrspur: Mix, Gegenseite (nur die Anruf-App), eigenes Mikrofon, optional externes Gerät |
| Bild | nur bei Videokonferenzen bzw. eingeschalteter Kamera | Bildschirmaufnahme. Unter Linux/Wayland wird der einmalig freigegebene Bildschirm aufgenommen, nicht nur das Konferenzfenster (Hinweis in den Metadaten) |
| Metadaten (`session.json`) | je Sitzung | Sitzungs-ID, Projekt-ID, Einwilligungs-Referenz, App-Kennung (z. B. `zoom`), Zeiten, Spuren, Prüfsummen, technische Warnungen |
| Transkript und Zusammenfassung (`processed/`) | nach jeder Sitzung, lokal (ADR-023) | `transcript.json` (maschinenlesbar: Metadaten, Transkript mit Zeitmarken und Sprecherrolle Ich/Gegenseite, Zusammenfassung, Vereinbarungen, Aufgaben mit Verantwortlichen, Dringlichkeit und Fristen – nur sofern im Gespräch genannt) und `transcript.md`; erzeugt mit whisper.cpp und einem lokalen Sprachmodell, ohne Netzwerk |
| Importierte Aufnahmen | nur nach `captation import` | Audiodatei eines mobilen Aufnahmegeräts, unverändert (ohne Datei-Tags) |
| Bestätigung des Haftungsausschlusses | einmal je Benutzer und Textfassung (`captation accept`) | lokal im Statusverzeichnis: Textfassung, Prüfsumme, Zeitpunkt, Benutzername des Betriebssystems, Art der Bestätigung, Programmversion |

**Nicht** gespeichert werden: Fenstertitel (Voreinstellung), Namen, Telefonnummern,
Meeting-IDs, die ursprünglichen Dateinamen importierter Aufnahmen, Inhalte der
Einwilligungserklärungen (nur deren Referenz).

## 3. Speicherort und Benennung

- Ausschließlich lokal unter `data_root/<Projekt-ID oder unassigned>/<Sitzungs-ID>/`
  (Ordner nur für die eigene Benutzerkennung lesbar, 0700).
- Sitzungs-IDs `S-JJJJMMTT-HHMMSS-xxxx` und Dateinamen `segNN_<rolle>.<endung>` enthalten keine
  personenbezogenen Daten.
- Keine Cloud, keine Telemetrie, keine Update-Prüfung durch das Programm.
- **Hinweis zu OBS:** OBS Studio lädt bei jedem Start Listen von Streaming-Diensten von
  obsproject.com sowie von Twitch und Amazon IVS herunter. Dabei wird die IP-Adresse des Rechners
  übermittelt, aber keine Aufnahme. Abschaltbar ist das nur über eine Netzwerksperre für OBS.

## 4. Aufbewahrung

Standard: **keine automatische Löschung**. Optional `storage.retention_days`: dann werden ganze
Sitzungen nach N Tagen gelöscht (protokolliert; laufende Aufnahmen nie).

Auf dem Mac des Eigentümers gilt `storage.media_retention_days: 90` (ADR-023): **Ton und Bild**
einer Sitzung werden 90 Tage nach ihrem Ende gelöscht, **aber nur, wenn ihr Transkript
vollständig ist**. Erhalten bleiben `session.json`, die Prüfsummen und `processed/`
(Transkript, Zusammenfassung und `media_deleted.json` mit den gelöschten Dateien und deren
Prüfsummen). Ein unvollständiges oder fehlgeschlagenes Transkript verhindert die Löschung.

## 5. Technische und organisatorische Maßnahmen

| Maßnahme | Umsetzung |
|---|---|
| Einwilligungsschutz | Aufnahme nur im Studienmodus; Start immer deaktiviert; Studienmodus endet automatisch (`auto_disarm_after_minutes`) oder – ohne Zeitlimit (ADR-021) – beim Beenden, Abmelden oder Neustart, mit Erinnerung alle 4 h; er wird nie gespeichert |
| Transparenz | Countdown mit „Abbrechen“ vor jeder Aufnahme; während der Aufnahme sichtbares rotes Tray-Symbol; Erinnerung, die Einwilligung mündlich zu bestätigen. Diese Anzeigen lassen sich nicht abschalten; ohne Tray-Symbol wird der Studienmodus verweigert |
| Kein Vorlauf | Keine Pre-Roll-/Replay-Puffer: nichts vor dem Countdown wird aufgezeichnet |
| Lokale Speicherung | siehe Abschnitt 3 |
| Netzwerkschutz | Die OBS-Steuerschnittstelle ist passwortgeschützt; eine Firewall-Regel sperrt sie für das Netzwerk; ohne diese Regel wird der Studienmodus verweigert |
| Verschlüsselung | Prüfung, ob `data_root` auf einem verschlüsselten Datenträger liegt (Warnung oder Sperre); optional zusätzliche Dateiverschlüsselung mit `age` |
| Integrität | SHA-256-Prüfsummen (`SHA256SUMS`), fertige Dateien schreibgeschützt |
| Qualitätskontrolle | Warnung bei stummer Gegenseite oder stummem eigenem Mikrofon und bei Übersteuerung |
| Protokolle | ohne Titel, Namen und Dateiinhalte; Apps nur über ihre Kennung |

## 6. Mobile Aufnahmegeräte

Für Anrufe, die direkt am iPhone geführt werden, werden Hardware-Rekorder (z. B. Plaud Note Pro,
viaim RecDot) verwendet. Für Studiendaten gilt: Hersteller-Cloud und KI-Funktionen **aus**, nur
lokaler Export, Import mit `captation import --consent-ref <REF>`. Siehe `docs/import-devices.md`.

## 7. Bekannte Grenzen

- Bildschirmaufnahme kann andere Bildschirminhalte enthalten (Wayland: ganzer freigegebener
  Bildschirm). Andere Fenster vor der Aufnahme schließen.
- Die ersten Sekunden eines Gesprächs fehlen (Erkennung + Countdown); die mündliche Bestätigung
  der Einwilligung erst nach dem roten Symbol einholen.
- Später hinzukommende Personen in Gruppengesprächen sind nicht automatisch erfasst
  (siehe `docs/einwilligung-workflow.md`).
- Weitere: `docs/known-limitations.md`.
