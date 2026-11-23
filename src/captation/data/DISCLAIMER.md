# Établi Captation — Disclaimer and conditions of use / Haftungsausschluss und Nutzungsbedingungen

Version: 2026-11-23

> ## ⚠️ Strict notice / Strenger Hinweis
>
> 1. **Any use of this software requires the prior written approval of the copyright holder
>    (R. Heller).** Publishing the source code does not grant any right of use. Without written
>    approval you may not run, install, copy or distribute it.
> 2. **Every recording requires prior approval:** the informed, documented consent of **all**
>    persons recorded, given **before** the recording, and — for research — the approval of the
>    responsible ethics committee and the data protection officer. Without these approvals the
>    software must not be used.
>
> 1. **Jede Nutzung dieser Software bedarf der vorherigen schriftlichen Genehmigung des
>    Rechteinhabers (R. Heller).** Die Veröffentlichung des Quellcodes räumt kein Nutzungsrecht
>    ein. Ohne schriftliche Genehmigung darf die Software weder ausgeführt, installiert,
>    vervielfältigt noch weitergegeben werden.
> 2. **Jede Aufnahme bedarf der vorherigen Zustimmung:** der informierten, dokumentierten
>    Einwilligung **aller** aufgenommenen Personen **vor** der Aufnahme und – in der Forschung –
>    des Votums der zuständigen Ethikkommission sowie der Freigabe durch den
>    Datenschutzbeauftragten. Ohne diese Zustimmungen darf die Software nicht verwendet werden.

The software asks you to confirm this text with a checkbox before its first use. Without that
confirmation it does not arm and does not import recordings. / Vor der ersten Nutzung fragt die
Software die Bestätigung dieses Textes per Checkbox ab. Ohne Bestätigung lässt sich der
Studienmodus nicht aktivieren und nichts importieren.

In case of differences, the German version (Deutsche Fassung) prevails.

---

## English version

### 1. Intended use

Établi Captation records calls and online meetings through OBS Studio **for research with the
prior written consent of all participants**. It only records while a user has deliberately
armed it, shows a countdown with a cancel option and a visible recording indicator, and stores
recordings locally.

**Prohibited uses** include, without limitation: covert or secret recording; recording any
person who has not consented; monitoring of employees, students, patients, partners, family
members or other persons; use for surveillance, profiling, law enforcement or intelligence
purposes; circumventing or disabling the consent safeguards; and any use that violates the law
of any place where a participant is located.

### 2. Your responsibility

You alone are responsible for using the software lawfully. Whoever decides to record is the
**controller** of the personal data (Art. 4(7) GDPR) and must, among other things:

- have a legal basis for every recording (Art. 6 GDPR; for health data and other special
  categories additionally Art. 9 GDPR) and obtain consent that meets Art. 7 GDPR;
- inform the participants beforehand (Art. 13/14 GDPR);
- carry out a data protection impact assessment where required (Art. 35 GDPR — likely for
  recordings of research conversations), keep records of processing (Art. 30 GDPR) and
  implement appropriate security (Art. 32 GDPR);
- honour data subject rights, including withdrawal of consent and erasure;
- ensure that any further processing (transcription, transfer to other systems or services,
  hooks) is covered by the consent and by contracts where needed.

### 3. Legal framework (examples, not exhaustive)

- **Germany:** recording the non-publicly spoken word of another person without consent is a
  criminal offence (§ 201 StGB). Image recordings may violate § 201a StGB and the right to one's
  own image (§§ 22, 23 KUG). Health professionals are bound by professional secrecy (§ 203 StGB)
  and their professional codes (e.g. § 15 MBO-Ä: advice of an ethics committee before research on
  humans). In employment settings, the rules on employee data protection apply (Art. 88 GDPR,
  § 26 BDSG), and technical devices suited to monitoring are subject to works-council
  co-determination (§ 87(1) no. 6 BetrVG); public employers are bound by staff-council law.
