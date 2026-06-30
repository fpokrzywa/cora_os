"""Deterministic check of the /chat SSE streaming path. NO live model, NO egress:
httpx.AsyncClient is monkeypatched with a fake streaming response, and app.llm's
settings are swapped for a stand-in. Covers llm.stream_text delta parsing for both
backends, the fail-closed error path, the SSE frame encoder, and the ChatRequest
opt-in field.

    docker cp apps/cora-api/scripts/verify_chat_streaming.py cora-api:/tmp/vs.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vs.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys
from types import SimpleNamespace

import httpx

from app import llm
from app.routers.chat import ChatRequest, _sse_event


class _FakeResp:
    def __init__(self, lines: list[str], status: int = 200):
        self._lines = lines
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://dgx/x"),
                response=httpx.Response(self.status_code),
            )

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self) -> _FakeResp:
        return self._resp

    async def __aexit__(self, *a) -> bool:
        return False


class _FakeClient:
    resp: _FakeResp = _FakeResp([])
    last_url: str = ""
    last_json: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    def stream(self, method, url, **kw):
        _FakeClient.last_url = url
        _FakeClient.last_json = kw.get("json") or {}
        return _FakeStreamCtx(_FakeClient.resp)


async def _collect(agen) -> list[str]:
    out: list[str] = []
    async for d in agen:
        out.append(d)
    return out


async def main() -> int:
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    fake_settings = SimpleNamespace(
        dgx_chat_backend="openai",
        dgx_openai_endpoint="http://dgx:8000/v1",
        dgx_openai_api_key=None,
        dgx_openai_model="gpt-oss-120b",
        dgx_model_endpoint="http://dgx:11434",
        dgx_model_name="qwen",
        dgx_keep_alive="30m",
    )
    orig_client, orig_settings = httpx.AsyncClient, llm.settings
    httpx.AsyncClient = _FakeClient  # type: ignore[misc,assignment]
    llm.settings = fake_settings  # type: ignore[assignment]
    try:
        # --- openai backend: parse choices[0].delta.content, skip empty, stop on [DONE]
        fake_settings.dgx_chat_backend = "openai"
        _FakeClient.resp = _FakeResp(
            [
                'data: {"choices":[{"delta":{"content":"Hel"}}]}',
                "",  # SSE keep-alive blank line tolerated
                'data: {"choices":[{"delta":{"content":"lo"}}]}',
                'data: {"choices":[{"delta":{}}]}',  # no content -> skipped
                "data: [DONE]",
                'data: {"choices":[{"delta":{"content":"AFTER"}}]}',  # past [DONE] -> ignored
            ]
        )
        deltas = await _collect(llm.stream_text("hi"))
        expect(deltas == ["Hel", "lo"], f"openai stream yields content deltas (got {deltas})")
        expect("".join(deltas) == "Hello", "openai deltas reassemble to full text")
        expect(_FakeClient.last_url.endswith("/chat/completions"),
               "openai stream posts to /chat/completions")
        expect(_FakeClient.last_json.get("stream") is True,
               "openai stream request sets stream=true")

        # --- ollama backend: parse NDJSON {"response","done"}, stop on done=true
        fake_settings.dgx_chat_backend = "ollama"
        _FakeClient.resp = _FakeResp(
            [
                '{"response":"Hel","done":false}',
                '{"response":"lo","done":false}',
                "not-json-ignored",
                '{"response":"","done":true}',
                '{"response":"AFTER","done":false}',  # past done -> ignored
            ]
        )
        deltas = await _collect(llm.stream_text("hi"))
        expect(deltas == ["Hel", "lo"], f"ollama stream yields response deltas (got {deltas})")
        expect(_FakeClient.last_url.endswith("/api/generate"),
               "ollama stream posts to /api/generate")
        expect(_FakeClient.last_json.get("stream") is True,
               "ollama stream request sets stream=true")

        # --- fail-closed: a >=400 status raises httpx.HTTPError before any delta
        fake_settings.dgx_chat_backend = "openai"
        _FakeClient.resp = _FakeResp([], status=502)
        raised = False
        try:
            await _collect(llm.stream_text("hi"))
        except httpx.HTTPError:
            raised = True
        expect(raised, "stream_text raises httpx.HTTPError on a 5xx (caller's except still works)")
    finally:
        httpx.AsyncClient, llm.settings = orig_client, orig_settings

    # --- SSE frame encoder
    frame = _sse_event({"type": "delta", "text": "hi\nthere"})
    expect(frame.endswith(b"\n\n"), "_sse_event terminates the frame with a blank line")
    expect(frame.startswith(b"data: "), "_sse_event emits a single data: line")
    import json as _json
    body = _json.loads(frame[len(b"data: "):].strip())
    expect(body == {"type": "delta", "text": "hi\nthere"},
           "_sse_event round-trips the payload as JSON (newlines preserved)")

    # --- ChatRequest opt-in field
    expect(ChatRequest(message="x").stream is False, "ChatRequest.stream defaults to False")
    expect(ChatRequest(message="x", stream=True).stream is True, "ChatRequest accepts stream=True")

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: /chat SSE streaming backend verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
