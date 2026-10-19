"""Optional transcription: local faster-whisper (GPU/CPU) or an OpenAI-compatible API, all in memory."""

import asyncio
import importlib.util
import io
import os

from curl_cffi import CurlMime

from agent_wire.engine.http_pool import HttpPool
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.media.subtitle_cleaner import to_paragraphs

MAX_AUDIO_BYTES = 200 * 1024 * 1024
DEFAULT_API = "https://api.openai.com/v1/audio/transcriptions"


class TranscriptionUnavailable(Exception):
    pass


def enabled() -> bool:
    return os.environ.get("WIRE_TRANSCRIBE") == "1"


def backend() -> str | None:
    if importlib.util.find_spec("faster_whisper"):
        return "local"
    if os.environ.get("WIRE_TRANSCRIBE_API_KEY"):
        return "api"
    return None


async def transcribe(pool: HttpPool, audio_url: str) -> str:
    kind = backend()
    if not kind:
        raise TranscriptionUnavailable(
            "install agent-wire-cli[local-whisper] or set WIRE_TRANSCRIBE_API_KEY for an OpenAI-compatible API"
        )
    f = await pool.get(audio_url, impersonate=None, timeout=300)
    if not f.ok or len(f.content) > MAX_AUDIO_BYTES:
        raise TranscriptionUnavailable(
            f"audio download failed or too large ({len(f.content)} bytes)"
        )
    if kind == "local":
        return await asyncio.to_thread(_local, f.content)
    return await _api(pool, f.content, audio_url.rsplit("/", 1)[-1].split("?")[0] or "audio.mp3")


def _local(audio: bytes) -> str:
    from faster_whisper import WhisperModel

    model = WhisperModel(
        os.environ.get("WIRE_WHISPER_MODEL", "small"), device="auto", compute_type="auto"
    )
    segments, _ = model.transcribe(io.BytesIO(audio), vad_filter=True)
    return to_paragraphs([(s.start, s.text.strip()) for s in segments])


async def _api(pool: HttpPool, audio: bytes, filename: str) -> str:
    # Opt-in only: this uploads the (public) audio to the configured provider.
    mime = CurlMime()
    mime.addpart(name="model", data=os.environ.get("WIRE_TRANSCRIBE_MODEL", "whisper-1").encode())
    mime.addpart(
        name="file", filename=filename, content_type="application/octet-stream", data=audio
    )
    try:
        f = await pool.get(
            os.environ.get("WIRE_TRANSCRIBE_URL", DEFAULT_API), method="POST", impersonate=None,
            headers={"Authorization": f"Bearer {os.environ['WIRE_TRANSCRIBE_API_KEY']}"},
            multipart=mime, timeout=600,
        )  # fmt: skip
    finally:
        mime.close()
    if not f.ok:
        raise TranscriptionUnavailable(f"transcription API returned HTTP {f.status}")
    return parse_json(f.content).get("text", "")
