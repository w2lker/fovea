"""Configuration read from the environment on every call."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_MODEL = "~typesafe/jev-latest"
MODES = ("active", "shadow", "off")


def _float(env, name: str, default: float, minimum: float = 0.0) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if math.isnan(value) or math.isinf(value) or value < minimum:
        return default
    return value


def _int(env, name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (ValueError, OverflowError):
        return default
    return value if value >= minimum else default


@dataclass
class Config:
    mode: str
    mode_valid: bool
    base_url: str
    key_env: str
    api_key: str
    model: str
    threshold: float
    timeout: float
    min_chars: int
    harness_limit: int
    shadow_sample: float
    home: str
    retention_days: float
    max_store_mb: float


def load(env=None) -> Config:
    env = os.environ if env is None else env
    raw_mode = env.get("FOVEA_MODE", "").strip().lower() or "active"
    mode_valid = raw_mode in MODES
    key_env = env.get("FOVEA_API_KEY_ENV", "").strip() or DEFAULT_KEY_ENV
    home = env.get("FOVEA_HOME", "").strip() or "~/.fovea"
    sample = _float(env, "FOVEA_SHADOW_SAMPLE", 0.15)
    return Config(
        mode=raw_mode,
        mode_valid=mode_valid,
        base_url=env.get("FOVEA_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        key_env=key_env,
        api_key=env.get(key_env, "").strip(),
        model=env.get("FOVEA_MODEL", "").strip() or DEFAULT_MODEL,
        threshold=_float(env, "FOVEA_THRESHOLD", 0.2),
        timeout=_float(env, "FOVEA_TIMEOUT", 4.0) or 4.0,
        min_chars=_int(env, "FOVEA_MIN_CHARS", 3500),
        # Claude Code truncates Bash output at this many characters before hooks see it,
        # and persists the full output itself.
        harness_limit=_int(env, "BASH_MAX_OUTPUT_LENGTH", 30000),
        shadow_sample=min(sample, 1.0),
        home=os.path.abspath(os.path.expanduser(home)),
        retention_days=_float(env, "FOVEA_RETENTION_DAYS", 30.0),
        max_store_mb=_float(env, "FOVEA_MAX_STORE_MB", 500.0),
    )
