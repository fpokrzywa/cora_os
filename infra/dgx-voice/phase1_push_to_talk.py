"""
Cora voice — Phase 1: VAD-triggered browser <-> Spark voice loop.

Adapted from pipecat-examples/p2p-webrtc/voice-agent/bot.py for the Cora
stack: openai-whisper (PyTorch CUDA) STT, Qwen3 4B via Ollama's OpenAI-
compatible endpoint, and Kokoro-82M TTS, all riding Pipecat 1.1.0's
SmallWebRTCTransport.

The doc says "no VAD" for Phase 1 strictly, but the canonical Pipecat
example uses Silero VAD as a basic pipeline element rather than a Phase
3 streaming feature. Disabling it just to re-enable in Phase 2 is more
fight than reward, so VAD is on. User experience is "click Connect,
then speak" — close enough to push-to-talk.

Run via server.py, not directly. server.py mounts run_bot() as a
background task per WebRTC connection.
"""

import asyncio
import io
import json
import os
import re
import time
import uuid
import wave
from typing import AsyncGenerator

import aiohttp
import numpy as np
import whisper
from kokoro import KPipeline
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMRunFrame,
    OutputTransportMessageFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport


SYSTEM_PROMPT = """\
You are Cora, Freddie's personal assistant. You help with coding (Python,
JavaScript, ServiceNow, React Native, Next.js, Supabase, Postgres,
MongoDB), web searches, inbox monitoring, crypto monitoring, AI news
monitoring, and task management.

VOICE
- Friendly but not eager. Warm without being saccharine.
- Dry, understated humor when it fits — never forced, never constant.
- Conversational and casual. Use contractions. Sound like a sharp
  coworker, not a customer service bot.
- Match length to the request. Short asks get short answers.

DON'T
- Don't open with "Hi, I'm Cora" or "How can I help today?" Just respond.
- Don't close with "Anything else?" or "Let me know if you need more."
- Don't over-confirm. "Done" is usually enough.
- Don't be performatively cheerful. Skip the exclamation points unless
  something genuinely deserves one.
- Don't apologize or hedge unnecessarily.

CONFIRMATIONS
Short and natural. "Done." "Marked complete." "Sent." "Pulling that up."
A dry aside is fine when it fits, but if the moment is plain, leave it plain.

EXAMPLES — these illustrate TONE only. They are not real conversations.
Use them ONLY to calibrate your style. Never copy their content. If
Freddie's actual question matches one of these topically, answer from
the SCREEN DATA section below — never from these examples.

User: Why is this throwing an undefined error?
Cora: You're reading user.email before the fetch resolves. Await it
or move the access inside the .then().

User: Can you write this in TypeScript instead?
Cora: Sure. Two lines change — want the patch or just the diff?

User: I'm thinking of pushing this on Friday.
Cora: Friday at 4 is the "ask me again Monday" zone. Wait.

User: Got it.
Cora: Done.

OUTPUT FORMAT — your replies are spoken aloud
- Use plain spoken English. No emojis, smileys, asterisks, markdown,
  code blocks, bullet lists, or any character that doesn't translate
  to speech.
- For code or technical content, describe it conversationally rather
  than literally reading it out.
- Keep replies brief — voice replies that run long lose the room.

TOOLS
You have these tools. ALWAYS fire the matching tool when Freddie's
request maps to one — never just say "done" without firing it.

UI control (drives the cora_v2 web frontend):
- open_card(slug) — opens a response card. Use when he asks to open,
  show, pull up, or display a card. Slugs (c1, c2, …) are in the
  screen context.
- open_panel(panel) — opens 'skills' or 'activity' panel.
- toggle_panel(panel, open?) — close or toggle a panel.
- open_agent_tab(agent) — opens one of the agent tabs at the top
  (cora / atlas / scribe / forge / pulse / signal / chronos).
- focus_skill_section(category) — opens skills panel and focuses a
  category (communication / research / coding / calendar / memory).
- focus_activity_lane(lane) — opens activity panel and focuses a lane
  (inbox_replies / scout_tasks / flux_tasks / relay_drafts / pulse_news).
- set_view_mode(mode) — switch between 'cards' / 'bubbles' / 'both'.
- open_settings(target?, tab?) — open the settings modal.
- close_settings() — close whichever settings modal is open. Use for
  "close settings", "dismiss settings", "exit settings".
- close_modal() — close an open card or bubble detail dialog. Use for
  "close the card", "back out", "dismiss this".

Memory (SCRIBE — Cora's three-tier memory system):
- scribe_save(body, kind?, tags?, importance?, title?) — save a memory
  to the long-term store. Use when Freddie tells you to remember
  something or when you decide a fact / decision / preference is
  worth keeping. Kind options: 'fact', 'decision', 'preference',
  'observation', 'note' (default). Tag liberally for future recall.
- scribe_recall(query, tag?, limit?) — search past memories. Use when
  Freddie asks "what did we decide about X" or "do you remember Y".
  The "Recent memories" block in your context already covers the last
  20 days — call this for older saves or when the recent block isn't
  enough.

Browser automation (drives a real headed Chromium on Freddie's box
that he can SEE on his screen):
- browser_open(url), browser_click(selector), browser_type(selector,
  text, submit=False), browser_screenshot(), browser_back(),
  browser_close(). Use whenever he asks you to do something on a real
  website — "open arxiv", "search for X on Google", "pull up my
  GitHub notifications", "log into Gmail". DO NOT refuse browser
  requests; you DO have this capability via these tools. For
  multi-step flows (open → click → type), wait for his next
  push-to-talk before the next tool call.

After firing any tool, a one-line spoken confirmation like "Pulling
that up", "Opened your skills", or "Opening arxiv now" is plenty —
don't recite contents unless asked.

If Freddie asks ABOUT something on the screen, answer from the screen
context; don't open it.
"""

# =============================================================================
# CONFIGURATION — every model + endpoint is overridable via env var.
# =============================================================================
# Defaults below are the current production values; an empty environment
# (no overrides) reproduces today's behaviour exactly.
#
# Override pattern: the systemd unit's EnvironmentFile is
# `~/.config/cora/env` — drop `KEY=value` lines there and restart
# `cora-voice`. See `voice/cora.env.example` in the repo for a
# documented template you can copy.
#
# Why env vars (vs a YAML / TOML config file): the systemd unit already
# wires EnvironmentFile, the user already keeps secrets there
# (HF_TOKEN), and a new model swap is a one-line edit + service
# restart. Adding a config-file format would be more code with no
# capability gain.

def _env(name: str, default: str) -> str:
    """Trimmed env lookup with default. Empty string treated as unset."""
    v = os.environ.get(name, "")
    return v.strip() if v.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name, "").strip()
        return int(v) if v else default
    except ValueError:
        return default


# ---- STT (Whisper or Deepgram — Phase 3 of cloud-stack migration) ---------
# Provider switch:
#   whisper  → local openai-whisper on torch CUDA (default, current).
#   deepgram → Deepgram REST /v1/listen (requires DEEPGRAM_API_KEY).
# Backout: set CORA_STT_PROVIDER=whisper (or unset) and restart cora-voice.
CORA_STT_PROVIDER = _env("CORA_STT_PROVIDER", "whisper").lower()
# `CORA_STT_MODEL` is any name openai-whisper.load_model accepts:
#   tiny / base / small / medium / large / large-v2 / large-v3 /
#   large-v3-turbo (default — ~155ms on Spark, sweet-spot for voice).
CORA_STT_MODEL    = _env("CORA_STT_MODEL", "large-v3-turbo")
CORA_STT_DEVICE   = _env("CORA_STT_DEVICE", "cuda")     # cuda | cpu
CORA_STT_LANGUAGE = _env("CORA_STT_LANGUAGE", "en")     # ISO 639-1
CORA_STT_FP16     = _env_bool("CORA_STT_FP16", True)
# Deepgram-only config (read but unused when CORA_STT_PROVIDER=whisper).
# The API key itself is read in run_bot() where the STT is constructed.
CORA_DEEPGRAM_MODEL    = _env("DEEPGRAM_MODEL", "nova-3")
CORA_DEEPGRAM_LANGUAGE = _env("DEEPGRAM_LANGUAGE", CORA_STT_LANGUAGE)

