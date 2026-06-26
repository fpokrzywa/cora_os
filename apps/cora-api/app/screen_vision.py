"""Tier-2 Screen Vision (opt-in) — analyze a screenshot the user deliberately shares.

Builds on Screen Context Awareness v0.1 (which is screenshot-FREE Tier-1 structured
context). Tier-2 adds an OPT-IN visual path: when the user clicks "Share screen" in
the chat composer, the browser's getDisplayMedia picker grabs a SINGLE frame (the
track is stopped immediately — no continuous capture, no auto-capture) and attaches
it to their next message. The backend forwards that frame to a LOCAL vision model on
the DGX Spark (Ollama `/api/generate` with the `images` param) and returns the answer.

Fail-closed, like every sensitive capability here. The master gate is the DEDICATED
`SCREEN_VISION_ENABLED` switch (NOT the global external_execution kill switch — this
path makes no third-party call: the image goes only to the self-hosted DGX on the
internal network). The gate also requires a configured vision model + DGX endpoint.
With the switch off (the production default) nothing is sent to any model; the user
gets a clear, audited denial.

Privacy: the screenshot bytes are NEVER persisted, logged, or traced — only metadata
(decision, model, byte count, a short question preview, latency) lands in
`screen_vision_events`. The image lives only in memory for the single model call.
"""

import base64
import binascii
import logging
import time
from typing import Optional

import httpx

from app import runtime_switches
from app.clients import clients
from app.config import settings
from app.runtime_traces import write_trace

logger = logging.getLogger(__name__)

PERSONA = "Cora"
VISION_TIMEOUT_SECONDS = 90.0
# Cap the model context so the KV cache fits the DGX memory budget — the model's
# default context is very large (it OOMs an 8B VL model at ~45 GiB); 8192 tokens is
# ample for one screenshot + a short question/answer and loads in a few seconds.
VISION_NUM_CTX = 8192
# Decoded-image ceiling. A single screen frame as JPEG is well under this; anything
# larger is refused before any model call (defensive — also bounds DGX memory).
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_ALLOWED_MIME = ("image/png", "image/jpeg", "image/webp")

TRACE_REQUESTED = "screen_vision_requested"
TRACE_ANALYZED = "screen_vision_analyzed"
TRACE_DENIED = "screen_vision_denied"
TRACE_FAILED = "screen_vision_failed"

_PROMPT = (
    "You are Cora's screen-vision helper. The user shared a single screenshot of their "
    "screen and asked a question about it. Answer ONLY from what is actually visible in "
    "the image. Be concise. If the answer is not visible, say so plainly — do not guess "
    "or invent UI that is not shown.\n\nUser question: {q}"
)


async def _gate() -> dict:
    """Fail-closed screen-vision decision: dedicated master switch + a configured
    vision model + a configured DGX endpoint. Returns {allowed, reason}. The master
    switch reads the admin DB override (app-toggleable) if set, else SCREEN_VISION_ENABLED."""
    reasons = []
    if not await runtime_switches.effective("screen_vision_enabled"):
        reasons.append("SCREEN_VISION_ENABLED is OFF (master gate)")
    if not settings.vision_model_name:
        reasons.append("no vision model configured (VISION_MODEL_NAME unset)")
    if not settings.dgx_model_endpoint:
        reasons.append("DGX endpoint not configured")
    return {"allowed": not reasons, "reason": "; ".join(reasons) or "all checks pass"}


