"""Multi-agent routing — deterministic subagent selection for ATLAS.

ATLAS classifies a user message and routes it to the best specialist subagent
(FORGE for engineering, PULSE for research) or falls back to the Cora persona.
Selection is purely keyword-based and deterministic in v0.1 — no model call and
no autonomous behavior. Each subagent module owns its own routing keywords; this
module owns the matching + scoring + tie-break policy.

Scoring: the subagent with the most matched keywords wins. Ties are broken by
the order subagents are listed in `_CANDIDATES` (earlier = preferred). If no
subagent matches, the Cora persona handles the message directly.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.agents import chronos, forge, pulse, signal

logger = logging.getLogger(__name__)

PERSONA_NAME = "Cora"

# (agent_name, routing_keywords). Order is the tie-break priority — FORGE first
# as the established specialist, then PULSE, SIGNAL, CHRONOS.
_CANDIDATES: list[tuple[str, list[str]]] = [
    (forge.NAME, forge.FORGE_ROUTING_KEYWORDS),
    (pulse.NAME, pulse.PULSE_ROUTING_KEYWORDS),
    (signal.NAME, signal.SIGNAL_ROUTING_KEYWORDS),
    (chronos.NAME, chronos.CHRONOS_ROUTING_KEYWORDS),
]


def match_keywords(message: str, keywords: list[str]) -> list[str]:
    """Return the keywords that occur in `message`.

    Single-word keywords match on a word boundary; multi-word phrases (e.g.
    "stack trace", "pros and cons") use a looser substring check since `\\b`
    doesn't compose cleanly across spaces.
    """
    lowered = message.lower()
    matched: list[str] = []
    for kw in keywords:
        if " " in kw:
            if kw in lowered:
                matched.append(kw)
        elif re.search(rf"\b{re.escape(kw)}\b", lowered):
            matched.append(kw)
    return matched


def _specificity(matched: list[str]) -> int:
    """Total matched-keyword length — a proxy for how specific the match is."""
    return sum(len(k) for k in matched)


def select_subagent(
    message: str,
    keyword_overrides: Optional[dict[str, list[str]]] = None,
) -> tuple[str, list[str]]:
    """Pick a specialist subagent for the given user message.

    Scoring: most matched keywords wins. Ties are broken by *specificity*
    (longer total matched text — so a strong domain phrase like
    "stakeholder update" beats an incidental short word like "news"), and only
    then by `_CANDIDATES` order (earlier = preferred). Returns
    ('Cora', []) when nothing matches.

    `keyword_overrides` (agent_name -> keywords) lets DB-managed
    metadata.routing_keywords replace the Python constants per agent. Missing or
    empty entries fall back to the Python keywords, so it is always safe to pass
    a partial or empty map.
    """
    best_name = PERSONA_NAME
    best_matched: list[str] = []
    for name, keywords in _CANDIDATES:
        if keyword_overrides:
            override = keyword_overrides.get(name)
            if override:
                keywords = override
        matched = match_keywords(message, keywords)
        if not matched:
            continue
        better = len(matched) > len(best_matched) or (
            len(matched) == len(best_matched)
            and _specificity(matched) > _specificity(best_matched)
        )
        if better:
            best_name, best_matched = name, matched
    return (best_name, best_matched)


@dataclass
class RoutingDiagnostics:
    selected_agent: str
    scores: dict[str, int]
    matched_keywords: dict[str, list[str]]
    tie_break_applied: bool
    matched_for_selected: list[str] = field(default_factory=list)


def diagnose_routing(
    message: str,
    keyword_overrides: Optional[dict[str, list[str]]] = None,
) -> RoutingDiagnostics:
    """Read-only routing diagnostics for the admin test harness. Mirrors
    `select_subagent` scoring without affecting live routing. `tie_break_applied`
    is True when ≥2 candidates share the top (non-zero) match count, meaning the
    specificity/positional tiebreak decided the winner.
    """
    scores: dict[str, int] = {}
    matched_keywords: dict[str, list[str]] = {}
    for name, keywords in _CANDIDATES:
        if keyword_overrides and keyword_overrides.get(name):
            keywords = keyword_overrides[name]
        matched = match_keywords(message, keywords)
        scores[name] = len(matched)
        if matched:
            matched_keywords[name] = matched

    selected_agent, sel_matched = select_subagent(message, keyword_overrides)

    top = max(scores.values(), default=0)
    tie_break_applied = top > 0 and sum(1 for v in scores.values() if v == top) > 1

    return RoutingDiagnostics(
        selected_agent=selected_agent,
        scores=scores,
        matched_keywords=matched_keywords,
        tie_break_applied=tie_break_applied,
        matched_for_selected=sel_matched,
    )


# --------------------------------------------------------------------------- #
# Semantic routing fallback (LLM classifier)
#
# Keyword routing is exact and deterministic but blind to phrasing the keyword
# lists don't anticipate ("dig up what people are saying about X" never hits any
# of PULSE's literal keywords). When `select_subagent` scores 0 — and no explicit
# intent override fired — a single cheap LLM classification picks a specialist
# from the user's intent. This matters most for voice, where phrasing varies far
# more than typed prompts.
#
# Embeddings were evaluated FIRST and rejected: against the live nomic-embed-text
# model, unrelated chit-chat ("thanks, that was helpful") scores a HIGHER cosine
# to the agent profiles (~0.48) than some correct routes (~0.47), and the top-2
# margin doesn't separate real routes from noise either — so neither an absolute
# floor nor a margin gate is reliable. A constrained classification call is.
#
# The whole path is opt-in (settings.semantic_routing_enabled) and fail-open: any
# error, an empty reply, or an unrecognized label leaves routing on the Cora
# persona — byte-for-byte today's behavior.
# --------------------------------------------------------------------------- #

# name -> one-line domain description shown to the classifier.
_SEMANTIC_DESCRIPTIONS: list[tuple[str, str]] = [
    (forge.NAME, "engineering and technical work: writing or debugging code, errors "
                 "and stack traces, APIs, Docker, infrastructure, deployment, "
                 "databases, system architecture"),
    (pulse.NAME, "research and information gathering: investigating or comparing "
                 "things, analysis, summaries, looking into a topic, news and "
                 "current events, best practices"),
    (signal.NAME, "communication and writing for people: drafting emails, messages, "
                  "replies, announcements, status or stakeholder updates, notes, memos"),
    (chronos.NAME, "time and planning: scheduling, calendars, meetings, deadlines, "
                   "reminders, timelines, milestones, availability, planning a day "
                   "or week"),
]

_CLASSIFIER_SYSTEM = (
    "You are an intent router for an AI assistant. Read the user's message and "
    "decide which ONE internal specialist should handle it, or NONE if it is "
    "general conversation that needs no specialist. Reply with EXACTLY one word "
    "and nothing else."
)

# The DGX serves a reasoning model (gpt-oss): it spends hidden reasoning tokens
# before emitting the final-channel answer, so a tiny budget gets cut off mid-
# reasoning and returns an empty string. 256 leaves comfortable room for the
# reasoning plus the one-word label; the backend returns only the clean final
# label, so parsing stays trivial. (Measured: empty at 8/32, clean at 128.)
_CLASSIFIER_MAX_TOKENS = 256


def _build_classifier_prompt(message: str) -> str:
    lines = ["Specialists:"]
    for name, desc in _SEMANTIC_DESCRIPTIONS:
        lines.append(f"- {name}: {desc}")
    lines.append("- NONE: general chat, greetings, or anything no specialist fits")
    lines.append("")
    lines.append(f'User message: "{message.strip()}"')
    lines.append("")
    lines.append(
        "Answer with one of: "
        + ", ".join(name for name, _ in _SEMANTIC_DESCRIPTIONS)
        + ", NONE."
    )
    return "\n".join(lines)


def _parse_classifier_choice(raw: Optional[str]) -> str:
    """Map a raw classifier reply to an agent name, or PERSONA_NAME for NONE /
    empty / unrecognized output. If several agent names appear, the earliest-
    mentioned wins. Deterministic — no model call, so it is unit-testable."""
    up = (raw or "").upper()
    best_pos: Optional[int] = None
    best_name = PERSONA_NAME
    for name, _ in _SEMANTIC_DESCRIPTIONS:
        i = up.find(name)
        if i != -1 and (best_pos is None or i < best_pos):
            best_pos, best_name = i, name
    return best_name


async def semantic_route(
    message: str,
    *,
    generate: Optional[Callable[..., Awaitable[str]]] = None,
) -> tuple[str, str]:
    """LLM-classify `message` to a specialist when keyword routing found nothing.

    Returns (agent_name, raw_reply). agent_name is PERSONA_NAME ('Cora') whenever
    the classifier says NONE, returns nothing usable, or the call fails — so the
    caller can treat 'stayed on Cora' as 'no fallback applied'. `generate` is
    injectable for tests; defaults to app.llm.generate_text.
    """
    if not message or not message.strip():
        return (PERSONA_NAME, "")
    if generate is None:
        from app.llm import generate_text as generate
    prompt = _build_classifier_prompt(message)
    try:
        raw = await generate(
            prompt,
            system=_CLASSIFIER_SYSTEM,
            max_tokens=_CLASSIFIER_MAX_TOKENS,
            temperature=0.0,
        )
    except Exception:
        logger.exception("semantic_route classification failed; staying on persona")
        return (PERSONA_NAME, "")
    return (_parse_classifier_choice(raw), (raw or "").strip())