# ---- LLM ------------------------------------------------------------------
# Provider switch (Phase 5 of cloud-stack migration):
#   openai     (default) — OpenAI-compatible endpoint. Used for the
#               local Ollama / vLLM stack and for any cloud provider
#               that exposes /v1/chat/completions (Groq, OpenAI,
#               OpenRouter, etc.). Configure via CORA_LLM_BASE_URL +
#               CORA_LLM_MODEL + CORA_LLM_API_KEY.
#   anthropic  — Anthropic API (Claude Haiku / Sonnet / Opus). Uses
#               pipecat's AnthropicLLMService directly because the
#               Messages API isn't OpenAI-compatible. Set
#               CORA_LLM_MODEL=claude-haiku-4-5-20251001 (or any
#               Anthropic model id) and CORA_LLM_API_KEY=<your key>.
# Backout: CORA_LLM_PROVIDER=openai (or unset) + restart cora-voice.
CORA_LLM_PROVIDER = _env("CORA_LLM_PROVIDER", "openai").lower()
CORA_LLM_MODEL    = _env("CORA_LLM_MODEL",    "cora-qwen3:4b")
CORA_LLM_BASE_URL = _env("CORA_LLM_BASE_URL", "http://localhost:11434/v1")
CORA_LLM_API_KEY  = _env("CORA_LLM_API_KEY",  "ollama")

# ---- TTS (Kokoro or ElevenLabs — Phase 4 of cloud-stack migration) --------
# Provider switch:
#   kokoro     → local Kokoro-82M on torch (default, current).
#   elevenlabs → ElevenLabs Flash 2.5 streaming; requires ELEVENLABS_API_KEY.
# Backout: CORA_TTS_PROVIDER=kokoro (or unset) + restart cora-voice.
CORA_TTS_PROVIDER = _env("CORA_TTS_PROVIDER", "kokoro").lower()
# Kokoro-only — CORA_TTS_LANG_CODE: 'a' American English, 'b' British,
# 'j' Japanese, 'z' Mandarin, 'e' Spanish, 'f' French, etc. — must match
# the voice's language. CORA_TTS_VOICE values: see
# https://huggingface.co/hexgrad/Kokoro-82M.
CORA_TTS_REPO_ID     = _env("CORA_TTS_REPO_ID", "hexgrad/Kokoro-82M")
CORA_TTS_LANG_CODE   = _env("CORA_TTS_LANG_CODE", "a")
CORA_TTS_VOICE       = _env("CORA_TTS_VOICE", "af_heart")
CORA_TTS_SAMPLE_RATE = _env_int("CORA_TTS_SAMPLE_RATE", 24000)
# ElevenLabs-only (ignored when CORA_TTS_PROVIDER=kokoro). The API key
# itself is read in run_bot() where the TTS is constructed.
CORA_ELEVENLABS_MODEL_ID   = _env("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
CORA_ELEVENLABS_VOICE_ID   = _env("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
CORA_ELEVENLABS_SAMPLE_RATE = _env_int("ELEVENLABS_SAMPLE_RATE", 24000)

# ---- Cleanup pass (also goes through the LLM endpoint) ---------------------
# By default, the post-STT cleanup uses the same LLM as the main reply.
# Set CORA_CLEANUP_MODEL to a different value to use a smaller/faster
# model just for cleanup (e.g. a Qwen3 0.6B). CORA_CLEANUP_ENABLED=0
# turns the cleanup pass off entirely.
CORA_CLEANUP_ENABLED = _env_bool("CORA_CLEANUP_ENABLED", True)
CORA_CLEANUP_MODEL   = _env("CORA_CLEANUP_MODEL", CORA_LLM_MODEL)

# ---- cora_v2 screen-context endpoint --------------------------------------
# The Windows machine running the cora_v2 web app, reachable on the tailnet.
CORA_V2_URL = _env(
    "CORA_V2_URL", "http://3cprimary.tail343b33.ts.net:8000"
).rstrip("/")

# Log resolved config at module load so it shows up in cora-voice.log
# right next to [startup] banners. Easy to confirm an env override
# actually took effect.
logger.info(
    f"[config] STT provider={CORA_STT_PROVIDER} "
    f"(whisper: model={CORA_STT_MODEL} device={CORA_STT_DEVICE} "
    f"lang={CORA_STT_LANGUAGE} fp16={CORA_STT_FP16}) "
    f"(deepgram: model={CORA_DEEPGRAM_MODEL} lang={CORA_DEEPGRAM_LANGUAGE})"
)
logger.info(
    f"[config] LLM provider={CORA_LLM_PROVIDER} model={CORA_LLM_MODEL} "
    f"base_url={CORA_LLM_BASE_URL if CORA_LLM_PROVIDER == 'openai' else '<anthropic api>'}"
)
logger.info(
    f"[config] TTS provider={CORA_TTS_PROVIDER} "
    f"(kokoro: repo={CORA_TTS_REPO_ID} voice={CORA_TTS_VOICE} "
    f"lang_code={CORA_TTS_LANG_CODE} sr={CORA_TTS_SAMPLE_RATE}) "
    f"(elevenlabs: model={CORA_ELEVENLABS_MODEL_ID} "
    f"voice={CORA_ELEVENLABS_VOICE_ID} sr={CORA_ELEVENLABS_SAMPLE_RATE})"
)
logger.info(
    f"[config] cleanup enabled={CORA_CLEANUP_ENABLED} model={CORA_CLEANUP_MODEL}"
)
logger.info(f"[config] cora_v2_url={CORA_V2_URL}")


# TTS sanitiser. Even with the system prompt above, Qwen3 sometimes
# emits emoji or stray markdown punctuation. Kokoro's phonemiser will
# either skip them or, worse, read out their accessibility names
# ("smiling face with smiling eyes"). Strip them before synthesis.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # main pictograph + emoji blocks
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F1E0-\U0001F1FF"  # regional indicators (flags)
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "‍"                 # zero-width joiner (used in compound emoji)
    "]+",
    flags=re.UNICODE,
)
# Drop common markdown punctuation that the model occasionally leaks.
# Keep apostrophes and hyphens — those are spoken-language characters.
_MD_RE = re.compile(r"\*+|_{2,}|`+|~{2,}|#{1,6}\s")


def sanitize_for_tts(text: str) -> str:
    text = _EMOJI_RE.sub("", text)
    text = _MD_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

LOGS_DIR = os.path.expanduser("~/cora/logs")
TRACE_PATH = os.path.join(LOGS_DIR, "trace.jsonl")
os.makedirs(LOGS_DIR, exist_ok=True)


class TraceLog:
    """One JSONL line per utterance.

    Captures wall-clock offsets relative to the start of stt for:
        stt_final, llm_first_token, tts_start, tts_first_audio, tts_done.
    Phase 6 latency harness reads these.
    """

    def __init__(self, path: str = TRACE_PATH):
        self.path = path
        self.current: dict | None = None

    def begin(self):
        self.current = {"utt": str(uuid.uuid4()), "t0_wall": time.time()}
        self._t0 = time.perf_counter()

    def mark(self, key: str):
        if self.current is not None:
            self.current[key] = round(time.perf_counter() - self._t0, 4)

    def flush(self):
        if self.current is None:
            return
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(self.current) + "\n")
        finally:
            self.current = None


TRACE = TraceLog()


# Load both models at module import time so `python server.py` finishes
# warming up BEFORE it starts accepting WebRTC connections. Loading inside
# run_bot() means the first connection burns 30+ seconds before it can
# respond; the WebRTC data channel times out at 10s and the connection
# dies. Module-level load fixes this — each new connection just reuses the
# already-warm models. First `python server.py` takes ~35 s to reach
# "Uvicorn running", but every subsequent connection is instant.
#
# Whisper is only loaded when CORA_STT_PROVIDER=whisper (the default).
# With CORA_STT_PROVIDER=deepgram we skip the 30 s warm-up + the GPU
# memory entirely; Deepgram is REST, no local model. To flip back to
# Whisper, just change the env var and restart cora-voice (warm-up will
# happen at next startup).
if CORA_STT_PROVIDER == "whisper":
    logger.info(f"[startup] loading whisper {CORA_STT_MODEL} on {CORA_STT_DEVICE}")
    _WHISPER_MODEL = whisper.load_model(CORA_STT_MODEL, device=CORA_STT_DEVICE)
    _WHISPER_MODEL.transcribe(
        np.zeros(16000, dtype="float32"),
        language=CORA_STT_LANGUAGE,
        fp16=CORA_STT_FP16,
    )
    logger.info("[startup] whisper warm")
