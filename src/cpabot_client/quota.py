from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .format import display_name, is_cooling, is_unhealthy
from .management import CPAError, ManagementClient

PLATFORM_ALIASES = {
    "claude": "claude",
    "anthropic": "claude",
    "codex": "codex",
    "gpt": "codex",
    "openai": "codex",
    "antigravity": "antigravity",
    "反重力": "antigravity",
    "kimi": "kimi",
    "xai": "xai",
    "x-ai": "xai",
    "grok": "xai",
    "gemini-cli": "gemini-cli",
    "gemini": "gemini-cli",
}

PLATFORM_TITLES = {
    "claude": "Claude",
    "codex": "Codex",
    "antigravity": "Antigravity",
    "kimi": "Kimi",
    "xai": "xAI / Grok",
    "gemini-cli": "Gemini CLI",
    "other": "其他",
}

PLATFORM_ORDER = ("claude", "codex", "antigravity", "kimi", "xai", "gemini-cli", "other")

CLAUDE_WINDOWS = (
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d Opus"),
    ("seven_day_sonnet", "7d Sonnet"),
    ("seven_day_oauth_apps", "7d OAuth"),
    ("seven_day_cowork", "7d Cowork"),
    ("iguana_necktie", "7d Fable"),
)

ANTIGRAVITY_URLS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
)

ANTIGRAVITY_PLAN_URLS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist",
)

ANTIGRAVITY_PLAN_BODY = '{"metadata":{"ideType":"ANTIGRAVITY"}}'

_AUTH_MECHANISMS = {"oauth", "api_key", "api-key", "apikey", "session", "token"}

_PLAN_BY_TIER_ID = {
    "free-tier": "Free",
    "g1-pro-tier": "Pro",
    "g1-plus-tier": "Plus",
    "g1-ultra-tier": "Ultra",
    "g1-ultra-lite-tier": "Ultra Lite",
    "legacy-tier": "Legacy",
}

WINDOW_ORDER = (
    "gemini-5h",
    "gemini-week",
    "gemini-month",
    "claude-gpt-5h",
    "claude-gpt-week",
    "claude-gpt-month",
    "code-5h",
    "code-7d",
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_oauth_apps",
    "seven_day_cowork",
    "iguana_necktie",
    "extra",
    "billing",
    "grok-build",
    "grok-chat",
    "usage",
)

_WINDOW_5H = 5 * 60 * 60
_WINDOW_7D = 7 * 24 * 60 * 60


@dataclass
class QuotaWindow:
    id: str
    label: str
    used_percent: float | None = None
    remaining_percent: float | None = None
    remaining: float | None = None
    limit: float | None = None
    reset_label: str = "-"


@dataclass
class AccountQuota:
    platform: str
    name: str
    auth_index: str
    plan: str = ""
    status: str = "unknown"
    error: str = ""
    windows: list[QuotaWindow] = field(default_factory=list)
    disabled: bool = False
    cooling: bool = False
    client_name: str = ""


@dataclass
class PlatformQuota:
    platform: str
    title: str
    accounts: list[AccountQuota]
    window_remain_sum: dict[str, float]
    window_remain_count: dict[str, int]
    window_labels: dict[str, str] = field(default_factory=dict)
    remaining_sum: float = 0.0
    limit_sum: float = 0.0


@dataclass
class QuotaBoard:
    platforms: list[PlatformQuota]
    queried: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    cached: bool = False


_cache_key = ""
_cache_expires = 0.0
_cache_board: QuotaBoard | None = None


def normalize_platform(value: str) -> str:
    raw = value.strip().lower().replace("_", "-")
    return PLATFORM_ALIASES.get(raw, "") or PLATFORM_ALIASES.get(value.strip(), "")


def platform_of(file: dict[str, Any]) -> str:
    raw = str(file.get("provider") or file.get("type") or "").strip().lower().replace("_", "-")
    return PLATFORM_ALIASES.get(raw, "other")


def is_platform_query(value: str) -> bool:
    return bool(normalize_platform(value))


