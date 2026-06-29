"""Deterministic check that the remaining text-gen paths route through app.llm
(backend-selectable) instead of a hard-coded Ollama /api/generate call. The model
call is monkeypatched — NO live model, NO egress. agent_admin's test-response route
is the same one-line swap (covered by py_compile + review).

    docker cp apps/cora-api/scripts/verify_text_gen_backend.py cora-api:/tmp/vt.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vt.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys

from app.clients import clients, init_clients  # noqa: F401 (init for news DB lookup)
from app import llm
from app.agents import scribe
from app import news_briefing
from app import chat_email_lifecycle as cel

SENTINEL = "ROUTED-THROUGH-LLM"


async def main() -> int:
    await init_clients()
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    calls: list[dict] = []

    async def fake(prompt, *, system=None, max_tokens=512, temperature=0.7, timeout=120.0):
        calls.append({"chars": len(prompt), "max_tokens": max_tokens, "timeout": timeout})
        return SENTINEL

    orig_gen, orig_cfg = llm.generate_text, llm.is_chat_configured
    llm.generate_text = fake
    llm.is_chat_configured = lambda: True
    try:
        s = await scribe.summarize_messages(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
        expect(s == SENTINEL, "scribe.summarize_messages routes through llm.generate_text")

        e = await cel._generate_text("Subject: x\n\nbody")
        expect(e == SENTINEL, "chat_email_lifecycle._generate_text routes through llm.generate_text")

        n = await news_briefing.generate_briefing_summary([{"title": "T", "short_preview": "p"}])
        expect(n == SENTINEL, "news_briefing.generate_briefing_summary routes through llm.generate_text")

        expect(len(calls) == 3 and all(c["max_tokens"] >= 800 for c in calls),
               "each path made one llm.generate_text call with a sufficient max_tokens")

        # fail-closed: unconfigured backend -> news briefing returns None (no call).
        llm.is_chat_configured = lambda: False
        expect(await news_briefing.generate_briefing_summary([{"title": "T"}]) is None,
               "news briefing returns None when the backend is unconfigured (fail-closed)")
    finally:
        llm.generate_text, llm.is_chat_configured = orig_gen, orig_cfg

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: text-gen paths routed through app.llm")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
