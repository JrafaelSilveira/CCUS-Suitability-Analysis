# build_exe.ps1 — build CCUSApp.exe as a portable Windows folder under dist/.
# Run from the project root:    powershell -ExecutionPolicy Bypass -File build_exe.ps1

$ErrorActionPreference = "Stop"

Write-Host "[build] cleaning previous build artifacts..." -ForegroundColor Cyan
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist  }

Write-Host "[build] regenerating logo.ico from logo.png..." -ForegroundColor Cyan
python -c @'
from PIL import Image
img = Image.open("assets/logo.png").convert("RGBA")
img.save("assets/logo.ico", format="ICO", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
print("logo.ico regenerated")
'@

Write-Host "[build] running PyInstaller (this takes several minutes)..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm CCUSApp.spec

if (-not (Test-Path "dist/CCUSApp/CCUSApp.exe")) {
    Write-Host "[build] FAILED: dist/CCUSApp/CCUSApp.exe not produced" -ForegroundColor Red
    exit 1
}

# Copy assets/data/output next to the .exe (visible top-level), not buried in
# _internal/. The app chdir's into the .exe folder at startup so these resolve.
Write-Host "[build] copying assets/, data/, output/figures/ alongside CCUSApp.exe..." -ForegroundColor Cyan
Copy-Item -Recurse -Force assets       dist/CCUSApp/assets
Copy-Item -Recurse -Force data         dist/CCUSApp/data
New-Item   -ItemType Directory -Force  dist/CCUSApp/output | Out-Null
if (Test-Path output/figures) {
    Copy-Item -Recurse -Force output/figures dist/CCUSApp/output/figures
}

$size = (Get-ChildItem -Recurse dist/CCUSApp | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("[build] OK: dist/CCUSApp/  (~{0:N0} MB)" -f $size) -ForegroundColor Green
Write-Host "[build] Ship the whole dist/CCUSApp/ folder (assets/, data/, output/ are visible top-level)." -ForegroundColor Green
