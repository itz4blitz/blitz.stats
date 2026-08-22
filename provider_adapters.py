"""Provider account discovery with metadata-only results."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

UTF8 = "utf-8"
ZAI_BASE_HOST = "z.ai"
ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"

@dataclass(frozen=True)
class ProviderAccount:
    provider_id: str
    label: str
    source: str
    config_path: Path | None = None


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding=UTF8))  # pragma: no mutate - None is UTF-8 on this host
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _zai_config(home: Path) -> bool:
    for path in (home / ".zcode" / "v2" / "config.json", home / ".zcode" / "cli" / "config.json"):
        config = _read_json(path)
        providers = config.get("provider", {}) if isinstance(config, dict) else {}
        if any("zai" in str(key).lower() or "glm" in str(key).lower() for key in providers):
            return True
    claude = _read_json(home / ".claude" / "settings.json")
    env = claude.get("env", {}) if isinstance(claude, dict) else {}
    return ZAI_BASE_HOST in str(env.get(ANTHROPIC_BASE_URL)).lower()


def discover_local_accounts(env: Mapping[str, str] | None = None, home: Path | None = None) -> list[ProviderAccount]:
    environment = dict(env or os.environ)
    root = Path(home or Path.home())
    candidates: dict[str, ProviderAccount] = {}
    if _zai_config(root):
        candidates["zai"] = ProviderAccount("zai", "Z.ai / GLM", "zcode")
    if environment.get("XAI_API_KEY") or environment.get("GROK_API_KEY"):
        candidates["grok"] = ProviderAccount("grok", "Grok", "environment")
    if (root / ".grok" / "auth.json").is_file():
        candidates["grok"] = ProviderAccount("grok", "Grok", "environment")
    if environment.get("OPENROUTER_API_KEY"):
        candidates["openrouter"] = ProviderAccount("openrouter", "OpenRouter", "environment")
    return [candidates[key] for key in sorted(candidates)]