- **European Union:** the GDPR applies to every recording of identifiable persons.
- **Other countries:** laws differ widely. Some require the consent of all parties (e.g. several
  US states such as California, Penal Code § 632), others of one party (e.g. US federal law,
  18 U.S.C. § 2511(2)(d)); see also Austria § 120 StGB and Switzerland Art. 179bis and 179ter StGB.
  The law of every place where a participant is located may apply.
- **Platform terms:** the terms of service of meeting and messaging services (e.g. Zoom,
  Microsoft Teams, WhatsApp, Signal) may restrict or govern recording.

### 4. What consent must cover

Consent must be given in writing **before** the recording by **every** participant (including
persons who join later; otherwise they must be excluded or the recording stopped). It should
cover at least: the purpose, audio and/or video, where and how long recordings are stored, who
has access, any transcription or transfer, and the right to withdraw at any time. For minors or
persons unable to consent, the consent of their legal representatives is required. At the start
of each recording, consent should additionally be confirmed verbally so that it is on the
recording. The software stores only a consent *reference*, never the consent document.

### 5. Technical safeguards are not legal compliance

The consent guard, countdown, indicator, firewall rule, encryption check and quality checks
reduce risks; they do **not** make a recording lawful. Detection can fail (missed or late
starts, wrong app, other on-screen content in screen captures, silent tracks). OBS Studio itself
contacts third-party services at start-up (see the project documentation,
`docs/known-limitations.md` in the source repository). Check every recording and its warnings.

### 6. No legal advice

This text and the documentation are no legal advice. Obtain advice from your ethics committee,
data protection officer or a lawyer before use.

### 7. Warranty and liability

The software is provided "as is", without warranty of any kind, express or implied, including
fitness for a particular purpose, freedom from errors, completeness of recordings or legal
compliance. To the extent permitted by law, the copyright holder is not liable for any damage,
loss of data, fines or claims arising from the use or inability to use the software.
**This does not limit liability for intent or gross negligence, for injury to life, body or
health, under the German Product Liability Act, or where liability cannot be excluded or limited
by law.** For a slightly negligent breach of an essential obligation (one whose fulfilment makes
the proper use possible in the first place and on which you may regularly rely), liability is
limited to the foreseeable damage typical for this kind of provision. To the extent permitted by
law, you indemnify the copyright holder against third-party claims resulting from your unlawful
use of the software.

### 8. Third-party software

OBS Studio (GPL-2.0-or-later), FFmpeg and other components are separate programs under their own
licences. Captation does not include OBS Studio; it controls it as a separate process.

### 9. Acceptance

Before the first use, the software shows this text and asks you to tick a checkbox. The
acceptance (text version, a checksum of this text, time, operating-system user name, method of
acceptance and the version of the software) is stored locally in the user's state directory.
When this text changes, it must be accepted again. Acceptance does not replace the approvals in
the strict notice above.

### 10. Applicable law

German law applies, excluding the UN Convention on Contracts for the International Sale of
Goods. Mandatory consumer protection provisions of the country of your habitual residence remain
unaffected. Should a provision of these conditions be invalid, the remaining provisions stay in
force. Contact: etabli.dev@outlook.com.

---

## Deutsche Fassung

### 1. Bestimmungsgemäße Verwendung

Établi Captation zeichnet Telefonate und Online-Besprechungen über OBS Studio **für
Forschungszwecke mit vorheriger schriftlicher Einwilligung aller Beteiligten** auf. Aufgenommen
wird nur, solange der Studienmodus bewusst aktiviert ist; vor jeder Aufnahme erscheint ein
Countdown mit Abbruchmöglichkeit, während der Aufnahme ein sichtbares Aufnahmesymbol.
Aufnahmen werden lokal gespeichert.

**Unzulässig** ist insbesondere: heimliches oder verdecktes Aufzeichnen; das Aufzeichnen von
Personen ohne deren Einwilligung; die Überwachung von Beschäftigten, Studierenden, Patientinnen
und Patienten, Partnerinnen und Partnern, Familienangehörigen oder sonstigen Personen; der
Einsatz zu Überwachungs-, Profiling-, Strafverfolgungs- oder nachrichtendienstlichen Zwecken; das
Umgehen oder Abschalten der Einwilligungsschutzfunktionen; jede Nutzung, die gegen das Recht
eines Ortes verstößt, an dem sich eine beteiligte Person befindet.

