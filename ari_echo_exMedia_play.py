#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Optional
from urllib.parse import urlencode

import requests
import urllib3
import websocket


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


def normalize_bridge_type(raw: str) -> str:
    """
    ARI bridge 'type' is an attribute list (e.g. mixing,proxy_media), not technology name.
    Provide a convenient alias:
      - softmix -> mixing,dtmf_events
    """
    value = (raw or "").strip().lower()
    if value == "softmix":
        return "mixing,dtmf_events"
    return raw


@dataclass
class Config:
    ari_base_url: str
    ari_user: str
    ari_pass: str
    ari_verify_ssl: bool
    stasis_app: str
    media_app: str
    bridge_type: str
    wav_out: str
    record_seconds: int
    rtp_advertise_host: str
    rtp_port: int
    rtp_bind_host: str
    ext_media_format: str
    rtp_inject_host_override: str
    rtp_inject_port_override: int
    rtp_preroll_ms: int
    conf_force_softmix: bool
    conf_helper_external_host: str
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
        raise RuntimeError("Use create_bridge_with_type()")

    def create_bridge_with_type(self, bridge_id: str, bridge_type: str) -> None:
        self.post_json("/bridges", params={"type": bridge_type, "bridgeId": bridge_id, "name": bridge_id})

    def add_channel_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        self.post_json(f"/bridges/{bridge_id}/addChannel", params={"channel": channel_id})

    def answer_channel(self, channel_id: str) -> None:
        self.post_json(f"/channels/{channel_id}/answer")

    def create_external_media(self, *, app: str, external_host: str, media_format: str) -> dict[str, Any]:
        return self.post_json(
            "/channels/externalMedia",
            params={
                "app": app,
                "external_host": external_host,
                "format": media_format,
                "transport": "UDP",
                "encapsulation": "rtp",
            },
        )

    def get_bridge(self, bridge_id: str) -> dict[str, Any]:
        return self._request("GET", f"/bridges/{bridge_id}").json()

    def get_channel_var(self, channel_id: str, variable: str) -> str:
        data = self._request("GET", f"/channels/{channel_id}/variable", params={"variable": variable}).json()
        return str(data.get("value", ""))

    def hangup(self, channel_id: str) -> None:
        self.delete(f"/channels/{channel_id}")

    def delete_bridge(self, bridge_id: str) -> None:
        self.delete(f"/bridges/{bridge_id}")


