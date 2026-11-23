# Security

## obs-websocket

- Auth is always on. `captation obs bootstrap` writes a random 43-character password
  (`secrets.token_urlsafe(32)`) into the obs-websocket config (user-only, 0600) and the OS
  keyring (service `etabli-captation`, user `obs-websocket`). The environment variable
  `CAPTATION_OBS_PASSWORD` is a fallback. The password is never logged, never printed and never
  passed on a command line (OBS is started without `--websocket_password`).
- A config the user set up with an unknown password is never overwritten; `captation obs
  set-password` stores it instead. Before any change the file is backed up
  (`config.json.bak-<timestamp>`).
- The agent connects to loopback only (`obs.websocket.host` is restricted to `127.0.0.1`, `::1`,
  `localhost` by the schema).

## Exposure policy and firewall rule

obs-websocket listens on all interfaces and has no loopback-only option (PF-005). With
`exposure_policy: require_firewall` (default) arming is refused until the rule is confirmed.

**Linux** (`captation obs firewall --apply`, needs sudo once):

| Artefact | Content |
|---|---|
| `/etc/etabli-captation/obs-websocket-guard.nft` | table `inet etabli_captation`, input hook priority filter − 10: `tcp dport <port> ip saddr != 127.0.0.0/8 drop`, `tcp dport <port> ip6 saddr != ::1 drop` |
| `/etc/systemd/system/etabli-captation-obs-guard.service` | oneshot unit that loads the table at boot (`ExecStart=nft -f …`) and deletes it on stop |

The rule is independent of ufw/iptables front ends and also blocks connections from the host's
own LAN address and its other local interface addresses (PF-015). `--check` runs without root:
the unit must be active, and connections from every local non-loopback address must fail.

**Remove:** `captation obs firewall --remove` (disables the unit, deletes both files and the
table). `uninstall-autostart` does not touch the rule.

**macOS** (prepared, pending verification PF-123…125):

| Artefact | Content |
|---|---|
| `/etc/pf.anchors/de.etabli.captation` | `block drop in quick proto tcp from ! 127.0.0.1 to any port <port>` and the `inet6`/`::1` equivalent |
| `/Library/LaunchDaemons/de.etabli.captation.pf.plist` | root job at boot: enables pf with its own reference (token in `/var/run/de.etabli.captation.pf.token`) and loads the anchor `com.apple/250.EtabliCaptation` |

`/etc/pf.conf` is never edited. `--remove` unloads the job, flushes the anchor, releases the pf
reference and deletes the token, the rule file and the plist.

**Windows** (prepared, pending verification PF-230…232): an inbound *Block* rule
"Etabli Captation obs-websocket" for TCP on the websocket port, all profiles, created and removed
through an elevated PowerShell (UAC prompt). Windows Firewall does not filter loopback, so the
agent keeps working; the block must be confirmed once from a second device.

## OBS network behaviour

- Linux OBS 33 has no built-in updater and no automatic crash-log upload; crash logs can only be
  uploaded manually from *Help → Crash Logs* (PF-012). The agent still passes the verified
  `--disable-updater` flag.
- **At every start, OBS's bundled `rtmp-services` plugin downloads the streaming-service list
  from obsproject.com and the Twitch and Amazon IVS ingest lists (PF-023).** This cannot be
  switched off in OBS; it reveals the machine's IP address to those services but carries no
  recording data. To prevent it, block outbound traffic of the OBS sandbox at the network level.
- End-to-end scenario E10 checks the TCP sockets of the agent and of OBS while recording: the
  agent has only its loopback connection to the websocket port; OBS has no live non-loopback
  connection (only CLOSE-WAIT leftovers of the start-up fetch above).

## IPC

- Unix socket `$XDG_RUNTIME_DIR/captation/agent.sock` (0600, directory 0700) and a 32-byte key in
  the user state dir (0600).
- HMAC-SHA256 challenge-response per connection with a 2 s timeout, in the connection's own
  thread (a silent client cannot block others), JSON payloads only (no pickle), 64 KiB limit,
  a whitelist of message types and fields. Malformed messages are rejected and logged.

## Hooks

`hooks.on_session_complete` is an argv list. Placeholders (`{session_dir}`, `{session_id}`,
`{project_id}`) are substituted as whole arguments, no shell is involved, the hook runs with a
timeout in its own process group, and a failure only adds a warning. Hooks are off by default.

## Import

Imported files are hashed, copied with a lossless container re-mux that drops all tags
(`-map_metadata -1`), and verified by an audio stream hash. Original file names and tag text are
never stored in `session.json`, logs or output (they can hold phone numbers). **Exception:** if
a format cannot be re-muxed losslessly, the file is copied unchanged and the session gets the
warning `import:metadata_not_stripped`; such a file can still carry the device's tags. Check
these sessions and strip the tags manually if needed.

## Dependencies

Runtime dependencies are checked by `tools/licence_gate.py` (CI). A CycloneDX SBOM is produced by
the release workflow. Before each release, `pip-audit` is run against the runtime dependencies
and the result is noted in the release notes.
