# Builds an installable folder dist\Jarvis (Jarvis.exe + config + data) and a zip of it.
# Uses a separate build venv with CPU PyTorch so the build does not carry CUDA (~3 GB).
# Run from the project root:  powershell -ExecutionPolicy Bypass -File scripts\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".build-venv\Scripts\python.exe")) {
    py -3.12 -m venv .build-venv
}
$py = ".build-venv\Scripts\python.exe"
& $py -m pip install -q --upgrade pip
& $py -m pip install -q torch --index-url https://download.pytorch.org/whl/cpu
& $py -m pip install -q -r requirements.txt pyinstaller

& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build scripts\jarvis.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }

# Next to Jarvis.exe: settings, scenarios and an empty data folder; models are downloaded on first start.
Copy-Item -Recurse -Force config dist\Jarvis\config
New-Item -ItemType Directory -Force dist\Jarvis\data | Out-Null
if (Test-Path ".env.example") { Copy-Item ".env.example" "dist\Jarvis\.env.example" }
$version = (& $py -c "import assistant; print(assistant.__version__)").Trim()
Compress-Archive -Force -Path dist\Jarvis -DestinationPath "dist\Jarvis-$version.zip"
Write-Host "Готово: dist\Jarvis-$version.zip (распаковать и запустить Jarvis.exe)" -ForegroundColor Green
