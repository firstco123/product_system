variable "project_id" {
  description = "GCP project that hosts this stack."
  type        = string
}

variable "region" {
  description = "Default region for Cloud SQL / Run / Tasks / Scheduler."
  type        = string
  default     = "asia-northeast1"
}

variable "service_image" {
  description = "Fully qualified container image for the main service. Default is the public hello placeholder so the initial terraform apply can finish before we have a real image to deploy."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "jobs_image" {
  description = "Container image for the verification Cloud Run Jobs. Empty = fall back to service_image. Set this (NOT service_image) to deploy a verification image to the jobs without redeploying the live service."
  type        = string
  default     = ""
}

variable "bigquery_dataset" {
  description = "Existing BigQuery dataset where daily exports land."
  type        = string
}

variable "db_tier" {
  description = "Cloud SQL machine type."
  type        = string
  default     = "db-f1-micro"
}

variable "db_password" {
  description = "Bootstrap password for the postgres user (rotate after first deploy)."
  type        = string
  sensitive   = true
}

variable "shopify_shop_domain" {
  description = "Shopify admin domain (e.g. xxxxx.myshopify.com)."
  type        = string
}

variable "rakuten_shop_url" {
  description = "Rakuten storefront URL (e.g. https://www.rakuten.co.jp/yourshop/)."
  type        = string
}

variable "admin_username" {
  description = "Basic Auth username for the admin UI."
  type        = string
  default     = "admin"
}

variable "admin_password" {
  description = "Basic Auth password for the admin UI."
  type        = string
  sensitive   = true
}

# ---- Phase 1-B Stage 0 verification jobs (docs/14) ----

variable "create_verification_jobs" {
  description = "Create the 4 verification Cloud Run Jobs + recon scratch bucket. Verification-only; set true to deploy for Stage 1 verification, false to tear down (docs/14 §7)."
  type        = bool
  default     = false
}

variable "slack_webhook_secret_ready" {
  description = "Wire SLACK_WEBHOOK_URL=slack-webhook-url:latest into the verify-slack job. The gate exists because referencing a version-less secret makes the job fail to start even for --mode=empty. Default flipped to true on 2026-09-23 so it matches reality: slack-webhook-url version 1 is enabled and the deployed job already carries the secret, so the false default only misrepresented the environment."
  type        = bool
  default     = true
}

variable "recon_verify_bucket" {
  description = "GCS bucket name for F1.8 reconcile dummy CSVs (gs:// input to reconcile_admin). Globally unique."
  type        = string
  default     = "product-system-verify"
}

# ---- Artifact Registry (cloudbuild.yaml push target) ----

variable "manage_bigquery_iam" {
  description = "Manage the app service account's two BigQuery grants in Terraform. Default false: the developer account's Editor role lacks setIamPolicy, so only a project OWNER can apply them. Unlike manage_artifact_registry this needs no prior import — the resources are additive IAM _member grants, so applying over a binding that already exists is a no-op rather than a 409."
  type        = bool
  default     = false
}

variable "manage_artifact_registry" {
  description = "Manage the Artifact Registry repo in Terraform. Default false because it was created imperatively (scripts/deploy_to_cloud_run.ps1) and already exists; import it first, then set true (see artifact_registry.tf)."
  type        = bool
  default     = false
}

variable "artifact_registry_repo" {
  description = "Artifact Registry repository id for the container image (matches cloudbuild.yaml _REPO)."
  type        = string
  default     = "product-system"
}

# ---- Cloud Build trigger (push-to-main image build) ----

variable "create_build_trigger" {
  description = "Create the push-to-main Cloud Build trigger. Requires the GitHub repo to be connected to Cloud Build first (one-time, console — see docs/14 §10)."
  type        = bool
  default     = false
}

variable "github_owner" {
  description = "GitHub org/user that owns the repo (for the Cloud Build trigger)."
  type        = string
  default     = "TopMaster-dev"
}

variable "github_repo" {
  description = "GitHub repository name (for the Cloud Build trigger)."
  type        = string
  default     = "product_system"
}
