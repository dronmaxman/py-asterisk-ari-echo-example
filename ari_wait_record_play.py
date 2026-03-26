#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Optional
from urllib.parse import urlencode

import requests
import websocket
import urllib3


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    low = v.strip().lower()
    if low in {"1", "true", "yes", "on"}:
        return True
    if low in {"0", "false", "no", "off"}:
        return False
    return default


def to_ws_url(ari_base_url: str) -> str:
    base = ari_base_url.rstrip("/")
    if "/ari" not in base:
        base = f"{base}/ari"
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/events"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/events"
    raise ValueError(f"Unsupported ARI base URL: {ari_base_url}")


@dataclass
class Config:
    ari_base_url: str
    ari_user: str
    ari_pass: str
    ari_verify_ssl: bool
    stasis_app: str
    wav_out: str
    record_seconds: int
    loop: bool


class AriClient:
    def __init__(self, cfg: Config) -> None:
        base = cfg.ari_base_url.rstrip("/")
        if "/ari" not in base:
            base = f"{base}/ari"
        self.base_url = base
        self.s = requests.Session()
        self.s.auth = (cfg.ari_user, cfg.ari_pass)
        self.s.verify = cfg.ari_verify_ssl
        if not cfg.ari_verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(
        self, method: str, path: str, *, params: Optional[dict[str, Any]] = None, stream: bool = False
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        r = self.s.request(method=method, url=url, params=params, timeout=30, stream=stream)
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {path} failed: {r.status_code} {r.text}")
        return r

    def post_json(self, path: str, *, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        r = self._request("POST", path, params=params)
        return r.json() if r.text else {}

    def delete(self, path: str, *, params: Optional[dict[str, Any]] = None) -> None:
        self._request("DELETE", path, params=params)

    def create_bridge(self, bridge_id: str) -> None:
        self.post_json("/bridges", params={"type": "mixing", "bridgeId": bridge_id, "name": bridge_id})

    def add_channel_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        self.post_json(f"/bridges/{bridge_id}/addChannel", params={"channel": channel_id})

    def record_bridge(self, bridge_id: str, recording_name: str, seconds: int) -> None:
        self.post_json(
            f"/bridges/{bridge_id}/record",
            params={
                "name": recording_name,
                "format": "wav",
                "maxDurationSeconds": seconds,
                "ifExists": "overwrite",
                "terminateOn": "none",
                "beep": "false",
            },
        )

    def play_recording(self, channel_id: str, recording_name: str, playback_id: str) -> None:
        self.post_json(
            f"/channels/{channel_id}/play",
            params={
                "media": f"recording:{recording_name}",
                "playbackId": playback_id,
            },
        )

    def download_recording(self, recording_name: str, out_path: str) -> None:
        r = self._request("GET", f"/recordings/stored/{recording_name}/file", stream=True)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

    def hangup(self, channel_id: str) -> None:
        self.delete(f"/channels/{channel_id}")

    def delete_bridge(self, bridge_id: str) -> None:
        self.delete(f"/bridges/{bridge_id}")


class AriEventStream:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.queue: Queue[dict[str, Any]] = Queue()
        self.ws: Optional[websocket.WebSocketApp] = None
        self.error: Optional[str] = None

    def _headers(self) -> list[str]:
        token = base64.b64encode(f"{self.cfg.ari_user}:{self.cfg.ari_pass}".encode("utf-8")).decode("ascii")
        return [f"Authorization: Basic {token}"]

    def connect(self) -> None:
        ws_url = f"{to_ws_url(self.cfg.ari_base_url)}?{urlencode({'app': self.cfg.stasis_app})}"
        self.ws = websocket.WebSocketApp(
            ws_url,
            header=self._headers(),
            on_message=self._on_message,
            on_error=self._on_error,
        )
        sslopt: dict[str, Any] = {}
        if ws_url.startswith("wss://") and not self.cfg.ari_verify_ssl:
            sslopt["cert_reqs"] = ssl.CERT_NONE
        self.ws.run_forever(sslopt=sslopt)

    def _on_message(self, _: websocket.WebSocketApp, message: str) -> None:
        try:
            obj = json.loads(message)
            if isinstance(obj, dict):
                self.queue.put(obj)
        except Exception:
            pass

    def _on_error(self, _: websocket.WebSocketApp, err: Any) -> None:
        self.error = str(err)

    def next_event(self, timeout: float = 60.0) -> dict[str, Any]:
        try:
            return self.queue.get(timeout=timeout)
        except Empty as e:
            if self.error:
                raise RuntimeError(f"ARI websocket error: {self.error}") from e
            raise TimeoutError("Timeout waiting ARI event") from e

    def wait_for_stasis_start(self) -> str:
        while True:
            evt = self.next_event(timeout=300.0)
            if evt.get("type") != "StasisStart":
                continue
            if evt.get("application") != self.cfg.stasis_app:
                continue
            ch = evt.get("channel") or {}
            channel_id = ch.get("id")
            if channel_id:
                return str(channel_id)

    def wait_for_recording_finished(self, recording_name: str, timeout: float = 30.0) -> None:
        end_time = time.time() + timeout
        while time.time() < end_time:
            evt = self.next_event(timeout=timeout)
            et = evt.get("type")
            if et == "RecordingFinished":
                rec = evt.get("recording") or {}
                if rec.get("name") == recording_name:
                    return
            if et == "RecordingFailed":
                rec = evt.get("recording") or {}
                if rec.get("name") == recording_name:
                    raise RuntimeError(f"Recording failed for {recording_name}")
        raise TimeoutError(f"RecordingFinished timeout for {recording_name}")

    def wait_for_playback_finished(self, playback_id: str, timeout: float = 60.0) -> None:
        end_time = time.time() + timeout
        while time.time() < end_time:
            evt = self.next_event(timeout=timeout)
            if evt.get("type") != "PlaybackFinished":
                continue
            pb = evt.get("playback") or {}
            if pb.get("id") == playback_id:
                return
        raise TimeoutError(f"PlaybackFinished timeout for playback_id={playback_id}")


def process_one_call(ari: AriClient, events: AriEventStream, cfg: Config, channel_id: str) -> None:
    bridge_id = f"bridge-{uuid.uuid4().hex}"
    recording_name = f"ari_loopback_{uuid.uuid4().hex}"
    playback_id = f"pb-{uuid.uuid4().hex}"

    print(f"[INFO] Processing channel {channel_id}")
    print(f"[INFO] bridge={bridge_id} recording={recording_name}")
    try:
        ari.create_bridge(bridge_id)
        ari.add_channel_to_bridge(bridge_id, channel_id)
        ari.record_bridge(bridge_id, recording_name, cfg.record_seconds)
        events.wait_for_recording_finished(recording_name, timeout=max(20, cfg.record_seconds + 15))

        ari.download_recording(recording_name, cfg.wav_out)
        print(f"[INFO] Saved WAV to {cfg.wav_out}")

        ari.play_recording(channel_id, recording_name, playback_id)
        events.wait_for_playback_finished(playback_id, timeout=90)

        ari.hangup(channel_id)
        print(f"[INFO] Hangup done for {channel_id}")
    finally:
        try:
            ari.delete_bridge(bridge_id)
        except Exception as e:
            print(f"[WARN] bridge cleanup failed: {e}", file=sys.stderr)


def build_config() -> Config:
    load_env_file(".env")
    parser = argparse.ArgumentParser(description="Wait call -> record 5s WAV -> play back -> hangup (ARI).")
    parser.add_argument("--ari-url", default=os.getenv("ARI_BASE_URL", "http://127.0.0.1:8088/ari"))
    parser.add_argument("--ari-user", default=os.getenv("ARI_USER", ""))
    parser.add_argument("--ari-pass", dest="ari_pass", default=os.getenv("ARI_PASS", ""))
    parser.add_argument("--verify-ssl", action="store_true", default=env_bool("ARI_VERIFY_SSL", True))
    parser.add_argument("--stasis-app", default=os.getenv("STASIS_APP") or os.getenv("EXT_MEDIA_APP", "extmedia-ai"))
    parser.add_argument("--wav-out", default="./ari_loopback_record.wav")
    parser.add_argument("--record-seconds", type=int, default=5)
    parser.add_argument("--once", action="store_true", default=False, help="Handle one call and exit.")
    args = parser.parse_args()

    if not args.ari_user or not args.ari_pass:
        raise RuntimeError("ARI credentials are required: set ARI_USER and ARI_PASS or pass CLI args.")
    if args.record_seconds <= 0:
        raise RuntimeError("--record-seconds must be > 0")

    return Config(
        ari_base_url=args.ari_url,
        ari_user=args.ari_user,
        ari_pass=args.ari_pass,
        ari_verify_ssl=args.verify_ssl,
        stasis_app=args.stasis_app,
        wav_out=args.wav_out,
        record_seconds=args.record_seconds,
        loop=(not args.once),
    )


def main() -> int:
    cfg = build_config()
    print(f"[INFO] ARI={cfg.ari_base_url} app={cfg.stasis_app} loop={cfg.loop}")

    ari = AriClient(cfg)
    events = AriEventStream(cfg)

    # Blocking websocket loop in a separate thread to receive events continuously.
    import threading

    t = threading.Thread(target=events.connect, daemon=True)
    t.start()
    time.sleep(0.3)

    while True:
        print("[INFO] Waiting for incoming call (StasisStart)...")
        channel_id = events.wait_for_stasis_start()
        try:
            process_one_call(ari, events, cfg, channel_id)
        except Exception as e:
            print(f"[ERROR] call processing failed for {channel_id}: {e}", file=sys.stderr)
        if not cfg.loop:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

