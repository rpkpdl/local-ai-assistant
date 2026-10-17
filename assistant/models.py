"""Request options that depend on which Claude model a call uses."""


def fallback_options(model: str) -> dict:
    """Server-side fallback, so a declined request is retried on a suitable model.

    Haiku has no server-side fallback and rejects the parameter, so it gets none.
    """
    if model.startswith("claude-haiku"):
        return {}
    return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
