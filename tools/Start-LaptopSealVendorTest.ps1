param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\.env.laptopseal.test'),
    [switch]$EnableVendorPayments
)
$ErrorActionPreference = 'Stop'

$portProbe = [System.Net.Sockets.TcpClient]::new()
try {
    $portProbe.Connect('127.0.0.1', 5050)
    throw 'Port 5050 is already in use. Identify and stop only the existing isolated test backend before starting this runner.'
} catch [System.Net.Sockets.SocketException] {
    # No listener: safe to start the isolated runner.
} finally {
    $portProbe.Dispose()
}

if (-not (Test-Path -LiteralPath $EnvFile)) { throw 'Isolated test configuration is missing.' }
Get-Content -LiteralPath $EnvFile | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { Set-Item -Path ('Env:' + $matches[1].Trim()) -Value $matches[2] }
}
if ($env:MONGO_DB_NAME -ne 'laptopseal_test') { throw 'Selected database must be laptopseal_test.' }
foreach ($name in 'MONGO_URI','LAPTOPSEAL_LICENSE_PEPPER','LAPTOPSEAL_VENDOR_SIGNING_PRIVATE_KEY_PEM') {
    if (-not (Get-Item -Path ('Env:' + $name) -ErrorAction SilentlyContinue).Value) {
        throw "$name is required for isolated vendor testing."
    }
}
if ($env:LAPTOPSEAL_VENDOR_TEST_CODE_CAPTURE -ne '1') {
    throw 'Local code capture must be enabled to avoid sending real email.'
}
$env:LAPTOPSEAL_TEST_MODE = '1'
$env:LAPTOPSEAL_PAYMENTS_ENABLED = '0'
if ($EnableVendorPayments) {
    if ($env:PAYSTACK_PUBLIC_KEY -notlike 'pk_test_*' -or $env:PAYSTACK_SECRET_KEY -notlike 'sk_test_*') {
        throw 'Only Paystack test keys may enable isolated vendor checkout.'
    }
    $env:LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED = '1'
} else {
    $env:LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED = '0'
}
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
# The current admin login uses a hash. Derive it only for this isolated
# process when an older test-only env file still holds ADMIN_PASSWORD.
if (-not $env:ADMIN_PASSWORD_HASH -and $env:ADMIN_PASSWORD) {
    $generatedHash = & $python -c "import os; from werkzeug.security import generate_password_hash; print(generate_password_hash(os.environ['ADMIN_PASSWORD']))"
    if ($LASTEXITCODE -ne 0 -or -not $generatedHash) { throw 'Test administrator hash could not be prepared.' }
    $env:ADMIN_PASSWORD_HASH = $generatedHash.Trim()
}
Remove-Item Env:ADMIN_PASSWORD -ErrorAction SilentlyContinue
Push-Location $project
try { & $python -c "from app import app; app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)" }
finally { Pop-Location }
