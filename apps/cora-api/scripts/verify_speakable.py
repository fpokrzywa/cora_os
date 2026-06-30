"""Deterministic check of speakable (TTS) output: the to_speakable normalizer's
transforms + idempotency, and the ChatRequest.speakable opt-in field. NO model, NO net.

    docker cp apps/cora-api/scripts/verify_speakable.py cora-api:/tmp/vsp.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vsp.py     # 0=PASS 1=FAIL
"""
import sys

from app.speakable import SPEAKABLE_STYLE, to_speakable
from app.routers.chat import ChatRequest


def main() -> int:
    fails: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    s = to_speakable("## Heading\n\nText.")
    expect(s == "Heading\nText.", f"heading marker stripped (got {s!r})")
    expect(to_speakable("**bold** and *italic* and `code`") == "bold and italic and code",
           "bold/italic/inline-code unwrapped")
    expect(to_speakable("See [the docs](https://x.com/y).") == "See the docs.",
           "markdown link -> link text only")
    expect(to_speakable("Go to https://example.com now") == "Go to a link now",
           "bare URL -> 'a link'")
    expect(to_speakable("- one\n- two\n- three") == "one\ntwo\nthree",
           "bullet markers stripped")
    expect("(code omitted)" in to_speakable("Here:\n```python\nprint(1)\n```\ndone"),
           "fenced code block replaced, not read aloud")
    tbl = to_speakable("| Name | Port |\n|------|------|\n| api | 8000 |")
    expect("|" not in tbl and "api, 8000" in tbl,
           f"table linearized to prose, pipes gone (got {tbl!r})")
    em = to_speakable("Done \U0001F512 ready ✅")
    expect("\U0001F512" not in em and "✅" not in em and "Done" in em and "ready" in em,
           f"emoji stripped (got {em!r})")
    once = to_speakable("## H\n\n**b** [l](http://x) `c`")
    expect(to_speakable(once) == once, "idempotent on already-normalized text")
    expect(to_speakable("Plain sentence already.") == "Plain sentence already.",
           "plain text passes through unchanged")
    expect(to_speakable("") == "", "empty in -> empty out")

    expect(isinstance(SPEAKABLE_STYLE, str) and "VOICE" in SPEAKABLE_STYLE
           and "markdown" in SPEAKABLE_STYLE.lower(),
           "SPEAKABLE_STYLE is a voice/no-markdown instruction")

    expect(ChatRequest(message="x").speakable is False, "ChatRequest.speakable defaults False")
    expect(ChatRequest(message="x", speakable=True).speakable is True,
           "ChatRequest accepts speakable=True")

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(fails))
        return 1
    print("PASS: speakable (TTS) output verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
