"""Backend-selectable chat-model client.

Cora's chat + fact-extraction can talk to either:
  - "ollama"  -> the DGX Ollama native API (POST /api/generate, prompt string), or
  - "openai"  -> an OpenAI-compatible server such as vLLM serving gpt-oss-120b
                 (POST /v1/chat/completions, messages).

Selected by settings.dgx_chat_backend; default "ollama" so nothing changes until it
is flipped via .env (DGX_CHAT_BACKEND=openai). Embeddings, vision, and the
agent-runtime tool-calling loop are NOT routed here — they stay on Ollama.
"""
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def chat_backend() -> str:
    return (settings.dgx_chat_backend or "ollama").strip().lower()


def active_chat_model() -> str:
    """The chat model id for the active backend (for traces/logs)."""
    if chat_backend() == "openai":
        return settings.dgx_openai_model or ""
    return settings.dgx_chat_model_name or settings.dgx_model_name or ""


def active_chat_endpoint() -> str:
    """The chat endpoint for the active backend (for traces/logs)."""
    if chat_backend() == "openai":
        return settings.dgx_openai_endpoint or ""
    return settings.dgx_model_endpoint or ""


def is_chat_configured() -> bool:
    return bool(active_chat_endpoint() and active_chat_model())


async def generate_text(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
    timeout: float = 120.0,
) -> str:
    """Single-shot text generation against the active backend. Returns the model's
    text (stripped). Raises httpx.HTTPError on a transport/HTTP failure so callers
    can keep their existing `except httpx.HTTPError` handling. A malformed but 2xx
    body yields "" rather than raising.

    For "openai", the whole `prompt` is sent as one user message (validated to give
    the same quality as the Ollama prompt-string form); an optional `system` becomes
    a system message.
    """
    if chat_backend() == "openai":
        base = (settings.dgx_openai_endpoint or "").rstrip("/")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        headers = {"Content-Type": "application/json"}
        if settings.dgx_openai_api_key:
            headers["Authorization"] = f"Bearer {settings.dgx_openai_api_key}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                json={
                    "model": settings.dgx_openai_model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        try:
            return (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError):
            logger.warning("openai chat response missing choices/content")
            return ""

    # default backend: ollama native /api/generate
    base = (settings.dgx_model_endpoint or "").rstrip("/")
    full = f"{system}\n\n{prompt}" if system else prompt
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base}/api/generate",
            json={
                "model": settings.dgx_model_name,
                "prompt": full,
                "stream": False,
                "keep_alive": settings.dgx_keep_alive,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return (data.get("response") or "").strip()
