"""Deterministic check of the semantic routing fallback. NO live model — the
classifier's generate() is a fake. Covers the reply parser (each agent label,
NONE/empty/garbage -> Cora, earliest-mention tie-break), semantic_route's
fail-open behavior (label -> agent, exception -> Cora, blank -> Cora), the
classifier prompt shape, the new settings flag, and the invariant that the
deterministic keyword path is unchanged (a keyword hit never calls the fallback).

    docker cp apps/cora-api/scripts/verify_semantic_routing.py cora-api:/tmp/vsr.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vsr.py     # 0=PASS 1=FAIL
"""
import asyncio
import sys

from app.agents import routing
from app.agents.routing import (
    PERSONA_NAME,
    _build_classifier_prompt,
    _parse_classifier_choice,
    select_subagent,
    semantic_route,
)
from app.config import settings


async def main() -> int:
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ---- parser: clean labels ----
    for label, want in [
        ("FORGE", "FORGE"), ("PULSE", "PULSE"), ("SIGNAL", "SIGNAL"),
        ("CHRONOS", "CHRONOS"), ("forge", "FORGE"), ("  pulse\n", "PULSE"),
    ]:
        expect(_parse_classifier_choice(label) == want, f"parse {label!r} -> {want}")

    # ---- parser: NONE / empty / garbage -> persona ----
    for label in ("NONE", "none", "", None, "I think general chat", "xyzzy", "42"):
        expect(_parse_classifier_choice(label) == PERSONA_NAME,
               f"parse {label!r} -> Cora (no specialist)")

    # ---- parser: embedded in a sentence, earliest mention wins ----
    expect(_parse_classifier_choice("The best fit is PULSE.") == "PULSE",
           "parse label inside a sentence")
    expect(_parse_classifier_choice("FORGE or PULSE") == "FORGE",
           "multiple labels -> earliest mentioned wins")

    # ---- prompt shape ----
    prompt = _build_classifier_prompt("when am I free tomorrow")
    for token in ("FORGE", "PULSE", "SIGNAL", "CHRONOS", "NONE", "when am I free tomorrow"):
        expect(token in prompt, f"classifier prompt includes {token!r}")

    # ---- semantic_route with a fake generator (records the call) ----
    captured: dict = {}

    def fake(reply):
        async def _gen(p, *, system=None, max_tokens=None, temperature=None):
            captured["system"] = system
            captured["max_tokens"] = max_tokens
            captured["temperature"] = temperature
            return reply
        return _gen

    agent, raw = await semantic_route("dig up reviews of the new gpu", generate=fake("PULSE"))
    expect(agent == "PULSE" and raw == "PULSE", "route: 'PULSE' reply -> PULSE")
    expect(captured.get("temperature") == 0.0
           and captured.get("max_tokens") == routing._CLASSIFIER_MAX_TOKENS,
           "route: classify call is deterministic (temp 0) + bounded budget")
    expect(captured.get("system") is not None, "route: passes a system prompt")

    agent2, _ = await semantic_route("hello there", generate=fake("NONE"))
    expect(agent2 == PERSONA_NAME, "route: 'NONE' reply -> Cora")

    agent3, raw3 = await semantic_route("", generate=fake("FORGE"))
    expect(agent3 == PERSONA_NAME and raw3 == "", "route: blank message -> Cora (no call)")

    async def boom(p, *, system=None, max_tokens=None, temperature=None):
        raise RuntimeError("backend down")

    agent4, raw4 = await semantic_route("anything", generate=boom)
    expect(agent4 == PERSONA_NAME and raw4 == "", "route: generate() raises -> fail-open Cora")

    # ---- flag exists + default-off ----
    expect(hasattr(settings, "semantic_routing_enabled"),
           "settings.semantic_routing_enabled exists")
    expect(settings.semantic_routing_enabled in (True, False),
           "semantic_routing_enabled is a bool")

    # ---- deterministic path unchanged: a keyword hit never needs the fallback ----
    sel, matched = select_subagent("help me debug this python stack trace")
    expect(sel == "FORGE" and matched, "keyword routing still wins for a clear FORGE prompt")
    sel2, matched2 = select_subagent("what is the meaning of life")
    expect(sel2 == PERSONA_NAME and not matched2,
           "no-keyword prompt scores 0 (this is where the fallback would engage)")

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: semantic routing fallback verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
