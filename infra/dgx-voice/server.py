"""
FastAPI server hosting the Cora voice WebRTC endpoint.

Adapted from pipecat-examples/p2p-webrtc/voice-agent/server.py. Spawns
phase1_push_to_talk.run_bot() as a background task per WebRTC connection.

Run:
    cd ~/cora && source venv/bin/activate
    python server.py --host 0.0.0.0 --port 7860 -v

Tailscale `tailscale serve --bg --https=443 http://localhost:7860`
fronts this with TLS so phones on the tailnet can use getUserMedia.
"""

import argparse
import io
import os
import struct
import sys
import wave
from contextlib import asynccontextmanager

import numpy as np
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pydantic import BaseModel, Field

from phase1_push_to_talk import (
    CORA_TTS_LANG_CODE,
    CORA_TTS_SAMPLE_RATE,
    CORA_TTS_VOICE,
    _KOKORO_PIPE,
    run_bot,
    sanitize_for_tts,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await small_webrtc_handler.close()


app = FastAPI(lifespan=lifespan)

# Cora's web app (cora_v2) lives on a different origin (Windows machine
# at http://localhost:8000 or https://3cprimary.<tailnet>.ts.net) and
# needs to POST/PATCH /api/offer cross-origin. Allow * for now since
# this server is tailnet-only (Tailscale ACLs gate who can reach it).
# Tighten if you ever expose this beyond the tailnet.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

small_webrtc_handler = SmallWebRTCRequestHandler()


@app.post("/api/offer")
async def offer(
    request: SmallWebRTCRequest,
    background_tasks: BackgroundTasks,
    voice: str | None = None,
    lang_code: str | None = None,
):
    """WebRTC offer entry. Optional ?voice= and ?lang_code= query params
    let the browser-side voice picker override the server's default
    Kokoro voice on a per-session basis (without restarting the
    service or editing the env file)."""

    async def webrtc_connection_callback(connection):
        background_tasks.add_task(
            run_bot,
            connection,
            voice=voice,
            lang_code=lang_code,
        )

    return await small_webrtc_handler.handle_web_request(
        request=request,
        webrtc_connection_callback=webrtc_connection_callback,
    )


@app.patch("/api/offer")
async def ice_candidate(request: SmallWebRTCPatchRequest):
    await small_webrtc_handler.handle_patch_request(request)
    return {"status": "success"}


@app.get("/")
async def serve_index():
    return FileResponse("index.html")


# ---------------------------------------------------------------------------
# Voice picker — preview a Kokoro voice without starting a WebRTC session.
# ---------------------------------------------------------------------------
# The cora_v2 web app's Main settings → Voice tab has a "Test" button that
# POSTs to /api/tts-test with {voice, text}. We synthesise on Spark and
# return a WAV blob the browser plays via <audio>. No persistent state —
# the chosen voice still has to be saved client-side and sent through with
# the next /api/offer to take effect on a real conversation.

class TTSTestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    voice: str = Field(min_length=1, max_length=64)
    # Kokoro pipeline lang_code. Some voices need 'a' (American), others
    # 'b' (British), 'j' (Japanese), 'z' (Mandarin), etc. The picker
    # passes this through from the voice's `metadata.lang_code` in
    # common_data so we don't have to maintain a voice→lang map here.
    lang_code: str | None = Field(default=None, max_length=4)


def _pcm16_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw int16 LE PCM in a minimal WAV container so the browser
    can play it as audio/wav without us needing a separate streaming
    format. Mono, 16-bit. Cheap; ~44 byte overhead."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)         # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


@app.post("/api/tts-test")
async def tts_test(body: TTSTestRequest) -> Response:
    text = sanitize_for_tts(body.text)
    if not text:
        raise HTTPException(400, "text is empty after sanitisation")
    voice = body.voice.strip()
    lang_code = (body.lang_code or CORA_TTS_LANG_CODE).strip() or "a"

    # Kokoro's KPipeline is stateful w.r.t. lang_code (the phoneme
    # backend differs per language). The module-level _KOKORO_PIPE was
    # built for the configured default lang_code; if the requested
    # voice needs a different one we'd really want a separate pipeline.
    # For voice preview the wrong-pipeline path still produces audio
    # (just lower quality), and we don't want to pay the cost of
    # spinning up another pipeline per request. If lang_code differs,
    # log a hint and proceed.
    if lang_code != CORA_TTS_LANG_CODE:
        logger.info(
            f"[tts-test] voice={voice!r} lang_code={lang_code!r} differs "
            f"from active pipeline {CORA_TTS_LANG_CODE!r}; quality may be off"
        )

    try:
        chunks = list(_KOKORO_PIPE(text, voice=voice))
    except Exception as e:
        logger.exception(f"[tts-test] synth failed for voice={voice!r}")
        raise HTTPException(500, f"kokoro synth failed: {e}") from e

    # Concatenate float32 audio chunks → int16 PCM.
    arrs = []
    for _g, _p, audio_t in chunks:
        if audio_t is None:
            continue
        arr = audio_t.detach().cpu().numpy() if hasattr(audio_t, "detach") else audio_t
        arrs.append(np.clip(arr, -1.0, 1.0))
    if not arrs:
        raise HTTPException(500, "kokoro produced no audio")
    audio_f32 = np.concatenate(arrs)
    pcm16 = (audio_f32 * 32767.0).astype(np.int16).tobytes()
    wav_bytes = _pcm16_to_wav_bytes(pcm16, CORA_TTS_SAMPLE_RATE)
    return Response(content=wav_bytes, media_type="audio/wav")


# Serve /image/* (Cora logo + branding) when the directory exists.
# `index.html` references `./image/logo_icon.png`; without this mount
# the browser would 404 on it.
if os.path.isdir("image"):
    app.mount("/image", StaticFiles(directory="image"), name="image")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cora voice WebRTC server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--verbose", "-v", action="count", default=0)
    args = parser.parse_args()

    # Pipecat removes the default loguru handler during its own init
    # (the "ᓚᘏᗢ Pipecat" banner is printed via Pipecat's replacement
    # handler), so handler 0 may already be gone when we get here.
    try:
        logger.remove(0)
    except ValueError:
        pass
    logger.add(sys.stderr, level="TRACE" if args.verbose else "DEBUG")

    uvicorn.run(app, host=args.host, port=args.port)
