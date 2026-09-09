[CmdletBinding()]
param(
  [string]$Environment = 'STM32F103RE_creality',
  [switch]$RecoverStale
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot

function Stop-Build([string]$Message) {
  throw $Message
}

$lockPath = Join-Path $repoRoot '.octoprint_run.lock'
$lockCreated = $false

try {
  if (Test-Path $lockPath) {
    $lockText = Get-Content $lockPath -Raw
    $lockPid = 0
    $lockAge = [TimeSpan]::MaxValue
    if ($lockText -match 'PID=(\d+)') { $lockPid = [int]$Matches[1] }
    if ($lockText -match 'UTC=([^\r\n]+)') {
      $lockTime = [DateTime]::Parse($Matches[1]).ToUniversalTime()
      $lockAge = [DateTime]::UtcNow - $lockTime
    }
    $liveProcess = if ($lockPid) { Get-Process -Id $lockPid -ErrorAction SilentlyContinue } else { $null }
    if ($liveProcess -and $lockAge.TotalHours -lt 2) {
      Stop-Build "A build lock is active for PID $lockPid. Wait for the other build to finish."
    }
    Remove-Item $lockPath -Force
  }

  New-Item $lockPath -ItemType File -ErrorAction Stop | Out-Null
  $lockCreated = $true
  Set-Content $lockPath -Value "PID=$PID`r`nUTC=$([DateTime]::UtcNow.ToString('o'))" -Encoding ASCII

  $otherBuilds = Get-CimInstance Win32_Process |
    Where-Object {
      $_.ProcessId -ne $PID -and
      $_.CommandLine -and
      $_.CommandLine -match '(?i)(platformio.*run|scons.*STM32F103RE_creality|STM32F103RE_creality.*scons)'
    }
  if ($otherBuilds) {
    Stop-Build 'Another PlatformIO/SCons build is active. Close it before starting this build.'
  }

  $pioCandidates = @(
    (Join-Path $env:USERPROFILE '.platformio\penv\Scripts\platformio.exe'),
    (Get-Command platformio -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
    (Join-Path $env:APPDATA 'Python\Python311\Scripts\platformio.exe')
  ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

  if (-not $pioCandidates) {
    Stop-Build 'PlatformIO was not found. Open Auto Build Marlin once or install PlatformIO Core, then retry.'
  }
  $pio = $pioCandidates | Select-Object -First 1
  Write-Host "PlatformIO: $pio"
  Write-Host "Environment: $Environment"
  & $pio --version

  $logDirectory = Join-Path $repoRoot '.pio\logs'
  New-Item $logDirectory -ItemType Directory -Force | Out-Null
  $logPath = Join-Path $logDirectory ("build-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

  function Invoke-FirmwareBuild {
    & $pio run --silent -e $Environment 2>&1 | Tee-Object -FilePath $logPath
    return $LASTEXITCODE
  }

  $exitCode = Invoke-FirmwareBuild
  if ($exitCode -ne 0 -and $RecoverStale -and (Select-String -Path $logPath -Pattern '\.scons311\.dblite|missing|not found|No such file' -Quiet)) {
    Write-Warning 'The build failed with a stale generated-state symptom; cleaning ignored state and retrying once.'
    & git clean -fdx -e buildroot/bin/build_firmware.ps1
    if ($LASTEXITCODE -ne 0) { Stop-Build 'git clean -fdx failed; no retry was attempted.' }
    $exitCode = Invoke-FirmwareBuild
  }
  if ($exitCode -ne 0) {
    Stop-Build "PlatformIO failed with exit code $exitCode. See $logPath"
  }

  $artifact = Get-ChildItem (Join-Path $repoRoot ".pio\build\$Environment") -Filter '*.bin' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $artifact) {
    Stop-Build 'PlatformIO returned success but no firmware .bin was found.'
  }
  $hash = (Get-FileHash $artifact.FullName -Algorithm SHA256).Hash
  Write-Host "Firmware: $($artifact.FullName)"
  Write-Host "Size: $($artifact.Length) bytes"
  Write-Host "SHA256: $hash"
  Write-Host "Build log: $logPath"
}
finally {
  if ($lockCreated -and (Test-Path $lockPath)) {
    Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
  }
}