from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .config import Config
from .format import match_auth
from .management import CPAError, ManagementClient
from .protocol import (
    ALLOWED_ACTIONS,
    AccountQuotaDTO,
    CodexRefreshPayload,
    CodexRefreshResult,
    Envelope,
    PROTOCOL_VERSION,
    QuotaQueryPayload,
    QuotaQueryResult,
    QuotaWindowDTO,
)
from .quota import (
    AccountQuota,
    collect_quotas,
    consume_codex_reset,
    is_platform_query,
    normalize_platform,
    platform_of,
    stamp_client,
)


def account_to_dto(account: AccountQuota) -> AccountQuotaDTO:
    return AccountQuotaDTO(
        platform=account.platform,
        name=account.name,
        auth_index="",
        plan=account.plan,
        status=account.status,
        error=account.error,
        windows=[
            QuotaWindowDTO(
                id=window.id,
                label=window.label,
                used_percent=window.used_percent,
                remaining_percent=window.remaining_percent,
                remaining=window.remaining,
                limit=window.limit,
                reset_label=window.reset_label,
                reset_at=window.reset_at,
            )
            for window in account.windows
        ],
        disabled=account.disabled,
        cooling=account.cooling,
        client_name=account.client_name,
        subscription_expires_at=account.subscription_expires_at,
        subscription_expires_label=account.subscription_expires_label,
        reset_credits=account.reset_credits,
    )


async def handle_envelope(cfg: Config, client: ManagementClient, raw: dict[str, Any]) -> dict[str, Any] | None:
    try:
        envelope = Envelope.model_validate(raw)
    except Exception:
        return None
    if envelope.type != "request":
        return None
    if envelope.version != PROTOCOL_VERSION:
        return Envelope(
            version=PROTOCOL_VERSION,
            type="response",
            id=envelope.id,
            ok=False,
            error="不支持的协议版本",
        ).model_dump()
    if envelope.action not in ALLOWED_ACTIONS:
        return Envelope(
            version=PROTOCOL_VERSION,
            type="response",
            id=envelope.id,
            ok=False,
            error="未知的 action",
        ).model_dump()
    try:
        if envelope.action == "quota.query":
            result = await run_quota_query(cfg, client, envelope.payload or {})
            result_dict = result.model_dump()
        elif envelope.action == "codex.refresh":
            refresh_res = await run_codex_refresh(cfg, client, envelope.payload or {})
            result_dict = refresh_res.model_dump()
        else:
            return Envelope(
                version=PROTOCOL_VERSION,
                type="response",
                id=envelope.id,
                ok=False,
                error="未支持的 action",
            ).model_dump()
    except (CPAError, ValidationError) as exc:
        return Envelope(
            version=PROTOCOL_VERSION,
            type="response",
            id=envelope.id,
            ok=False,
            error=str(exc),
        ).model_dump()
    return Envelope(
        version=PROTOCOL_VERSION,
        type="response",
        id=envelope.id,
        ok=True,
        result=result_dict,
    ).model_dump()


async def run_codex_refresh(
    cfg: Config, client: ManagementClient, payload: dict[str, Any]
) -> CodexRefreshResult:
    if not cfg.codex_refresh_enabled:
        raise CPAError("客户端未启用 Codex 额度刷新功能（CODEX_REFRESH_ENABLED=false）")
    req = CodexRefreshPayload.model_validate(payload)
    files = await client.list_auth_files()
    codex_files = [item for item in files if platform_of(item) == "codex"]
    matched = match_auth(codex_files, req.account)
    if not matched:
        raise CPAError(f"没有找到 Codex 凭证：{req.account}")
    if len(matched) > 1:
        raise CPAError(f"「{req.account}」匹配到多个 Codex 凭证")
    msg, rem = await consume_codex_reset(matched[0], client=client, cfg=cfg)
    return CodexRefreshResult(message=msg, remaining_credits=rem)


async def run_quota_query(cfg: Config, client: ManagementClient, payload: dict[str, Any]) -> QuotaQueryResult:
    query = QuotaQueryPayload.model_validate(payload)
    files = await client.list_auth_files()
    platform = normalize_platform(query.platform) if query.platform and is_platform_query(query.platform) else query.platform
    target = files
    single = False
    if query.account:
        matched = match_auth(files, query.account)
        if not matched:
            raise CPAError(f"没有找到凭证：{query.account}")
        if len(matched) > 1:
            raise CPAError(f"「{query.account}」匹配到多个凭证")
        target = matched
        single = True
    elif platform:
        target = [item for item in files if platform_of(item) == platform]
        if not target:
            raise CPAError(f"没有 {platform} 平台的凭证。")
    board = await collect_quotas(
        target,
        platform=None if single else platform,
        force=query.fresh,
        skip_disabled=not single,
        client=client,
        cfg=cfg,
    )
    stamp_client(board, cfg.client_name)
    accounts = [account_to_dto(account) for section in board.platforms for account in section.accounts]
    return QuotaQueryResult(
        client_name=cfg.client_name,
        queried_at=datetime.now(timezone.utc).isoformat(),
        cached=board.cached,
        accounts=accounts,
    )
