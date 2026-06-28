"""Deterministic verification of chat memory CAPTURE (app.agents.scribe).

Covers the bug where natural "remember this" / "save to memory" requests silently
did NOT persist (only a strict `remember that <X>` regex wrote memory) while the
LLM confabulated "I've stored that". Pure-function checks — NO DB, NO model call —
so it is CI/offline safe. The LLM extraction itself is exercised live.

Parts:
  A) match_chat_memory_intent routing — the strict `remember that <X>` /
     `remember globally that` / `remember as system that` forms still save their
     inline text; the broader contextual ("can you remember that", "remember this")
     and save-to-memory ("store all of these in memory") forms route to
     'remember_extract'; declaratives and meta-questions do NOT trigger a save.
  B) _parse_memory_facts robustness — clean JSON array, prose-wrapped array, string
     items, malformed/empty -> [], in-batch dedup, and the 25-item cap.

    docker cp apps/cora-api/scripts/verify_chat_memory.py cora-api:/tmp/vm.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vm.py   # 0=PASS 1=FAIL
"""
import sys

from app.agents import scribe
from app.routers.chat import (
    CORA_SYSTEM_PROMPT,
    _format_memory_block,
    _rank_fuse_memories,
)


def main() -> int:
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        if cond:
            print(f"  ok   {msg}")
        else:
            fails.append(msg)
            print(f"  FAIL {msg}")

    def kind(message: str):
        r = scribe.match_chat_memory_intent(message)
        return r["kind"] if r else None

    # ---- A) intent routing ----
    print("A) memory intent routing")

    # The exact phrasings from the reported transcript that USED to silently no-op:
    expect(kind("Can you remember that") == "remember_extract",
           "'Can you remember that' -> remember_extract (was a silent no-op)")
    expect(kind("Can you make sure all of these are stored in permanent memory.")
           == "remember_extract",
           "'... stored in permanent memory.' -> remember_extract")
    expect(kind("remember this") == "remember_extract",
           "'remember this' -> remember_extract")
    expect(kind("please remember all of this") == "remember_extract",
           "'please remember all of this' -> remember_extract")
    expect(kind("save this to memory") == "remember_extract",
           "'save this to memory' -> remember_extract")

    # Declaratives are NOT save commands (captured later via an explicit request).
    expect(kind("Dorothy is my wife and I called her Meshu") is None,
           "a plain statement does not trigger a save")
    expect(kind("My nickname for Cailey is Goose, and Ashleigh is Ash") is None,
           "another plain statement does not trigger a save")

    # Meta-questions about memory must not trigger a save.
    expect(kind("do you keep things in memory?") is None,
           "a question about memory does not trigger a save")
    expect(kind("what's the weather today") is None,
           "an unrelated message does not trigger a save")

    # The strict inline forms still save their literal text (unchanged behavior).
    r = scribe.match_chat_memory_intent("remember that the garage code is 1990")
    expect(r and r["kind"] == "remember_user" and r["text"] == "the garage code is 1990",
           "'remember that <X>' still saves the inline text (remember_user)")
    expect(kind("remember globally that the office closes at 5") == "remember_global",
           "'remember globally that <X>' -> remember_global (unchanged)")
    expect(kind("remember as system that retries cap at 3") == "remember_system",
           "'remember as system that <X>' -> remember_system (unchanged)")
    expect(kind("show memories about dogs") == "show",
           "'show memories about <X>' -> show (unchanged)")

    # ---- B) _parse_memory_facts robustness ----
    print("B) _parse_memory_facts robustness")
    clean = scribe._parse_memory_facts('[{"text":"A fact."},{"text":"Another."}]')
    expect(len(clean) == 2 and clean[0]["text"] == "A fact.",
           "parses a clean JSON array of {text}")
    prose = scribe._parse_memory_facts(
        'Sure, here you go: [{"text":"Wife is Dorothy."}] -- done')
    expect(len(prose) == 1 and prose[0]["text"] == "Wife is Dorothy.",
           "extracts a JSON array embedded in prose")
    strs = scribe._parse_memory_facts('["plain string fact", "second"]')
    expect(len(strs) == 2 and strs[0]["text"] == "plain string fact",
           "accepts bare-string items")
    dup = scribe._parse_memory_facts('[{"text":"Same"},{"text":"same"},{"text":"Other"}]')
    expect(len(dup) == 2, "dedups within the batch (case-insensitive)")
    expect(scribe._parse_memory_facts("no json here at all") == [],
           "malformed reply -> [] (never a bogus save)")
    expect(scribe._parse_memory_facts("") == [], "empty reply -> []")
    big = "[" + ",".join('{"text":"f%d"}' % i for i in range(40)) + "]"
    expect(len(scribe._parse_memory_facts(big)) == 25, "caps the batch at 25 facts")

    # ---- C) recall merge: Reciprocal Rank Fusion ----
    # The live bug: 20 semantic rows were listed before any keyword row, then only
    # the top 5 were injected — so an exact keyword hit ('Goose') the embedding
    # ranked low (semantic rank 15) was never injected.
    print("C) recall rank fusion")
    semantic = [{"id": f"s{i}"} for i in range(20)]
    semantic[15] = {"id": "goose"}              # the nickname memory, ranked low by embeddings
    keyword = [{"id": "goose"}, {"id": "blob"}]  # but it's the top exact keyword hit
    fused = _rank_fuse_memories(semantic, keyword, limit=20)
    top5 = [r["id"] for r in fused[:5]]
    expect("goose" in top5,
           "a low-semantic-rank exact keyword hit lands in the injected top-5")
    expect(fused[0]["id"] == "goose",
           "a memory in BOTH lists fuses to the very top (co-occurrence boost)")
    ids = [r["id"] for r in fused]
    expect(len(ids) == len(set(ids)), "fusion dedups rows by id")

    kw_only = _rank_fuse_memories(
        [{"id": "s0"}, {"id": "s1"}], [{"id": "kwhit"}], limit=5)
    expect("kwhit" in [r["id"] for r in kw_only][:5],
           "a keyword-only top hit is not starved by semantic rows")
    expect(_rank_fuse_memories([], [], limit=5) == [], "empty inputs -> []")

    # ---- D) concise-answer framing (style guardrails in the prompt) ----
    # The model recited the whole stored memory ("Goose is the nickname for my
    # daughter Cailey Amanda Pokrzywa, born February 29, 2000") instead of answering
    # the question. These guard that the framing instructing concise, non-recited
    # answers stays in the prompt.
    print("D) concise-answer framing")
    blk = _format_memory_block([{"title": "T", "content": "C"}])
    expect("do not recite" in blk.lower(),
           "memory block tells the model not to recite/quote entries")
    expect("recite stored facts" in CORA_SYSTEM_PROMPT
           and "one-sentence" in CORA_SYSTEM_PROMPT,
           "system prompt directs concise, one-sentence factual answers")
    expect("never as" in blk.lower() and "you" in blk.lower(),
           "memory block pins second-person voice (you/your, not I/my/we)")
    expect("address the user as" in CORA_SYSTEM_PROMPT,
           "system prompt pins second-person address (never speak as the user)")

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: chat memory capture verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
