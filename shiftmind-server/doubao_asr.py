"""Doubao Seed ASR streaming relay.

Docs: https://www.volcengine.com/docs/82379/2516286?lang=zh
Resource id for Seed ASR 2.0 duration billing: volc.seedasr.sauc.duration.
"""
from __future__ import annotations

import gzip
import json
import os
import struct
import uuid
from typing import Any

import websockets


DEFAULT_BASE_URL = "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"

MSG_FULL_CLIENT_REQUEST = 0x1
MSG_AUDIO_ONLY_REQUEST = 0x2
MSG_FULL_SERVER_RESPONSE = 0x9
MSG_ERROR = 0xF
FLAG_POS_SEQUENCE = 0x1
FLAG_NEG_SEQUENCE = 0x2
FLAG_NEG_WITH_SEQUENCE = 0x3
SERIALIZATION_JSON = 0x1
COMPRESSION_GZIP = 0x1


class DoubaoSeedAsrStream:
    def __init__(self) -> None:
        self.base_url = os.getenv("DOUBAO_ASR_BASE_URL", DEFAULT_BASE_URL)
        self.api_key = os.getenv("DOUBAO_ASR_API_KEY") or os.getenv("VOLCENGINE_ASR_API_KEY")
        self.resource_id = os.getenv("DOUBAO_ASR_RESOURCE_ID", DEFAULT_RESOURCE_ID)
        self.model = os.getenv("DOUBAO_ASR_MODEL", "bigmodel")
        self.ws: Any | None = None
        self.seq = 1

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def __aenter__(self) -> "DoubaoSeedAsrStream":
        if not self.configured:
            raise RuntimeError("DOUBAO_ASR_API_KEY not configured")
        req_id = str(uuid.uuid4())
        headers = {
            "X-Api-Key": self.api_key or "",
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": req_id,
            "X-Api-Connect-Id": req_id,
            "X-Api-Sequence": "-1",
        }
        self.ws = await websockets.connect(self.base_url, additional_headers=headers, max_size=8 * 1024 * 1024)
        await self._send_full_request()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def send_audio(self, pcm: bytes, *, final: bool = False) -> None:
        if not self.ws:
            return
        seq = -self.seq if final else self.seq
        self.seq += 1
        payload = gzip.compress(pcm)
        flags = FLAG_NEG_WITH_SEQUENCE if final else FLAG_POS_SEQUENCE
        await self.ws.send(_pack(MSG_AUDIO_ONLY_REQUEST, flags, payload, seq=seq))

    async def recv_text(self) -> tuple[str, bool] | None:
        if not self.ws:
            return None
        raw = await self.ws.recv()
        if isinstance(raw, str):
            return _extract_text(_loads_json(raw))
        msg_type, payload, is_last = _unpack(raw)
        if msg_type == MSG_ERROR:
            raise RuntimeError(payload.decode("utf-8", errors="replace"))
        data = _loads_json(payload)
        item = _extract_text(data)
        if item and is_last:
            return item[0], True
        return item

    async def _send_full_request(self) -> None:
        assert self.ws is not None
        req = {
            "user": {"uid": os.getenv("DOUBAO_ASR_UID", "shiftmind")},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": self.model,
                "enable_punc": True,
                "enable_itn": True,
                "enable_ddc": True,
                "show_utterances": True,
                "enable_nonstream": False,
            },
        }
        payload = gzip.compress(json.dumps(req, ensure_ascii=False).encode("utf-8"))
        await self.ws.send(_pack(MSG_FULL_CLIENT_REQUEST, FLAG_POS_SEQUENCE, payload, seq=self.seq))
        self.seq += 1


def _pack(msg_type: int, flags: int, payload: bytes, *, seq: int) -> bytes:
    header = bytes([
        0x11,  # version 1, header size 1 * 4 bytes
        (msg_type << 4) | flags,
        (SERIALIZATION_JSON << 4) | COMPRESSION_GZIP,
        0x00,
    ])
    return header + struct.pack(">iI", seq, len(payload)) + payload


def _unpack(raw: bytes) -> tuple[int, bytes, bool]:
    if len(raw) < 4:
        return MSG_ERROR, raw, False
    header_size = raw[0] & 0x0F
    msg_type = raw[1] >> 4
    flags = raw[1] & 0x0F
    compression = raw[2] & 0x0F
    offset = header_size * 4
    is_last = bool(flags & 0x02)
    if flags & 0x01:
        offset += 4
    if flags & 0x04:
        offset += 4
    if msg_type == MSG_FULL_SERVER_RESPONSE and len(raw) >= offset + 4:
        size = struct.unpack(">I", raw[offset:offset + 4])[0]
        offset += 4
        payload = raw[offset:offset + size]
    elif msg_type == MSG_ERROR and len(raw) >= offset + 8:
        code = raw[offset:offset + 4]
        size = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
        offset += 8
        payload = code + b" " + raw[offset:offset + size]
    else:
        payload = raw[offset:]
    if compression == COMPRESSION_GZIP and payload:
        payload = gzip.decompress(payload)
    return msg_type, payload, is_last


def _loads_json(payload: bytes | str) -> Any:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    try:
        return json.loads(payload)
    except Exception:
        return {"text": payload}


def _extract_text(data: Any) -> tuple[str, bool] | None:
    if not isinstance(data, dict):
        return None
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    text = (
        result.get("text")
        or result.get("utterance")
        or result.get("transcript")
        or result.get("result")
        or data.get("text")
        or ""
    )
    if not text and isinstance(result.get("utterances"), list):
        text = " ".join(str(u.get("text") or "") for u in result["utterances"] if isinstance(u, dict)).strip()
    if not text:
        return None
    final = bool(
        result.get("is_final")
        or result.get("final")
        or result.get("definite")
        or data.get("is_final")
        or data.get("final")
    )
    return str(text), final
