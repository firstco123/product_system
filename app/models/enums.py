"""Domain enums — stored as short strings to keep migrations Postgres-portable."""

from __future__ import annotations

from enum import StrEnum


class ChannelEnum(StrEnum):
    RAKUTEN = "rakuten"
    SHOPIFY = "shopify"
    AMAZON = "amazon"
    WHOLESALE = "wholesale"


class OrderStatusEnum(StrEnum):
    PENDING_MAPPING = "pending_mapping"
    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class InventoryEventTypeEnum(StrEnum):
    ORDER_CONSUMED = "order_consumed"
    CANCELLATION_RETURNED = "cancellation_returned"
    MANUAL_ADJUST = "manual_adjust"
    STOCKTAKE = "stocktake"
    RECEIPT = "receipt"


class MappingAlertStatusEnum(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class WebhookStatusEnum(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    REJECTED = "rejected"
    FAILED = "failed"


class FulfillmentTypeEnum(StrEnum):
    SELF = "self"
    FBA = "fba"
    MFN = "mfn"


class SyncAttemptTypeEnum(StrEnum):
    PUSH_INVENTORY = "push_inventory"
    RECONCILE = "reconcile"


class SyncAttemptStatusEnum(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReconcileRunStatusEnum(StrEnum):
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class ReconcileRunTypeEnum(StrEnum):
    """What kind of count produced a run.

    All three share one approval path — the only code that deliberately
    overwrites a snapshot — and are told apart by this column alone. Every
    query that means "the daily CROSS MALL reconciliation" must say so, or a
    Shopify audit surfaces as a pending reconciliation in the operator's queue.
    """

    #: Daily CROSS MALL CSV. Retires when CROSS MALL shuts down.
    RECONCILE = "reconcile"
    #: Physical count entered from a CSV (P2-028).
    STOCKTAKE = "stocktake"
    #: Daily comparison against Shopify's own inventory, which replaces
    #: CROSS MALL as the external check (P2-035).
    SHOPIFY_AUDIT = "shopify_audit"
    #: One imported 流通BMS wholesale file (P2-051, docs/29 §8).
    #:
    #: The odd one out, and deliberately so. The other three SET stock to a
    #: counted absolute and carry their lines as `reconcile_diffs` for an
    #: operator to approve. Wholesale is a MOVEMENT, its lines live in
    #: `inventory_events` keyed by 取引番号 and 取引明細番号, and a run of this
    #: kind holds NO diffs — it is the audit record of an import that was already
    #: confirmed on screen. `reconcile_diffs` could not hold those lines anyway:
    #: it is UNIQUE on (run, master_sku_id) and one voucher may name the same
    #: product twice, which would collapse two lines into one and lose the per
    #: line key that makes a re-upload a no-op.
    #:
    #: So never call `approve_diff` for this kind, and note that no migration was
    #: needed to add it — `run_type` is a plain String(16) with no CHECK.
    WHOLESALE = "wholesale"


class ReconcileDiffDecisionEnum(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    SKIPPED = "skipped"


class NonInventoryKindEnum(StrEnum):
    """Why a master SKU carries no physical stock.

    Set together with `master_skus.is_stock_managed = false`; a CHECK constraint
    keeps the two in lockstep. Such SKUs never produce inventory events, so they
    cannot accumulate the runaway negatives seen in Phase 1-B (gift boxes reached
    -12509), and they are excluded from analytics and reorder.
    """

    PACKAGING = "packaging"  # ギフトボックス・ラッピング材
    COUPON = "coupon"  # クーポン・値引き行
    MADE_TO_ORDER = "made_to_order"  # 受注生産・長さ変更等
    SERVICE = "service"  # 役務・手数料
    OTHER = "other"
