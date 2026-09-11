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
    Envelope,
    PROTOCOL_VERSION,
    QuotaQueryPayload,
    QuotaQueryResult,
    QuotaWindowDTO,
)
from .quota import AccountQuota, collect_quotas, is_platform_query, normalize_platform, platform_of, stamp_client


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
            )
            for window in account.windows
        ],
        disabled=account.disabled,
        cooling=account.cooling,
        client_name=account.client_name,
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
            error="只允许 quota.query",
        ).model_dump()
    try:
        result = await run_quota_query(cfg, client, envelope.payload or {})
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
        result=result.model_dump(),
    ).model_dump()


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
