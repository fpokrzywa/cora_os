"""OpenAI-compatible chat façade: POST /v1/chat/completions.

Lets any OpenAI-compatible client — first consumer: the DGX Pipecat voice
service (`OpenAILLMService` pointed at this base_url) — talk to the REAL
Cora chat pipeline (ATLAS routing, memory recall, governance, traces,
streaming, mid-stream cancellation) instead of a bare model endpoint.

Mapping:
  - auth: the normal cora-api JWT, sent by OpenAI clients as the Bearer
    api_key. No new auth surface.
  - the LAST user message in `messages` becomes ChatRequest.message; the
    rest of the client-side transcript is ignored — cora-api owns the
    conversation (sessions, memory) server-side.
  - session affinity: `X-Cora-Session-Id` header (the voice service sends
    one uuid per WebRTC connection). Absent that, calls reuse a rolling
    per-user session with a 15-minute idle TTL.
  - `X-Cora-Speakable: true` → ChatRequest.speakable (TTS-clean replies).
  - `tools` in the request body are ignored (the pipeline has its own
    governed tools); no tool_calls are ever emitted.

Streaming responses re-encode the internal meta/delta/done/error SSE
frames as OpenAI chat.completion.chunk frames; a client disconnect
propagates to the inner stream, which finalizes the turn as `cancelled`
(the voice barge-in path). stream=false aggregates the same stream into
one chat.completion object.
"""

import json
import logging
import time
import uuid
from typing import Annotated, Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth import CurrentUser, get_current_user
from app.routers.chat import ChatRequest, ChatResponse, chat
from app.speakable import to_speakable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compat"])

# Session affinity: (user_id, client_key) → [cora session_id, last_used_ts].
# In-process is fine — cora-api runs single-replica, and losing the map on
# restart only means a voice turn starts a fresh conversation.
_SESSIONS: dict[tuple[str, str], list] = {}
_HEADER_TTL_S = 2 * 60 * 60  # keyed by the client's own session id
_DEFAULT_TTL_S = 15 * 60     # rolling per-user session when no header sent
_ROLE_CHUNK_SENT = "role"


class ChatCompletionsIn(BaseModel):
    model: str = "cora"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    # Accepted-and-ignored OpenAI fields (tools, temperature, etc.) are
    # tolerated via extra="allow" so strict clients don't 422.
    model_config = {"extra": "allow"}


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # OpenAI content-part arrays: join the text parts.
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(t for t in parts if t)
    return ""


def _session_for(user_id: str, header_session: Optional[str]) -> tuple[tuple[str, str], Optional[str]]:
    """Return (map key, existing cora session_id or None), pruning stale rows."""
    now = time.time()
    for k in [k for k, v in _SESSIONS.items() if now - v[1] > _HEADER_TTL_S]:
        _SESSIONS.pop(k, None)
    key = (user_id, header_session or "default")
    row = _SESSIONS.get(key)
    if row is None:
        return key, None
    ttl = _HEADER_TTL_S if header_session else _DEFAULT_TTL_S
    if now - row[1] > ttl:
        _SESSIONS.pop(key, None)
        return key, None
    row[1] = now
    return key, row[0]


def _chunk(model: str, chunk_id: str, delta: dict[str, Any], finish: Optional[str] = None) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


async def _frames(body: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    """Parse the internal SSE byte stream into meta/delta/done/error events."""
    buf = ""
    async for raw in body:
        buf += raw.decode("utf-8", errors="replace")
        while True:
            sep = buf.find("\n\n")
            if sep == -1:
                break
            frame, buf = buf[:sep], buf[sep + 2:]
            for line in frame.split("\n"):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    yield json.loads(data)
                except ValueError:
                    continue


@router.post("/chat/completions", summary="OpenAI-compatible chat over the Cora pipeline")
async def chat_completions(
    payload: ChatCompletionsIn,
    current: Annotated[CurrentUser, Depends(get_current_user)],
    x_cora_session_id: Annotated[Optional[str], Header()] = None,
    x_cora_speakable: Annotated[Optional[str], Header()] = None,
):
    message = _last_user_message(payload.messages)
    if not message.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="messages must contain a non-empty user message",
        )
    speakable = (x_cora_speakable or "").strip().lower() in ("1", "true", "yes")
    key, session_id = _session_for(str(current.id), x_cora_session_id)

    inner = await chat(
        ChatRequest(
            message=message,
            session_id=session_id,
            stream=True,
            speakable=speakable,
        ),
        current,
    )
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    model = payload.model or "cora"

    def _remember(sid: Optional[str]) -> None:
        if sid:
            _SESSIONS[key] = [sid, time.time()]

    # Deterministic handlers (calendar, inbox, briefing, memory commands, …)
    # short-circuit the pipeline and return a plain ChatResponse even when
    # stream=true. Emit it as a one-shot completion so OpenAI clients — and
    # the voice pipeline — never see a non-streamed body.
    if isinstance(inner, ChatResponse):
        _remember(inner.session_id)
        text = to_speakable(inner.response) if speakable else inner.response
        if payload.stream:
            async def one_shot() -> AsyncIterator[bytes]:
                yield _chunk(model, chunk_id, {"role": "assistant"})
                if text:
                    yield _chunk(model, chunk_id, {"content": text})
                yield _chunk(model, chunk_id, {}, finish="stop")
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                one_shot(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return {
            "id": chunk_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    if not isinstance(inner, StreamingResponse):  # defensive
        raise HTTPException(status_code=500, detail="chat pipeline did not stream")

    if payload.stream:
        async def translate() -> AsyncIterator[bytes]:
            sent_role = False
            streamed = ""
            try:
                async for evt in _frames(inner.body_iterator):
                    etype = evt.get("type")
                    if etype in ("meta", "done"):
                        _remember(evt.get("session_id"))
                    if etype == "delta":
                        text = evt.get("text") or ""
                        if not text:
                            continue
                        if not sent_role:
                            sent_role = True
                            yield _chunk(model, chunk_id, {"role": "assistant"})
                        streamed += text
                        yield _chunk(model, chunk_id, {"content": text})
                    elif etype == "done":
                        full = evt.get("response") or ""
                        # The authoritative reply can extend the deltas
                        # (draft/proposal suffixes) — emit the tail.
                        if full.startswith(streamed) and len(full) > len(streamed):
                            if not sent_role:
                                sent_role = True
                                yield _chunk(model, chunk_id, {"role": "assistant"})
                            yield _chunk(model, chunk_id, {"content": full[len(streamed):]})
                        break
                    elif etype == "error":
                        logger.warning(
                            "openai-compat: upstream error for user=%s: %s",
                            current.id, evt.get("detail"),
                        )
                        break
            finally:
                # Always complete the OpenAI framing; a barge-in disconnect
                # cancels this generator and the inner stream finalizes the
                # turn as cancelled on its own.
                yield _chunk(model, chunk_id, {}, finish="stop")
                yield b"data: [DONE]\n\n"

        return StreamingResponse(
            translate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming: aggregate the same internal stream into one completion.
    text = ""
    async for evt in _frames(inner.body_iterator):
        etype = evt.get("type")
        if etype in ("meta", "done"):
            _remember(evt.get("session_id"))
        if etype == "delta":
            text += evt.get("text") or ""
        elif etype == "done":
            text = evt.get("response") or text
            break
        elif etype == "error":
            raise HTTPException(status_code=502, detail=evt.get("detail") or "upstream error")
    return {
        "id": chunk_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