### 2. Ihre Verantwortung

Für die rechtmäßige Nutzung sind allein Sie verantwortlich. Wer über eine Aufnahme entscheidet,
ist **Verantwortlicher** für die personenbezogenen Daten (Art. 4 Nr. 7 DSGVO) und muss unter
anderem:

- für jede Aufnahme eine Rechtsgrundlage haben (Art. 6 DSGVO; bei Gesundheitsdaten und anderen
  besonderen Kategorien zusätzlich Art. 9 DSGVO) und eine Einwilligung einholen, die Art. 7 DSGVO
  genügt;
- die Beteiligten vorab informieren (Art. 13/14 DSGVO);
- soweit erforderlich eine Datenschutz-Folgenabschätzung durchführen (Art. 35 DSGVO – bei der
  Aufzeichnung von Forschungsgesprächen regelmäßig naheliegend), ein Verzeichnis der
  Verarbeitungstätigkeiten führen (Art. 30 DSGVO) und angemessene Sicherheitsmaßnahmen treffen
  (Art. 32 DSGVO);
- die Betroffenenrechte wahren, einschließlich Widerruf der Einwilligung und Löschung;
- sicherstellen, dass jede Weiterverarbeitung (Transkription, Übertragung an andere Systeme oder
  Dienste, Hooks) von der Einwilligung und erforderlichenfalls von Verträgen gedeckt ist.

### 3. Rechtsrahmen (Beispiele, nicht abschließend)

- **Deutschland:** Wer das nichtöffentlich gesprochene Wort eines anderen ohne Einwilligung auf
  einen Tonträger aufnimmt, macht sich strafbar (§ 201 StGB). Bildaufnahmen können § 201a StGB
  und das Recht am eigenen Bild (§§ 22, 23 KUG) verletzen. Angehörige der Heilberufe unterliegen
  der Schweigepflicht (§ 203 StGB) und ihrem Berufsrecht (z. B. § 15 MBO-Ä: Beratung durch eine
  Ethikkommission vor Forschung am Menschen). Im Beschäftigungskontext gelten die Regeln zum
  Beschäftigtendatenschutz (Art. 88 DSGVO, § 26 BDSG); technische Einrichtungen, die zur
  Überwachung geeignet sind, unterliegen der Mitbestimmung des Betriebsrats (§ 87 Abs. 1 Nr. 6
  BetrVG), bei öffentlichen Arbeitgebern dem Personalvertretungsrecht.
- **Europäische Union:** Für jede Aufnahme identifizierbarer Personen gilt die DSGVO.
- **Andere Staaten:** Die Rechtslage ist sehr unterschiedlich. Manche verlangen die Einwilligung
  aller Beteiligten (z. B. mehrere US-Bundesstaaten wie Kalifornien, Penal Code § 632), andere die
  einer Partei (z. B. US-Bundesrecht, 18 U.S.C. § 2511(2)(d)); vgl. auch Österreich § 120 StGB und
  die Schweiz Art. 179bis und 179ter StGB. Das Recht jedes Ortes, an dem sich eine beteiligte
  Person befindet, kann anwendbar sein.
- **Plattformbedingungen:** Die Nutzungsbedingungen von Besprechungs- und Messengerdiensten
  (z. B. Zoom, Microsoft Teams, WhatsApp, Signal) können Aufzeichnungen einschränken oder regeln.

### 4. Inhalt der Einwilligung

Die Einwilligung ist **vor** der Aufnahme schriftlich von **jeder** beteiligten Person einzuholen
(auch von später Hinzukommenden; andernfalls sind diese auszuschließen oder ist die Aufnahme zu
beenden). Sie sollte mindestens umfassen: Zweck, Ton- und/oder Bildaufnahme, Speicherort und
Speicherdauer, Zugriffsberechtigte, etwaige Transkription oder Weitergabe sowie das Recht, die
Einwilligung jederzeit zu widerrufen. Bei Minderjährigen oder nicht einwilligungsfähigen Personen
ist die Einwilligung der gesetzlichen Vertretung erforderlich. Zu Beginn jeder Aufnahme sollte
die Einwilligung zusätzlich mündlich bestätigt werden, damit sie auf der Aufnahme dokumentiert
ist. Die Software speichert nur eine *Referenz* auf die Einwilligung, nie das Dokument selbst.

