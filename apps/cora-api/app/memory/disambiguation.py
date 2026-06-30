"""Ambiguous-recall detection.

When memory recall surfaces two or more entries that are the SAME single-valued
personal fact (identical normalized title) but hold DIFFERENT content, the
assistant can't know which one the user means — e.g. two "Family Dog" entries
with different names. Rather than silently pick one or merge them, the chat path
asks a single clarifying question (which reads as a spoken "which one?" in voice
mode).

This detector is deliberately HIGH-PRECISION / low-recall: interrupting the user
with a question is costly, so it fires ONLY on a same-title / different-content
collision among personal-fact types. Distinct titles (wife vs. dog) and exact
duplicates (same title AND same content) never trigger it.
"""

import re
from typing import Optional

# Types that represent a single-valued personal fact, where two entries under the
# same title is a genuine "which one?" conflict. Docs / news / workspace knowledge
# are excluded — same-title chunks are normal and expected there.
_AMBIGUOUS_TYPES = {
    "family", "personal", "preference", "note", "fact", "contact", "identity",
}


def _norm(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def detect_ambiguous_recall(memories: list[dict]) -> Optional[dict]:
    """Return a disambiguation descriptor for the first same-title /
    different-content collision among personal-fact memories, else None.

    Descriptor: {"title": <original title>, "type": <type>, "count": <n entries>}.
    Pure and deterministic — safe to unit-test without a DB or model.
    """
    by_title: dict[str, list[dict]] = {}
    for m in memories or []:
        if (m.get("type") or "") not in _AMBIGUOUS_TYPES:
            continue
        title = _norm(m.get("title"))
        if not title:
            continue
        by_title.setdefault(title, []).append(m)

    for group in by_title.values():
        if len(group) < 2:
            continue
        contents = {_norm(g.get("content")) for g in group}
        if len(contents) >= 2:
            return {
                "title": group[0].get("title"),
                "type": group[0].get("type"),
                "count": len(group),
            }
    return None


def disambiguation_instruction(descriptor: dict) -> str:
    """One-line system-prompt instruction telling the assistant to ask a single
    short clarifying question instead of guessing. Reads naturally as a spoken
    'which one?' in voice mode."""
    title = descriptor.get("title") or "this"
    count = descriptor.get("count") or 2
    return (
        f"NOTE: memory holds {count} different entries titled \"{title}\" with "
        "conflicting details. If the user's request depends on which one, ask a "
        "single short question to confirm which they mean before answering — do "
        "not guess or combine them."
    )
