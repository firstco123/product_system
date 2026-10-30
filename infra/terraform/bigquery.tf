# BigQuery — dataset already exists in the project (created by client).
# We reference it via a data source and only manage the tables + IAM.
#
# Adding a column to a table that is ALREADY DEPLOYED must use
# "mode": "NULLABLE". REQUIRED forces Terraform to destroy and re-create the
# table, which silently deletes every row in it. tests/test_bq_schema_parity.py
# pins each JSON against the ORM; see its docstring.

data "google_bigquery_dataset" "main" {
  dataset_id = var.bigquery_dataset
}

# New in Phase 2 W3. REQUIRED modes are safe here only because the table does
# not exist yet — a create, not an alter.
resource "google_bigquery_table" "product_categories" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "product_categories"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/product_categories.json")
}

resource "google_bigquery_table" "master_skus" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "master_skus"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/master_skus.json")
}

resource "google_bigquery_table" "channel_sku_mappings" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "channel_sku_mappings"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/channel_sku_mappings.json")
}

resource "google_bigquery_table" "orders" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "orders"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/orders.json")

  time_partitioning {
    type  = "DAY"
    field = "ordered_at"
  }
}

resource "google_bigquery_table" "order_items" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "order_items"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/order_items.json")
}

resource "google_bigquery_table" "inventory_events" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "inventory_events"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/inventory_events.json")

  time_partitioning {
    type  = "DAY"
    field = "occurred_at"
  }
}

resource "google_bigquery_table" "inventory_snapshots" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "inventory_snapshots"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/inventory_snapshots.json")
}

# NOTE: BigQuery needs TWO grants, both performed by the project/dataset OWNER
# (the client) — the developer account cannot set IAM. BOTH are required; the
# second was missing until 2026-08-19, so the daily export had never succeeded
# (every run failed with "does not have bigquery.jobs.create permission", and the
# job endpoint used to swallow that as HTTP 200). See docs/25.
#
# 1) dataset-level: permission to WRITE rows
#   bq add-iam-policy-binding \
#       --project_id=inventory-496204 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/bigquery.dataEditor \
#       product_system
#
# 2) project-level: permission to CREATE the load job (NOT implied by dataEditor)
#   gcloud projects add-iam-policy-binding inventory-496204 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/bigquery.jobUser
#
# Both grants are also declared below, so the two that the export depends on are
# recorded as configuration rather than as a comment someone has to notice. They
# are gated behind manage_bigquery_iam (default false) for the same reason the
# commands above are manual: the developer account holds Editor, which lacks
# resourcemanager.projects.setIamPolicy, so an ungated resource here would fail
# every `terraform apply` the developer runs — including the image-pin apply that
# follows each deploy.

# ADDITIVE on purpose: _member, never _binding or _policy.
#
# `google_bigquery_dataset_iam_binding` is authoritative for its role and
# `_policy` for the whole dataset, so either would delete the client's own
# access to a dataset THEY created and still use — silently, on the first apply.
# `_member` adds one grant and leaves every other binding alone.
resource "google_bigquery_dataset_iam_member" "app_data_editor" {
  count = var.manage_bigquery_iam ? 1 : 0

  dataset_id = data.google_bigquery_dataset.main.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.app.email}"
}

# The grant whose absence meant the daily export had never once succeeded.
# dataEditor permits writing rows; it does not permit creating the load job that
# writes them, and the job endpoint reported the resulting failure as HTTP 200
# "partial" — so nothing surfaced until the rows were counted. See docs/25.
#
# Also additive: `google_project_iam_binding` would strip every other member of
# roles/bigquery.jobUser from the project.
resource "google_project_iam_member" "app_bq_job_user" {
  count = var.manage_bigquery_iam ? 1 : 0

  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.app.email}"

  depends_on = [google_project_service.required]
}