else:
    logger.info(
        f"[startup] skipping whisper load (CORA_STT_PROVIDER={CORA_STT_PROVIDER!r})"
    )
    _WHISPER_MODEL = None

if CORA_TTS_PROVIDER == "kokoro":
    logger.info(f"[startup] loading kokoro repo={CORA_TTS_REPO_ID} voice={CORA_TTS_VOICE}")
    _KOKORO_PIPE = KPipeline(lang_code=CORA_TTS_LANG_CODE, repo_id=CORA_TTS_REPO_ID)
    list(_KOKORO_PIPE("Hello.", voice=CORA_TTS_VOICE))
    logger.info("[startup] kokoro warm")
else:
    logger.info(
        f"[startup] skipping kokoro load (CORA_TTS_PROVIDER={CORA_TTS_PROVIDER!r})"
    )
    _KOKORO_PIPE = None


class CoraWhisperSTT(SegmentedSTTService):
    """openai-whisper (large-v3-turbo) on PyTorch CUDA.

    SegmentedSTTService hands run_stt() complete WAV bytes after VAD
    detects end-of-speech. We decode → resample to 16k → transcribe →
    yield TranscriptionFrame. The model itself is module-level (see
    `_WHISPER_MODEL` above) so per-connection construction is instant.
    """

    def __init__(
        self,
        *,
        language: str | None = None,
        sample_rate: int | None = 16000,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._model = _WHISPER_MODEL
        self._language = language or CORA_STT_LANGUAGE
        self._fp16 = CORA_STT_FP16

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        TRACE.begin()
        TRACE.mark("stt_start")

        with wave.open(io.BytesIO(audio), "rb") as wf:
            sr = wf.getframerate()
            pcm = wf.readframes(wf.getnframes())

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if sr != 16000:
            target_n = int(len(samples) * 16000 / sr)
            samples = np.interp(
                np.linspace(0, len(samples) - 1, target_n),
                np.arange(len(samples)),
                samples,
            ).astype("float32")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._model.transcribe(samples, language=self._language, fp16=self._fp16),
        )
        text = result.get("text", "").strip()
        TRACE.mark("stt_final")
        logger.info(f"[stt] {text!r}")
        if text:
            yield TranscriptionFrame(text, user_id="user", timestamp=time.time())


