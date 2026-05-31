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

import re
from dataclasses import dataclass, field
from typing import Optional

from app.agents import chronos, forge, pulse, signal

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