def peek_quota_cache(
    files: list[dict[str, Any]],
    *,
    platform: str | None = None,
    skip_disabled: bool = True,
    ttl: float = 60.0,
) -> QuotaBoard | None:
    wanted = _wanted_files(files, platform=platform, skip_disabled=skip_disabled)
    cache_id = _cache_id(wanted, platform)
    now = time.monotonic()
    if _cache_board and _cache_key == cache_id and now < _cache_expires:
        _cache_board.cached = True
        return _cache_board
    if platform and _cache_board and now < _cache_expires:
        full = _wanted_files(files, platform=None, skip_disabled=skip_disabled)
        if _cache_key == _cache_id(full, None):
            reports = [
                account
                for section in _cache_board.platforms
                if section.platform == platform
                for account in section.accounts
            ]
            if reports:
                board = _build_board(reports)
                board.cached = True
                return board
    return None


async def collect_quotas(
    files: list[dict[str, Any]],
    *,
    platform: str | None = None,
    force: bool = False,
    skip_disabled: bool = True,
    client: ManagementClient,
    cfg: Config,
) -> QuotaBoard:
    wanted = _wanted_files(files, platform=platform, skip_disabled=skip_disabled)
    cache_id = _cache_id(wanted, platform)
    now = time.monotonic()
    ttl = cfg.cpa_quota_cache_ttl
    global _cache_key, _cache_expires, _cache_board
    if not force and _cache_board and _cache_key == cache_id and now < _cache_expires:
        _cache_board.cached = True
        return _cache_board

    sem = asyncio.Semaphore(max(1, cfg.cpa_quota_concurrency))
    reports = await asyncio.gather(
        *(_one_account(client, cfg, item, sem) for item in wanted)
    )
    board = _build_board(list(reports))
    _cache_key = cache_id
    _cache_expires = now + max(0.0, ttl)
    _cache_board = board
    return board


def clear_quota_cache() -> None:
    global _cache_key, _cache_expires, _cache_board
    _cache_key = ""
    _cache_expires = 0.0
    _cache_board = None


def stamp_client(board: QuotaBoard, client_name: str) -> QuotaBoard:
    for section in board.platforms:
        for account in section.accounts:
            account.client_name = client_name
    return board


def board_from_accounts(accounts: list[AccountQuota], *, cached: bool = False) -> QuotaBoard:
    board = _build_board(accounts)
    board.cached = cached
    return board


def accounts_from_result(data: dict[str, Any] | Any) -> list[AccountQuota]:
    from .protocol import QuotaQueryResult

    result = data if isinstance(data, QuotaQueryResult) else QuotaQueryResult.model_validate(data)
    accounts: list[AccountQuota] = []
    for item in result.accounts:
        accounts.append(
            AccountQuota(
                platform=item.platform,
                name=item.name,
                auth_index=item.auth_index,
                plan=item.plan,
                status=item.status,
                error=item.error,
                windows=[
                    QuotaWindow(
                        id=window.id,
                        label=window.label,
                        used_percent=window.used_percent,
                        remaining_percent=window.remaining_percent,
                        remaining=window.remaining,
                        limit=window.limit,
                        reset_label=window.reset_label,
                    )
                    for window in item.windows
                ],
                disabled=item.disabled,
                cooling=item.cooling,
                client_name=item.client_name or result.client_name,
            )
        )
    return accounts


def format_quota_board(board: QuotaBoard, *, account_limit: int = 12) -> list[str]:
    if not board.platforms:
        return ["没有可展示的额度账号。"]
    head = [
        "额度总览（按平台）",
        f"查询 {board.queried} | 成功 {board.ok} | 失败 {board.failed} | 仅健康 {board.skipped}"
        + (" | 缓存" if board.cached else ""),
        "合计格式：窗口 剩余当量/账号数（均剩%）。1.00 = 满额一个号，不要把各账号百分比直接相加。",
    ]
    chunks: list[str] = []
    current = list(head)
    for section in board.platforms:
        block = _format_platform(section, account_limit)
        if sum(len(line) + 1 for line in current) + sum(len(line) + 1 for line in block) > 3400:
            chunks.append("\n".join(current))
            current = block
        else:
            if current is not head and current:
                current.append("")
            current.extend(block)
    if current:
        chunks.append("\n".join(current))
    return chunks or ["没有可展示的额度账号。"]