class TranscriptCleanup(FrameProcessor):
    """Light-touch cleanup pass between STT and the LLM.

    Whisper produces verbatim transcripts including fillers ("um",
    "uh"), self-corrections ("I mean..."), and occasional proper-noun
    misspellings. Sending those straight to the LLM works but feels
    rough — and the proper-noun mistakes break tool calls (e.g.
    `open_card("Helics")` won't match a real slug).

    This processor intercepts each TranscriptionFrame, asks Qwen3 to
    produce a lightly-cleaned version, and forwards a new frame with
    the cleaned text. ~150-300ms added per turn (Ollama is local).

    Conservative by design:
    - Empty / whitespace-only / very-short utterances skip cleanup.
    - LLM is told NOT to answer or change intent — just clean.
    - If the cleaned output is wildly longer than the input, we
      assume hallucination and fall back to the original.
    - Vocabulary hint biases proper-noun spellings toward known
      names without forcing them.
    """

    # Words we know Whisper sometimes mangles. Hint to the LLM, not a
    # mandate. Extend as we discover more.
    _BASE_VOCAB = (
        # Card-related proper nouns from the seed deck
        "Helix Ridgeline Aperture Linear Framer Westbound Cora "
        # Tech Cora actually helps with
        "Python JavaScript TypeScript ServiceNow React Native "
        "Next.js Supabase Postgres MongoDB "
        # User
        "Freddie"
    ).split()

    def __init__(
        self,
        *,
        ollama_url: str = "http://localhost:11434",
        model: str = "cora-qwen3:4b",
        extra_vocab: list[str] | None = None,
    ):
        super().__init__()
        self._url = ollama_url.rstrip("/") + "/v1/chat/completions"
        self._model = model
        # Dedupe + preserve order
        seen = set()
        vocab = []
        for w in self._BASE_VOCAB + (extra_vocab or []):
            if w and w not in seen:
                seen.add(w)
                vocab.append(w)
        self._vocab = vocab
        self._session: aiohttp.ClientSession | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text and frame.text.strip():
            cleaned = await self._cleanup(frame.text)
            if cleaned and cleaned != frame.text:
                logger.info(f"[cleanup] {frame.text!r} -> {cleaned!r}")
                # Replace the frame with one carrying the cleaned text.
                # All other fields preserved so downstream consumers
                # (LLMUserAggregator) can't tell the difference.
                replacement = TranscriptionFrame(
                    cleaned,
                    user_id=getattr(frame, "user_id", "user"),
                    timestamp=getattr(frame, "timestamp", time.time()),
                )
                await self.push_frame(replacement, direction)
                return
        await self.push_frame(frame, direction)

    async def _cleanup(self, text: str) -> str:
        # Skip very short utterances — fillers in 1-2 words are usually
        # the entire utterance, and self-corrections need context to
        # exist anyway.
        if len(text.split()) < 3:
            return text

        vocab_line = (
            f"Known names/words: {', '.join(self._vocab)}.\n"
            if self._vocab
            else ""
        )
        prompt = (
            "You are a transcript cleaner. Lightly clean voice transcripts "
            "before another assistant processes them.\n"
            "\n"
            "Rules:\n"
            "- Remove filler words: um, uh, er, ah. Remove 'like' and "
            "'you know' only when used as filler, not when meaningful.\n"
            "- Resolve self-corrections: if the user said 'X — I mean Y' "
            "or 'X, no, Y', return only the corrected version.\n"
            "- If a proper noun looks misheard, fix the spelling using "
            "the known-names list below. Do NOT substitute words that "
            "are clearly intentional.\n"
            "- Preserve all other meaning. Do NOT answer the user. Do NOT "
            "explain. Do NOT add commentary. Do NOT change the request.\n"
            "- If the transcript is already clean, return it unchanged.\n"
            "- Return ONLY the cleaned transcript. Nothing else.\n"
            "\n"
            f"{vocab_line}"
            f"Transcript: {text}\n"
            "Cleaned transcript:"
        )

        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=3.0)
                )
            payload = {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 200,
                "stream": False,
            }
            async with self._session.post(self._url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning(f"[cleanup] HTTP {resp.status}; using raw")
                    return text
                data = await resp.json()
                cleaned = (
                    (data.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
        except Exception as e:
            logger.warning(f"[cleanup] failed: {type(e).__name__}: {e}; using raw")
            return text

        if not cleaned:
            return text
        # Strip prefixes the LLM sometimes adds ("Cleaned transcript:")
        for prefix in ("Cleaned transcript:", "Cleaned:", "Output:"):
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[len(prefix):].strip()
        # Strip wrapping quotes
        cleaned = cleaned.strip().strip('"').strip("'").strip()
        # Sanity check: rampant hallucination → fall back
        if len(cleaned) > len(text) * 3 + 40:
            logger.warning(
                f"[cleanup] result too long ({len(cleaned)} vs {len(text)}); using raw"
            )
            return text
        return cleaned


class CoraKokoroTTS(TTSService):
    """Kokoro-82M, single-pass synthesis.

    Outputs 24kHz int16 PCM. Streams chunks as Kokoro produces them
    (per-sentence under the hood), so first audio reaches the user
    before the whole reply is synthesised. The Kokoro pipeline itself
    is module-level (see `_KOKORO_PIPE` above) so per-connection
    construction is instant.
    """

    def __init__(
        self,
        *,
        voice: str | None = None,
        sample_rate: int | None = None,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate or CORA_TTS_SAMPLE_RATE, **kwargs)
        self._pipe = _KOKORO_PIPE
        self._voice = voice or CORA_TTS_VOICE

    async def run_tts(
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        text = sanitize_for_tts(text)
        if not text:
            # All content got stripped (e.g. reply was just an emoji).
            # Don't synthesise silence — Pipecat handles the empty case
            # by just not pushing audio frames downstream.
            return

        TRACE.mark("tts_start")
        loop = asyncio.get_event_loop()
        try:
            chunks = await loop.run_in_executor(
                None, lambda: list(self._pipe(text, voice=self._voice))
            )
        except Exception as e:
            logger.exception("[tts] kokoro synth failed")
            yield ErrorFrame(error=f"kokoro: {e}")
            TRACE.flush()
            return

        yield TTSStartedFrame()
        first_chunk = True
        for _graphemes, _phonemes, audio_t in chunks:
            if audio_t is None:
                continue
            arr = audio_t.detach().cpu().numpy() if hasattr(audio_t, "detach") else audio_t
            arr = np.clip(arr, -1.0, 1.0)
            pcm = (arr * 32767.0).astype(np.int16).tobytes()
            if first_chunk:
                TRACE.mark("tts_first_audio")
                first_chunk = False
            yield TTSAudioRawFrame(
                audio=pcm, sample_rate=self.sample_rate, num_channels=1
            )
        yield TTSStoppedFrame()
        TRACE.mark("tts_done")
        TRACE.flush()


async def fetch_voice_config() -> dict[str, str]:
    """Fetch the active cora_voice agent_configs row + rendered
    agent_prompt_examples block from cora_v2. Lets the prompt be
    edited via the web UI / SQL without redeploying voice.

    Returns {'system_prompt': str, 'examples_block': str}. Either may
    be empty on failure or if no DB row exists; caller treats empty
    `system_prompt` as "fall back to the hard-coded SYSTEM_PROMPT."
    """
    url = f"{CORA_V2_URL}/api/agents/voice-config"
    try:
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"[voice-cfg] {url} → HTTP {resp.status}")
                    return {"system_prompt": "", "examples_block": ""}
                payload = await resp.json()
                sp = (payload.get("system_prompt") or "").strip()
                ex = (payload.get("examples_block") or "").strip()
                logger.info(
                    f"[voice-cfg] fetched system_prompt={len(sp)} chars, "
                    f"examples_block={len(ex)} chars, source={payload.get('source')}"
                )
                return {"system_prompt": sp, "examples_block": ex}
    except Exception as e:
        logger.warning(
            f"[voice-cfg] fetch failed for {url}: {type(e).__name__}: {e or '(no detail)'}"
        )
        return {"system_prompt": "", "examples_block": ""}


async def fetch_screen_context() -> str:
    """Pull the cards + activity + tasks summary from the cora_v2 web
    app over the tailnet. Same endpoint and same content the chat path
    uses — keeping voice and text aware of the same on-screen state.

    On any failure (cora_v2 down, tailnet glitch, timeout) we return an
    empty string and the bot just runs without screen awareness rather
    than refusing to start. Fetched once per WebRTC connection; if the
    user changes the screen mid-session the new state isn't picked up
    until they reconnect.
    """
    # data_only=true: skip the persona block (we have our own,
    # tailored for voice). We just want the cards/activity/tasks data.
    url = f"{CORA_V2_URL}/api/screen-context?data_only=true"
    # 5 s budget — tailnet roundtrip + cora_v2 first-byte (uvicorn cold
    # path through the chat infra) can hit ~1-2 s. Anything slower than
    # 5 s probably means cora_v2 isn't actually listening, and we'd
    # rather degrade to no-context than hang the WebRTC handshake.
    try:
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"[ctx] {url} → HTTP {resp.status}")
                    return ""
                payload = await resp.json()
                snap = (payload.get("snapshot") or "").strip()
                if snap:
                    logger.info(f"[ctx] fetched {len(snap)} chars of screen context")
                else:
                    logger.warning(f"[ctx] {url} → empty snapshot")
                return snap
    except Exception as e:
        logger.warning(
            f"[ctx] fetch failed for {url}: {type(e).__name__}: {e or '(no detail)'}"
        )
        return ""


def _build_stt():
    """STT provider factory (Phase 3 of cloud-stack migration).

    Reads `CORA_STT_PROVIDER` at call time. Returning a fresh service
    per WebRTC connection matches how Pipecat expects to own service
    lifecycles. Both providers extend SegmentedSTTService — the
    rest of the pipeline can't tell the difference.

    Backout: set CORA_STT_PROVIDER=whisper (or unset) and restart.
    """
    if CORA_STT_PROVIDER == "deepgram":
        # Lazy import so an environment without aiohttp+stt_deepgram
        # (or without the new file deployed) still runs Whisper.
        from stt_deepgram import CoraDeepgramSTT
        api_key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
        if not api_key:
            # Whisper wasn't loaded at startup (we skipped it because
            # provider=deepgram), so we can't silently fall back to it
            # mid-session — that would crash with 'NoneType' on every
            # frame. Surface a clear error and tell the operator what
            # to do. The systemd unit will keep retrying connections,
            # but each one logs this same line until the key is fixed.
            raise RuntimeError(
                "CORA_STT_PROVIDER=deepgram but DEEPGRAM_API_KEY is empty. "
                "Either set DEEPGRAM_API_KEY in ~/.config/cora/env and "
                "restart cora-voice, or set CORA_STT_PROVIDER=whisper "
                "(or unset it) for the local-Whisper backout."
            )
        logger.info(
            f"[stt] using Deepgram (model={CORA_DEEPGRAM_MODEL}, "
            f"language={CORA_DEEPGRAM_LANGUAGE})"
        )
        return CoraDeepgramSTT(
            api_key=api_key,
            model=CORA_DEEPGRAM_MODEL,
            language=CORA_DEEPGRAM_LANGUAGE,
        )
    # Default: local Whisper.
    if _WHISPER_MODEL is None:
        # Defensive — should only happen if CORA_STT_PROVIDER was set
        # to something we don't recognise (so Whisper was skipped at
        # module load) and then run_bot still asked for it.
        raise RuntimeError(
            "Whisper model wasn't loaded at startup. Set "
            "CORA_STT_PROVIDER=whisper (or unset it) and restart cora-voice."
        )
    return CoraWhisperSTT()


def _build_llm():
    """LLM provider factory (Phase 5 of cloud-stack migration).

    Reads `CORA_LLM_PROVIDER` at call time:
      openai     → OpenAILLMService (works with Ollama, vLLM, Groq,
                   OpenAI, OpenRouter — anything OpenAI-compatible).
      anthropic  → AnthropicLLMService (Claude Haiku / Sonnet / Opus
                   via the native Messages API).

    The pipeline downstream sees a uniform LLMService — frame shapes
    and tool-call routing are identical between providers, so the
    rest of run_bot() doesn't care which we return.

    Backout: set CORA_LLM_PROVIDER=openai (or unset) and restart.
    """
    if CORA_LLM_PROVIDER == "anthropic":
        # Lazy import: the anthropic package is only installed when
        # this provider is used. If it's missing we surface a clear
        # error rather than a generic ImportError.
        try:
            from pipecat.services.anthropic.llm import AnthropicLLMService
        except ImportError as e:
            raise RuntimeError(
                f"CORA_LLM_PROVIDER=anthropic but the anthropic package "
                f"isn't installed in the venv: {e}. Run "
                f"'/home/fpokrzywa/cora/venv/bin/pip install anthropic' "
                f"on Spark, then restart cora-voice. Or set "
                f"CORA_LLM_PROVIDER=openai (or unset) for the local-Ollama backout."
            ) from e
        if not CORA_LLM_API_KEY or CORA_LLM_API_KEY == "ollama":
            raise RuntimeError(
                "CORA_LLM_PROVIDER=anthropic but CORA_LLM_API_KEY isn't set "
                "(or is the default 'ollama'). Set CORA_LLM_API_KEY=<anthropic_key> "
                "in ~/.config/cora/env and restart cora-voice."
            )
        logger.info(
            f"[llm] using Anthropic Messages API (model={CORA_LLM_MODEL})"
        )
        return AnthropicLLMService(
            api_key=CORA_LLM_API_KEY,
            model=CORA_LLM_MODEL,
        )
    # Default: OpenAI-compatible (Ollama / vLLM / Groq / OpenAI / OpenRouter —
    # or cora-api's /v1 façade, which routes through the full Cora AI OS
    # pipeline: ATLAS routing, memory, governance, barge-in cancellation).
    #
    # CORA_LLM_SESSION_HEADERS=1 sends two extra headers the cora-api façade
    # understands (harmless elsewhere): a fresh X-Cora-Session-Id per WebRTC
    # connection (one Cora conversation per voice session) and
    # X-Cora-Speakable (short, markdown-free spoken replies).
    default_headers = None
    if _env_bool("CORA_LLM_SESSION_HEADERS", False):
        default_headers = {
            "X-Cora-Session-Id": str(uuid.uuid4()),
            "X-Cora-Speakable": _env("CORA_LLM_SPEAKABLE", "true"),
        }
    logger.info(
        f"[llm] using OpenAI-compatible endpoint "
        f"(base_url={CORA_LLM_BASE_URL}, model={CORA_LLM_MODEL}, "
        f"session_headers={'on' if default_headers else 'off'})"
    )
    return OpenAILLMService(
        api_key=CORA_LLM_API_KEY,
        base_url=CORA_LLM_BASE_URL,
        model=CORA_LLM_MODEL,
        default_headers=default_headers,
    )


def _build_tts(*, voice: str | None = None):
    """TTS provider factory (Phase 4 of cloud-stack migration).

    Reads `CORA_TTS_PROVIDER` at call time. Returns a fresh service
    per WebRTC connection to match Pipecat's lifecycle ownership.
    Both providers extend TTSService and emit the same frame
    sequence (TTSStartedFrame → TTSAudioRawFrame[…] → TTSStoppedFrame),
    so the pipeline downstream can't tell the difference.

    `voice` is the per-session override the browser passes in /api/offer.
    For kokoro it's a Kokoro voice id ('af_heart' etc.); for elevenlabs
    it's an ElevenLabs voice id. Passing None falls back to the env-
    configured default in each case.

    Backout: set CORA_TTS_PROVIDER=kokoro (or unset) and restart.
    """
    if CORA_TTS_PROVIDER == "elevenlabs":
        from tts_elevenlabs import CoraElevenLabsTTS
        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            # Kokoro wasn't loaded at startup (gated on provider==kokoro),
            # so don't silently fall back — that would crash on first use.
            # Match Phase 3's pattern: surface a clear error.
            raise RuntimeError(
                "CORA_TTS_PROVIDER=elevenlabs but ELEVENLABS_API_KEY is empty. "
                "Either set ELEVENLABS_API_KEY in ~/.config/cora/env and "
                "restart cora-voice, or set CORA_TTS_PROVIDER=kokoro "
                "(or unset it) for the local-Kokoro backout."
            )
        # The browser's voice picker passes Kokoro-format ids
        # ('af_heart', 'bf_isabella', etc.) — those return 404 when
        # sent to ElevenLabs as voice_ids. Ignore Kokoro-shaped
        # overrides and fall back to the env-configured EL voice.
        # ElevenLabs voice ids are 20-char base64-ish strings; Kokoro
        # ids match ^[ab][fm]_.
        if voice and re.match(r"^[abef][fm]_", voice):
            logger.info(
                f"[tts] ignoring Kokoro-format voice override {voice!r} "
                f"on ElevenLabs (use ELEVENLABS_VOICE_ID env var instead)"
            )
            voice_id = CORA_ELEVENLABS_VOICE_ID
        else:
            voice_id = voice or CORA_ELEVENLABS_VOICE_ID
        logger.info(
            f"[tts] using ElevenLabs (model={CORA_ELEVENLABS_MODEL_ID}, "
            f"voice={voice_id}, sr={CORA_ELEVENLABS_SAMPLE_RATE})"
        )
        return CoraElevenLabsTTS(
            api_key=api_key,
            voice_id=voice_id,
            model_id=CORA_ELEVENLABS_MODEL_ID,
            sample_rate=CORA_ELEVENLABS_SAMPLE_RATE,
        )
    # Default: local Kokoro.
    if _KOKORO_PIPE is None:
        raise RuntimeError(
            "Kokoro pipeline wasn't loaded at startup. Set "
            "CORA_TTS_PROVIDER=kokoro (or unset it) and restart cora-voice."
        )
    return CoraKokoroTTS(voice=voice)


async def run_bot(
    webrtc_connection,
    *,
    voice: str | None = None,
    lang_code: str | None = None,
):
    """Per-connection bot. Mounted by server.py as a background task.

    `voice` and `lang_code` (if provided by the offer's query string)
    override the server-default Kokoro voice for THIS session only.
    They don't persist across restarts — env vars are still the way
    to change the default. Picker UX writes the choice to localStorage
    on the browser and includes it in /api/offer's query params.
    """
    if voice:
        logger.info(f"[bot] session voice override: {voice!r} (lang_code={lang_code!r})")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_10ms_chunks=2,
        ),
    )

    stt = _build_stt()

    # ---- Tools the LLM can call -----------------------------------
    # Phase 2 of the voice build is "Cora can interact with the page."
    # First tool: open_card. Bigger tool surface (close_card, switch
    # view, create task, etc.) lands as follow-ups once the loop is
    # validated end-to-end.
    open_card_tool = FunctionSchema(
        name="open_card",
        description=(
            "Open a card on the user's screen so they can see its full "
            "details. Use this when the user explicitly asks to open, "
            "show, or pull up a card. Card slugs (c1, c2, …) are listed "
            "in the screen context above; use the slug exactly."
        ),
        properties={
            "slug": {
                "type": "string",
                "description": "The card's slug, e.g. 'c4' for the Helix pipeline-win card.",
            },
        },
        required=["slug"],
    )

    # Browser automation tools (Phase A) — Cora drives a real headed
    # Chromium on Freddie's box via the cora_v2 web app's /api/browser/*
    # endpoints. Each tool just sends a `cora_action` over the WebRTC data
    # channel; the browser-side handler (shell.js handleCoraAction) calls
    # the existing executeCoraAction dispatcher which POSTs to the right
    # endpoint. Freddie sees the headed Chromium window directly — that's
    # the visual feedback in voice mode (we can't render screenshots
    # verbally).
    browser_open_tool = FunctionSchema(
        name="browser_open",
        description=(
            "Open a URL in Cora's controlled headed Chromium. Use when "
            "Freddie asks to navigate to, open, or visit a website (e.g. "
            "'open arxiv.org', 'pull up GitHub'). The browser window is "
            "visible to him on screen."
        ),
        properties={
            "url": {"type": "string", "description": "Full URL including scheme, e.g. 'https://arxiv.org'."},
        },
        required=["url"],
    )
    browser_click_tool = FunctionSchema(
        name="browser_click",
        description=(
            "Click an element in Cora's controlled Chromium. Selector is "
            "a Playwright selector — prefer stable attributes like "
            "[name=q], [aria-label='Search'], or text=Login."
        ),
        properties={
            "selector": {"type": "string", "description": "Playwright selector, e.g. \"[name='q']\"."},
        },
        required=["selector"],
    )
    browser_type_tool = FunctionSchema(
        name="browser_type",
        description=(
            "Type text into an input in Cora's controlled Chromium. Set "
            "submit=true to press Enter after typing (search boxes, login "
            "forms)."
        ),
        properties={
            "selector": {"type": "string", "description": "Playwright selector for the input."},
            "text":     {"type": "string", "description": "Text to type."},
            "submit":   {"type": "boolean", "description": "Press Enter after typing. Default false."},
        },
        required=["selector", "text"],
    )
    browser_screenshot_tool = FunctionSchema(
        name="browser_screenshot",
        description=(
            "Take a fresh screenshot of Cora's controlled Chromium. Use "
            "when Freddie asks 'what's on the page' or after a click "
            "where the page may have changed."
        ),
        properties={},
        required=[],
    )
    browser_back_tool = FunctionSchema(
        name="browser_back",
        description="Go back one step in Cora's controlled Chromium history.",
        properties={},
        required=[],
    )
    browser_close_tool = FunctionSchema(
        name="browser_close",
        description=(
            "Close Cora's controlled Chromium and end the browser "
            "session. Cookies persist across closes; only use this when "
            "Freddie explicitly asks to close the browser."
        ),
        properties={},
        required=[],
    )

    # UI control tools — drive the cora_v2 web frontend the same way the
    # chat path does. Each one sends a `cora_action` over the data
    # channel; shell.js handleCoraAction forwards to executeCoraAction
    # which has the dispatch logic for all of these.
    open_panel_tool = FunctionSchema(
        name="open_panel",
        description=(
            "Open one of the side panels on Freddie's screen. Use when "
            "he asks to 'open the skills panel' or 'show activity'."
        ),
        properties={
            "panel": {
                "type": "string",
                "enum": ["skills", "activity"],
                "description": "Which panel to open.",
            },
        },
        required=["panel"],
    )
    toggle_panel_tool = FunctionSchema(
        name="toggle_panel",
        description=(
            "Toggle (or force open/closed) a side panel. Use when "
            "Freddie says 'close the skills panel' or 'hide activity'."
        ),
        properties={
            "panel": {
                "type": "string",
                "enum": ["skills", "activity"],
                "description": "Which panel.",
            },
            "open": {
                "type": "boolean",
                "description": "true=force open, false=force close, omit=toggle.",
            },
        },
        required=["panel"],
    )
    open_agent_tab_tool = FunctionSchema(
        name="open_agent_tab",
        description=(
            "Open one of the agent tabs at the top of the screen "
            "(CORA / ATLAS / SCRIBE / FORGE / PULSE / SIGNAL / CHRONOS). "
            "Use when Freddie asks 'show me PULSE' or 'open the SCRIBE tab'."
        ),
        properties={
            "agent": {
                "type": "string",
                "enum": ["cora", "atlas", "scribe", "forge", "pulse", "signal", "chronos"],
                "description": "Lowercase agent slug.",
            },
        },
        required=["agent"],
    )
    focus_skill_section_tool = FunctionSchema(
        name="focus_skill_section",
        description=(
            "Open the Skills panel and focus a category section. Use "
            "when Freddie asks 'show me my coding tools' or 'help me "
            "with research'."
        ),
        properties={
            "category": {
                "type": "string",
                "enum": ["communication", "research", "coding", "calendar", "memory"],
                "description": "Skill category slug.",
            },
        },
        required=["category"],
    )
    focus_activity_lane_tool = FunctionSchema(
        name="focus_activity_lane",
        description=(
            "Expand the Activity panel and focus a specific lane. Use "
            "when Freddie asks 'what's in my inbox' or 'show me Scout's tasks'."
        ),
        properties={
            "lane": {
                "type": "string",
                "enum": ["inbox_replies", "scout_tasks", "flux_tasks", "relay_drafts", "pulse_news"],
                "description": "Activity lane slug.",
            },
        },
        required=["lane"],
    )
    set_view_mode_tool = FunctionSchema(
        name="set_view_mode",
        description=(
            "Switch the cards/bubbles view. Use when Freddie says "
            "'switch to bubbles', 'show cards', or 'show both'."
        ),
        properties={
            "mode": {
                "type": "string",
                "enum": ["cards", "bubbles", "both"],
                "description": "View mode.",
            },
        },
        required=["mode"],
    )
    open_settings_tool = FunctionSchema(
        name="open_settings",
        description=(
            "Open the settings modal. Use when Freddie says 'open settings' "
            "or 'open the orb tuner'."
        ),
        properties={
            "target": {
                "type": "string",
                "enum": ["main", "orb"],
                "description": "Which settings panel. Default 'main'.",
            },
            "tab": {
                "type": "string",
                "description": "Optional tab slug (background / display / orb / voice / agents).",
            },
        },
        required=[],
    )
    close_settings_tool = FunctionSchema(
        name="close_settings",
        description=(
            "Close whichever settings modal is currently open. Use when "
            "Freddie asks to close, dismiss, or exit settings."
        ),
        properties={},
        required=[],
    )
    close_modal_tool = FunctionSchema(
        name="close_modal",
        description=(
            "Close any open card or bubble detail dialog. Use when "
            "Freddie asks to close the card or back out."
        ),
        properties={},
        required=[],
    )

    # SCRIBE memory tools (Phase 1 of Cora's three-tier memory).
    # Save = persist a memory; Recall = search the long-term store.
    # Recent memories (last 20 days) are auto-injected into context, so
    # only call scribe_recall when the recent block doesn't have it.
    scribe_save_tool = FunctionSchema(
        name="scribe_save",
        description=(
            "Save a memory to SCRIBE so you remember it across "
            "sessions. Use when Freddie tells you to remember "
            "something, or when you decide a fact / decision / "
            "preference is worth keeping."
        ),
        properties={
            "body":  {"type": "string", "description": "Memory body in plain text."},
            "title": {"type": "string", "description": "Optional one-line headline."},
            "kind":  {
                "type": "string",
                "enum": ["fact", "decision", "preference", "observation", "note"],
                "description": "Default 'note'.",
            },
            "tags":  {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lowercase short tags. Examples: 'helix', 'pulse', 'budget'.",
            },
            "importance": {
                "type": "integer",
                "description": "1-10. Higher = more central. Default 5.",
            },
        },
        required=["body"],
    )
    scribe_recall_tool = FunctionSchema(
        name="scribe_recall",
        description=(
            "Search SCRIBE memories. Use when Freddie asks 'what did "
            "we decide about X' or 'do you remember Y'. Recent memories "
            "(last 20 days) are already in your context — call this for "
            "older saves or when the recent block doesn't cover it."
        ),
        properties={
            "query": {"type": "string", "description": "Free-text search query."},
            "tag":   {"type": "string", "description": "Optional tag filter."},
            "limit": {"type": "integer", "description": "Max results. Default 10."},
        },
        required=["query"],
    )

    # SIGNAL (Gmail) tools — Phase 1: read + draft only, no send.
    signal_inbox_tool = FunctionSchema(
        name="signal_inbox",
        description=(
            "Fetch Gmail inbox threads. The Inbox block in your "
            "context already shows the top 5 unread; call this for "
            "more or to include read mail."
        ),
        properties={
            "limit":       {"type": "integer", "description": "1-25, default 10."},
            "unread_only": {"type": "boolean", "description": "Default false."},
        },
        required=[],
    )
    signal_read_thread_tool = FunctionSchema(
        name="signal_read_thread",
        description="Pull up a Gmail thread for full reading.",
        properties={
            "thread_id": {"type": "string", "description": "Gmail thread id."},
        },
        required=["thread_id"],
    )
    signal_draft_reply_tool = FunctionSchema(
        name="signal_draft_reply",
        description=(
            "Save a Gmail draft. Phase 1 only DRAFTS — never auto-sends. "
            "Freddie reviews and sends in Gmail himself."
        ),
        properties={
            "thread_id":   {"type": "string"},
            "to":          {"type": "string"},
            "subject":     {"type": "string"},
            "body":        {"type": "string"},
            "in_reply_to": {"type": "string"},
        },
        required=["to", "subject", "body"],
    )
    signal_search_tool = FunctionSchema(
        name="signal_search",
        description="Gmail search using the user's `q=` syntax.",
        properties={
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        required=["query"],
    )

    tools = ToolsSchema(standard_tools=[
        open_card_tool,
        browser_open_tool,
        browser_click_tool,
        browser_type_tool,
        browser_screenshot_tool,
        browser_back_tool,
        browser_close_tool,
        open_panel_tool,
        toggle_panel_tool,
        open_agent_tab_tool,
        focus_skill_section_tool,
        focus_activity_lane_tool,
        set_view_mode_tool,
        open_settings_tool,
        close_settings_tool,
        close_modal_tool,
        scribe_save_tool,
        scribe_recall_tool,
        signal_inbox_tool,
        signal_read_thread_tool,
        signal_draft_reply_tool,
        signal_search_tool,
    ])

    llm = _build_llm()
    # If the browser passed a voice override, use it; otherwise fall
    # back to the configured CORA_TTS_VOICE (the provider class
    # handles None → config-default itself).
    tts = _build_tts(voice=voice or None)

    # Pull the on-screen snapshot from cora_v2 once at session start.
    # Cards, activity items, and (for an authed caller, when we wire
    # that later) the user's task list — same content the chat side
    # already injects as a developer message.
    #
    # We combine the voice-tone instructions and the screen context
    # into a SINGLE system message rather than two. Qwen3 4B
    # (smaller models in general) tends to weight only the first
    # system message; concatenating ensures the screen state is
    # actually consumed instead of silently dropped.
    # Two parallel HTTP fetches at session start: the screen snapshot
    # AND the live cora_voice prompt config. The DB-driven prompt
    # (migrations 022/023) lets Freddie edit the system prompt and
    # add few-shot examples without redeploying voice; if it's empty
    # we fall back to the hard-coded SYSTEM_PROMPT.
    screen_ctx, voice_cfg = await asyncio.gather(
        fetch_screen_context(),
        fetch_voice_config(),
    )
    base_prompt = voice_cfg["system_prompt"] or SYSTEM_PROMPT
    examples_block = voice_cfg["examples_block"]
    logger.info(
        f"[ctx] using {len(screen_ctx)} chars of screen context, "
        f"{len(base_prompt)} chars of base prompt "
        f"(source={'db' if voice_cfg['system_prompt'] else 'fallback'}), "
        f"{len(examples_block)} chars of examples"
    )

    # Extra vocabulary harvested from the screen snapshot - proper nouns
    # that are currently on Freddie's screen get prioritised in cleanup
    # so misheard card titles ("Helics", "Ridge line") snap to the real
    # spelling before reaching the LLM. Capitalised words >= 4 chars; we
    # rely on the cleanup prompt to pick the right one based on context.
    _vocab_re = re.compile(r"\b[A-Z][a-zA-Z]{3,}\b")
    extra_vocab = list({m.group(0) for m in _vocab_re.finditer(screen_ctx)}) if screen_ctx else []
    if extra_vocab:
        logger.info(f"[cleanup] extra vocab from screen: {extra_vocab}")

    # Derive the Ollama base URL from CORA_LLM_BASE_URL (the OpenAI-style
    # endpoint, e.g. http://host:11434/v1) by stripping the /v1 suffix.
    # CORA_CLEANUP_BASE_URL overrides the derivation — required when
    # CORA_LLM_BASE_URL points at the cora-api façade, so the cleanup pass
    # stays on the fast local Ollama instead of going through the Cora
    # pipeline (which would pollute conversations with cleanup prompts).
    _ollama_base = _env(
        "CORA_CLEANUP_BASE_URL",
        CORA_LLM_BASE_URL.rsplit("/v1", 1)[0] if CORA_LLM_BASE_URL.endswith("/v1") else CORA_LLM_BASE_URL,
    )
    cleanup = TranscriptCleanup(
        ollama_url=_ollama_base,
        model=CORA_CLEANUP_MODEL,
        extra_vocab=extra_vocab,
    ) if CORA_CLEANUP_ENABLED else None
    # Build the static front-matter once: base prompt + examples block.
    # Both come from the DB if available, otherwise from the hard-coded
    # constants that ship with the file.
    static_front = base_prompt
    if examples_block:
        static_front = static_front + "\n\n" + examples_block

    if screen_ctx:
        # Trimmed grounding (5/9/2026 latency pass) — qwen3:4b's
        # first-token time scales with prompt size; the previous
        # ~2K-token grounding block was the single biggest tax.
        # Kept the load-bearing rules; dropped the verbose examples
        # and the headline ASCII banners.
        combined_system = (
            static_front
            + "\n\n## SCREEN DATA (the ONLY source of truth)\n"
            + "Anything not listed below does not exist. Don't invent slugs, "
            + "titles, statuses, dates, names, or numbers. If Freddie asks "
            + "about something not here, say \"I don't see that on your screen.\"\n"
            + "Refer to items by their human title, not by slug/eyebrow/status. "
            + "Talk like a person summarising for a person.\n"
            + "For broad questions give a 2-3 sentence overview that groups "
            + "by category. For specific items, one or two sentences max.\n"
            + "\n"
            + screen_ctx
            + "\n## END SCREEN DATA"
        )
    else:
        combined_system = static_front

    # Tools must be set on the LLMContext (not on the service constructor)
    # for Pipecat 1.1.0's OpenAILLMService to actually pass them in each
    # /v1/chat/completions request. Verified via direct curl that
    # cora-qwen3:4b emits proper tool_calls when tools are present in
    # the request body.
    context = LLMContext(
        [{"role": "system", "content": combined_system}],
        tools=tools,
    )
    # Belt-and-suspenders — also call the explicit setter, in case the
    # constructor kwarg is silently dropped by some aggregator wrap.
    try:
        context.set_tools(tools)
    except Exception:
        logger.exception("[tools] context.set_tools failed; relying on ctor arg")
    # Diagnostic: log what the context thinks it has after construction.
    try:
        ctx_tools = getattr(context, "tools", None)
        names = []
        if ctx_tools is not None and hasattr(ctx_tools, "standard_tools"):
            for t in (ctx_tools.standard_tools or []):
                names.append(getattr(t, "_name", getattr(t, "name", repr(t))))
        logger.info(f"[tools] LLMContext tools after construction: {names!r}")
    except Exception:
        logger.exception("[tools] introspection failed")
    # VAD tuning — default `stop_secs` is ~0.8s which makes the loop
    # feel sluggish (the user stops talking, Pipecat waits 800ms to be
    # sure, THEN runs STT). Tightened to 0.4s — snappier turnaround at
    # the cost of cutting off long mid-sentence pauses. start_secs is
    # the onset threshold; default 0.2s is fine.
    _vad_params = VADParams(
        confidence=0.7,
        start_secs=0.2,
        stop_secs=0.4,
        min_volume=0.6,
    )
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=_vad_params),
        ),
    )

    # Build the pipeline — cleanup is optional (CORA_CLEANUP_ENABLED=0
    # drops it; useful for debugging or if a future model doesn't need it).
    stages = [transport.input(), stt]
    if cleanup is not None:
        stages.append(cleanup)
    stages.extend([user_agg, llm, tts, transport.output(), assistant_agg])
    pipeline = Pipeline(stages)

    # Tool handlers — registered AFTER the pipeline is built so the
    # transport's output is wired and can send data-channel messages.
    # Each handler is invoked when the LLM emits a matching tool_call;
    # we forward the action to the browser via the WebRTC data channel
    # and ack to the LLM so it can continue speaking.
    async def handle_open_card(params):
        slug = (params.arguments or {}).get("slug", "").strip()
        if not slug:
            await params.result_callback({"ok": False, "error": "missing slug"})
            return
        logger.info(f"[tool] open_card slug={slug!r}")
        try:
            await transport.output().send_message(
                OutputTransportMessageFrame(
                    message={
                        "type": "cora_action",
                        "action": "open_card",
                        "slug": slug,
                    }
                )
            )
            await params.result_callback({"ok": True, "slug": slug})
        except Exception as e:
            logger.exception("[tool] open_card send failed")
            await params.result_callback({"ok": False, "error": str(e)})

    llm.register_function("open_card", handle_open_card)

    # browser_* handlers: each one ships a `cora_action` message over
    # the data channel. shell.js handleCoraAction forwards it to
    # executeCoraAction which POSTs the right /api/browser/* endpoint.
    # We ack the LLM immediately ({ok:true}) so it can keep speaking;
    # the actual Chromium work runs asynchronously on Freddie's box.
    async def _send_browser_action(action: str, **fields):
        try:
            payload = {"type": "cora_action", "action": action, **fields}
            await transport.output().send_message(
                OutputTransportMessageFrame(message=payload)
            )
            return {"ok": True}
        except Exception as e:
            logger.exception(f"[tool] {action} send failed")
            return {"ok": False, "error": str(e)}

    async def handle_browser_open(params):
        url = (params.arguments or {}).get("url", "").strip()
        if not url:
            await params.result_callback({"ok": False, "error": "missing url"})
            return
        logger.info(f"[tool] browser_open url={url!r}")
        await params.result_callback(await _send_browser_action("browser_open", url=url))

    async def handle_browser_click(params):
        sel = (params.arguments or {}).get("selector", "").strip()
        if not sel:
            await params.result_callback({"ok": False, "error": "missing selector"})
            return
        logger.info(f"[tool] browser_click selector={sel!r}")
        await params.result_callback(await _send_browser_action("browser_click", selector=sel))

    async def handle_browser_type(params):
        args = params.arguments or {}
        sel = (args.get("selector") or "").strip()
        text = args.get("text") or ""
        submit = bool(args.get("submit", False))
        if not sel:
            await params.result_callback({"ok": False, "error": "missing selector"})
            return
        logger.info(f"[tool] browser_type selector={sel!r} submit={submit}")
        await params.result_callback(await _send_browser_action(
            "browser_type", selector=sel, text=text, submit=submit,
        ))

    async def handle_browser_screenshot(params):
        logger.info("[tool] browser_screenshot")
        await params.result_callback(await _send_browser_action("browser_screenshot"))

    async def handle_browser_back(params):
        logger.info("[tool] browser_back")
        await params.result_callback(await _send_browser_action("browser_back"))

    async def handle_browser_close(params):
        logger.info("[tool] browser_close")
        await params.result_callback(await _send_browser_action("browser_close"))

    llm.register_function("browser_open",       handle_browser_open)
    llm.register_function("browser_click",      handle_browser_click)
    llm.register_function("browser_type",       handle_browser_type)
    llm.register_function("browser_screenshot", handle_browser_screenshot)
    llm.register_function("browser_back",       handle_browser_back)
    llm.register_function("browser_close",      handle_browser_close)

    # UI control handlers — same forward-via-data-channel pattern; the
    # browser-side coraExecuteAction handles each action's dispatch.
    async def handle_open_panel(params):
        panel = (params.arguments or {}).get("panel", "").strip()
        if panel not in ("skills", "activity"):
            await params.result_callback({"ok": False, "error": "invalid panel"})
            return
        logger.info(f"[tool] open_panel panel={panel!r}")
        await params.result_callback(await _send_browser_action("open_panel", panel=panel))

    async def handle_toggle_panel(params):
        args = params.arguments or {}
        panel = (args.get("panel") or "").strip()
        if panel not in ("skills", "activity"):
            await params.result_callback({"ok": False, "error": "invalid panel"})
            return
        fields = {"panel": panel}
        if "open" in args:
            fields["open"] = bool(args["open"])
        logger.info(f"[tool] toggle_panel {fields}")
        await params.result_callback(await _send_browser_action("toggle_panel", **fields))

    async def handle_open_agent_tab(params):
        agent = (params.arguments or {}).get("agent", "").strip().lower()
        if not agent:
            await params.result_callback({"ok": False, "error": "missing agent"})
            return
        logger.info(f"[tool] open_agent_tab agent={agent!r}")
        await params.result_callback(await _send_browser_action("open_agent_tab", agent=agent))

    async def handle_focus_skill_section(params):
        category = (params.arguments or {}).get("category", "").strip()
        if not category:
            await params.result_callback({"ok": False, "error": "missing category"})
            return
        logger.info(f"[tool] focus_skill_section category={category!r}")
        await params.result_callback(await _send_browser_action("focus_skill_section", category=category))

    async def handle_focus_activity_lane(params):
        lane = (params.arguments or {}).get("lane", "").strip()
        if not lane:
            await params.result_callback({"ok": False, "error": "missing lane"})
            return
        logger.info(f"[tool] focus_activity_lane lane={lane!r}")
        await params.result_callback(await _send_browser_action("focus_activity_lane", lane=lane))

    async def handle_set_view_mode(params):
        mode = (params.arguments or {}).get("mode", "").strip()
        if mode not in ("cards", "bubbles", "both"):
            await params.result_callback({"ok": False, "error": "invalid mode"})
            return
        logger.info(f"[tool] set_view_mode mode={mode!r}")
        await params.result_callback(await _send_browser_action("set_view_mode", mode=mode))

    async def handle_open_settings(params):
        args = params.arguments or {}
        fields = {}
        if args.get("target"):
            fields["target"] = args["target"]
        if args.get("tab"):
            fields["tab"] = args["tab"]
        logger.info(f"[tool] open_settings {fields}")
        await params.result_callback(await _send_browser_action("open_settings", **fields))

    async def handle_close_settings(params):
        logger.info("[tool] close_settings")
        await params.result_callback(await _send_browser_action("close_settings"))

    async def handle_close_modal(params):
        logger.info("[tool] close_modal")
        await params.result_callback(await _send_browser_action("close_modal"))

    async def handle_scribe_save(params):
        args = params.arguments or {}
        body = (args.get("body") or "").strip()
        if not body:
            await params.result_callback({"ok": False, "error": "missing body"})
            return
        fields: dict[str, Any] = {"body": body}
        for k in ("title", "kind", "tags", "importance"):
            if k in args and args[k] not in (None, "", []):
                fields[k] = args[k]
        logger.info(f"[tool] scribe_save kind={fields.get('kind','note')!r}")
        await params.result_callback(await _send_browser_action("scribe_save", **fields))

    async def handle_scribe_recall(params):
        args = params.arguments or {}
        query = (args.get("query") or "").strip()
        if not query:
            await params.result_callback({"ok": False, "error": "missing query"})
            return
        fields: dict[str, Any] = {"query": query}
        if args.get("tag"):
            fields["tag"] = args["tag"]
        if args.get("limit"):
            fields["limit"] = int(args["limit"])
        logger.info(f"[tool] scribe_recall query={query!r}")
        await params.result_callback(await _send_browser_action("scribe_recall", **fields))

    llm.register_function("open_panel",          handle_open_panel)
    llm.register_function("toggle_panel",        handle_toggle_panel)
    llm.register_function("open_agent_tab",      handle_open_agent_tab)
    llm.register_function("focus_skill_section", handle_focus_skill_section)
    llm.register_function("focus_activity_lane", handle_focus_activity_lane)
    llm.register_function("set_view_mode",       handle_set_view_mode)
    llm.register_function("open_settings",       handle_open_settings)
    llm.register_function("close_settings",      handle_close_settings)
    llm.register_function("close_modal",         handle_close_modal)
    llm.register_function("scribe_save",         handle_scribe_save)
    llm.register_function("scribe_recall",       handle_scribe_recall)

    # SIGNAL (Gmail) handlers — fire-and-forget over the data channel,
    # frontend's executeCoraAction makes the actual REST call.
    async def handle_signal_inbox(params):
        args = params.arguments or {}
        fields: dict[str, Any] = {}
        if "limit" in args: fields["limit"] = int(args["limit"])
        if "unread_only" in args: fields["unread_only"] = bool(args["unread_only"])
        logger.info(f"[tool] signal_inbox {fields}")
        await params.result_callback(await _send_browser_action("signal_inbox", **fields))

    async def handle_signal_read_thread(params):
        tid = (params.arguments or {}).get("thread_id", "").strip()
        if not tid:
            await params.result_callback({"ok": False, "error": "missing thread_id"})
            return
        logger.info(f"[tool] signal_read_thread {tid}")
        await params.result_callback(await _send_browser_action("signal_read_thread", thread_id=tid))

    async def handle_signal_draft_reply(params):
        args = params.arguments or {}
        if not args.get("to") or "body" not in args:
            await params.result_callback({"ok": False, "error": "missing to or body"})
            return
        fields: dict[str, Any] = {
            "to":      args["to"],
            "subject": args.get("subject", ""),
            "body":    args.get("body", ""),
        }
        for k in ("thread_id", "in_reply_to"):
            if args.get(k): fields[k] = args[k]
        logger.info(f"[tool] signal_draft_reply to={fields['to']}")
        await params.result_callback(await _send_browser_action("signal_draft_reply", **fields))

    async def handle_signal_search(params):
        args = params.arguments or {}
        q = (args.get("query") or "").strip()
        if not q:
            await params.result_callback({"ok": False, "error": "missing query"})
            return
        fields: dict[str, Any] = {"query": q}
        if "limit" in args: fields["limit"] = int(args["limit"])
        logger.info(f"[tool] signal_search {q!r}")
        await params.result_callback(await _send_browser_action("signal_search", **fields))

    llm.register_function("signal_inbox",        handle_signal_inbox)
    llm.register_function("signal_read_thread",  handle_signal_read_thread)
    llm.register_function("signal_draft_reply",  handle_signal_draft_reply)
    llm.register_function("signal_search",       handle_signal_search)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            # Enable barge-in: when the VAD detects user speech while
            # Cora is mid-TTS, Pipecat cancels the in-flight TTS and the
            # frame-aggregator starts a fresh user turn. Without this,
            # Cora monologues to completion no matter what the user does.
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(transport, client):
        logger.info("[bot] client connected")

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(transport, client):
        logger.info("[bot] client disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
