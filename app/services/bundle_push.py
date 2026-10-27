"""BundlePushService — batched push of derived bundle/shared-stock availability.

Per client decision D-6, channel pushes are BATCHED (post-reconcile / scheduled),
not fired inside order ingestion. For each bundle/shared-stock parent this
computes get_bundle_available (max(0, min over components)) and pushes it via
InventoryPushService. A component that just changed (e.g. shared N23) fans out to
every dependent bundle via `dependent_bundle_ids`.

The service does NOT commit; the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ChannelAdapter
from app.logging import get_logger
from app.models import BundleComponent, MasterSku, SyncAttempt
from app.notifications.slack import SlackNotifier
from app.services.inventory import InventoryService
from app.services.inventory_push import InventoryPushService, PushRequest
from app.services.mapping import channel_targets

log = get_logger(__name__)


class BundlePushService:
    def __init__(self, session: AsyncSession, notifier: SlackNotifier | None = None) -> None:
        self._session = session
        self._inventory = InventoryService(session)
        self._push = InventoryPushService(session, notifier)

    async def all_bundle_ids(self) -> list[int]:
        """Every LIVE is_bundle master (set parents + shared-stock brackets).

        Archived and 在庫管理対象外 parents are skipped: pushing availability for
        a retired set overwrites whatever the channel currently holds for a
        product the shop no longer sells, and every such push burns a channel API
        call in a job that is already rate-limited. This is the one place that
        SELECTS `is_bundle` rather than excluding it, so the scope predicate is
        spelled out here instead of coming from `sku_scope`.
        """
        result = await self._session.execute(
            select(MasterSku.id).where(
                MasterSku.is_bundle.is_(True),
                MasterSku.is_stock_managed.is_(True),
                MasterSku.archived_at.is_(None),
            )
        )
        return list(result.scalars().all())

    async def dependent_bundle_ids(self, component_ids: Iterable[int]) -> list[int]:
        """The distinct bundles that consume any of these components — the set to
        re-derive & re-push when a component's stock changes (shared N23 -> its 4
        sets; an anklet -> its bracelet)."""
        ids = list(component_ids)
        if not ids:
            return []
        result = await self._session.execute(
            select(BundleComponent.bundle_master_sku_id)
            .where(BundleComponent.component_master_sku_id.in_(ids))
            .distinct()
        )
        return list(result.scalars().all())

    async def push_bundles(
        self,
        adapter: ChannelAdapter,
        bundle_ids: Iterable[int],
        *,
        triggered_by: str,
        dry_run: bool = False,
    ) -> list[SyncAttempt]:
        """Compute each bundle's derived availability and push it EVERYWHERE that
        bundle is sold on the adapter's channel.

        Every active mapping, not one of them. This asked `_channel_sku` for "the"
        channel SKU and took `.scalars().first()` with no ORDER BY — but one
        master is sold from several Rakuten pages (558 of 609 masters held more
        than one, measured 2026-10-27), so there was no single answer to give. The
        effect was that one page got the correct availability, the others kept
        whatever they had, and WHICH page was the lucky one could change between
        runs because nothing ordered the rows.

        A bundle with no active mapping on this channel is skipped, which is not
        the same thing and is logged as itself.
        """
        attempts: list[SyncAttempt] = []
        for bundle_id in bundle_ids:
            targets = await channel_targets(
                self._session, master_sku_id=bundle_id, channel=adapter.channel
            )
            if not targets:
                log.info(
                    "bundle_push.skip_no_mapping",
                    bundle_master_sku_id=bundle_id,
                    channel=adapter.channel,
                )
                continue
            available = await self._inventory.get_bundle_available(bundle_id)
            for target in targets:
                if dry_run:
                    log.info(
                        "bundle_push.dry_run",
                        bundle_master_sku_id=bundle_id,
                        channel_sku=target.channel_sku,
                        variant_key=target.variant_key,
                        available=available,
                    )
                    continue
                attempts.append(
                    await self._push.push_single(
                        adapter,
                        PushRequest(
                            master_sku_id=bundle_id,
                            channel_sku=target.channel_sku,
                            variant_key=target.variant_key,
                            quantity=available,
                            triggered_by=triggered_by,
                        ),
                    )
                )
        return attempts