def platform_total_chips(section: PlatformQuota) -> list[str]:
    account_n = len(section.accounts)
    chips: list[str] = []
    ordered = sort_windows(
        [
            QuotaWindow(id=window_id, label=section.window_labels.get(window_id, window_id))
            for window_id in section.window_remain_sum
        ]
    )
    for window in ordered:
        window_id = window.id
        total = section.window_remain_sum[window_id]
        count = section.window_remain_count.get(window_id, 0)
        denom = count or account_n
        equiv = total / 100.0
        avg = (equiv / denom * 100.0) if denom else 0.0
        label = section.window_labels.get(window_id, window_id)
        chips.append(f"{label} {equiv:.2f}/{denom} ({avg:.0f}%)")
    if section.limit_sum:
        chips.append(f"绝对剩余 {section.remaining_sum:.0f}/{section.limit_sum:.0f}")
    return chips


def format_account_quota(account: AccountQuota) -> str:
    lines = [f"[{PLATFORM_TITLES.get(account.platform, account.platform)}] {account.name}"]
    if account.plan:
        lines.append(f"套餐：{account.plan}")
    lines.append(f"状态：{account.status}")
    if account.error:
        lines.append(f"错误：{account.error}")
    if account.windows:
        lines.append("额度：")
        for window in account.windows:
            lines.append(f"  {_window_text(window)}")
    return "\n".join(lines)


async def _one_account(
    client: ManagementClient,
    cfg: Config,
    file: dict[str, Any],
    sem: asyncio.Semaphore,
) -> AccountQuota:
    platform = platform_of(file)
    report = AccountQuota(
        platform=platform,
        name=display_name(file, public=True),
        auth_index=str(file.get("auth_index") or ""),
        plan=_plan_from_auth_file(file),
        status=str(file.get("status") or "unknown"),
        disabled=bool(file.get("disabled")),
        cooling=is_cooling(file),
    )
    if is_unhealthy(file) and report.status in {"ready", "ok", "active", "unknown"}:
        report.status = "cooling" if report.cooling else str(file.get("status") or "error")
    if not report.auth_index or platform == "other":
        return report
    async with sem:
        try:
            if platform == "claude":
                await _fill_claude(client, cfg, report)
            elif platform == "codex":
                await _fill_codex(client, cfg, file, report)
            elif platform == "kimi":
                await _fill_kimi(client, cfg, report)
            elif platform == "xai":
                await _fill_xai(client, cfg, report)
            elif platform == "antigravity":
                await _fill_antigravity(client, cfg, report)
            elif platform == "gemini-cli":
                await _fill_gemini(client, cfg, report)
        except CPAError as exc:
            report.error = str(exc)
            report.status = "error"
    return report


async def _fill_claude(client: ManagementClient, cfg: Config, report: AccountQuota) -> None:
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "GET",
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    if payload is None:
        return
    windows, plan = parse_claude_usage(payload)
    report.windows = sort_windows(windows)
    report.status = _status_from_windows(report.windows, report)
    report.plan = _sanitize_plan(plan) or report.plan
    report.error = ""


async def _fill_codex(
    client: ManagementClient,
    cfg: Config,
    file: dict[str, Any],
    report: AccountQuota,
) -> None:
    headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
    }
    account_id = _first_str(
        file.get("chatgpt_account_id"),
        file.get("chatgptAccountId"),
        file.get("account_id"),
        file.get("accountId"),
        file.get("account"),
    )
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "GET",
        "https://chatgpt.com/backend-api/wham/usage",
        headers,
    )
    if payload is None:
        return
    windows, plan = parse_codex_usage(payload)
    report.windows = sort_windows(windows)
    report.status = _status_from_windows(report.windows, report)
    report.plan = _sanitize_plan(plan) or report.plan
    report.error = ""


async def _fill_kimi(client: ManagementClient, cfg: Config, report: AccountQuota) -> None:
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "GET",
        "https://api.kimi.com/coding/v1/usages",
        {"Authorization": "Bearer $TOKEN$"},
    )
    if payload is None:
        return
    report.windows = sort_windows(parse_kimi_usage(payload))
    report.status = _status_from_windows(report.windows, report)
    report.error = ""


async def _fill_xai(client: ManagementClient, cfg: Config, report: AccountQuota) -> None:
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "GET",
        "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
        {
            "Authorization": "Bearer $TOKEN$",
            "x-xai-token-auth": "xai-grok-cli",
            "x-grok-client-version": "0.2.91",
            "accept": "*/*",
        },
    )
    if payload is None:
        return
    report.windows = sort_windows(parse_xai_billing(payload))
    report.status = _status_from_windows(report.windows, report)
    report.error = ""


