# ARI Wait → Record → Play → Hangup

Python ARI client that:

1. waits for an incoming call in a Stasis app,
2. records 5 seconds to WAV,
3. plays the recording back to the same channel,
4. hangs up the call,
5. repeats in loop mode.

Built for Asterisk ARI with REST + WebSocket events.

## Features

- Waits for `StasisStart` via `/ari/events?app=<app>`
- Creates a `mixing` bridge and adds incoming channel
- Records bridge audio via `POST /bridges/{bridgeId}/record`
- Waits for `RecordingFinished`
- Downloads WAV via `GET /recordings/stored/{recordingName}/file`
- Plays back using `POST /channels/{channelId}/play?media=recording:<name>`
- Waits for `PlaybackFinished`
- Hangs up channel and cleans bridge

## Project Files

- `ari_wait_record_play.py` — main client script
- `.env` — ARI credentials and defaults
- `ari_api_docs/` — copied ARI API docs from your server

## Requirements

- Python 3.9+
- Packages:
  - `requests`
  - `websocket-client`
- Asterisk ARI configured and reachable
- Dialplan must route calls to your Stasis app

## Configuration

Script auto-loads `.env` from project root.

Example:

```env
ARI_BASE_URL=https://ari.example.com:8089
ARI_MEDIA_APP=extmedia-ai
ARI_USER=ai_user
ARI_PASS=your_password
ARI_VERIFY_SSL=0
```

Notes:

- `ARI_BASE_URL` may be with or without `/ari` (script handles both).
- Current default Stasis app in script comes from:
  1. `STASIS_APP` (if present),
  2. otherwise `EXT_MEDIA_APP`,
  3. fallback: `extmedia-ai`.
- If `ARI_VERIFY_SSL=0`, SSL verification is disabled and warning is suppressed.

## Run

### Loop mode (default)

```bash
python ari_wait_record_play.py
```

### One call only

```bash
python ari_wait_record_play.py --once
```

### Custom parameters

```bash
python ari_wait_record_play.py \
  --stasis-app extmedia-ai \
  --record-seconds 5 \
  --wav-out ./ari_loopback_record.wav
```

## CLI Options

- `--ari-url` — ARI base URL
- `--ari-user` — ARI username
- `--ari-pass` — ARI password
- `--verify-ssl` — enable SSL verification explicitly
- `--stasis-app` — Stasis app to subscribe for `StasisStart`
- `--record-seconds` — recording duration (default `5`)
- `--wav-out` — local output WAV path (default `./ari_loopback_record.wav`)
- `--once` — process one call and exit

## Runtime Flow

1. Connect WebSocket to ARI events.
2. Wait `StasisStart`.
3. Create bridge and add channel.
4. Start bridge recording (`wav`, `maxDurationSeconds=5`).
5. Wait `RecordingFinished`.
6. Download recorded WAV locally.
7. Play recording back to same channel.
8. Wait `PlaybackFinished`.
9. Hang up channel.
10. Delete bridge.

## Troubleshooting

- `Timeout waiting ARI event`
  - Call may not be entering the configured Stasis app.
  - Verify dialplan `Stasis(<app>)` and `--stasis-app`.

- `Channel not found` / `Bridge not in Stasis application`
  - Ensure channel is actually in the target Stasis app.

- Playback starts but no audio
  - Verify recording was created and exists in Asterisk stored recordings.
  - Check that `media=recording:<recordingName>` is valid for your setup.

- TLS/Cert issues
  - Set valid certs and use SSL verification.
  - Or keep `ARI_VERIFY_SSL=0` for test environments only.

## GitHub Quick Start

```bash
git init
git add .
git commit -m "Add ARI loopback record/play client and docs"
```

Then create a GitHub repository and push:

```bash
git remote add origin <your_repo_url>
git branch -M main
git push -u origin main
```