### 5. Technische Schutzfunktionen ersetzen keine Rechtmäßigkeit

Einwilligungsschutz, Countdown, Aufnahmesymbol, Firewall-Regel, Verschlüsselungsprüfung und
Qualitätskontrollen verringern Risiken; sie machen eine Aufnahme **nicht** rechtmäßig. Die
Erkennung kann fehlschlagen (verpasster oder verspäteter Start, falsche Anwendung, weitere
Bildschirminhalte bei Bildschirmaufnahmen, stumme Spuren). OBS Studio selbst kontaktiert beim
Start Dienste Dritter (siehe die Projektdokumentation, `docs/known-limitations.md` im
Quellcode-Repository). Prüfen Sie jede Aufnahme und ihre Warnungen.

### 6. Keine Rechtsberatung

Dieser Text und die Dokumentation sind keine Rechtsberatung. Holen Sie vor der Nutzung Rat bei
Ihrer Ethikkommission, Ihrem Datenschutzbeauftragten oder einer Rechtsanwältin bzw. einem
Rechtsanwalt ein.

### 7. Gewährleistung und Haftung

Die Software wird „wie besehen“ bereitgestellt, ohne ausdrückliche oder stillschweigende Gewähr,
insbesondere ohne Gewähr für Eignung zu einem bestimmten Zweck, Fehlerfreiheit, Vollständigkeit
von Aufnahmen oder Rechtskonformität. Soweit gesetzlich zulässig, haftet der Rechteinhaber nicht
für Schäden, Datenverluste, Bußgelder oder Ansprüche, die aus der Nutzung oder der
Nichtnutzbarkeit der Software entstehen. **Unberührt bleibt die Haftung für Vorsatz und grobe
Fahrlässigkeit, für Schäden aus der Verletzung von Leben, Körper oder Gesundheit, nach dem
Produkthaftungsgesetz sowie in allen Fällen, in denen die Haftung gesetzlich nicht ausgeschlossen
oder beschränkt werden kann.** Bei leicht fahrlässiger Verletzung einer wesentlichen Pflicht
(deren Erfüllung die ordnungsgemäße Nutzung überhaupt erst ermöglicht und auf deren Einhaltung
Sie regelmäßig vertrauen dürfen) ist die Haftung auf den vorhersehbaren, typischerweise
eintretenden Schaden begrenzt. Soweit gesetzlich zulässig, stellen Sie den Rechteinhaber von
Ansprüchen Dritter frei, die aus Ihrer rechtswidrigen Nutzung der Software entstehen.

### 8. Software Dritter

OBS Studio (GPL-2.0-or-later), FFmpeg und weitere Komponenten sind eigenständige Programme unter
eigenen Lizenzen. Captation enthält OBS Studio nicht, sondern steuert es als separaten Prozess.

### 9. Bestätigung

Vor der ersten Nutzung zeigt die Software diesen Text an und verlangt das Setzen einer Checkbox.
Die Bestätigung (Textversion, Prüfsumme dieses Textes, Zeitpunkt, Benutzername des
Betriebssystems, Art der Bestätigung und Version der Software) wird lokal im Statusverzeichnis
des Benutzers gespeichert. Ändert sich dieser Text, ist er erneut zu bestätigen. Die Bestätigung
ersetzt nicht die im strengen Hinweis oben genannten Zustimmungen.

### 10. Anwendbares Recht

Es gilt deutsches Recht unter Ausschluss des UN-Kaufrechts. Zwingende
Verbraucherschutzvorschriften des Staates Ihres gewöhnlichen Aufenthalts bleiben unberührt.
Sollte eine Bestimmung dieser Bedingungen unwirksam sein, bleiben die übrigen Bestimmungen
wirksam. Kontakt: etabli.dev@outlook.com.
