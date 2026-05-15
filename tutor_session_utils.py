"""Shared helpers for kid tutor agent (env parsing, pronunciation_rules avatar mapping)."""

from __future__ import annotations

import os
from typing import Any


def env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def use_bithuman_avatar() -> bool:
    """True unless KID_TUTOR_USE_AVATAR or USE_BITUMAN_AVATAR is set falsey."""
    v = os.getenv("KID_TUTOR_USE_AVATAR", "").strip()
    if v:
        return env_flag("KID_TUTOR_USE_AVATAR", "1")
    return env_flag("USE_BITUMAN_AVATAR", "1")


def avatar_cue_for_band(rules: dict[str, Any], band: str) -> dict[str, str] | None:
    """Map score band to emotion/animation from pronunciation_rules.avatarBehaviorMapping."""
    m = rules.get("avatarBehaviorMapping")
    if not isinstance(m, dict):
        return None
    spec = m.get(band)
    if not isinstance(spec, dict):
        return None
    em = spec.get("emotion")
    anim = spec.get("animation")
    if em is None and anim is None:
        return None
    out: dict[str, str] = {}
    if isinstance(em, str):
        out["emotion"] = em
    if isinstance(anim, str):
        out["animation"] = anim
    return out or None
