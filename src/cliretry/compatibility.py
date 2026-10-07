"""Release-gated UI combinations. Populate only with recorded live validation.

An empty registry deliberately means observation works but automatic input has
not yet been certified for a real host. Unit tests inject a fake capability gate.
"""

# (iTerm2 version, SDK version, native Codex SHA-256, UI profile revision)
VERIFIED_COMBINATIONS: frozenset[tuple[str, str, str, str]] = frozenset()


def verified(iterm_version: str, sdk_version: str, codex_sha256: str, profile: str) -> bool:
    return (iterm_version, sdk_version, codex_sha256, profile) in VERIFIED_COMBINATIONS

