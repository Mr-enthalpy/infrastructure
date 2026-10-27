param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "CitespecResource")
)

$ErrorActionPreference = "Stop"
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