async def _fill_antigravity(client: ManagementClient, cfg: Config, report: AccountQuota) -> None:
    headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/cli/1.0.13 (aidev_client; os_type=darwin; arch=arm64)",
    }
    payload = None
    for url in ANTIGRAVITY_URLS:
        payload = await _upstream_json(client, cfg, report, "POST", url, headers, data="{}")
        if payload is not None:
            break
    if payload is None:
        return
    report.windows = sort_windows(parse_antigravity_summary(payload))
    report.status = _status_from_windows(report.windows, report)
    report.error = ""
    plan = await _antigravity_plan(client, cfg, report)
    if plan:
        report.plan = plan


async def _fill_gemini(client: ManagementClient, cfg: Config, report: AccountQuota) -> None:
    payload = await _upstream_json(
        client,
        cfg,
        report,
        "POST",
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
        },
        data="{}",
    )
    if payload is None:
        return
    windows: list[QuotaWindow] = []
    models = payload.get("models") or payload.get("quotas") or []
    if isinstance(models, list):
        for item in models[:8]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("name") or item.get("model") or item.get("displayName") or "模型")
            remain = _first_number(item.get("remainingFraction"), item.get("remaining"))
            limit = _first_number(item.get("limit"), item.get("quota"))
            used = _first_number(item.get("used"), item.get("usedPercent"))
            window = QuotaWindow(id=label, label=label, reset_label=_iso_reset(item.get("resetTime") or item.get("resetAt")))
            if remain is not None and remain <= 1:
                window.remaining_percent = _clamp(remain * 100.0)
                window.used_percent = _clamp(100.0 - window.remaining_percent)
            elif used is not None and used <= 100:
                window.used_percent = _clamp(used)
                window.remaining_percent = _clamp(100.0 - window.used_percent)
            elif remain is not None and limit:
                window.remaining = remain
                window.limit = limit
                window.remaining_percent = _clamp(remain / limit * 100.0)
                window.used_percent = _clamp(100.0 - window.remaining_percent)
            windows.append(window)
    report.windows = sort_windows(windows)
    report.status = _status_from_windows(report.windows, report)
    report.error = ""


def parse_claude_usage(payload: dict[str, Any]) -> tuple[list[QuotaWindow], str]:
    windows: list[QuotaWindow] = []
    for key, label in CLAUDE_WINDOWS:
        item = payload.get(key)
        if not isinstance(item, dict):
            continue
        used = _number(item.get("utilization"))
        window = QuotaWindow(id=key, label=label, reset_label=_iso_reset(item.get("resets_at")))
        if used is not None:
            window.used_percent = _clamp(used)
            window.remaining_percent = _clamp(100.0 - window.used_percent)
        windows.append(window)
    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = _number(extra.get("utilization"))
        window = QuotaWindow(id="extra", label="Extra", reset_label="-")
        if used is not None:
            window.used_percent = _clamp(used)
            window.remaining_percent = _clamp(100.0 - window.used_percent)
        windows.append(window)
    return sort_windows(windows), _sanitize_plan(payload.get("plan_type") or payload.get("planType"))


def parse_codex_usage(payload: dict[str, Any]) -> tuple[list[QuotaWindow], str]:
    plan = _first_str(payload.get("plan_type"), payload.get("planType"))
    rate = _as_dict(payload.get("rate_limit") or payload.get("rateLimit"))
    five, weekly = _codex_windows(rate)
    windows: list[QuotaWindow] = []
    if five:
        windows.append(_codex_window("code-5h", "5h", five, rate))
    if weekly:
        windows.append(_codex_window("code-7d", "周", weekly, rate))
    return sort_windows([item for item in windows if item]), _sanitize_plan(plan)


def parse_kimi_usage(payload: dict[str, Any]) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    usage = payload.get("usage")
    if isinstance(usage, dict):
        windows.append(_kimi_window("usage", "用量", usage))
    limits = payload.get("limits")
    if isinstance(limits, list):
        for index, item in enumerate(limits):
            if not isinstance(item, dict):
                continue
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else item
            label = str(item.get("title") or item.get("name") or item.get("scope") or f"限额{index + 1}")
            windows.append(_kimi_window(f"limit-{index}", label, detail if isinstance(detail, dict) else item))
    return sort_windows(
        [item for item in windows if item.used_percent is not None or item.remaining is not None]
    )


