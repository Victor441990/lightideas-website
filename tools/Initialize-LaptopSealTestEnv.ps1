param([string]$Path = (Join-Path $PSScriptRoot '..\.env.laptopseal.test'))

$resolved = [IO.Path]::GetFullPath($Path)
if (-not $resolved.EndsWith('.env.laptopseal.test', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to write anything except .env.laptopseal.test.'
}
if (-not (Test-Path -LiteralPath $resolved)) { throw 'Test environment template is missing.' }

function New-Secret([int]$Bytes = 32) {
    $buffer = [byte[]]::new($Bytes)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($buffer).Replace('+', '-').Replace('/', '_')
}

$lines = Get-Content -LiteralPath $resolved
$replacement = @{
    'FLASK_SECRET_KEY' = (New-Secret 32)
    'LAPTOPSEAL_LICENSE_PEPPER' = (New-Secret 48)
    'LAPTOPSEAL_DELIVERY_KEY' = (New-Secret 32)
}
$updated = foreach ($line in $lines) {
    $matched = $false
    foreach ($name in $replacement.Keys) {
        if ($line -eq ($name + '=__GENERATED_BY_INITIALIZER__')) {
            $matched = $true
            $name + '=' + $replacement[$name]
            break
        }
    }
    if (-not $matched) { $line }
}
Set-Content -LiteralPath $resolved -Value $updated -Encoding UTF8 -ErrorAction Stop
Write-Output 'Local test secrets generated. Values were not displayed.'
