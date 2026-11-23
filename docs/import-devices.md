# Importing recordings from mobile recorders

Calls taken on the iPhone itself cannot be recorded in software (known limitation L-01). They
are recorded on separate hardware and imported into the same verified session structure as
OBS sessions (architecture §11):

```
captation import <file-or-folder> --device <profile> --consent-ref <REF>
                 [--started-at <ISO-8601>] [--app-id <id>] [--recursive] [--dry-run] [--force]
```

## Format status

**No real device has been tested yet.** Every export format below is
**unverified**: it is the expected behaviour from vendor documentation, not an observation.
There are no PF entries for these devices yet. When a real export is checked, record it in
`docs/platform-facts.md` (device, firmware/app version, container, codec, sample rate,
channels, which tags and timestamps the file carries) and update the "Format status" line of
that device here.

The import itself does not depend on the device profile: any file with an allowed extension
(`import.allowed_extensions`, default `wav flac mp3 m4a opus ogg`) and an audio stream is
accepted. The profile is recorded as `origin.device` and sets the default app ID.

## Rules for study data (all devices)

- **Vendor cloud sync, vendor AI, transcription and summary features stay off** for study data.
  Turn them off before the first study recording, not after. If a recording reached the vendor
  cloud, treat it as a data protection incident.
- Export **locally** only: USB mass storage, or the vendor app's export to local storage
  followed by a cable transfer (or AirDrop) to the computer. No e-mail, messenger or
  third-party cloud on the way.
- Import with `--dry-run` first. The output lists SHA-256 prefixes, never file names.
- After a successful import, delete the original on the device and on the phone by hand. The
  tool never deletes sources.

What the importer guarantees (tests in `tests/unit/test_importer.py`):

| Guarantee | How |
|---|---|
| Consent reference required | `--consent-ref` is mandatory and must match `consent.consent_ref_pattern` |
| File names are never stored | Only `origin.source_sha256` is kept; the file becomes `seg01_mix.<ext>`; tool runs use a neutral staged name |
| Device text metadata is removed | Lossless re-mux `-map 0:a -c copy -map_metadata -1 -map_chapters -1`; cover art is dropped |
| The audio bitstream is unchanged | Stream hash (`-f streamhash -hash sha256`) before = after; otherwise the file is copied unchanged and `import:metadata_not_stripped` is recorded |
| No double import | `<data_root>/.import-index.json` (0600) holds source hashes; `--force` overrides and is logged |
| Same integrity as OBS sessions | QC, `SHA256SUMS`, read-only flags, schema validation, optional age encryption, hooks |

Start time (`started_at`), in this order:

1. container timestamp (`creation_time`, or BWF date + time) → `time_source: file_metadata`;
2. `--started-at` → `user_input`;
3. the file's modification time → `file_mtime` plus the warning `import:time_from_mtime`.

`ended_at = started_at + duration`. A container timestamp **wins over** `--started-at`; if a
device writes a wrong clock into its files, fix the device clock (see the limits below).

## Device profiles

### `plaud_note_pro` (Plaud Note Pro)

- **Default app ID:** `mobile_plaud`.
- **Local export (expected):** transfer the recording from the device to the Plaud app, then
  use the app's audio export to local storage, or copy from device storage where the firmware
  exposes it over USB. Do not use the share-to-cloud or link-sharing functions.
- **Format status:** unverified (expected MP3 or WAV export).
- **Known limits:** the app may require an account; check that cloud sync, "AI summary" and
  transcription are disabled for the study workspace. Check whether the app export is the
  original recording or a re-encoded copy (compare durations and bitrates once). The container
  timestamp may be the export time rather than the recording time; check once against a known
  call and use `--started-at` only if the file carries no timestamp.

### `viaim_recdot` (viaim RecDot)

- **Default app ID:** `mobile_viaim`.
- **Local export (expected):** export the audio file from the viaim app to local storage,
  then transfer it by cable or AirDrop.
- **Format status:** unverified.
- **Known limits:** the app is built around cloud transcription. Check where data is processed
  and whether a data processing agreement exists before any study use. Keep AI features off.

### `recordergear_pr200` (RecorderGear PR200)

- **Default app ID:** `mobile_pr200`.
- **Local export (expected):** the device mounts as USB mass storage; copy the MP3 files.
- **Format status:** unverified (expected MP3).
- **Known limits:** recording of WhatsApp and Signal calls is unconfirmed; test it with a
  non-study call first. The device clock may drift or reset; a wrong clock produces a wrong
  `started_at` (from MP3 tags or the file time). Set the clock before each study day.

### `magmo_pro` (Magmo Pro)

- **Default app ID:** `mobile_magmo`.
- **Local export (expected):** export from the companion app to local storage.
- **Format status:** unverified.
- **Known limits:** the device or app may **name files after the caller's phone number**. The
  importer never stores, logs or prints file names (only the SHA-256), but the originals on the
  phone and in any export folder still carry the number: delete them after import.

### `generic`

- **Default app ID:** `mobile_generic`.
- Any recorder or app that produces WAV, FLAC, MP3, M4A, Opus or Ogg files.
- **Format status:** the container handling (tag stripping with unchanged stream hash) is
  tested with synthetic MP3, M4A and WAV files and was checked by hand for FLAC, Opus and Ogg
  (ffmpeg 7.0.2). Real recorder files are unverified.

## Troubleshooting

| Output | Meaning |
|---|---|
| `duplicate (already_imported)` | The same source bytes were imported before; `--force` imports again |
| `rejected (extension_not_allowed)` | Extension not in `import.allowed_extensions` |
| `rejected (too_large)` | Larger than `import.max_file_gb` (GiB) |
| `rejected (unreadable_media)` / `rejected (no_audio_stream)` | ffprobe found no usable audio |
| `rejected (low_disk)` | Less than `storage.min_free_gb` free on the data root |
| warning `import:metadata_not_stripped` | Lossless tag removal was not possible; the file was copied unchanged and may still carry device text tags |
| warning `import:time_from_mtime` | Neither file metadata nor `--started-at`: the start time is the file modification time |
