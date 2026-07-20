# 시크릿 버전 주입 — **운영자가 직접 실행**(값은 화면에 출력되지 않음). infra/README.md 2단계.
# server/scripts/.env 에 이미 있는 키는 자동으로 읽고(토스·Anthropic·텔레그램),
# 없는 키(API_KEY 새 강한 값·DATABASE_URL·NAVER 2종)는 비표시 프롬프트로 입력받는다.
# 실행: powershell -File infra\inject-secrets.ps1
param([string]$Project = "toss-trader-kr")
$ErrorActionPreference = "Stop"

$envPath = Join-Path $PSScriptRoot "..\server\scripts\.env"
$fromEnv = @{}
if (Test-Path $envPath) {
    foreach ($ln in Get-Content $envPath) {
        if ($ln -match '^\s*([A-Za-z_]+)\s*=\s*(.+)$') {
            $fromEnv[$Matches[1].ToUpper()] = $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
}

$names = "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "ANTHROPIC_API_KEY", "API_KEY",
         "DATABASE_URL", "NOTIFY_TELEGRAM_BOT_TOKEN", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"

foreach ($n in $names) {
    $v = $fromEnv[$n]
    if ($v) {
        Write-Host "$n <- server/scripts/.env"
    } else {
        $sec = Read-Host "$n 값 입력(빈 입력 = 건너뜀, 화면 미표시)" -AsSecureString
        $v = [System.Net.NetworkCredential]::new("", $sec).Password
    }
    if (-not $v) { Write-Host "SKIP: $n"; continue }
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmp, $v)   # 개행 없이 — 값에 \n 이 붙으면 인증 실패
        gcloud secrets versions add $n --data-file="$tmp" --project=$Project | Out-Null
        Write-Host "OK: $n"
    } finally {
        Remove-Item $tmp -Force
    }
}
Write-Host "`n완료 — 다음: infra 에서 terraform apply (README 3단계)"
