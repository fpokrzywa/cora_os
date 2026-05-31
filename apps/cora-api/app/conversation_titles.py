"""Deterministic chat-title generation for Chat Management v0.1.

Derives a short, friendly title from the user's first message — no LLM. Strips
common lead-in phrases, collapses whitespace, caps length, and falls back to
"New chat" when nothing meaningful remains.
"""

import re

TITLE_MAX_LEN = 60

# Longest-first so "can you help me ..." strips fully before "can you" matches.
_PREFIXES = [
    "can you please help me",
    "can you help me",
    "could you please help me",
    "could you help me",
    "i would like you to",
    "i would like to",
    "i'd like to",
    "i want you to",
    "i want to",
    "can you please",
    "could you please",
    "would you please",
    "can you",
    "could you",
    "would you",
    "help me",
    "please",
]


def generate_title(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return "New chat"
    # First line only, whitespace collapsed.
    text = re.sub(r"\s+", " ", text.splitlines()[0]).strip()

    # Iteratively peel known lead-ins (handles e.g. "please can you ...").
    changed = True
    while changed:
        changed = False
        lowered = text.lower()
        for prefix in _PREFIXES:
            if lowered == prefix:
                text = ""
                changed = True
                break
            if lowered.startswith(prefix + " "):
                text = text[len(prefix):].lstrip(" ,:;-—").strip()
                changed = True
                break

    text = text.strip()
    if not text:
        return "New chat"

    if len(text) > TITLE_MAX_LEN:
        cut = text[:TITLE_MAX_LEN].rsplit(" ", 1)[0].rstrip(" ,:;-—")
        text = (cut or text[:TITLE_MAX_LEN].rstrip()) + "…"

    return text[0].upper() + text[1:]
