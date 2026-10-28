# 本番DBに対して app.cli.* を実行する共通ラッパー（Cloud SQL Auth Proxy 経由）
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_cli.ps1 -Cli sync_shopify_images -DryRun
#   powershell -ExecutionPolicy Bypass -File scripts\run_cli.ps1 -Cli zero_legacy_stock -Args "--all-negative" -DryRun
#
# 目的:
#   Phase 1-B の運用で、プロキシの起動忘れ・$env:DATABASE_URL の設定漏れ・Shopify
#   認証情報の未設定により、本番CLIの実行が繰り返し失敗した（ローカルDBに接続して
#   "database does not exist"、プレースホルダのドメインで 401 など）。
#   本スクリプトは proxy 起動・接続文字列・チャネル認証情報の取得を一括で行う。
#
# 安全策:
#   -DryRun を付けると CLI に --dry-run を渡す。破壊的な操作は必ず
#   「--dry-run の出力をレビュー → 本実行」の2段で行うこと（docs/24 参照）。

#   -Apply は -DryRun の逆で、既定が参照のみのCLI (sync_shopify_masters) に
#   書き込みを指示する。既定が書き込みのCLIに -DryRun で歯止めをかけるのと
#   同じ2段階を、向きを変えて維持するためのもの。

param(
    [Parameter(Mandatory = $true)][string]$Cli,
    [string]$Args = "",
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$WithShopify,
    [switch]$WithRakuten
)

# 既定で参照のみ、--apply を付けたときだけ書き込むCLI。
# マスタ行を新規作成するため、フラグなしの実行が安全側になるよう反転させてある。
$APPLY_OPT_IN = @("sync_shopify_masters", "link_shared_stock", "create_rakuten_variant_masters")

$ErrorActionPreference = "Stop"

# Python の標準出力をUTF-8に固定する。CLIの出力は日本語を含み、stdout がコンソール
# でないときは Python が ANSI コードページ (このマシンでは 1252) を選ぶため、最初の
# print で UnicodeEncodeError になって即死する。`| Tee-Object` で運用ログを残そうと
# すると全CLIが落ちていた（2026-10-28 判明）。PYTHONUTF8 ではなく PYTHONIOENCODING を
# 使うこと: 前者は open() の既定エンコーディングまで変えてしまい、CP932 の EDI ファイル
# の読み取りに影響する。
$env:PYTHONIOENCODING = "utf-8"

$PROJECT_ID = "inventory-496204"
$REGION     = "asia-northeast1"
$INSTANCE   = "product-system"
$DB         = "product_system"
$LOCAL_PORT = 5433
$CONN_NAME  = "${PROJECT_ID}:${REGION}:${INSTANCE}"

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

