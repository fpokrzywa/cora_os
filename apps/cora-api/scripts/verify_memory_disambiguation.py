"""Deterministic check of ambiguous-recall disambiguation. NO DB, NO model.
Covers detect_ambiguous_recall (same-title/different-content fires; exact dup,
distinct titles, excluded types, empties do NOT), title normalization, the
instruction text, and the _format_memory_block integration (instruction appended
only when ambiguous).

    docker cp apps/cora-api/scripts/verify_memory_disambiguation.py cora-api:/tmp/vmd.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vmd.py     # 0=PASS 1=FAIL
"""
import sys

from app.memory import detect_ambiguous_recall, disambiguation_instruction
from app.routers.chat import _format_memory_block


def main() -> int:
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def mem(t, title, content):
        return {"type": t, "title": title, "content": content}

    # ---- fires: same title, different content, personal type ----
    ambig = [mem("family", "Family Dog", "Our dog is Linda, nicknamed Bean"),
             mem("family", "Family Dog", "Our dog is Rex")]
    d = detect_ambiguous_recall(ambig)
    expect(d is not None and d["title"] == "Family Dog" and d["count"] == 2 and d["type"] == "family",
           "same-title/different-content -> ambiguous descriptor")

    # ---- title normalization (case + whitespace) still collides ----
    d2 = detect_ambiguous_recall([
        mem("note", "Work Address", "123 Main St"),
        mem("note", "  work   address ", "456 Oak Ave")])
    expect(d2 is not None and d2["count"] == 2, "title match is case/whitespace-insensitive")

    # ---- does NOT fire ----
    expect(detect_ambiguous_recall([
        mem("family", "Family Dog", "Linda"),
        mem("family", "Family Dog", "Linda")]) is None,
        "exact duplicate (same title AND content) -> not ambiguous")
    expect(detect_ambiguous_recall([
        mem("family", "Dorothy Pokrzywa", "my wife"),
        mem("family", "Family Dog", "Linda")]) is None,
        "distinct titles (wife vs dog) -> not ambiguous")
    expect(detect_ambiguous_recall([
        mem("workspace_knowledge", "Example Domain", "a"),
        mem("workspace_knowledge", "Example Domain", "b")]) is None,
        "excluded type (workspace_knowledge) -> not ambiguous")
    expect(detect_ambiguous_recall([
        mem("news_article", "Headline", "x"),
        mem("news_article", "Headline", "y")]) is None,
        "excluded type (news_article) -> not ambiguous")
    expect(detect_ambiguous_recall([]) is None, "empty recall -> None")
    expect(detect_ambiguous_recall([mem("note", "", "x"), mem("note", "", "y")]) is None,
           "blank titles -> None")

    # ---- instruction text ----
    instr = disambiguation_instruction(d)
    expect("Family Dog" in instr and "single short question" in instr
           and "do not guess" in instr.lower(),
           "instruction names the title + asks one question + says don't guess")

    # ---- _format_memory_block integration ----
    block_ambig = _format_memory_block(ambig)
    expect("single short question" in block_ambig,
           "memory block APPENDS the disambiguation instruction when ambiguous")
    block_clear = _format_memory_block([
        mem("family", "Dorothy Pokrzywa", "my wife"),
        mem("note", "Our family dog", "Linda, aka Bean")])
    expect("single short question" not in block_clear,
           "memory block is unchanged when recall is NOT ambiguous")

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: ambiguous-recall disambiguation verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
