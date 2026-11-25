param([string]$EnvFile = (Join-Path $PSScriptRoot '..\.env.laptopseal.test'))

if (-not (Test-Path -LiteralPath $EnvFile)) { throw 'Local test environment file is missing.' }
Get-Content -LiteralPath $EnvFile | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { Set-Item -Path ('Env:' + $matches[1].Trim()) -Value $matches[2] }
}
foreach ($name in 'PAYSTACK_PUBLIC_KEY','PAYSTACK_SECRET_KEY','MONGO_URI','MONGO_DB_NAME','BREVO_API_KEY','LAPTOPSEAL_LICENSE_PEPPER','LAPTOPSEAL_DELIVERY_KEY') {
    if (-not (Get-Item -Path ('Env:' + $name) -ErrorAction SilentlyContinue).Value) { throw "$name is required before test checkout." }
}
if ($env:PAYSTACK_PUBLIC_KEY -notlike 'pk_test_*' -or $env:PAYSTACK_SECRET_KEY -notlike 'sk_test_*') {
    throw 'Only Paystack test keys are accepted in the isolated test environment.'
}
if ($env:MONGO_DB_NAME.Trim().ToLowerInvariant() -eq 'lightideas') {
    throw 'LaptopSeal payment testing cannot use the production database name.'
}
if ($env:MONGO_DB_NAME -notmatch '^[A-Za-z0-9_-]{1,63}$') {
    throw 'MONGO_DB_NAME must be a valid non-production database name.'
}
if ($env:LAPTOPSEAL_PAYMENTS_ENABLED -ne '1') { throw 'Checkout remains disabled. Set to 1 only after credential/database verification.' }
$env:LAPTOPSEAL_TEST_MODE = '1'
$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
# The current admin login uses a hash. Derive it only for this isolated
# process when an older test-only env file still holds ADMIN_PASSWORD.
if (-not $env:ADMIN_PASSWORD_HASH -and $env:ADMIN_PASSWORD) {
    $generatedHash = & $python -c "import os; from werkzeug.security import generate_password_hash; print(generate_password_hash(os.environ['ADMIN_PASSWORD']))"
    if ($LASTEXITCODE -ne 0 -or -not $generatedHash) { throw 'Test administrator hash could not be prepared.' }
    $env:ADMIN_PASSWORD_HASH = $generatedHash.Trim()
}
Remove-Item Env:ADMIN_PASSWORD -ErrorAction SilentlyContinue
& $python app.py
