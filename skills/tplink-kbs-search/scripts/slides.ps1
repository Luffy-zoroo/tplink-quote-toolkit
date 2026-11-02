# Export selected slides of a .pptx to PNG
# (for pulling official topology / UI screenshots out of solution decks)
#
# Usage:
#   <shell> -NoProfile -ExecutionPolicy Bypass -File slides.ps1 -Pptx "D:\deck.pptx" -Pages 9,16 -Out "D:\out"
#
# Requirement: Microsoft Office PowerPoint installed locally.

param(
  [Parameter(Mandatory=$true)][string]$Pptx,
  [Parameter(Mandatory=$true)][string]$Pages,
  [Parameter(Mandatory=$true)][string]$Out,
  [int]$Width = 1920,
  [int]$Height = 1080
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Pptx)) { Write-Output "ERROR: file not found $Pptx"; exit 1 }
if (-not (Test-Path $Out)) { New-Item -ItemType Directory -Force -Path $Out | Out-Null }

$list = @()
foreach ($seg in ($Pages -split ',')) {
  $seg = $seg.Trim()
  if ($seg -match '^(\d+)\s*-\s*(\d+)$') {
    $a = [int]$Matches[1]; $b = [int]$Matches[2]
    for ($i = $a; $i -le $b; $i++) { $list += $i }
  } elseif ($seg -match '^\d+$') {
    $list += [int]$seg
  }
}

$app  = New-Object -ComObject PowerPoint.Application
$pres = $null
try {
  $pres  = $app.Presentations.Open($Pptx, $true, $false, $false)
  $total = $pres.Slides.Count
  Write-Output ("total slides: " + $total)
  foreach ($i in $list) {
    if ($i -lt 1 -or $i -gt $total) { Write-Output ("skip out-of-range: " + $i); continue }
    $p = Join-Path $Out ("s{0:d2}.png" -f $i)
    $pres.Slides.Item($i).Export($p, "PNG", $Width, $Height)
    Write-Output ("exported: " + $p)
  }
}
finally {
  if ($pres) { $pres.Close() }
  $app.Quit()
  [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
}
Write-Output "done"
