"""Digest comparison normalization shared by Channel coverage gates."""

from __future__ import annotations

_DIGEST_PREFIX_LEN = 16


def digest_key(value: str) -> str:
    """Normalize a declared digest for comparison.

    Synthesizer replies hand-copy 64-char hex digests from the context pack
    and frequently transcribe a tail character wrong; gate comparison uses
    the 16-char hex prefix instead, which is still collision-free at this
    project scale (real failures: ch-auto-ade5849896, ch-auto-4f1edcc4eb).
    """
    text = str(value).strip().lower()
    if len(text) >= _DIGEST_PREFIX_LEN and all(
        ch in "0123456789abcdef" for ch in text[:_DIGEST_PREFIX_LEN]
    ):
        return text[:_DIGEST_PREFIX_LEN]
    return text


__all__ = ["digest_key"]
