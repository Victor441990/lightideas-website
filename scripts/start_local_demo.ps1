$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$biztrackRoot = 'C:\Users\user\Desktop\biztrack-landing page\biztrack-landing'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$port = '3107'
$baseUrl = "http://127.0.0.1:$port"

$integrationCredential = Get-Credential -UserName 'local-integration' -Message 'Create a temporary local Biztrack integration token (not a production password)'
$env:BIZTRACK_LIGHTIDEAS_INTEGRATION_TOKEN = $integrationCredential.GetNetworkCredential().Password
$env:WAITLIST_DB_PATH = ':memory:'
$env:NODE_ENV = 'test'
$env:PORT = $port
$nodeProcess = Start-Process -FilePath 'node.exe' -ArgumentList 'server.mjs' -WorkingDirectory $biztrackRoot -WindowStyle Hidden -PassThru

try {
    Start-Sleep -Seconds 2
    $registrations = @(
        @{ fullName='Ada Demo'; businessName='Ada Retail'; email='ada-demo@example.test'; businessType='Retail'; willingness='Yes'; utm_source='instagram' },
        @{ fullName='Bola Demo'; businessName='Bola Services'; email='bola-demo@example.test'; businessType='Professional services'; willingness='Maybe'; utm_source='facebook' },
        @{ fullName='Test Record'; businessName='Demo Test'; email='excluded-demo@example.test'; businessType='Other'; willingness='No'; testMode=$true; utm_source='test' }
    )
    foreach ($registration in $registrations) {
        Invoke-RestMethod -Uri "$baseUrl/api/register" -Method Post -ContentType 'application/json' -Body ($registration | ConvertTo-Json -Compress) | Out-Null
    }
    for ($i = 0; $i -lt 3; $i++) {
        $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        Invoke-RestMethod -Uri "$baseUrl/api/analytics" -WebSession $session -Method Post -ContentType 'application/json' -Body (@{ event='visit'; utm_source=@('instagram','facebook','google')[$i] } | ConvertTo-Json -Compress) | Out-Null
    }
    $testSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    Invoke-RestMethod -Uri "$baseUrl/api/analytics" -WebSession $testSession -Method Post -ContentType 'application/json' -Body (@{ event='visit'; testMode=$true; utm_source='test' } | ConvertTo-Json -Compress) | Out-Null

    $adminCredential = Get-Credential -UserName 'demo-admin@local.test' -Message 'Create the temporary local Light Ideas administrator password'
    $env:LIGHTIDEAS_DEMO_PASSWORD = $adminCredential.GetNetworkCredential().Password
    $env:FLASK_SECRET_KEY = [Convert]::ToBase64String([byte[]](1..48 | ForEach-Object { Get-Random -Maximum 256 }))
    $env:LIGHTIDEAS_LOCAL_DEMO = '1'
    $env:ADMIN_PASSWORD_HASH = & $python (Join-Path $PSScriptRoot 'local_demo_password_hash.py')
    Remove-Item Env:LIGHTIDEAS_DEMO_PASSWORD
    $env:MONGO_URI = 'mongodb://127.0.0.1:27017/lightideas_local_demo'
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:TRUST_PROXY_HEADERS = 'false'
    $env:BIZTRACK_SERVICE_URL = $baseUrl
    $env:BIZTRACK_INTEGRATION_TOKEN = $env:BIZTRACK_LIGHTIDEAS_INTEGRATION_TOKEN

    Write-Host 'Local services ready. Flask admin login: http://127.0.0.1:5050/victor-admin' -ForegroundColor Green
    & $python (Join-Path $PSScriptRoot 'local_demo_server.py')
}
finally {
    Remove-Item Env:LIGHTIDEAS_DEMO_PASSWORD -ErrorAction SilentlyContinue
    Stop-Process -Id $nodeProcess.Id -ErrorAction SilentlyContinue
}