def _alaw_decode_byte(a_val: int) -> int:
    a_val ^= 0x55
    t = (a_val & 0x0F) << 4
    seg = (a_val & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return -t if (a_val & 0x80) == 0 else t


def _alaw_encode_sample(pcm: int) -> int:
    sign = 0x80 if pcm >= 0 else 0x00
    if pcm < 0:
        pcm = -pcm
    if pcm > 32635:
        pcm = 32635

    if pcm >= 256:
        seg = 0
        temp = pcm >> 8
        while temp:
            seg += 1
            temp >>= 1
        aval = seg << 4 | ((pcm >> (seg + 3)) & 0x0F)
    else:
        aval = pcm >> 4
    return aval ^ (sign ^ 0x55)


def _ulaw_decode_byte(u_val: int) -> int:
    u_val = ~u_val & 0xFF
    t = ((u_val & 0x0F) << 3) + 0x84
    t <<= (u_val & 0x70) >> 4
    return 132 - t if (u_val & 0x80) else t - 132


def _ulaw_encode_sample(pcm: int) -> int:
    BIAS = 0x84
    CLIP = 32635
    sign = 0x80 if pcm < 0 else 0
    if pcm < 0:
        pcm = -pcm
    if pcm > CLIP:
        pcm = CLIP
    pcm += BIAS
    exponent = 7
    exp_mask = 0x4000
    while exponent > 0 and (pcm & exp_mask) == 0:
        exponent -= 1
        exp_mask >>= 1
    mantissa = (pcm >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def decode_g711_to_pcm16(payload: bytes, media_format: str) -> bytes:
    out = bytearray()
    if media_format == "alaw":
        for b in payload:
            out += int(_alaw_decode_byte(b)).to_bytes(2, byteorder="little", signed=True)
        return bytes(out)
    if media_format == "ulaw":
        for b in payload:
            out += int(_ulaw_decode_byte(b)).to_bytes(2, byteorder="little", signed=True)
        return bytes(out)
    raise RuntimeError(f"Unsupported EXT_MEDIA_FORMAT={media_format}. Use alaw or ulaw.")


def encode_pcm16_to_g711(pcm16: bytes, media_format: str) -> bytes:
    if len(pcm16) % 2 != 0:
        raise RuntimeError("PCM16 payload must have even size")
    out = bytearray(len(pcm16) // 2)
    for i in range(0, len(pcm16), 2):
        sample = int.from_bytes(pcm16[i : i + 2], byteorder="little", signed=True)
        if media_format == "alaw":
            out[i // 2] = _alaw_encode_sample(sample)
        elif media_format == "ulaw":
            out[i // 2] = _ulaw_encode_sample(sample)
        else:
            raise RuntimeError(f"Unsupported EXT_MEDIA_FORMAT={media_format}. Use alaw or ulaw.")
    return bytes(out)


def parse_rtp_payload(packet: bytes) -> bytes:
    if len(packet) < 12:
        return b""
    version = packet[0] >> 6
    if version != 2:
        return b""
    csrc_count = packet[0] & 0x0F
    extension = (packet[0] >> 4) & 0x01
    header_len = 12 + 4 * csrc_count
    if len(packet) < header_len:
        return b""
    if extension:
        if len(packet) < header_len + 4:
            return b""
        ext_len_words = int.from_bytes(packet[header_len + 2 : header_len + 4], "big")
        header_len += 4 + ext_len_words * 4
    if len(packet) < header_len:
        return b""
    return packet[header_len:]


def record_rtp_to_wav(
    *, sock: socket.socket, seconds: int, media_format: str, wav_out: str
) -> tuple[int, int]:
    deadline = time.time() + seconds
    pcm_data = bytearray()
    rtp_packets = 0
    payload_bytes = 0
    sock.settimeout(0.2)
    while time.time() < deadline:
        try:
            packet, _ = sock.recvfrom(2048)
        except socket.timeout:
            continue
        payload = parse_rtp_payload(packet)
        if not payload:
            continue
        rtp_packets += 1
        payload_bytes += len(payload)
        pcm_data += decode_g711_to_pcm16(payload, media_format)

    wav_dir = os.path.dirname(wav_out)
    if wav_dir:
        os.makedirs(wav_dir, exist_ok=True)
    with wave.open(wav_out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(bytes(pcm_data))
    return rtp_packets, payload_bytes


def play_wav_as_rtp(
    *,
    sock: socket.socket,
    wav_path: str,
    target_host: str,
    target_port: int,
    media_format: str,
) -> int:
    with wave.open(wav_path, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 8000:
            raise RuntimeError("WAV must be mono, 16-bit PCM, 8000 Hz")
        pcm = wf.readframes(wf.getnframes())

    g711 = encode_pcm16_to_g711(pcm, media_format)
    samples_per_packet = 160  # 20 ms at 8kHz
    seq = 0
    ts = 0
    ssrc = (uuid.uuid4().int & 0xFFFFFFFF)
    payload_type = 8 if media_format == "alaw" else 0

    sent_packets = 0
    for i in range(0, len(g711), samples_per_packet):
        chunk = g711[i : i + samples_per_packet]
        rtp = bytearray(12)
        rtp[0] = 0x80  # V=2
        rtp[1] = payload_type & 0x7F
        rtp[2:4] = seq.to_bytes(2, "big")
        rtp[4:8] = ts.to_bytes(4, "big")
        rtp[8:12] = ssrc.to_bytes(4, "big")
        sock.sendto(bytes(rtp) + chunk, (target_host, target_port))
        sent_packets += 1
        seq = (seq + 1) & 0xFFFF
        ts = (ts + len(chunk)) & 0xFFFFFFFF
        time.sleep(0.02)
    return sent_packets


def send_rtp_silence(
    *,
    sock: socket.socket,
    target_host: str,
    target_port: int,
    media_format: str,
    duration_ms: int,
) -> int:
    if duration_ms <= 0:
        return 0
    payload_type = 8 if media_format == "alaw" else 0
    # Typical digital silence bytes for G.711.
    silence_byte = 0xD5 if media_format == "alaw" else 0xFF
    samples_per_packet = 160  # 20 ms at 8kHz
    packet_count = max(1, duration_ms // 20)
    seq = 0
    ts = 0
    ssrc = (uuid.uuid4().int & 0xFFFFFFFF)
    payload = bytes([silence_byte]) * samples_per_packet
    for _ in range(packet_count):
        rtp = bytearray(12)
        rtp[0] = 0x80
        rtp[1] = payload_type & 0x7F
        rtp[2:4] = seq.to_bytes(2, "big")
        rtp[4:8] = ts.to_bytes(4, "big")
        rtp[8:12] = ssrc.to_bytes(4, "big")
        sock.sendto(bytes(rtp) + payload, (target_host, target_port))
        seq = (seq + 1) & 0xFFFF
        ts = (ts + samples_per_packet) & 0xFFFFFFFF
        time.sleep(0.02)
    return packet_count


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
                іу
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
            # Ignore ARI-created helper channels (externalMedia/recorder-like channels),
            # we only want real inbound call legs.
            ch_name = str(ch.get("name", ""))
            if ch_name.startswith("UnicastRTP/") or ch_name.startswith("Recorder/"):
                continue
            if channel_id:
                return str(channel_id)
def _extract_external_media_channel_id(resp: dict[str, Any]) -> str:
    if isinstance(resp.get("channel"), dict) and resp["channel"].get("id"):
        return str(resp["channel"]["id"])
    if resp.get("id"):
        return str(resp["id"])
    raise RuntimeError(f"Unexpected externalMedia response: {resp}")


def _wait_for_channel_var(ari: AriClient, channel_id: str, variable: str, tries: int = 20) -> str:
    for _ in range(tries):
        try:
            val = ari.get_channel_var(channel_id, variable)
            if val:
                return val
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Failed to get {variable} for channel {channel_id}")


def _add_channel_with_retry(ari: AriClient, bridge_id: str, channel_id: str, tries: int = 20) -> None:
    last: Optional[Exception] = None
    for _ in range(tries):
        try:
            ari.add_channel_to_bridge(bridge_id, channel_id)
            return
        except Exception as e:
            last = e
            time.sleep(0.2)
    raise RuntimeError(f"Could not add channel {channel_id} to bridge {bridge_id}: {last}")


def process_one_call(ari: AriClient, cfg: Config, channel_id: str) -> None:
    bridge_id = f"bridge-{uuid.uuid4().hex}"
    ext_channel_id: Optional[str] = None
    helper_channel_id: Optional[str] = None

    print(f"[INFO] Processing channel {channel_id}")
    print(f"[INFO] bridge={bridge_id}")
    rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp_sock.bind((cfg.rtp_bind_host, cfg.rtp_port))
    print(f"[INFO] RTP socket bound (RX/TX) on {cfg.rtp_bind_host}:{cfg.rtp_port}")
    try:
        try:
            ari.answer_channel(channel_id)
            print(f"[INFO] Channel answered: {channel_id}")
        except Exception as e:
            # If already answered, continue; otherwise bridge step will fail and surface error.
            print(f"[WARN] Channel answer returned error (continuing): {e}")

        ari.create_bridge_with_type(bridge_id, cfg.bridge_type)
        _add_channel_with_retry(ari, bridge_id, channel_id)

        external_host = f"{cfg.rtp_advertise_host}:{cfg.rtp_port}"
        ext = ari.create_external_media(app=cfg.media_app, external_host=external_host, media_format=cfg.ext_media_format)
        ext_channel_id = _extract_external_media_channel_id(ext)
        _add_channel_with_retry(ari, bridge_id, ext_channel_id)

        if cfg.conf_force_softmix:
            helper = ari.create_external_media(
                app=cfg.media_app,
                external_host=cfg.conf_helper_external_host,
                media_format=cfg.ext_media_format,
            )
            helper_channel_id = _extract_external_media_channel_id(helper)
            _add_channel_with_retry(ari, bridge_id, helper_channel_id)
            print(
                "[INFO] CONF_FORCE_SOFTMIX enabled: helper channel added "
                f"{helper_channel_id} -> {cfg.conf_helper_external_host}"
            )

        try:
            b = ari.get_bridge(bridge_id)
            print(
                f"[INFO] Bridge technology={b.get('technology')} "
                f"type={b.get('bridge_type')} channels={len(b.get('channels', []))}"
            )
        except Exception as e:
            print(f"[WARN] Could not fetch bridge details: {e}")
        print(
            "[INFO] RTP link Asterisk -> Python: "
            f"asterisk sends to {cfg.rtp_advertise_host}:{cfg.rtp_port}, "
            f"python listening on {cfg.rtp_bind_host}:{cfg.rtp_port}"
        )
        inject_addr = _wait_for_channel_var(ari, ext_channel_id, "UNICASTRTP_LOCAL_ADDRESS")
        inject_port = int(_wait_for_channel_var(ari, ext_channel_id, "UNICASTRTP_LOCAL_PORT"))
        if cfg.rtp_inject_host_override:
            inject_addr = cfg.rtp_inject_host_override
        if cfg.rtp_inject_port_override > 0:
            inject_port = cfg.rtp_inject_port_override
        print(
            "[INFO] RTP link Python -> Asterisk: "
            f"python sends to {inject_addr}:{inject_port} (UNICASTRTP_LOCAL_ADDRESS/PORT)"
        )
        pre_packets = send_rtp_silence(
            sock=rtp_sock,
            target_host=inject_addr,
            target_port=inject_port,
            media_format=cfg.ext_media_format,
            duration_ms=cfg.rtp_preroll_ms,
        )
        if pre_packets:
            print(f"[INFO] RTP pre-roll (before record) packets={pre_packets} duration_ms={cfg.rtp_preroll_ms}")

        print(f"[INFO] Recording RTP to WAV ({cfg.record_seconds}s)...")
        rx_packets, rx_payload_bytes = record_rtp_to_wav(
            sock=rtp_sock,
            seconds=cfg.record_seconds,
            media_format=cfg.ext_media_format,
            wav_out=cfg.wav_out,
        )
        print(f"[INFO] Saved WAV to {cfg.wav_out}")
        print(f"[INFO] RTP RX packets={rx_packets} payload_bytes={rx_payload_bytes}")

        print(f"[INFO] Playing WAV back via RTP to {inject_addr}:{inject_port}")
        tx_packets = play_wav_as_rtp(
            sock=rtp_sock,
            wav_path=cfg.wav_out,
            target_host=inject_addr,
            target_port=inject_port,
            media_format=cfg.ext_media_format,
        )
        print(f"[INFO] RTP TX packets={tx_packets}")
        # Give bridge mixer a moment to flush last packets to the caller.
        time.sleep(0.8)

        ari.hangup(channel_id)
        print(f"[INFO] Hangup done for {channel_id}")
    finally:
        try:
            rtp_sock.close()
        except Exception:
            pass
        if ext_channel_id:
            try:
                ari.hangup(ext_channel_id)
            except Exception:
                pass
        if helper_channel_id:
            try:
                ari.hangup(helper_channel_id)
            except Exception:
                pass
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
    parser.add_argument("--media-app", default=os.getenv("ARI_MEDIA_APP", "extmedia-ai"))
    parser.add_argument("--bridge-type", default=os.getenv("BRIDGE_TYPE", "mixing,proxy_media"))
    parser.add_argument("--wav-out", default="./ari_loopback_record.wav")
    parser.add_argument("--record-seconds", type=int, default=5)
    parser.add_argument("--rtp-advertise-host", default=os.getenv("RTP_ADVERTISE_HOST", "127.0.0.1"))
    parser.add_argument("--rtp-port", type=int, default=int(os.getenv("RTP_PORT", "18080")))
    parser.add_argument("--rtp-bind-host", default=os.getenv("RTP_BIND_HOST", "0.0.0.0"))
    parser.add_argument("--ext-media-format", default=os.getenv("EXT_MEDIA_FORMAT", "alaw"))
    parser.add_argument("--rtp-inject-host", default=os.getenv("RTP_INJECT_HOST", ""))
    parser.add_argument("--rtp-inject-port", type=int, default=int(os.getenv("RTP_INJECT_PORT", "0")))
    parser.add_argument("--rtp-preroll-ms", type=int, default=int(os.getenv("RTP_PREROLL_MS", "800")))
    parser.add_argument("--conf-force-softmix", action="store_true", default=env_bool("CONF_FORCE_SOFTMIX", False))
    parser.add_argument("--conf-helper-external-host", default=os.getenv("CONF_HELPER_EXTERNAL_HOST", "127.0.0.1:9"))
    parser.add_argument("--once", action="store_true", default=False, help="Handle one call and exit.")
    args = parser.parse_args()

    if not args.ari_user or not args.ari_pass:
        raise RuntimeError("ARI credentials are required: set ARI_USER and ARI_PASS or pass CLI args.")
    if args.record_seconds <= 0:
        raise RuntimeError("--record-seconds must be > 0")
    if args.ext_media_format not in {"alaw", "ulaw"}:
        raise RuntimeError("--ext-media-format must be alaw or ulaw")
    if args.rtp_preroll_ms < 0:
        raise RuntimeError("--rtp-preroll-ms must be >= 0")

    return Config(
        ari_base_url=args.ari_url,
        ari_user=args.ari_user,
        ari_pass=args.ari_pass,
        ari_verify_ssl=args.verify_ssl,
        stasis_app=args.stasis_app,
        media_app=args.media_app,
        bridge_type=normalize_bridge_type(args.bridge_type),
        wav_out=args.wav_out,
        record_seconds=args.record_seconds,
        rtp_advertise_host=args.rtp_advertise_host,
        rtp_port=args.rtp_port,
        rtp_bind_host=args.rtp_bind_host,
        ext_media_format=args.ext_media_format,
        rtp_inject_host_override=args.rtp_inject_host,
        rtp_inject_port_override=args.rtp_inject_port,
        rtp_preroll_ms=args.rtp_preroll_ms,
        conf_force_softmix=args.conf_force_softmix,
        conf_helper_external_host=args.conf_helper_external_host,
        loop=(not args.once),
    )


def main() -> int:
    cfg = build_config()
    print(
        f"[INFO] ARI={cfg.ari_base_url} app={cfg.stasis_app} media_app={cfg.media_app} "
        f"bridge_type={cfg.bridge_type} loop={cfg.loop} "
        f"rtp={cfg.rtp_advertise_host}:{cfg.rtp_port}/{cfg.ext_media_format}"
    )

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
            process_one_call(ari, cfg, channel_id)
        except Exception as e:
            print(f"[ERROR] call processing failed for {channel_id}: {e}", file=sys.stderr)
        if not cfg.loop:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

