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
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ChannelAdapter
from app.logging import get_logger
from app.models import BundleComponent, ChannelSkuMapping, MasterSku, SyncAttempt
from app.notifications.slack import SlackNotifier
from app.services.inventory import InventoryService
from app.services.inventory_push import InventoryPushService, PushRequest
from app.services.mapping import channel_targets
from app.services.push_safety import Screened, screen_targets

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PushPlan:
    """What one master would write to one channel, and what it will not.

    `resolve_push_plan` builds these and both `push_bundles` and the
    operator-facing CLIs read them, so a preview and the write it previews
    cannot disagree about which targets are writable. A dry run that rebuilt
    this for itself would be free to drift, which is the specific way a preview
    stops being worth reading.
    """

    master_sku_id: int
    available: int
    screened: Screened

    @property
    def writes(self) -> int:
        return len(self.screened.targets)


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

    async def masters_mapped_on(self, channel: str) -> list[int]:
        """Every live, stock-managed master with an active mapping on this channel.

        `all_bundle_ids` cannot serve a Rakuten stock push: that catalogue is
        ordinary masters almost entirely, and bundles are the exception. One loop
        covers both because `get_bundle_available` returns a component-less
        master's own snapshot — its documented behaviour, not a coincidence being
        relied on.

        Archived and 在庫管理対象外 masters are excluded for the same reason
        `all_bundle_ids` excludes them: writing stock for something the shop no
        longer sells overwrites whatever the channel holds, and burns a call in a
        rate-limited job.
        """
        result = await self._session.execute(
            select(ChannelSkuMapping.master_sku_id)
            .join(MasterSku, MasterSku.id == ChannelSkuMapping.master_sku_id)
            .where(
                ChannelSkuMapping.channel == channel,
                ChannelSkuMapping.is_active.is_(True),
                MasterSku.is_stock_managed.is_(True),
                MasterSku.archived_at.is_(None),
            )
            .distinct()
            .order_by(ChannelSkuMapping.master_sku_id)
        )
        return list(result.scalars().all())

    async def resolve_push_plan(
        self,
        *,
        channel: str,
        master_ids: Iterable[int],
    ) -> list[PushPlan]:
        """Resolve, screen and price every master's targets, writing nothing.

        A master with no active mapping on this channel is absent from the result
        and logged as itself: an absence is not a refusal, and conflating the two
        would put ordinary unfinished mapping work into the queue that holds real
        data conflicts.
        """
        plans: list[PushPlan] = []
        for master_sku_id in master_ids:
            targets = await channel_targets(
                self._session, master_sku_id=master_sku_id, channel=channel
            )
            if not targets:
                log.info(
                    "bundle_push.skip_no_mapping",
                    bundle_master_sku_id=master_sku_id,
                    channel=channel,
                )
                continue
            plans.append(
                PushPlan(
                    master_sku_id=master_sku_id,
                    available=await self._inventory.get_bundle_available(master_sku_id),
                    screened=screen_targets(targets, channel=channel),
                )
            )
        return plans

    @staticmethod
    def _log_refusals(plan: PushPlan, *, channel: str) -> None:
        for refusal in plan.screened.blocked_pages:
            # Warning, not error: nothing failed and nothing will retry. This
            # page needs a data change before it can ever be written, so it
            # belongs in the operator's queue, not the alert channel.
            log.warning(
                "bundle_push.blocked",
                bundle_master_sku_id=plan.master_sku_id,
                channel=channel,
                channel_sku=refusal.channel_sku,
                code=refusal.code,
                message=refusal.message,
            )
        for refusal in plan.screened.skipped:
            log.debug(
                "bundle_push.skip_page_level",
                bundle_master_sku_id=plan.master_sku_id,
                channel=channel,
                channel_sku=refusal.channel_sku,
                code=refusal.code,
            )

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

        Targets are screened by `push_safety.screen_targets` before anything is
        written, so a Rakuten page-level row is no longer pushed to — it never
        had an address and failed every run — and a coarse master, one of ours
        standing in for several of Rakuten's SKUs, stops that page rather than
        repeating a single quantity across SKUs that hold different stock.
        """
        attempts: list[SyncAttempt] = []
        for plan in await self.resolve_push_plan(channel=adapter.channel, master_ids=bundle_ids):
            bundle_id, available = plan.master_sku_id, plan.available
            self._log_refusals(plan, channel=adapter.channel)
            if not plan.screened.targets:
                log.info(
                    "bundle_push.skip_nothing_writable",
                    bundle_master_sku_id=bundle_id,
                    channel=adapter.channel,
                    blocked_pages=len(plan.screened.blocked_pages),
                )
                continue
            for target in plan.screened.targets:
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