def parse_xai_billing(payload: dict[str, Any]) -> list[QuotaWindow]:
    config = _as_dict(payload.get("config")) or payload
    windows: list[QuotaWindow] = []
    weekly_used = _first_number(
        config.get("creditUsagePercent"),
        config.get("credit_usage_percent"),
        config.get("usedPercent"),
        payload.get("creditUsagePercent"),
    )
    weekly = QuotaWindow(
        id="billing",
        label="周",
        reset_label=_human_reset(
            config.get("billingPeriodEnd") or config.get("billing_period_end") or payload.get("billingPeriodEnd")
        ),
    )
    if weekly_used is not None:
        weekly.used_percent = _clamp(weekly_used)
        weekly.remaining_percent = _clamp(100.0 - weekly.used_percent)
        windows.append(weekly)
    for window_id, label, keys in (
        ("grok-build", "GrokBuild", ("grokBuildUsagePercent", "grok_build_usage_percent", "grokBuildUsage")),
        ("grok-chat", "GrokChat", ("grokChatUsagePercent", "grok_chat_usage_percent", "grokChatUsage")),
    ):
        used = None
        for key in keys:
            used = _number(config.get(key))
            if used is None:
                used = _number(payload.get(key))
            if used is not None:
                break
        if used is None:
            continue
        window = QuotaWindow(id=window_id, label=label)
        window.used_percent = _clamp(used)
        window.remaining_percent = _clamp(100.0 - window.used_percent)
        windows.append(window)
    products = payload.get("products") or config.get("products") or payload.get("usages")
    if isinstance(products, list):
        for item in products:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("product") or item.get("title") or "").strip()
            used = _first_number(item.get("usagePercent"), item.get("usedPercent"), item.get("creditUsagePercent"))
            if not name or used is None:
                continue
            key = name.lower().replace(" ", "")
            window_id = "grok-build" if "build" in key else "grok-chat" if "chat" in key else key
            if any(existing.id == window_id for existing in windows):
                continue
            window = QuotaWindow(id=window_id, label=name)
            window.used_percent = _clamp(used)
            window.remaining_percent = _clamp(100.0 - window.used_percent)
            windows.append(window)
    return sort_windows(windows)


def parse_antigravity_summary(payload: dict[str, Any]) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("displayName") or group.get("display_name") or "配额")
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            raw_label = str(
                bucket.get("displayName") or bucket.get("display_name") or bucket.get("window") or group_name
            )
            window_id, label = _antigravity_window(group_name, raw_label)
            remain = _first_number(bucket.get("remainingFraction"), bucket.get("remaining_fraction"))
            window = QuotaWindow(
                id=window_id,
                label=label,
                reset_label=_iso_reset(bucket.get("resetTime") or bucket.get("reset_time")),
            )
            if remain is not None:
                frac = remain if remain <= 1 else remain / 100.0
                window.remaining_percent = _clamp(frac * 100.0)
                window.used_percent = _clamp(100.0 - window.remaining_percent)
            windows.append(window)
    return sort_windows(windows)


def parse_antigravity_plan(payload: dict[str, Any]) -> str:
    current = payload.get("currentTier") or payload.get("current_tier")
    paid = payload.get("paidTier") or payload.get("paid_tier")
    current_tier = _as_dict(current) if not isinstance(current, str) else {"name": current, "id": current}
    paid_tier = _as_dict(paid) if not isinstance(paid, str) else {"name": paid, "id": paid}
    effective = paid_tier if paid_tier and (paid_tier.get("id") or paid_tier.get("name")) else current_tier
    if not effective:
        return ""
    tier_id = _first_str(effective.get("id"), effective.get("tierId"), effective.get("tier_id"))
    mapped = _PLAN_BY_TIER_ID.get(tier_id.lower()) if tier_id else ""
    if mapped:
        return mapped
    name = _first_str(effective.get("name"), effective.get("displayName"), effective.get("description"))
    return _plan_from_name(name) or _sanitize_plan(name)


def sort_windows(windows: list[QuotaWindow]) -> list[QuotaWindow]:
    indexed = {wid: index for index, wid in enumerate(WINDOW_ORDER)}

    def _key(window: QuotaWindow) -> tuple[int, int, str]:
        return (_window_span(window), indexed.get(window.id, len(WINDOW_ORDER)), window.label)

    return sorted(windows, key=_key)


