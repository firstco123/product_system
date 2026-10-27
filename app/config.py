"""Application configuration (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "dev", "staging", "prod"]
TaskQueueBackend = Literal["in_memory", "cloud_tasks"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnv = "local"
    app_log_level: str = "INFO"
    app_timezone: str = "Asia/Tokyo"

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/product_system",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://postgres:postgres@localhost:5432/product_system",
    )

    task_queue_backend: TaskQueueBackend = "in_memory"

    # Rakuten (filled later with client credentials)
    rakuten_service_secret: str = ""
    rakuten_license_key: str = ""
    rakuten_shop_url: str = ""

    # Shopify (filled later with client credentials)
    shopify_shop_domain: str = ""
    shopify_access_token: str = ""
    shopify_webhook_secret: str = ""
    shopify_api_version: str = "2025-04"

    # GCP (filled later)
    gcp_project_id: str = ""
    gcp_region: str = "asia-northeast1"
    bigquery_dataset: str = ""

    # Cloud Tasks (only used when task_queue_backend == "cloud_tasks")
    cloud_tasks_queue: str = ""
    cloud_tasks_target_url: str = ""
    cloud_tasks_invoker_sa: str = ""

    # Internal job endpoint auth. `allUsers` holds run.invoker so Shopify
    # webhooks can reach the service, which leaves /internal/jobs/* open too;
    # these verify the Google-signed token the schedulers already send.
    #
    # Defaults to "audit" — log the verdict, admit the request — because
    # switching straight to "enforce" would 401 every scheduled job at once if
    # anything about the token is not as expected. Flip to "enforce" once the
    # logs show production agreeing.
    internal_jobs_auth_mode: str = "audit"
    #: Empty falls back to cloud_tasks_invoker_sa: Cloud Scheduler and Cloud
    #: Tasks both sign as that same service account.
    internal_jobs_allowed_sa: str = ""

    # Admin UI
    admin_username: str = "admin"
    admin_password: str = "change_me_in_production"  # noqa: S105

    # Phase 1-B: inventory push & reconcile
    # Default Shopify Location ID for push_inventory. Empty = auto-discover the
    # shop's single primary location at startup. Set explicitly only when the
    # shop has multiple locations.
    shopify_location_id: str = ""

    # Slack notifications. Levels: critical / error / info. Empty URL disables
    # notifications (used in local dev and tests).
    slack_webhook_url: str = ""
    slack_notify_min_level: Literal["critical", "error", "info"] = "error"

    # Best-seller definition for the inventory list view. Top-N percent by
    # order_consumed count over the trailing window.
    best_seller_window_days: int = 30
    best_seller_top_percent: int = 20

    # 低在庫バッジ/絞り込みの境界。1〜(閾値-1) が「低在庫」。W6 で SKU ごとの
    # 販売速度から算出した値に置き換わるが、その時もこの値が「速度データが
    # まだ無い SKU」のフォールバックとして残る。
    low_stock_threshold: int = 10

    # 楽天SKU管理番号の「仮値」を判定する正規表現。RMS が採番した自動値
    # (r-sku00000001 形式) は商品を識別できないため、実SKUへの差し替えを
    # クライアントに依頼する必要がある。設定値にしてあるのは、RMS の採番規則が
    # 変わったときにコード変更なしで追随するため。
    rakuten_placeholder_sku_pattern: str = r"^r-sku\d+$"

    # Phase 1-B: daily reconcile CSV source. gs:// URI of the CROSS MALL stock
    # export that the scheduled reconcile job reads. Empty disables the
    # scheduled job (it logs and no-ops); the admin-UI CSV upload path is
    # independent of this setting.

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
