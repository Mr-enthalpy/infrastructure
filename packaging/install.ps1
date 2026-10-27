# Install the Citespec Resource client from an extracted full zip.
#
# Before running, verify the zip and this script with verify_release.ps1 (or the
# published SHA-256). -Verify <sha256> is required: use the installer_sha256
# value from the signed manifest's first_install entry.
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "CitespecResource"),
  [string]$Verify = ""
)

$ErrorActionPreference = "Stop"

if (-not $Verify) {
  throw "-Verify SHA256 is required; obtain it from the signed release manifest"
}

$expected = $Verify.ToLowerInvariant()
if ($expected -notmatch '^[0-9a-f]{64}$') {
  throw "-Verify must be a 64-character SHA-256"
}
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant()
if ($actual -ne $expected) {
  throw "install.ps1 SHA-256 mismatch: expected $expected, actual $actual"
}
Write-Host "Verified install.ps1 SHA-256: $actual"

$source = $PSScriptRoot
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $InstallDir -Recurse -Force

$launcher = Join-Path $InstallDir "citespec-resource.exe"
if (-not (Test-Path $launcher)) {
  throw "launcher not found after install: $launcher"
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$parts = @($userPath -split ";" | Where-Object { $_ })
if ($parts -notcontains $InstallDir) {
  $newPath = (@($parts) + $InstallDir) -join ";"
  [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
  Write-Host "Added to user PATH: $InstallDir"
}

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$shortcutPath = Join-Path $startMenu "Citespec Resource.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.Arguments = "gui"
$shortcut.WorkingDirectory = $InstallDir
$shortcut.Save()

Write-Host "Installed Citespec Resource to $InstallDir"
Write-Host "Open a new terminal, then run: citespec-resource setup --server <address> --user <linux-user>"
