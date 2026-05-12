# =============================================================================
# Siyadah Phase 0.2 — populate piece_registry
# =============================================================================
# 
# HOW TO USE:
#   1. Save this file in your orchestrator folder:
#        C:\Users\d10-2\Desktop\siyadah-orhestrtor\siyadah-orchestrator\phase-0.2.ps1
#   2. Open PowerShell, activate venv, then run it:
#        cd C:\Users\d10-2\Desktop\siyadah-orhestrtor\siyadah-orchestrator
#        .\.venv\Scripts\Activate.ps1
#        .\phase-0.2.ps1
# =============================================================================

Write-Host ""
Write-Host "=== Phase 0.2 — populating piece_registry ===" -ForegroundColor Cyan
Write-Host ""

# Step 1: Load .env into the current PowerShell session
if (-not (Test-Path ".env")) {
    Write-Host "ERROR: .env not found in $(Get-Location)" -ForegroundColor Red
    Write-Host "Make sure you're running this from the orchestrator folder." -ForegroundColor Red
    exit 1
}

Write-Host "Step 1: Loading .env ..." -ForegroundColor Yellow
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=][^=]*?)\s*=\s*(.*)$') {
        $name = $matches[1].Trim()
        $value = $matches[2].Trim().Trim('"').Trim("'")
        [System.Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

# Force local-dev TLS skip (we're on a local Postgres without certs)
$env:SIYADAH_SKIP_PG_SSL = "1"

# Step 2: Verify required vars are set
Write-Host ""
Write-Host "Step 2: Verifying environment ..." -ForegroundColor Yellow

$required = @("DATABASE_URL", "AP_BASE_URL", "AP_EMAIL", "AP_PASSWORD")
$missing = @()
foreach ($var in $required) {
    $value = [System.Environment]::GetEnvironmentVariable($var, "Process")
    if ([string]::IsNullOrEmpty($value)) {
        Write-Host "  [MISSING] $var" -ForegroundColor Red
        $missing += $var
    } else {
        Write-Host "  [OK]      $var" -ForegroundColor Green
    }
}

if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "ERROR: missing required env vars in .env: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "Add them to your .env file and re-run." -ForegroundColor Red
    exit 1
}

# Step 3: Run the sync (this is the long part — 2 to 3 minutes)
Write-Host ""
Write-Host "Step 3: Running sync_pieces --full (takes 2-3 minutes) ..." -ForegroundColor Yellow
Write-Host ""

python -m scripts.sync_pieces --full

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: sync_pieces exited with code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

# Step 4: Verify the result
Write-Host ""
Write-Host "Step 4: Verifying piece_registry ..." -ForegroundColor Yellow
Write-Host ""

docker exec siyadah-pg psql -U sy -d siyadah -c "SELECT COUNT(*) AS total_pieces, MAX(last_synced) AS last_sync, COUNT(*) FILTER (WHERE auth_type IS NOT NULL) AS with_auth_type FROM piece_registry;"

Write-Host ""
Write-Host "=== Phase 0.2 complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "If total_pieces is around 688, share the output and we move to 0.3."
Write-Host "If it's 0, copy any error messages from above and report back."