def format_reset_zh(reset_label: str) -> str:
    text = (reset_label or "").strip()
    if not text or text == "-":
        return ""
    if text == "已过期":
        return "额度已过期"
    parts: list[str] = []
    for amount, unit, zh in (
        (r"(\d+)\s*d", "d", "天"),
        (r"(\d+)\s*h", "h", "小时"),
        (r"(\d+)\s*m", "m", "分"),
    ):
        match = re.search(amount, text, re.I)
        if match:
            parts.append(f"{int(match.group(1))}{zh}")
    if parts:
        return "在 " + "".join(parts) + " 后刷新额度"
    return f"在 {text} 后刷新额度"


def _window_span(window: QuotaWindow) -> int:
    blob = f"{window.id} {window.label}".lower()
    if any(token in blob for token in ("5h", "five", "hour", "滚动", "rolling")):
        return 0
    if any(token in blob for token in ("week", "weekly", "7d", "seven", "周")):
        return 1
    if any(token in blob for token in ("month", "monthly", "月")):
        return 2
    return 3


def _plan_from_auth_file(file: dict[str, Any]) -> str:
    subscription = file.get("subscription")
    if isinstance(subscription, dict):
        plan = parse_antigravity_plan({"paidTier": subscription, "currentTier": subscription})
        if plan:
            return plan
        plan = _sanitize_plan(subscription.get("plan") or subscription.get("tierName") or subscription.get("name"))
        if plan:
            return plan
    for key in ("plan", "plan_type", "planType", "tier", "tier_name", "tierName"):
        plan = _sanitize_plan(file.get(key))
        if plan:
            return plan
    return ""


def _plan_from_name(name: str) -> str:
    blob = name.lower().replace("_", " ").replace("-", " ")
    if "ultra lite" in blob or "ultralite" in blob:
        return "Ultra Lite"
    if "ultra" in blob:
        return "Ultra"
    if "plus" in blob:
        return "Plus"
    if "pro" in blob:
        return "Pro"
    if "legacy" in blob:
        return "Legacy"
    if "free" in blob:
        return "Free"
    return ""


def _sanitize_plan(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" in text:
        return ""
    if text.lower().replace("_", "-") in _AUTH_MECHANISMS:
        return ""
    mapped = _PLAN_BY_TIER_ID.get(text.lower())
    if mapped:
        return mapped
    named = _plan_from_name(text)
    return named or text


async def _antigravity_plan(client: ManagementClient, cfg: Config, report: AccountQuota) -> str:
    headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/cli/1.0.13 (aidev_client; os_type=darwin; arch=arm64)",
    }
    saved_error = report.error
    saved_status = report.status
    try:
        for url in ANTIGRAVITY_PLAN_URLS:
            payload = await _upstream_json(client, cfg, report, "POST", url, headers, data=ANTIGRAVITY_PLAN_BODY)
            if payload is None:
                continue
            plan = parse_antigravity_plan(payload)
            if plan:
                return plan
        return ""
    finally:
        report.error = saved_error
        report.status = saved_status


def _antigravity_window(group_name: str, bucket_name: str) -> tuple[str, str]:
    group = group_name.lower()
    bucket = bucket_name.lower()
    is_gemini = "gemini" in group
    is_claude = "claude" in group or "gpt" in group
    is_5h = "five" in bucket or "5h" in bucket or "5-hour" in bucket or "hour" in bucket
    is_week = "week" in bucket or "weekly" in bucket or "seven" in bucket
    is_month = "month" in bucket or "monthly" in bucket or "30d" in bucket
    if is_gemini and is_5h:
        return "gemini-5h", "Gemini 5h"
    if is_gemini and is_week:
        return "gemini-week", "Gemini 周"
    if is_gemini and is_month:
        return "gemini-month", "Gemini 月"
    if is_claude and is_5h:
        return "claude-gpt-5h", "Claude/GPT 5h"
    if is_claude and is_week:
        return "claude-gpt-week", "Claude/GPT 周"
    if is_claude and is_month:
        return "claude-gpt-month", "Claude/GPT 月"
    return f"{group_name}-{bucket_name}", f"{group_name} {bucket_name}"


