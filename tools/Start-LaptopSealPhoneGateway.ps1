param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\.env.laptopseal.test')
)
$ErrorActionPreference = 'Stop'

$portProbe = [System.Net.Sockets.TcpClient]::new()
try {
    $portProbe.Connect('127.0.0.1', 5051)
    throw 'Port 5051 is already in use. Identify the existing process before starting the phone gateway.'
} catch [System.Net.Sockets.SocketException] {
    # No listener.
} finally {
    $portProbe.Dispose()
}

if (-not (Test-Path -LiteralPath $EnvFile)) { throw 'Isolated test configuration is missing.' }
Get-Content -LiteralPath $EnvFile | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { Set-Item -Path ('Env:' + $matches[1].Trim()) -Value $matches[2] }
}
if ($env:MONGO_DB_NAME -ne 'laptopseal_test') { throw 'Phone gateway requires laptopseal_test.' }
if ($env:PAYSTACK_PUBLIC_KEY -notlike 'pk_test_*' -or $env:PAYSTACK_SECRET_KEY -notlike 'sk_test_*') {
    throw 'Phone gateway requires Paystack TEST keys.'
}
$env:LAPTOPSEAL_TEST_MODE = '1'
$env:LAPTOPSEAL_PAYMENTS_ENABLED = '0'
$env:LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED = '0'

$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
Push-Location $project
try { & $python .\tools\laptopseal_phone_gateway.py }
finally { Pop-Location }
