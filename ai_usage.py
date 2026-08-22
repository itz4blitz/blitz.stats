"""Credential-free normalization helpers for the unified Omarchy AI bar."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

UTF8 = "utf-8"
WEEKLY_MARKERS = ("week", "7-day", "weekly")
SESSION_MARKERS = ("session", "5-hour", "5h")
ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
AUTHORIZATION_HEADER = "Authorization"
ACCEPT_HEADER = "Accept"
JSON_MEDIA_TYPE = "application/json"
XAI_TOKEN_AUTH_HEADER = "X-XAI-Token-Auth"
XAI_TOKEN_AUTH_VALUE = "xai-grok-cli"
OPENROUTER_ID = "openrouter"
CURSOR_ID = "cursor"
CURSOR_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
OPENROUTER_RESET_KINDS = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
GROK_PERIOD_KINDS = {
    "USAGE_PERIOD_TYPE_DAILY": ("daily", "Daily"),
    "USAGE_PERIOD_TYPE_WEEKLY": ("weekly", "Weekly"),
    "USAGE_PERIOD_TYPE_MONTHLY": ("monthly", "Monthly"),
}

SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
}

PROVIDER_ICONS = {
    "codex": "󰆍",
    "claude": "󰚩",
    "zai": "󰈙",
    "grok": "󰭻",
    "openrouter": "󰒋",
    "cursor": "󰆼",
}


def provider_icon(provider_id: str) -> str:
    return PROVIDER_ICONS.get(provider_id, "?")


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _clean(item)
            for key, item in value.items()
            if str(key).lower().replace("-", "_") not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _number(value: Any) -> int:
    try:
        return max(0, round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _limit_kind(label: Any) -> str | None:
    text = str(label).lower()
    if any(word in text for word in WEEKLY_MARKERS):
        return "weekly"
    if any(word in text for word in SESSION_MARKERS):
        return "session"
    return None


def _limits(raw_limits: Any) -> list[dict[str, Any]]:
    result = []
    for raw in raw_limits if isinstance(raw_limits, list) else []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind") or _limit_kind(raw.get("label"))
        if kind not in {"session", "weekly", "monthly"}:
            continue
        try:
            percent = float(raw.get("percent"))
        except (TypeError, ValueError):
            continue
        if not 0 <= percent <= 1:
            continue
        result.append(
            {
                "kind": kind,
                "label": str(raw.get("label") or kind.title()),
                "percent": percent,
                "resetsAt": str(raw.get("resetsAt") or ""),
            }
        )
    return result


def normalize_record(raw: dict[str, Any], provider_id: str) -> dict[str, Any]:
    cleaned = _clean(raw if isinstance(raw, dict) else {})
    limits = _limits(cleaned.get("limits"))
    status = str(cleaned.get("status") or ("ok" if cleaned.get("ready") else "unavailable"))
    return {
        "schemaVersion": 2,
        "id": provider_id,
        "icon": provider_icon(provider_id),
        "name": str(cleaned.get("name") or provider_id),
        "tierLabel": str(cleaned.get("tierLabel") or ""),
        "statusText": str(cleaned.get("usageStatusText") or ""),
        "authHelpText": str(cleaned.get("authHelpText") or ""),
        "ready": bool(cleaned.get("ready")),
        "status": status,
        "updatedAt": str(cleaned.get("updatedAt") or datetime.now(timezone.utc).isoformat()),
        "limits": limits,
        "today": {
            "prompts": _number(cleaned.get("todayPrompts", (cleaned.get("today") or {}).get("prompts"))),
            "sessions": _number(cleaned.get("todaySessions", (cleaned.get("today") or {}).get("sessions"))),
            "tokens": _number(cleaned.get("todayTotalTokens", (cleaned.get("today") or {}).get("tokens"))),
        },
        "recentDays": cleaned.get("recentDays") if isinstance(cleaned.get("recentDays"), list) else [],
        "modelUsage": cleaned.get("modelUsage") if isinstance(cleaned.get("modelUsage"), dict) else {},
        "balance": cleaned.get("balance") if isinstance(cleaned.get("balance"), dict) else None,
        "source": cleaned.get("source") if isinstance(cleaned.get("source"), dict) else {
            "local": bool(cleaned.get("hasLocalStats", True)),
            "authoritative": bool(limits),
        },
    }


def limit_percent(record: dict[str, Any], kind: str) -> float | None:
    for entry in (record.get("limits") or []) if isinstance(record, dict) else []:
        if entry.get("kind") == kind:
            return float(entry["percent"])
    return None


def safe_status(error: Exception | None, record: dict[str, Any] | None) -> str:
    if error is None:
        return str((record or {}).get("status") or "ok")
    text = str(error).lower()
    if "401" in text or "403" in text or isinstance(error, PermissionError):
        return "Auth required"
    if "429" in text:
        return "Rate limited"
    if isinstance(error, (TimeoutError, TimeoutError)) or "timeout" in text:
        return "Stale"
    return "Unavailable"


REQUESTED_PROVIDERS = (
    ("codex", "Codex"),
    ("claude", "Claude"),
    ("zai", "Z.ai / GLM"),
    ("grok", "Grok"),
    ("openrouter", "OpenRouter"),
    ("cursor", "Cursor"),
)


def _record_path(home: Path, provider_id: str) -> Path:
    return home / ".local" / "state" / "omarchy" / "agents" / "usage" / f"{provider_id}.json"


_NATIVE_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_NATIVE_TTL_SECONDS = {"claude": 900.0}


def _cached_native_record(provider_id: str, home: Path) -> dict[str, Any] | None:
    ttl = _NATIVE_TTL_SECONDS.get(provider_id)
    now = time.monotonic()
    if ttl:
        cached = _NATIVE_CACHE.get(provider_id)
        if cached and now - cached[0] < ttl:
            return cached[1]
    payload = _native_record(provider_id, home)
    if ttl:
        _NATIVE_CACHE[provider_id] = (now, payload)
    return payload


def _native_record(provider_id: str, home: Path, runner: Any = subprocess.run) -> dict[str, Any] | None:
    try:
        result = runner(
            [f"omarchy-agent-usage-{provider_id}", "--limits-only"],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "HOME": str(home)},
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return payload if isinstance(payload, dict) else None


def _grok_today(home: Path) -> dict[str, int]:
    path = home / ".grok" / "logs" / "unified.jsonl"
    total = 0
    sessions: set[str] = set()
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        lines = path.read_text(encoding=UTF8).splitlines()  # pragma: no mutate - None is UTF-8 on this host
    except OSError:
        return {"tokens": 0, "sessions": 0}
    for line in lines:
        try:
            entry = json.loads(line)
            context = entry.get("ctx")
            if not str(entry.get("ts")).startswith(today) or not isinstance(context, dict):
                continue
            tokens = sum(
                _number(context.get(key))
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "reasoning_tokens",
                    "cached_prompt_tokens",
                )
            )
            if tokens:
                total += tokens
                sessions.add(str(entry.get("sid") or f"request:{len(sessions)}"))
        except (TypeError, ValueError):
            continue
    return {"tokens": total, "sessions": len(sessions)}


def _grok_provider_key(home: Path) -> str:
    try:
        payload = json.loads((home / ".grok" / "auth.json").read_bytes())
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for scope, entry in payload.items():
        if str(scope).startswith("https://auth.x.ai::") and isinstance(entry, dict):
            return str(entry.get("key") or "")
    return ""


def _parse_grok_quota(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    config = payload.get("config")
    if not isinstance(config, dict):
        return None
    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        return None
    period_type = period.get("type")
    if period_type not in GROK_PERIOD_KINDS:
        return None
    period_info = GROK_PERIOD_KINDS[period_type]
    kind, label = period_info
    resets_at = str(period.get("end") or "")
    limits = []
    raw_percent = config.get("creditUsagePercent")
    used = None
    if raw_percent is None:
        used = 0.0
    elif isinstance(raw_percent, (int, float)) and not isinstance(raw_percent, bool) and 0 <= float(raw_percent) <= 100:
        used = float(raw_percent) / 100
    if used is not None:
        limits.append({
            "kind": kind,
            "label": label + " shared usage",
            "percent": used,
            "resetsAt": resets_at,
        })
    used_pct = max(0, min(100, round((used or 0.0) * 100)))
    status = f"{used_pct}% used" if limits else label + " percentage unavailable"
    if resets_at:
        status += " · resets " + resets_at
    return {
        "tierLabel": str(payload.get("subscriptionTier") or "Grok subscription"),
        "statusText": status,
        "limits": limits,
    }


def _fetch_grok_quota(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    request = urllib.request.Request(
        GROK_BILLING_URL,
        headers={
            AUTHORIZATION_HEADER: "Bearer " + token,
            XAI_TOKEN_AUTH_HEADER: XAI_TOKEN_AUTH_VALUE,
            ACCEPT_HEADER: JSON_MEDIA_TYPE,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError):
        return None
    return _parse_grok_quota(payload)


def _reset_iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _parse_zai_quota(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    limits = []
    raw_limits = data.get("limits")
    for item in raw_limits if isinstance(raw_limits, list) else []:
        if not isinstance(item, dict):
            continue
        signature = (item.get("type"), item.get("unit"), item.get("number"))
        if signature == ("TOKENS_LIMIT", 3, 5):
            kind, label = "session", "Session (5-hour)"
        elif item.get("type") == "TOKENS_LIMIT" and item.get("unit") == 6:
            kind, label = "weekly", "Weekly (7-day)"
        elif signature == ("TIME_LIMIT", 5, 1):
            kind, label = "monthly", "Monthly tools"
        else:
            continue
        try:
            percent = float(item.get("percentage")) / 100
        except (TypeError, ValueError):
            continue
        if not 0 <= percent <= 1:
            continue
        limits.append({
            "kind": kind,
            "label": label,
            "percent": percent,
            "resetsAt": _reset_iso(item.get("nextResetTime")),
        })
    level = str(data.get("level") or "").strip()
    return {
        "tierLabel": "GLM Coding " + level.title() if level else "GLM Coding Plan",
        "limits": limits,
    }


def _claude_window(payload: Any, key: str, kind: str, label: str) -> dict[str, Any] | None:
    bucket = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(bucket, dict):
        return None
    try:
        utilization = float(bucket.get("utilization"))
    except (TypeError, ValueError):
        return None
    if utilization < 0:
        return None
    percent = min(1.0, utilization / 100) if utilization > 1 or utilization == 1 else utilization
    if utilization >= 1:
        percent = min(1.0, utilization / 100)
    return {
        "kind": kind,
        "label": label,
        "percent": percent,
        "resetsAt": str(bucket.get("resets_at") or bucket.get("resetsAt") or ""),
    }


def _parse_claude_extra_usage(extra: Any) -> dict[str, Any] | None:
    if not isinstance(extra, dict) or extra.get("is_enabled") is False:
        return None
    used = extra.get("used_credits")
    limit = extra.get("monthly_limit")
    utilization = extra.get("utilization")
    percent = None
    try:
        if utilization is not None:
            percent = min(1.0, float(utilization) / 100)
    except (TypeError, ValueError):
        percent = None
    if percent is None and isinstance(used, (int, float)) and isinstance(limit, (int, float)) and float(limit) > 0:
        percent = min(1.0, float(used) / float(limit))
    if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and float(limit) > 0:
        used_usd, limit_usd = float(used), float(limit)
        if limit_usd >= 1000 and used_usd == int(used_usd) and limit_usd == int(limit_usd):
            used_usd, limit_usd = used_usd / 100, limit_usd / 100
            if percent is None:
                percent = min(1.0, used_usd / limit_usd)
        status = f"${used_usd:.2f} / ${limit_usd:.2f}"
        limits = []
        if percent is not None:
            limits.append({
                "kind": "monthly",
                "label": "Monthly spend",
                "percent": percent,
                "resetsAt": "",
            })
        return {"tierLabel": "Enterprise spend", "statusText": status, "limits": limits}
    if extra.get("is_enabled") and extra.get("monthly_limit") is None and isinstance(used, (int, float)):
        return {
            "tierLabel": "Enterprise spend",
            "statusText": f"${float(used):.2f} used · unlimited extra",
            "limits": [],
        }
    return None


def _parse_claude_usage(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    extra = _parse_claude_extra_usage(payload.get("extra_usage"))
    limits = list((extra or {}).get("limits") or [])
    for key, kind, label in (
        ("five_hour", "session", "Session (5-hour)"),
        ("seven_day", "weekly", "Weekly (7-day)"),
        ("seven_day_oauth_apps", "weekly", "Weekly (7-day)"),
    ):
        window = _claude_window(payload, key, kind, label)
        if window is not None and not any(item["kind"] == kind for item in limits):
            limits.append(window)
    if extra is None and not limits:
        return None
    return {
        "tierLabel": (extra or {}).get("tierLabel") or "Claude",
        "statusText": (extra or {}).get("statusText") or "",
        "limits": limits,
    }


def _parse_claude_spend(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("spendLimitCents") is not None or payload.get("usedCents") is not None:
        try:
            limit = float(payload.get("spendLimitCents"))
            used = float(payload.get("usedCents"))
        except (TypeError, ValueError):
            return None
        if limit <= 0 or used < 0:
            return None
        used_usd, limit_usd = used / 100, limit / 100
    else:
        try:
            limit_usd = float(payload.get("monthly_credit_limit"))
            used_usd = float(payload.get("used_credits"))
        except (TypeError, ValueError):
            return None
        if limit_usd <= 0 or used_usd < 0:
            return None
    return {
        "tierLabel": "Enterprise spend",
        "statusText": f"${used_usd:.2f} / ${limit_usd:.2f}",
        "limits": [{
            "kind": "monthly",
            "label": "Monthly spend",
            "percent": min(1.0, used_usd / limit_usd),
            "resetsAt": str(payload.get("disabledUntil") or payload.get("disabled_until") or ""),
        }],
    }


CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"


def _claude_oauth_login(home: Path) -> dict[str, Any]:
    try:
        payload = json.loads((home / ".claude" / ".credentials.json").read_bytes())
    except (OSError, ValueError):
        return {}
    login = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    return login if isinstance(login, dict) else {}


def _claude_access_token(home: Path) -> str:
    return str(_claude_oauth_login(home).get("accessToken") or "")


def _claude_refresh_secret(home: Path) -> str:
    return str(_claude_oauth_login(home).get("refreshToken") or "")


def _claude_org_id(runner: Any = subprocess.run) -> str:
    try:
        result = runner(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return ""
        payload = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return str(payload.get("orgId") or "") if isinstance(payload, dict) else ""


def _quota_cache_path(home: Path, name: str) -> Path:
    return home / ".cache" / "omarchy" / "agent-usage" / name


def _read_quota_cache(home: Path, name: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(_quota_cache_path(home, name).read_text(encoding=UTF8))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_quota_cache(home: Path, name: str, payload: dict[str, Any]) -> None:
    path = _quota_cache_path(home, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding=UTF8)
    except OSError:
        return


def _claude_usage_cache_path(home: Path) -> Path:
    return _quota_cache_path(home, "claude-extra.json")


def _read_claude_usage_cache(home: Path) -> dict[str, Any] | None:
    return _read_quota_cache(home, "claude-extra.json")


def _write_claude_usage_cache(home: Path, payload: dict[str, Any]) -> None:
    _write_quota_cache(home, "claude-extra.json", payload)


def _refresh_claude_access_token(home: Path) -> str:
    secret = _claude_refresh_secret(home)
    if not secret:
        return ""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": secret,
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
    }).encode(UTF8)
    request = urllib.request.Request(
        CLAUDE_OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "anthropic-beta": "oauth-2025-04-20",
            ACCEPT_HEADER: JSON_MEDIA_TYPE,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("access_token") or "")


def _fetch_claude_usage(token: str, attempts: int = 1, home: Path | None = None) -> dict[str, Any] | None:
    if not token:
        return None
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            AUTHORIZATION_HEADER: "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            ACCEPT_HEADER: JSON_MEDIA_TYPE,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 429 and home is not None:
            refreshed = _refresh_claude_access_token(home)
            if refreshed and refreshed != token:
                return _fetch_claude_usage(refreshed, attempts=1, home=None)
        return None
    except (OSError, ValueError):
        return None
    return _parse_claude_usage(payload)


def _fetch_claude_spend(home: Path, runner: Any = subprocess.run) -> dict[str, Any] | None:
    token = _claude_access_token(home)
    parsed = _fetch_claude_usage(token, home=home)
    if parsed is not None:
        _write_claude_usage_cache(home, parsed)
        return parsed
    cached = _read_claude_usage_cache(home)
    if cached is not None:
        return cached
    if not token:
        return None
    org = _claude_org_id(runner)
    if not org:
        return None
    request = urllib.request.Request(
        f"https://api.anthropic.com/api/oauth/organizations/{org}/overage_spend_limit",
        headers={
            AUTHORIZATION_HEADER: "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            ACCEPT_HEADER: JSON_MEDIA_TYPE,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError):
        return None
    return _parse_claude_spend(payload)


def _zai_provider_key(home: Path) -> str:
    try:
        config = json.loads((home / ".zcode" / "v2" / "config.json").read_text(encoding=UTF8))  # pragma: no mutate - explicit protocol encoding
    except (OSError, ValueError):
        return ""
    provider = (config.get("provider") or {}).get("builtin:zai-coding-plan") or {}
    options = provider.get("options") if isinstance(provider, dict) else {}
    return str((options or {}).get("apiKey") or "")


def _fetch_zai_quota(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    request = urllib.request.Request(
        ZAI_QUOTA_URL,
        headers={AUTHORIZATION_HEADER: token, ACCEPT_HEADER: JSON_MEDIA_TYPE},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode(UTF8, errors="replace"))  # pragma: no mutate - UTF-8 aliases/default are equivalent
    except (OSError, ValueError):
        return None
    return _parse_zai_quota(payload)


def _openrouter_secrets_path(home: Path | None = None) -> Path:
    root = Path(home or Path.home())
    return root / ".config" / "omarchy" / "plugins" / "blitz.ai" / "secrets.json"


def _openrouter_op_ref(home: Path | None = None) -> str:
    try:
        data = json.loads(_openrouter_secrets_path(home).read_text(encoding=UTF8))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    ref = str(data.get("openrouterOp") or data.get("openrouter_op") or "")
    return ref if ref.startswith("op://") else ""


def _openrouter_provider_key(
    environment: dict[str, str] | None = None,
    runner: Any = subprocess.run,
    home: Path | None = None,
) -> str:
    env = environment if environment is not None else os.environ
    if env.get("OPENROUTER_API_KEY"):
        return str(env["OPENROUTER_API_KEY"])
    ref = _openrouter_op_ref(home)
    if not ref:
        return ""
    try:
        result = runner(
            ["op", "read", ref],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _amount(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _parse_openrouter_key(payload: Any) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    limit = _amount(data.get("limit"))
    usage = _amount(data.get("usage"))
    remaining = _amount(data.get("limit_remaining"))
    reset = str(data.get("limit_reset") or "").lower()
    limits = []
    if limit > 0:
        kind = OPENROUTER_RESET_KINDS.get(reset, "monthly")
        limits.append({
            "kind": kind,
            "label": kind.title() + " spend",
            "percent": min(1.0, usage / limit),
            "resetsAt": "",
        })
    return {
        "tierLabel": "OpenRouter" + (" · " + reset + " cap" if reset else ""),
        "statusText": f"${usage:.2f} used" + (f" · ${remaining:.2f} left" if limit > 0 else ""),
        "limits": limits,
        "balance": {
            "limit": limit,
            "remaining": remaining,
            "usageTotal": usage,
            "usageWeekly": _amount(data.get("usage_weekly")),
            "usageMonthly": _amount(data.get("usage_monthly")),
        },
    }


def _cents(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    if amount < 0:
        return None
    return amount


def _parse_cursor_quota(payload: Any, membership: str = "") -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    plan = payload.get("planUsage")
    if not isinstance(plan, dict):
        return None
    limit = _cents(plan.get("limit"))
    if limit is None or limit <= 0:
        return None
    used = _cents(plan.get("includedSpend"))
    remaining = _cents(plan.get("remaining"))
    if used is None and remaining is not None:
        used = max(0.0, limit - remaining)
    if used is None:
        used = _cents(plan.get("totalSpend"))
    if used is None:
        return None
    if remaining is None:
        remaining = max(0.0, limit - used)
    used_usd, limit_usd, remaining_usd = used / 100, limit / 100, remaining / 100
    plan_name = str(membership or "").strip().title()
    return {
        "tierLabel": "Cursor " + plan_name if plan_name else "Cursor",
        "statusText": f"${used_usd:.2f} used · ${remaining_usd:.2f} left",
        "limits": [{
            "kind": "monthly",
            "label": "Monthly included",
            "percent": min(1.0, used / limit),
            "resetsAt": _reset_iso(payload.get("billingCycleEnd")),
        }],
        "balance": {
            "limit": limit_usd,
            "remaining": remaining_usd,
            "usageMonthly": used_usd,
            "usageWeekly": 0.0,
        },
    }


def _cursor_state_db(home: Path) -> Path:
    return home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _cursor_item(home: Path, key: str) -> str:
    path = _cursor_state_db(home)
    if not path.is_file():
        return ""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return ""
    try:
        row = conn.execute("SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    if not row:
        return ""
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode(UTF8, errors="replace")
    return str(value or "").strip()


def _cursor_session(home: Path) -> dict[str, str]:
    return {
        "token": _cursor_item(home, "cursorAuth/accessToken"),
        "membership": _cursor_item(home, "cursorAuth/stripeMembershipType"),
    }


def _fetch_cursor_quota(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    request = urllib.request.Request(
        CURSOR_USAGE_URL,
        data=b"{}",
        headers={
            AUTHORIZATION_HEADER: "Bearer " + token,
            ACCEPT_HEADER: JSON_MEDIA_TYPE,
            "Content-Type": JSON_MEDIA_TYPE,
            "Connect-Protocol-Version": "1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _fetch_openrouter_key(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    request = urllib.request.Request(
        OPENROUTER_KEY_URL,
        headers={AUTHORIZATION_HEADER: "Bearer " + token, ACCEPT_HEADER: JSON_MEDIA_TYPE},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError):
        return None
    return _parse_openrouter_key(payload)


def collect_records(home: Path | None = None) -> list[dict[str, Any]]:
    root = Path(home or Path.home())
    refresh_native = home is None or root.resolve() == Path.home().resolve()
    discovered = {account.provider_id: account for account in _discover(root)}
    openrouter_key = _openrouter_provider_key() if refresh_native else ""
    cursor_session = _cursor_session(root) if refresh_native else {"token": "", "membership": ""}
    detected_ids = set(discovered)
    if openrouter_key:
        detected_ids.add(OPENROUTER_ID)
    if cursor_session["token"]:
        detected_ids.add(CURSOR_ID)
    records = []
    for provider_id, label in REQUESTED_PROVIDERS:
        path = _record_path(root, provider_id)
        raw = _cached_native_record(provider_id, root) if refresh_native and provider_id in {"codex", "claude"} else None
        if raw is None:
            try:
                raw = json.loads(path.read_text(encoding=UTF8))  # pragma: no mutate - None is UTF-8 on this host
            except (OSError, ValueError):
                raw = {"status": "not-authoritative" if provider_id in detected_ids else "unavailable"}
        record = normalize_record(raw, provider_id)
        if not record["name"] or record["name"] == provider_id:
            record["name"] = label
        if provider_id == "claude" and refresh_native and not record["limits"]:
            spend = _fetch_claude_spend(root)
            if spend is not None:
                record["ready"] = True
                record["status"] = "ok"
                record["statusText"] = spend["statusText"]
                record["limits"] = spend["limits"]
                record["tierLabel"] = spend["tierLabel"]
                record["source"] = {"local": True, "authoritative": True}
        if provider_id == "zai" and provider_id in discovered:
            quota = _fetch_zai_quota(_zai_provider_key(root))
            if quota is not None:
                record["ready"] = True
                record["status"] = "ok"
                record["statusText"] = ""
                record["limits"] = quota["limits"]
                record["tierLabel"] = quota["tierLabel"]
                record["source"] = {"local": True, "authoritative": True}
            else:
                record["statusText"] = "Quota unavailable"
        if provider_id == "grok" and provider_id in discovered:
            stats = _grok_today(root)
            record["today"] = {"prompts": 0, "sessions": stats["sessions"], "tokens": stats["tokens"]}
            quota = _fetch_grok_quota(_grok_provider_key(root)) if refresh_native else None
            if quota is not None:
                record["ready"] = True
                record["status"] = "ok" if quota["limits"] else "not-authoritative"
                record["statusText"] = quota["statusText"]
                record["limits"] = quota["limits"]
                record["tierLabel"] = quota["tierLabel"]
            record["source"] = {"local": True, "authoritative": bool(record["limits"])}
        if provider_id == "openrouter":
            quota = _fetch_openrouter_key(openrouter_key) if openrouter_key else None
            if quota is not None:
                record["ready"] = True
                record["status"] = "ok"
                record["statusText"] = quota["statusText"]
                record["limits"] = quota["limits"]
                record["tierLabel"] = quota["tierLabel"]
                record["balance"] = quota["balance"]
                record["source"] = {"local": True, "authoritative": True}
                _write_quota_cache(root, "openrouter-extra.json", {
                    "statusText": quota["statusText"],
                    "tierLabel": quota["tierLabel"],
                    "limits": quota["limits"],
                    "balance": quota["balance"],
                })
            else:
                cached = _read_quota_cache(root, "openrouter-extra.json")
                if cached is not None:
                    record["ready"] = True
                    record["status"] = "ok"
                    record["statusText"] = str(cached.get("statusText") or "")
                    record["limits"] = cached.get("limits") if isinstance(cached.get("limits"), list) else []
                    record["tierLabel"] = str(cached.get("tierLabel") or record["tierLabel"])
                    record["balance"] = cached.get("balance") if isinstance(cached.get("balance"), dict) else None
                    record["source"] = {"local": True, "authoritative": True}
                elif openrouter_key:
                    record["statusText"] = "Usage unavailable"
        if provider_id == CURSOR_ID:
            payload = _fetch_cursor_quota(cursor_session["token"]) if cursor_session["token"] else None
            quota = _parse_cursor_quota(payload, cursor_session["membership"]) if payload else None
            if quota is not None:
                record["ready"] = True
                record["status"] = "ok"
                record["statusText"] = quota["statusText"]
                record["limits"] = quota["limits"]
                record["tierLabel"] = quota["tierLabel"]
                record["balance"] = quota["balance"]
                record["source"] = {"local": True, "authoritative": True}
                _write_quota_cache(root, "cursor-extra.json", {
                    "statusText": quota["statusText"],
                    "tierLabel": quota["tierLabel"],
                    "limits": quota["limits"],
                    "balance": quota["balance"],
                })
            else:
                cached = _read_quota_cache(root, "cursor-extra.json")
                if cached is not None:
                    record["ready"] = True
                    record["status"] = "ok"
                    record["statusText"] = str(cached.get("statusText") or "")
                    record["limits"] = cached.get("limits") if isinstance(cached.get("limits"), list) else []
                    record["tierLabel"] = str(cached.get("tierLabel") or record["tierLabel"])
                    record["balance"] = cached.get("balance") if isinstance(cached.get("balance"), dict) else None
                    record["source"] = {"local": True, "authoritative": True}
                elif cursor_session["token"]:
                    record["statusText"] = "Usage unavailable"
        records.append(record)
    return records


def _discover(home: Path) -> list[Any]:
    from provider_adapters import discover_local_accounts
    return discover_local_accounts(home=home)


def main() -> int:
    print(json.dumps({"providers": collect_records()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