async def _upstream_json(
    client: ManagementClient,
    cfg: Config,
    report: AccountQuota,
    method: str,
    url: str,
    header: dict[str, str],
    data: str | None = None,
) -> dict[str, Any] | None:
    try:
        response = await client.api_call(
            report.auth_index,
            method,
            url,
            header=header,
            data=data,
            timeout=cfg.cpa_quota_timeout,
        )
    except CPAError as exc:
        report.error = str(exc)
        report.status = "error"
        return None
    status = int(response.get("status_code") or response.get("statusCode") or 0)
    body = _parse_body(response.get("body"))
    if status and not (200 <= status < 300):
        report.error = f"上游 HTTP {status}"
        report.status = "error"
        return None
    if body is None:
        report.error = "上游返回无法解析"
        report.status = "error"
        return None
    return body


def _build_board(reports: list[AccountQuota]) -> QuotaBoard:
    grouped: dict[str, list[AccountQuota]] = {}
    for report in reports:
        grouped.setdefault(report.platform, []).append(report)
    platforms: list[PlatformQuota] = []
    ok = failed = skipped = 0
    for key in PLATFORM_ORDER:
        accounts = grouped.pop(key, [])
        if not accounts:
            continue
        remain_sum: dict[str, float] = {}
        remain_count: dict[str, int] = {}
        labels: dict[str, str] = {}
        remaining_sum = 0.0
        limit_sum = 0.0
        for account in accounts:
            if account.error:
                failed += 1
            elif account.windows:
                ok += 1
            else:
                skipped += 1
            for window in account.windows:
                labels.setdefault(window.id, window.label)
                if window.remaining_percent is not None:
                    remain_sum[window.id] = remain_sum.get(window.id, 0.0) + window.remaining_percent
                    remain_count[window.id] = remain_count.get(window.id, 0) + 1
                if window.remaining is not None:
                    remaining_sum += window.remaining
                if window.limit is not None:
                    limit_sum += window.limit
        platforms.append(
            PlatformQuota(
                platform=key,
                title=PLATFORM_TITLES.get(key, key),
                accounts=accounts,
                window_remain_sum=remain_sum,
                window_remain_count=remain_count,
                window_labels=labels,
                remaining_sum=remaining_sum,
                limit_sum=limit_sum,
            )
        )
    for leftover, accounts in sorted(grouped.items()):
        platforms.append(
            PlatformQuota(
                platform=leftover,
                title=leftover,
                accounts=accounts,
                window_remain_sum={},
                window_remain_count={},
            )
        )
        skipped += len(accounts)
    return QuotaBoard(
        platforms=platforms,
        queried=len(reports),
        ok=ok,
        failed=failed,
        skipped=skipped,
    )


def _format_platform(section: PlatformQuota, account_limit: int) -> list[str]:
    account_n = len(section.accounts)
    lines = [f"【{section.title}】{account_n} 账号"]
    totals = platform_total_chips(section)
    if totals:
        lines.append("  合计：" + " · ".join(totals))
    visible = section.accounts[:account_limit]
    for account in visible:
        flags = []
        if account.disabled:
            flags.append("disabled")
        if account.cooling:
            flags.append("cooling")
        flag = f" [{' '.join(flags)}]" if flags else ""
        plan = f" ({account.plan})" if account.plan else ""
        if account.error:
            lines.append(f"  {account.name}{plan}{flag}  失败：{account.error}")
            continue
        if not account.windows:
            lines.append(f"  {account.name}{plan}{flag}  {account.status}（无上游额度）")
            continue
        windows = " · ".join(_window_text(window, compact=True) for window in account.windows[:4])
        lines.append(f"  {account.name}{plan}{flag}  {windows}")
    extra = len(section.accounts) - len(visible)
    if extra > 0:
        lines.append(f"  ... 另有 {extra} 个账号，用 cpa quota {section.platform} 查看")
    return lines


def _window_text(window: QuotaWindow, *, compact: bool = False) -> str:
    if window.remaining_percent is not None:
        body = f"剩 {window.remaining_percent:.0f}%"
    elif window.used_percent is not None:
        body = f"已用 {window.used_percent:.0f}%"
    elif window.remaining is not None and window.limit is not None:
        body = f"{window.remaining:.0f}/{window.limit:.0f}"
    else:
        body = "?"
    if compact:
        reset = f" →{window.reset_label}" if window.reset_label and window.reset_label != "-" else ""
        return f"{window.label} {body}{reset}"
    reset = f"  重置 {window.reset_label}" if window.reset_label and window.reset_label != "-" else ""
    return f"{window.label} {body}{reset}"