$tfvars = Get-Content "infra/terraform/terraform.tfvars" -Raw
$DB_PASSWORD = ([regex]'db_password\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value
if (-not $DB_PASSWORD) { throw "terraform.tfvars から db_password を読み取れませんでした" }

# --- proxy -----------------------------------------------------------------
$proxyDir = ".local/cloud-sql-proxy"
New-Item -ItemType Directory -Path $proxyDir -Force | Out-Null
if (-not (Test-Path "$proxyDir/cloud-sql-proxy.exe")) {
    Section "Cloud SQL Auth Proxy をダウンロード"
    Invoke-WebRequest `
        -Uri "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.13.0/cloud-sql-proxy.x64.exe" `
        -OutFile "$proxyDir/cloud-sql-proxy.exe" -UseBasicParsing
}

Section "Cloud SQL Auth Proxy 起動 (port $LOCAL_PORT)"
$proxy = Start-Process -PassThru -NoNewWindow `
    -FilePath "$proxyDir/cloud-sql-proxy.exe" `
    -ArgumentList @("$CONN_NAME", "--port=$LOCAL_PORT")

try {
    Start-Sleep -Seconds 5
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:${DB_PASSWORD}@127.0.0.1:${LOCAL_PORT}/$DB"
    $env:DATABASE_URL_SYNC = "postgresql+psycopg2://postgres:${DB_PASSWORD}@127.0.0.1:${LOCAL_PORT}/$DB"

    # BigQuery 系CLIは GCP_PROJECT_ID / BIGQUERY_DATASET が未設定だと
    # in-memory クライアントにフォールバックし「成功したのに何も書かれない」
    # 状態になる。プロジェクトとデータセットは秘密情報ではないため常に設定する。
    $env:GCP_PROJECT_ID = $PROJECT_ID
    $env:BIGQUERY_DATASET = ([regex]'bigquery_dataset\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value
    if (-not $env:BIGQUERY_DATASET) { $env:BIGQUERY_DATASET = "product_system" }

    if ($WithShopify) {
        Section "Shopify 認証情報を Secret Manager から取得"
        # ローカルの .env はプレースホルダ (local.myshopify.com) なので本番値で上書きする。
        # .Trim() は必須: Windows 由来のシークレットは末尾 CR が付き認証が壊れる。
        $env:SHOPIFY_SHOP_DOMAIN = ([regex]'shopify_shop_domain\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value
        $env:SHOPIFY_ACCESS_TOKEN = (gcloud secrets versions access latest `
                --secret=shopify-access-token --project=$PROJECT_ID).Trim()
        Write-Host "  shop=$env:SHOPIFY_SHOP_DOMAIN token_len=$($env:SHOPIFY_ACCESS_TOKEN.Length)"
    }

    if ($WithRakuten) {
        Section "楽天RMS 認証情報を Secret Manager から取得"
        # .Trim() は必須。Windows 由来のシークレットは末尾 CR が付き、
        # base64(serviceSecret:licenseKey) が壊れて全リクエストが 401 になる。
        # アダプタ側でも strip しているが、ここで落としておくと
        # inspect_rakuten_auth が「空白が含まれていた」と報告できる。
        $env:RAKUTEN_SERVICE_SECRET = (gcloud secrets versions access latest `
                --secret=rakuten-service-secret --project=$PROJECT_ID).Trim()
        $env:RAKUTEN_LICENSE_KEY = (gcloud secrets versions access latest `
                --secret=rakuten-license-key --project=$PROJECT_ID).Trim()
        $env:RAKUTEN_SHOP_URL = ([regex]'rakuten_shop_url\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value
        # 値そのものは出さない。長さだけで取得できたかは判断できる。
        Write-Host "  secret_len=$($env:RAKUTEN_SERVICE_SECRET.Length) key_len=$($env:RAKUTEN_LICENSE_KEY.Length)"
    }

    Section "接続先の確認"
    Write-Host ("  DB -> " + ($env:DATABASE_URL -replace ':[^:@]+@', ':***@'))
    Write-Host ("  BQ -> " + $env:GCP_PROJECT_ID + ":" + $env:BIGQUERY_DATASET)
    py -m alembic current

    $optIn = $APPLY_OPT_IN -contains $Cli
    if ($Apply -and -not $optIn) {
        throw "-Apply は $($APPLY_OPT_IN -join ', ') 専用です。他のCLIは既定が本実行で、-DryRun で抑止します。"
    }

    $cmd = "py -m app.cli.$Cli"
    if ($DryRun) { $cmd += " --dry-run" }
    if ($Apply)  { $cmd += " --apply" }
    if ($Args)   { $cmd += " $Args" }

    Section "実行: $cmd"
    # 参照のみのフラグ (--status など) で「本実行です」と出すと警告が形骸化し、
    # 本当に危険な実行のときに読み飛ばされる。
    # inspect_* は引数なしの参照専用CLI。export_unmapped_worksheet はDBを読み
    # 取ってローカルにCSVを書くだけで、DBもチャネルも変更しない。
    # $APPLY_OPT_IN のCLIは -Apply が無ければ何も書かないので、警告を出すと
    # 「確認したのに何も起きなかった」という読み方を招く。
    # verify_* / validate_* は読み取り専用（突合・検証のみ）。export_stocktake_sheet は
    # ローカルにCSVを書くだけ。export_to_bq は BigQuery に書くので含めない。
    $readOnly = ($Args -match '(^|\s)--(status|list|report)(\s|$)') -or ($Cli -match '^(inspect|verify|validate)_') -or ($Cli -match '^export_.*worksheets?$') -or ($Cli -eq 'export_stocktake_sheet') -or ($optIn -and -not $Apply)
    # -Args で --dry-run を渡した場合も dry-run として扱う。以前は -DryRun スイッチしか
    # 見ておらず、dry-run の実行に「本実行です」と表示していた（2026-09-23 判明）。
    $argsDryRun = $Args -match '(^|\s)--dry-run(\s|$)'
    if ($DryRun -or $argsDryRun) {
        Write-Host "  [dry-run] 保存しません。出力を確認してから本実行してください。" -ForegroundColor Cyan
    }
    elseif (-not $readOnly) {
        Write-Host "  [注意] 本実行です。--dry-run の出力を確認済みであることを前提とします。" -ForegroundColor Yellow
    }
    Invoke-Expression $cmd
}
finally {
    Section "Cloud SQL Auth Proxy 停止"
    Stop-Process -Id $proxy.Id -Force -ErrorAction SilentlyContinue
}
