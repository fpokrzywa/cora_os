"""Speakable (text-to-speech friendly) output for the voice path.

Two pieces, both opt-in via the chat request's `speakable` flag (default off, so the
text UI is untouched):
  - SPEAKABLE_STYLE: a system-prompt instruction asking the model to answer in short,
    plain, spoken sentences (no markdown/tables/code/URLs).
  - to_speakable(text): a deterministic normalizer that strips markdown + decoration a
    TTS engine would read literally ("asterisk asterisk", a raw URL, a table pipe), as
    a backstop for whatever markdown the model still emits.

Nothing here calls a model or the network.
"""
import re

SPEAKABLE_STYLE = (
    "VOICE MODE: your reply will be read aloud by a text-to-speech engine. Answer in "
    "short, plain, spoken sentences. Do NOT use markdown — no headings, bold/italic, "
    "bullet lists, tables, code blocks, or URLs. Spell out what matters in prose. Keep "
    "it brief: lead with the answer in one or two sentences; only add detail if the "
    "question needs it. Don't read out long lists or ids unless asked."
)

# Fenced code blocks ```...``` — never speak code aloud.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}.*$", re.MULTILINE)
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)", re.DOTALL)
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿️]"
)
_MULTI_NL_RE = re.compile(r"\n{2,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def _table_row_to_prose(line: str) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cells = [c for c in cells if c]
    return ", ".join(cells)


def to_speakable(text: str) -> str:
    """Normalize markdown/decorated text into TTS-friendly plain prose. Best-effort,
    deterministic, idempotent on already-plain text."""
    if not text:
        return ""
    t = text
    t = _FENCE_RE.sub(" (code omitted) ", t)
    t = _IMAGE_RE.sub(r"\1", t)
    t = _LINK_RE.sub(r"\1", t)
    t = _TABLE_SEP_RE.sub("", t)            # drop |---|---| separator rows
    # Linearize remaining table rows ("| a | b |" -> "a, b.")
    out_lines = []
    for line in t.split("\n"):
        if line.count("|") >= 2 and not line.strip().startswith("|--"):
            prose = _table_row_to_prose(line)
            out_lines.append(prose + "." if prose and not prose.endswith((".", "?", "!")) else prose)
        else:
            out_lines.append(line)
    t = "\n".join(out_lines)
    t = _HEADING_RE.sub("", t)
    t = _BLOCKQUOTE_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    t = _BOLD_RE.sub(r"\2", t)
    t = _ITALIC_RE.sub(r"\2", t)
    t = _INLINE_CODE_RE.sub(r"\1", t)
    t = _BARE_URL_RE.sub("a link", t)
    t = _EMOJI_RE.sub("", t)
    t = _MULTI_NL_RE.sub("\n", t)
    t = _MULTI_SPACE_RE.sub(" ", t)
    # Strip leftover markdown table pipes + tidy each line.
    t = "\n".join(line.strip().strip("|").strip() for line in t.split("\n"))
    return t.strip()
