"""Cora memory subsystem (semantic + keyword)."""

from .disambiguation import detect_ambiguous_recall, disambiguation_instruction
from .embeddings import (
    embed_memory_entry,
    embed_missing,
    generate_embedding,
    is_embedding_configured,
    semantic_search,
    vector_text,
)

__all__ = [
    "detect_ambiguous_recall",
    "disambiguation_instruction",
    "embed_memory_entry",
    "embed_missing",
    "generate_embedding",
    "is_embedding_configured",
    "semantic_search",
    "vector_text",
]
