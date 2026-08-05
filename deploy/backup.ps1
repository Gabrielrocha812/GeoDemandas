param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path $Destination).Path
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'Destino inválido' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$folder = Join-Path $root "geodemandas-$stamp"
New-Item -ItemType Directory -Path $folder -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath '.\geodemandas.db' -Destination (Join-Path $folder 'geodemandas.db') -ErrorAction Stop
Copy-Item -LiteralPath '.\uploads' -Destination (Join-Path $folder 'uploads') -Recurse -ErrorAction Stop
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $folder 'geodemandas.db') | Out-File (Join-Path $folder 'SHA256SUMS.txt')
Write-Output $folder
