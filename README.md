# ARI Wait -> Record -> Play -> Hangup

Python ARI client that:

1. waits for an incoming call in a Stasis app,
2. records 5 seconds to local WAV in Python (RTP capture),
3. plays the same WAV back into the bridge via RTP,
4. hangs up the call,
5. repeats in loop mode.

Built for Asterisk ARI with REST + WebSocket events + `externalMedia`.

## Features

- Waits for `StasisStart` via `/ari/events?app=<app>`
- Answers incoming channel via ARI
- Creates bridge and adds caller + `externalMedia` channel
- Receives RTP in Python and writes WAV locally
- Sends RTP (WAV playback) from Python back into Asterisk bridge
- Optional RTP pre-roll and softmix forcing mode
- Hangs up channel and cleans bridge

## Project Files

- `ari_echo_exMedia_play.py` — standalone script (`externalMedia` RTP capture/playback in Python)
- `ari_echo_file_play.py` — standalone script (Asterisk-side record/playback flow)
- `.env` — ARI credentials and defaults

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
ARI_BASE_URL=https://your-asterisk-host:8089
ARI_USER=your_ari_user
ARI_PASS=your_ari_password
ARI_VERIFY_SSL=1

STASIS_APP=extmedia-ai
BRIDGE_TYPE=mixing,proxy_media

RTP_INJECT_HOST=
RTP_INJECT_PORT=0
RTP_PREROLL_MS=800
CONF_FORCE_SOFTMIX=0
CONF_HELPER_EXTERNAL_HOST=127.0.0.1:9
```

Notes:

- `ARI_BASE_URL` may be with or without `/ari` (script handles both).
- `STASIS_APP` is app for incoming call events (`StasisStart`).
- If `ARI_VERIFY_SSL=0`, SSL verification is disabled and warning is suppressed.

## Run

### Loop mode (default)

```bash
python ari_echo_exMedia_play.py
```

### One call only

```bash
python ari_echo_exMedia_play.py --once
```

### Custom parameters

```bash
python ari_echo_exMedia_play.py \
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
- `--bridge-type` — bridge type attributes for ARI create bridge (default `mixing,proxy_media`), supports alias `softmix` -> `mixing,dtmf_events`
- `--record-seconds` — recording duration (default `5`)
- `--wav-out` — local output WAV path (default `./ari_loopback_record.wav`)
- `--media-app` — app for `externalMedia` channel (default from `ARI_MEDIA_APP`)
- `--rtp-advertise-host` — IP/host where Asterisk sends RTP to Python
- `--rtp-port` — UDP port for RTP receive in Python
- `--rtp-bind-host` — local bind host for RTP socket (`0.0.0.0` by default)
- `--ext-media-format` — codec for externalMedia (`alaw` or `ulaw`)
- `--rtp-inject-host` — force Python->Asterisk RTP destination IP (override ARI var)
- `--rtp-inject-port` — force Python->Asterisk RTP destination port (`0` = ARI var)
- `--rtp-preroll-ms` — send RTP silence before record/play to warm up symmetric RTP (default `800`)
- `--conf-force-softmix` — add helper channel to force 3-party conference behavior
- `--conf-helper-external-host` — helper externalMedia target (default `127.0.0.1:9`)
- `--once` — process one call and exit

## Runtime Flow

1. Connect WebSocket to ARI events.
2. Wait `StasisStart` and answer channel.
3. Create bridge and add caller channel.
4. Create/add externalMedia channel.
5. (Optional) send short RTP pre-roll to warm up path.
6. Capture RTP from Asterisk in Python for N seconds and save WAV.
7. Send WAV back as RTP from Python into bridge.
8. Hang up caller and cleanup bridge/channels.

## `ari_echo_file_play.py`

`ari_echo_file_play.py` is a standalone ARI script for media tests.

- Purpose: quick echo/record-play experiments.
- `ari_echo_file_play.py` records call audio on Asterisk and then plays it back on Asterisk (server-side media path).
- `ari_echo_exMedia_play.py` uses `externalMedia` in both directions, i.e. Python receives RTP from Asterisk and sends RTP back to Asterisk.
- Typical use: compare pure Asterisk media path vs externalMedia/Python media path.
- Run:

```bash
python ari_echo_file_play.py
```

If both scripts are present, prefer `ari_echo_exMedia_play.py` for the production-like flow (externalMedia + RTP capture/playback in Python).

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

