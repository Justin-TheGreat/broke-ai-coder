from __future__ import annotations

PROMPT_MAX_CHARS = 4000


def sanitize_prompt(prompt: str) -> str | None:
    """Strip NULs and surrounding whitespace; None when empty or too long."""
    cleaned = prompt.replace("\x00", "").strip()
    if not cleaned or len(cleaned) > PROMPT_MAX_CHARS:
        return None
    return cleaned
