"""Deepgram REST STT — Phase 3 of the cloud-stack migration.

A drop-in replacement for `CoraWhisperSTT` (segmented mode). Same
contract: VAD hands us a WAV blob, we POST it to Deepgram's /v1/listen,
yield a TranscriptionFrame.

Provider switch is in phase1_push_to_talk.py via `CORA_STT_PROVIDER`
env var:
    whisper   → local Whisper (default, current behaviour)
    deepgram  → this module

Backout: set CORA_STT_PROVIDER=whisper (or unset) and restart
cora-voice. The CoraWhisperSTT path is untouched.

We use the REST API rather than Deepgram's streaming WebSocket because
the existing pipeline is already segmented (Silero VAD chunks audio at
end-of-utterance). REST keeps the architecture identical and avoids
having to reshape the pipeline to a StreamingSTTService. If we want to
push for streaming gains later (~150-300ms savings per turn), that's a
separate phase.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncGenerator

import aiohttp
from loguru import logger

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.services.stt_service import SegmentedSTTService


DEEPGRAM_URL = os.getenv(
    "DEEPGRAM_URL", "https://api.deepgram.com/v1/listen"
)
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3")
DEEPGRAM_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "en")
DEEPGRAM_TIMEOUT_S = float(os.getenv("DEEPGRAM_TIMEOUT_S", "8.0"))


class CoraDeepgramSTT(SegmentedSTTService):
    """Deepgram-hosted Whisper-class STT via REST.

    Same shape as CoraWhisperSTT — extends SegmentedSTTService so VAD
    delivers complete WAV blobs to `run_stt`. We POST to Deepgram and
    yield a TranscriptionFrame on the first transcript that comes back.
    On any failure (network, 5xx, empty result) we yield nothing,
    matching Whisper's behaviour for empty transcripts.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        language: str | None = None,
        sample_rate: int | None = 16000,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        if not api_key:
            raise ValueError(
                "CoraDeepgramSTT requires an API key (DEEPGRAM_API_KEY)."
            )
        self._api_key = api_key
        self._model = model or DEEPGRAM_MODEL
        self._language = language or DEEPGRAM_LANGUAGE

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        t0 = time.monotonic()
        audio_bytes = len(audio)
        logger.info(f"[stt-deepgram] sending {audio_bytes} bytes to Deepgram")
        params = {
            "model": self._model,
            "language": self._language,
            "smart_format": "true",
            # punctuation is on by default with smart_format; redact off.
        }
        headers = {
            "Authorization": f"Token {self._api_key}",
            # WAV; Deepgram sniffs the header so we don't strictly need
            # to declare it, but being explicit is friendlier to debug.
            "Content-Type": "audio/wav",
        }
        timeout = aiohttp.ClientTimeout(total=DEEPGRAM_TIMEOUT_S)
        text = ""
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    DEEPGRAM_URL,
                    params=params,
                    headers=headers,
                    data=audio,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            f"[stt-deepgram] HTTP {resp.status}: "
                            f"{body[:200]}"
                        )
                        return
                    payload = await resp.json()
                    logger.info(f"[stt-deepgram] raw response: {payload}")
        except asyncio.TimeoutError:
            logger.warning(
                f"[stt-deepgram] timeout after {DEEPGRAM_TIMEOUT_S}s"
            )
            return
        except Exception as e:
            logger.warning(
                f"[stt-deepgram] request failed: {type(e).__name__}: {e or '(no detail)'}"
            )
            return

        # Deepgram response shape (Whisper-compatible models):
        #   results.channels[0].alternatives[0].transcript
        try:
            channels = (
                payload.get("results", {}).get("channels", []) or []
            )
            if channels:
                alternatives = channels[0].get("alternatives", []) or []
                if alternatives:
                    text = (alternatives[0].get("transcript") or "").strip()
        except Exception as e:
            logger.warning(
                f"[stt-deepgram] response parse failed: {type(e).__name__}: {e}"
            )
            return

        dt_ms = (time.monotonic() - t0) * 1000.0
        logger.info(f"[stt-deepgram] {text!r}  ({dt_ms:.0f}ms)")
        if text:
            yield TranscriptionFrame(text, user_id="user", timestamp=time.time())