def _decode_image(image_data: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """Validate + normalize the client image. Returns (clean_b64, byte_count, error).
    Accepts a data URL or bare base64; enforces the MIME allow-list + the size cap.
    The returned base64 is what Ollama's `images` param expects (no data-URL prefix)."""
    raw = (image_data or "").strip()
    if not raw:
        return None, None, "no image attached"
    if raw.startswith("data:"):
        header, _, b64 = raw.partition(",")
        if not b64:
            return None, None, "malformed data URL"
        mime = header[5:].split(";", 1)[0] or "image/(unknown)"
        if mime not in _ALLOWED_MIME:
            return None, None, f"unsupported image type ({mime})"
        raw = b64.strip()
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None, None, "image is not valid base64"
    n = len(decoded)
    if n == 0:
        return None, None, "empty image"
    if n > MAX_IMAGE_BYTES:
        return None, None, f"image too large ({n} bytes > {MAX_IMAGE_BYTES} cap)"
    return raw, n, None


async def _analyze(image_b64: str, question: str) -> str:
    prompt = _PROMPT.format(q=(question or "What am I looking at?").strip()[:1000])
    async with httpx.AsyncClient(timeout=VISION_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{settings.dgx_model_endpoint.rstrip('/')}/api/generate",
            json={"model": settings.vision_model_name, "prompt": prompt,
                  "images": [image_b64], "stream": False,
                  "options": {"num_ctx": VISION_NUM_CTX}})
        resp.raise_for_status()
        return (resp.json().get("response") or "").strip()


async def _audit(user_id, workspace_id, *, allowed, reason, model, image_bytes,
                 question, latency_ms):
    pool = clients.db_pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO screen_vision_events (user_id, workspace_id, allowed, reason, "
            "model, image_bytes, question_preview, latency_ms) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            user_id, workspace_id, allowed, (reason or "")[:300], model or None,
            image_bytes, (question or "")[:200], latency_ms)


async def _trace(session_id, user_id, workspace_id, *, trace_type, status="ok", result=None):
    await write_trace(
        session_id=session_id, user_id=user_id, trace_type=trace_type, status=status,
        selected_agent=PERSONA, tool_name="screen_vision", tool_result=result or {},
        workspace_id=workspace_id)


async def handle_screen_vision_turn(*, message, image_data, session_uuid, user_id,
                                    workspace_uuid, is_admin):
    """Entry for a screen-vision turn (the request carried a shared screenshot).
    Returns (handled, text). Fail-closed + fully audited; image bytes never stored."""
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_REQUESTED,
                 result={"has_image": bool(image_data)})

    decision = await _gate()
    if not decision["allowed"]:
        await _audit(user_id, workspace_uuid, allowed=False, reason=decision["reason"],
                     model=settings.vision_model_name, image_bytes=None,
                     question=message, latency_ms=None)
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_DENIED,
                     status="blocked", result={"reason": decision["reason"]})
        return True, (
            "**Screen vision is off.** I did not send your screenshot anywhere.\n\n"
            f"Reason: {decision['reason']}.\n\n"
            "This is a fail-closed, opt-in capability: an operator must enable "
            "`SCREEN_VISION_ENABLED` and configure a local vision model "
            "(`VISION_MODEL_NAME`, e.g. `qwen2.5-vl`) on the DGX Spark. Until then I can "
            "still answer from on-screen records via screen context — just ask."
        )

    image_b64, n, err = _decode_image(image_data)
    if err:
        await _audit(user_id, workspace_uuid, allowed=False, reason=err,
                     model=settings.vision_model_name, image_bytes=n,
                     question=message, latency_ms=None)
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_DENIED,
                     status="blocked", result={"reason": err})
        return True, f"I could not read that screenshot: {err}."

    started = time.monotonic()
    try:
        answer = await _analyze(image_b64, message)
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        logger.warning("screen vision model call failed: %s", exc)
        await _audit(user_id, workspace_uuid, allowed=True, reason=f"model error: {exc}",
                     model=settings.vision_model_name, image_bytes=n,
                     question=message, latency_ms=latency)
        await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_FAILED,
                     status="error", result={"error": str(exc)[:200]})
        return True, ("I could not analyze the screenshot — the vision model on the DGX "
                      "Spark did not respond. Your image was not stored.")

    latency = int((time.monotonic() - started) * 1000)
    await _audit(user_id, workspace_uuid, allowed=True, reason="analyzed",
                 model=settings.vision_model_name, image_bytes=n,
                 question=message, latency_ms=latency)
    await _trace(session_uuid, user_id, workspace_uuid, trace_type=TRACE_ANALYZED,
                 result={"image_bytes": n, "latency_ms": latency,
                         "answer_chars": len(answer)})
    if not answer:
        return True, "The vision model returned an empty response. Your image was not stored."
    return True, answer
