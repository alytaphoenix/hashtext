"""Index-line rendering: the compact, always-hot description of cold blocks.

One line per block — cheap enough to sit in an agent's system prompt, giving
the model the map it needs to declare an expansion.
"""

from __future__ import annotations

from .store import BlockMeta


def human_bytes(n: int) -> str:
    """512B / 4.1KB / 2.3MB — one decimal above bytes."""
    if n < 1024:
        return f"{n}B"
    value = float(n)
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}"
    return f"{value:.1f}GB"  # pragma: no cover - unreachable


def index_line(meta: BlockMeta) -> str:
    """``<ref> | <name> | <description> | 4.1KB→1.1KB``"""
    name = meta.name or "(unnamed)"
    description = meta.description or "-"
    return (
        f"{meta.id} | {name} | {description} | "
        f"{human_bytes(meta.bytes)}→{human_bytes(meta.stored)}"
    )


def render_index(metas: list[BlockMeta]) -> str:
    """All index lines, or a friendly empty state."""
    if not metas:
        return "no blocks"
    return "\n".join(index_line(meta) for meta in metas)
