"""Sync-error viewer + one-click retry (Phase 1-B F2.5).

Surfaces `sync_attempts` rows — by default the failed ones — so a
non-engineer operator can see *which* SKU failed to push to *which*
channel, read a Japanese explanation of the cause, and re-run the push
with one click. A retry re-pushes the SKU's CURRENT authoritative
quantity (derived for bundles, snapshot for normal SKUs), links the new
attempt to the failed one via `parent_attempt_id`, and lands back here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings, get_settings
from app.db import get_session
from app.models import (
    MasterSku,
    SyncAttempt,
    SyncAttemptStatusEnum,
    SyncAttemptTypeEnum,
)
from app.notifications.slack import get_slack_notifier
from app.services import InventoryService
from app.services.inventory_push import InventoryPushService, PushRequest
from app.ui.auth import OperatorDep
from app.ui.deps import templates

if TYPE_CHECKING:
    from app.adapters.rakuten import RakutenAdapter
    from app.adapters.shopify import ShopifyAdapter

router = APIRouter(prefix="/sync-errors")

PAGE_SIZE = 50

# error_code (exception class name) -> plain-Japanese guidance for non-engineer
# operators. Falls back to a generic line; the raw code/message is always shown
# alongside so an engineer can still diagnose.
_ERROR_GUIDANCE: dict[str, str] = {
    "TimeoutException": "チャネルへの接続がタイムアウトしました。時間をおいて再実行してください。",
    "ConnectTimeout": "チャネルへの接続がタイムアウトしました。時間をおいて再実行してください。",
    "ReadTimeout": "チャネルの応答待ちがタイムアウトしました。時間をおいて再実行してください。",
    "TimeoutError": "処理がタイムアウトしました。時間をおいて再実行してください。",
    "ConnectError": "チャネルに接続できませんでした。ネットワークを確認し再実行してください。",
    "ConnectionError": "チャネルに接続できませんでした。ネットワークを確認し再実行してください。",
    "HTTPStatusError": "チャネルAPIがエラー応答を返しました。時間をおいて再実行してください。",
    "HTTPError": "チャネルAPIがエラー応答を返しました。時間をおいて再実行してください。",
    "RakutenApiError": "楽天APIがエラーを返しました。SKU管理番号と在庫設定を確認してください。",
    "ShopifyApiError": "Shopify APIがエラーを返しました。SKUとロケーション設定を確認してください。",
    "RuntimeError": "設定または実行環境の問題で処理できませんでした。設定を確認してください。",
}
_ERROR_GUIDANCE_DEFAULT = "在庫の同期に失敗しました。詳細を確認のうえ再実行してください。"


def localize_error(error_code: str | None, error_message: str | None) -> str:
    """Plain-Japanese guidance for an operator, chosen from the error_code and,
    for HTTP failures, refined by well-known status codes in the message."""
    msg = error_message or ""
    if error_code in {"HTTPStatusError", "HTTPError"} or any(
        c in msg for c in ("429", "500", "502", "503", "401", "403")
    ):
        if "429" in msg:
            return "チャネルAPIのレート制限に達しました。数分待ってから再実行してください。"
        if "401" in msg or "403" in msg:
            return "チャネルの認証に失敗しました。APIキー・トークンの有効期限を確認してください。"
        if any(c in msg for c in ("500", "502", "503")):
            return "チャネル側で一時的な障害が発生しています。時間をおいて再実行してください。"
    return _ERROR_GUIDANCE.get(error_code or "", _ERROR_GUIDANCE_DEFAULT)


@router.get("")
async def sync_errors_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    status: str = "failed",
    channel: str = "",
    attempt_type: str = "",
    sku_code: str = "",
    offset: int = 0,
) -> Response:
    stmt = (
        select(
            SyncAttempt.id,
            SyncAttempt.attempt_type,
            SyncAttempt.channel,
            SyncAttempt.master_sku_id,
            SyncAttempt.payload,
            SyncAttempt.status,
            SyncAttempt.error_code,
            SyncAttempt.error_message,
            SyncAttempt.response_payload,
            SyncAttempt.attempt_count,
            SyncAttempt.parent_attempt_id,
            SyncAttempt.started_at,
            SyncAttempt.finished_at,
            MasterSku.sku_code,
        )
        .outerjoin(MasterSku, MasterSku.id == SyncAttempt.master_sku_id)
        .order_by(SyncAttempt.started_at.desc())
    )
    if status:
        stmt = stmt.where(SyncAttempt.status == status)
    if channel:
        stmt = stmt.where(SyncAttempt.channel == channel)
    if attempt_type:
        stmt = stmt.where(SyncAttempt.attempt_type == attempt_type)
    if sku_code:
        stmt = stmt.where(MasterSku.sku_code.ilike(f"%{sku_code}%"))

    stmt = stmt.offset(offset).limit(PAGE_SIZE + 1)
    rows = (await session.execute(stmt)).mappings().all()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    items = [
        {**dict(r), "guidance": localize_error(r["error_code"], r["error_message"])} for r in rows
    ]

    base: dict[str, str | int] = {
        "status": status,
        "channel": channel,
        "attempt_type": attempt_type,
        "sku_code": sku_code,
    }
    pagination = {
        "has_prev": offset > 0,
        "has_next": has_next,
        "qs_prev": urlencode({**base, "offset": max(0, offset - PAGE_SIZE)}),
        "qs_next": urlencode({**base, "offset": offset + PAGE_SIZE}),
    }
    return templates.TemplateResponse(
        request,
        "sync_errors.html",
        {
            "operator": operator,
            "version": __version__,
            "rows": items,
            "filters": {
                "status": status,
                "channel": channel,
                "attempt_type": attempt_type,
                "sku_code": sku_code,
            },
            "statuses": [s.value for s in SyncAttemptStatusEnum],
            "attempt_types": [t.value for t in SyncAttemptTypeEnum],
            "channels": ["rakuten", "shopify"],
            "pagination": pagination,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


def build_retry_adapter(channel: str, settings: Settings) -> RakutenAdapter | ShopifyAdapter | None:
    """Instantiate the live channel adapter for a retry, or None when the
    channel is unknown or its credentials are not configured."""
    from app.adapters.rakuten import RakutenAdapter
    from app.adapters.shopify import ShopifyAdapter

    try:
        if channel == "shopify":
            if not settings.shopify_shop_domain or not settings.shopify_access_token:
                return None
            return ShopifyAdapter(
                shop_domain=settings.shopify_shop_domain,
                access_token=settings.shopify_access_token,
                webhook_secret=settings.shopify_webhook_secret,
                api_version=settings.shopify_api_version,
                location_id=settings.shopify_location_id,
            )
        if channel == "rakuten":
            if not settings.rakuten_service_secret or not settings.rakuten_license_key:
                return None
            return RakutenAdapter(
                service_secret=settings.rakuten_service_secret,
                license_key=settings.rakuten_license_key,
                shop_url=settings.rakuten_shop_url or None,
            )
    except Exception:
        return None
    return None


@router.post("/{attempt_id}/retry")
async def sync_errors_retry(
    attempt_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    # Phase 1 — load + validate inside a read transaction, capturing the fields
    # we need as primitives so nothing is accessed lazily after commit.
    async with session.begin():
        attempt = await session.get(SyncAttempt, attempt_id)
        if attempt is None:
            return RedirectResponse(url="/admin/sync-errors?flash=notfound", status_code=303)
        if attempt.status != SyncAttemptStatusEnum.FAILED.value:
            return RedirectResponse(url="/admin/sync-errors?flash=notfailed", status_code=303)
        channel = attempt.channel
        master_sku_id = attempt.master_sku_id
        channel_sku = (attempt.payload or {}).get("channel_sku")
        # Rakuten needs the pair, so a retry that dropped the key could only
        # ever re-send the page-level write the adapter refuses.
        variant_key = (attempt.payload or {}).get("variant_key")
        parent_id = attempt.id
        retryable = (
            attempt.attempt_type == SyncAttemptTypeEnum.PUSH_INVENTORY.value
            and master_sku_id is not None
            and bool(channel)
            and bool(channel_sku)
        )
    if not retryable or channel is None or master_sku_id is None or channel_sku is None:
        return RedirectResponse(url="/admin/sync-errors?flash=notretryable", status_code=303)

    # Phase 2 — build the live adapter (no DB).
    adapter = build_retry_adapter(channel, get_settings())
    if adapter is None:
        return RedirectResponse(url="/admin/sync-errors?flash=nocreds", status_code=303)

    # Phase 3 — re-push the CURRENT authoritative quantity (derived for a bundle
    # parent, snapshot on_hand for a normal SKU), linked to the failed attempt.
    async with adapter, session.begin():
        quantity = await InventoryService(session).get_bundle_available(master_sku_id)
        result = await InventoryPushService(session, get_slack_notifier()).push_single(
            adapter,
            PushRequest(
                master_sku_id=master_sku_id,
                channel_sku=channel_sku,
                variant_key=variant_key,
                quantity=quantity,
                triggered_by=f"admin_retry:{operator}",
                parent_attempt_id=parent_id,
            ),
        )
        ok = result.status == SyncAttemptStatusEnum.SUCCEEDED.value
    return RedirectResponse(
        url=f"/admin/sync-errors?flash={'retried' if ok else 'retryfailed'}",
        status_code=303,
    )


def _flash(token: str | None) -> dict[str, str] | None:
    table = {
        "retried": ("ok", "再実行に成功しました。最新の在庫数をチャネルへ反映しました。"),
        "retryfailed": ("error", "再実行しましたが再び失敗しました。原因を確認してください。"),
        "notfound": ("error", "対象の同期記録が見つかりません。"),
        "notfailed": ("error", "失敗状態の記録のみ再実行できます。"),
        "notretryable": ("error", "この記録は再実行できません(在庫Push以外、または情報不足)。"),
        "nocreds": ("error", "チャネルの認証情報が未設定のため再実行できません。"),
    }
    if not token or token not in table:
        return None
    kind, message = table[token]
    return {"kind": kind, "message": message}
