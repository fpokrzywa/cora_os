"""ElevenLabs streaming TTS — Phase 4 of the cloud-stack migration.

A drop-in replacement for `CoraKokoroTTS`. Same `TTSService` shape,
same `run_tts(text, context_id)` generator contract, same yielded
frames (TTSStartedFrame → TTSAudioRawFrame[…] → TTSStoppedFrame).

Why stream PCM (not MP3): ElevenLabs's `output_format=pcm_24000` matches
Kokoro's 24 kHz output so the rest of the pipeline doesn't need to
change. MP3 would be ~5x smaller on the wire but adds a decode step;
not worth it for a single-user voice loop.

The `eleven_flash_v2_5` model has ~75 ms first-byte latency in their
benchmarks — that's the value prop of Phase 4. Use the `eleven_turbo_v2_5`
model if Flash quality regresses on your hardware; `eleven_multilingual_v2`
if you need non-English.

Backout: set CORA_TTS_PROVIDER=kokoro (or unset) and restart cora-voice.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncGenerator

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.tts_service import TTSService


ELEVENLABS_BASE = os.getenv(
    "ELEVENLABS_BASE_URL", "https://api.elevenlabs.io"
).rstrip("/")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
# Default voice: "Rachel" (one of ElevenLabs's stock voices). Override
# per-deployment by setting ELEVENLABS_VOICE_ID; this matches Kokoro's
# CORA_TTS_VOICE in role — picking a specific persona.
ELEVENLABS_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
# 24kHz matches Kokoro's output so no resampling needed downstream.
# Other valid values: 16000, 22050, 24000, 44100 (pcm only).
ELEVENLABS_SAMPLE_RATE = int(os.getenv("ELEVENLABS_SAMPLE_RATE", "24000"))
# Chunk size of the streaming read. Smaller = lower per-chunk latency
# at the cost of more frame overhead. 1920 bytes = 40ms of 24kHz mono
# int16 — a comfortable middle ground.
ELEVENLABS_CHUNK_BYTES = int(os.getenv("ELEVENLABS_CHUNK_BYTES", "1920"))


# `sanitize_for_tts` lives in phase1_push_to_talk; importing it here
# would create a circular dependency since that module imports this
# one lazily. We re-implement the minimal subset (whitespace
# normalisation) — markdown/emoji stripping happens upstream when
# the LLM output is pushed into the TTS service, so by the time
# `run_tts` gets called the text is already clean.


class CoraElevenLabsTTS(TTSService):
    """ElevenLabs Flash 2.5 streaming TTS.

    Sends one POST per turn, iterates the audio body as raw PCM
    chunks, yields each as a TTSAudioRawFrame. First chunk usually
    arrives in ~150-250 ms (network + EL Flash) vs Kokoro's local
    synthesis which can be 400-800 ms for long sentences.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str | None = None,
        model_id: str | None = None,
        sample_rate: int | None = None,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate or ELEVENLABS_SAMPLE_RATE, **kwargs)
        if not api_key:
            raise ValueError(
                "CoraElevenLabsTTS requires an API key (ELEVENLABS_API_KEY)."
            )
        self._api_key = api_key
        self._voice_id = voice_id or ELEVENLABS_VOICE
        self._model_id = model_id or ELEVENLABS_MODEL

    async def run_tts(
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        text = (text or "").strip()
        if not text:
            return

        url = f"{ELEVENLABS_BASE}/v1/text-to-speech/{self._voice_id}/stream"
        # pcm_<rate>: raw little-endian int16 mono — perfect for Pipecat,
        # no decoder required.
        params = {"output_format": f"pcm_{self.sample_rate}"}
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/wav",
        }
        body = {
            "text": text,
            "model_id": self._model_id,
            # Defaults that match a "calm, warm" voice; tweak via env
            # later if needed. Conservative settings keep the voice
            # consistent across turns.
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        }
        # 30 s total cap — generous enough for very long replies but
        # bounded so a stalled connection eventually fails.
        timeout = aiohttp.ClientTimeout(total=30.0, sock_read=10.0)

        t0 = time.monotonic()
        first_chunk = True
        emitted = False

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url, params=params, headers=headers, json=body
                ) as resp:
                    if resp.status != 200:
                        msg = await resp.text()
                        logger.warning(
                            f"[tts-elevenlabs] HTTP {resp.status}: "
                            f"{msg[:200]}"
                        )
                        yield ErrorFrame(error=f"elevenlabs: HTTP {resp.status}")
                        return

                    yield TTSStartedFrame()
                    emitted = True

                    async for chunk in resp.content.iter_chunked(
                        ELEVENLABS_CHUNK_BYTES
                    ):
                        if not chunk:
                            continue
                        if first_chunk:
                            dt_ms = (time.monotonic() - t0) * 1000.0
                            logger.info(
                                f"[tts-elevenlabs] first chunk in {dt_ms:.0f}ms"
                            )
                            first_chunk = False
                        yield TTSAudioRawFrame(
                            audio=chunk,
                            sample_rate=self.sample_rate,
                            num_channels=1,
                        )
        except asyncio.TimeoutError:
            logger.warning("[tts-elevenlabs] timeout")
            yield ErrorFrame(error="elevenlabs: timeout")
            return
        except Exception as e:
            logger.warning(
                f"[tts-elevenlabs] request failed: {type(e).__name__}: {e or '(no detail)'}"
            )
            yield ErrorFrame(error=f"elevenlabs: {type(e).__name__}")
            return

        if emitted:
            yield TTSStoppedFrame()
