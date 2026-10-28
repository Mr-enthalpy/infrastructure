# Verify a first-install package against the signed release manifest.
#
# Usage:
#   .\verify_release.ps1 --manifest citespec-resource-manifest.json `
#     --package citespec-resource-<version>-windows-x64-full.zip
#
# Requires Python 3.11+ with the cryptography package. Set PYTHON to choose an
# interpreter; verify_release.py is self-contained and does not import the
# installed Citespec package.
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$python = $env:PYTHON
if (-not $python) {
  $command = Get-Command python -ErrorAction SilentlyContinue
  if ($command) {
    $python = $command.Source
  }
}
if (-not $python) {
  throw "verification failed: Python 3.11+ with cryptography is required (set PYTHON)"
}

& $python (Join-Path $here "verify_release.py") @Rest
exit $LASTEXITCODE
