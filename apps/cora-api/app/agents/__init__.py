"""Cora subagents.

Cora is the user-facing AI assistant / AI operating system.
Subagents in this package operate internally and are not addressed by the
user directly. The user always talks to Cora; subagents are coordinated by
ATLAS, the orchestration layer.

Current subagents:
- ATLAS  — orchestration / routing (app.agents.atlas)
- SCRIBE — memory manager / summarization (app.agents.scribe)
- FORGE  — engineering / build / devops specialist (app.agents.forge)
- PULSE  — research / information synthesis specialist (app.agents.pulse)

Deterministic subagent selection lives in app.agents.routing.
"""
