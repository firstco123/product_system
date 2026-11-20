"""MasterSku — the canonical, channel-agnostic SKU."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MasterSku(Base, TimestampMixin):
    __tablename__ = "master_skus"
    __table_args__ = (
        # 在庫管理対象外フラグと種別は常に対で設定される(片方だけの状態を作らせない)。
        CheckConstraint(
            "is_stock_managed = (non_inventory_kind IS NULL)",
            name="ck_master_sku_non_inventory_kind",
        ),
        # 理由だけが残り停止していない状態は、画面上「停止中」と読めてしまう
        # 唯一の組み合わせなので作らせない。逆向き(理由なしの停止)は許す。
        CheckConstraint(
            "stock_push_paused_reason IS NULL OR stock_push_paused_at IS NOT NULL",
            name="ck_master_sku_stock_push_paused_reason",
        ),
        # カテゴリ別集計と「未分類件数」の両方がこの索引を使う。
        Index("ix_master_skus_category", "category_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sku_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    jan_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Shopify product/variant image, synced by app.cli.sync_shopify_images so the
    # admin screens can show a thumbnail. Nullable — not every SKU has an image.
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # A bundle/set or shared-stock (anklet/bracelet) parent — availability is
    # derived from its bundle_components, never stored, and never pushed directly.
    is_bundle: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # --- Phase 2 lifecycle flags (migration 0009) ---------------------------
    # 在庫を持たない商品(ギフトボックス・クーポン・受注生産など)。false のとき
    # InventoryService.resolve_consumption() が [] を返し、消費もキャンセル補償も
    # 発生しない。片側だけ効かせるとキャンセルが幽霊在庫を生むため、両経路が
    # 同時に no-op になるこの一点で判定する。
    is_stock_managed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    non_inventory_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 変異体移行で役目を終えた旧マスタなどの retire 用。可視性の概念であり、
    # 在庫の概念ではない。現時点の画面は除外するが、期間集計は除外しない
    # (除外すると移行前の売上が消え前期間比較が壊れる)。
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # --- 自動書き込みの一時停止 (migration 0016) ----------------------------
    # 「このSKUの在庫数をチャネルへ自動で書き込まない」。NULL が既定で、自動
    # 書き込みの対象を意味する。クリスマス販売期間はクライアントがShopify上の
    # 在庫を手動で管理するため、ギフトセット48件を対象外にする目的で入れた
    # (2026-11-19 クライアント判断)。
    # is_stock_managed = false では代替できない。あちらは resolve_consumption が
    # 親を見て [] を返すため、セットが売れても構成品の在庫が減らなくなる。
    # つまり手動運用で守りたい機能そのものを止めてしまう。こちらは引き落としを
    # 残したまま、書き込みだけを止める。
    stock_push_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stock_push_paused_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # --- Phase 2 categorisation (migration 0010) ---------------------------
    # 分析軸。NULL は「未分類」であり、カテゴリ別集計では独立した区分として
    # 表示する(黙って除外すると各カテゴリの合計と全体の合計が食い違う)。
    # ON DELETE RESTRICT: カテゴリを消しても SKU の分類が黙って消えないよう、
    # 割当済みの SKU がある限り削除を拒否する。
    category_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("product_categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
