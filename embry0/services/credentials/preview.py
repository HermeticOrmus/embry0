"""What an operator sees of a secret: never the value."""

_GLYPHS = "••••••••"


def masked_preview(secret: str) -> str:
    """First 4 + last 4 characters when the secret is 12+ long, else all glyphs.

    Same rule as api.v1.environment.partial_mask so the two surfaces agree.
    """
    if len(secret) < 12:
        return _GLYPHS
    return f"{secret[:4]}{_GLYPHS}{secret[-4:]}"
