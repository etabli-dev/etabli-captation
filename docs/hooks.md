# Session hooks

`hooks.on_session_complete` runs one command after a session has been finalised. It is **off by
default** (`null`). The three examples below are examples only; none of them is enabled by the
packaged configuration.

## How hooks run

- The value is an **argv list**. There is no shell: no `~` expansion, no globbing, no pipes, no
  `$VAR`, no `;`. Put anything more complex into a script and call the script.
- Placeholders are replaced inside each argument, so a value with spaces stays one argument:

  | Placeholder | Value |
  |---|---|
  | `{session_dir}` | absolute path of the session folder |
  | `{session_id}` | `S-YYYYMMDD-HHMMSS-xxxx` |
  | `{project_id}` | `PRJ-YYYY-NNN`, or `unassigned` |

  `"--out={session_dir}/transcripts"` is allowed: the placeholder is replaced in place.
- The hook runs **after** `session.json`, `SHA256SUMS`, QC and the read-only flags are final,
  and only for sessions with `status: complete` or `interrupted` (never for `cancelled` or
  `failed`). The working directory is the session folder.
- `hooks.timeout_s` (default 3600 s) is enforced; on timeout the hook's whole process group is
  killed.
- stdout and stderr are written to the agent log, truncated to 500 characters each. A hook
  must therefore **never print transcript text, names or numbers**.
- A failure never changes the media. It adds one warning to `session.json`:
  `hook:not_found`, `hook:error` (could not start), `hook:timeout` or `hook:failed:exit_<code>`.
- Media files are read-only (`postprocess.make_read_only`). A hook may add new files or folders
  to the session folder (for example `transcripts/`); new files are not listed in
  `SHA256SUMS`.
- With `storage.age_recipients` set, the hook only sees `*.age` files.
- A hook is the only way the agent can reach the network besides the loopback obs-websocket
  connection (docs/principles.md §3.3). Configure network hooks deliberately.

## Example 1: whisper.cpp transcription into `transcripts/`

whisper.cpp expects 16 kHz mono WAV. The hook converts a **temporary copy** in a private temp
folder; the recorded media stay untouched.

`/usr/local/bin/captation-transcribe` (mode 0755):

```sh
#!/bin/sh
# usage: captation-transcribe <session_dir>
set -eu
session="$1"
model="/opt/whisper.cpp/models/ggml-large-v3-turbo.bin"
out="$session/transcripts"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p -m 0700 "$out"
for media in "$session"/seg*_mix.flac "$session"/seg*_mix.wav "$session"/seg*_mix.mp3 "$session"/seg*_mix.m4a; do
  [ -e "$media" ] || continue
  name="$(basename "$media")"
  base="${name%.*}"
  ffmpeg -nostdin -loglevel error -i "$media" -ac 1 -ar 16000 -c:a pcm_s16le "$tmp/$base.wav"
  whisper-cli -m "$model" -l auto -f "$tmp/$base.wav" -otxt -osrt -of "$out/$base" >/dev/null 2>&1
done
echo "transcripts written"
```

```yaml
hooks:
  on_session_complete: ["/usr/local/bin/captation-transcribe", "{session_dir}"]
  timeout_s: 7200
```

Transcripts are personal data like the recordings: they stay in the session folder (0700) and
are covered by the same retention.

## Example 2: rsync to a VPS over SSH

Use a dedicated key without passphrase that may only write into one folder on the server
(e.g. `command="rrsync /srv/captation/inbox"` in `authorized_keys`). Encrypt sessions with
`storage.age_recipients` before they leave the machine.

```yaml
hooks:
  on_session_complete:
    - /usr/bin/rsync
    - --archive
    - --partial
    - --rsh=ssh -i /home/USER/.ssh/captation_upload_ed25519 -o BatchMode=yes -o IdentitiesOnly=yes
    - "{session_dir}"
    - "captation-upload@vps.example.org:/srv/captation/inbox/{project_id}/"
  timeout_s: 3600
```

`--rsh=…` is a single argument; rsync splits it into the ssh command line itself. Replace the
host name and paths with the real ones. A failed upload is logged and recorded as
`hook:failed:exit_<code>` in `session.json`; retry by hand with the same command.

## Example 3: copy into a transcription inbox folder

A local folder that a separate transcription tool watches. `--archive` keeps the read-only modes
and timestamps.

```yaml
hooks:
  on_session_complete:
    - /usr/bin/rsync
    - --archive
    - "{session_dir}"
    - /home/USER/transcription-inbox/captation/
  timeout_s: 600
```

On macOS use `/usr/bin/rsync` or `/bin/cp -Rp`; on Windows call a script, for example
`["C:\\Windows\\System32\\robocopy.exe", "{session_dir}", "D:\\transcription-inbox\\{session_id}", "/E", "/COPY:DAT"]`
(robocopy exits with 1 on success with copied files; wrap it in a script that maps exit codes
below 8 to 0).
