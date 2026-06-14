param(
    [ValidateSet("TEST", "PROD")]
    [string]$EnvName = "",
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$AppFile = Join-Path $ProjectDir "app.py"

if ([string]::IsNullOrWhiteSpace($EnvName)) {
    $EnvName = if ($env:APP_ENV) { $env:APP_ENV.ToUpperInvariant() } else { "TEST" }
}

if ($Port -le 0) {
    $Port = if ($EnvName -eq "PROD") { 8502 } else { 8501 }
}

$env:APP_ENV = $EnvName
$Url = "http://127.0.0.1:$Port"

if (-not (Test-Path $PythonExe)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("Python environment not found:`n$PythonExe", "WIN54 Launcher")
    exit 1
}

if (-not (Test-Path $AppFile)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("app.py not found:`n$AppFile", "WIN54 Launcher")
    exit 1
}

$PortOpen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue

if (-not $PortOpen) {
    Start-Process -FilePath $PythonExe `
        -ArgumentList "-m streamlit run app.py --server.port $Port --server.address 127.0.0.1 --server.headless true" `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Hidden

    $Ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
            $Ready = $true
            break
        } catch {
            $Ready = $false
        }
    }

    if (-not $Ready) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show("WIN54 $EnvName started, but the browser URL did not respond yet.`nTry opening $Url manually.", "WIN54 Launcher")
    }
}

Start-Process $Url