def _status_from_windows(windows: list[QuotaWindow], report: AccountQuota) -> str:
    if report.disabled:
        return "disabled"
    percents = [w.remaining_percent for w in windows if w.remaining_percent is not None]
    if not percents:
        return report.status or "unknown"
    lowest = min(percents)
    if lowest <= 0:
        return "exhausted"
    if lowest <= 15:
        return "low"
    if report.cooling:
        return "cooling"
    return "ok"


def _codex_windows(rate: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not rate:
        return None, None
    primary = _as_dict(rate.get("primary_window") or rate.get("primaryWindow"))
    secondary = _as_dict(rate.get("secondary_window") or rate.get("secondaryWindow"))
    five = weekly = None
    for candidate in (primary, secondary):
        if not candidate:
            continue
        duration = _first_number(candidate.get("limit_window_seconds"), candidate.get("limitWindowSeconds")) or 0
        if duration == _WINDOW_5H and five is None:
            five = candidate
        if duration == _WINDOW_7D and weekly is None:
            weekly = candidate
    if five is None:
        five = primary
    if weekly is None:
        weekly = secondary
    return five, weekly


def _codex_window(window_id: str, label: str, window: dict[str, Any], rate: dict[str, Any] | None) -> QuotaWindow:
    used = _first_number(window.get("used_percent"), window.get("usedPercent"))
    item = QuotaWindow(id=window_id, label=label, reset_label=_codex_reset(window))
    if used is not None:
        item.used_percent = _clamp(used)
        item.remaining_percent = _clamp(100.0 - item.used_percent)
    elif rate and (rate.get("limit_reached") or rate.get("limitReached")):
        item.used_percent = 100.0
        item.remaining_percent = 0.0
    return item


def _kimi_window(window_id: str, label: str, data: dict[str, Any]) -> QuotaWindow:
    used = _number(data.get("used"))
    limit = _number(data.get("limit"))
    remaining = _number(data.get("remaining"))
    window = QuotaWindow(
        id=window_id,
        label=label,
        remaining=remaining,
        limit=limit,
        reset_label=_iso_reset(data.get("resetAt") or data.get("reset_at") or data.get("resetTime")),
    )
    if remaining is not None and limit:
        window.remaining_percent = _clamp(remaining / limit * 100.0)
        window.used_percent = _clamp(100.0 - window.remaining_percent)
    elif used is not None and limit:
        window.used_percent = _clamp(used / limit * 100.0)
        window.remaining_percent = _clamp(100.0 - window.used_percent)
        window.remaining = (limit - used) if remaining is None else remaining
    return window


def _codex_reset(window: dict[str, Any]) -> str:
    ts = _first_number(window.get("reset_at"), window.get("resetAt"))
    if ts:
        return _relative_from_ts(ts / 1000.0 if ts > 1e12 else ts)
    secs = _first_number(window.get("reset_after_seconds"), window.get("resetAfterSeconds"))
    if secs:
        return _relative_from_ts(time.time() + secs)
    return "-"


def _iso_reset(value: Any) -> str:
    return _human_reset(value)


def _human_reset(value: Any) -> str:
    if value is None or value == "" or value == "-":
        return "-"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        return _relative_from_ts(ts / 1000.0 if ts > 1e12 else ts)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _relative_from_ts(parsed.timestamp())
    except ValueError:
        return text[:16]


def _relative_from_ts(ts: float) -> str:
    delta = ts - time.time()
    if delta <= 0:
        return "已过期"
    secs = int(delta)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"


def _wanted_files(
    files: list[dict[str, Any]],
    *,
    platform: str | None,
    skip_disabled: bool,
) -> list[dict[str, Any]]:
    wanted: list[dict[str, Any]] = []
    for item in files:
        if platform is not None and platform_of(item) != platform:
            continue
        if skip_disabled and item.get("disabled"):
            continue
        wanted.append(item)
    return wanted


def _cache_id(files: list[dict[str, Any]], platform: str | None) -> str:
    return f"{platform or '*'}:{len(files)}:{_file_stamp(files)}"


def _parse_body(body: Any) -> dict[str, Any] | None:
    if isinstance(body, dict):
        return body
    if isinstance(body, str) and body.strip():
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict) and "val" in value:
        return _number(value.get("val"))
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _first_str(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _file_stamp(files: list[dict[str, Any]]) -> str:
    parts = [str(item.get("auth_index") or item.get("name") or "") for item in files]
    parts.sort()
    return ",".join(parts[:80])
