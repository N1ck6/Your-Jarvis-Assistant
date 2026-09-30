# One-time setup: Python venv, dependencies, models, Ollama model.
# Run from the project root:  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "== Python 3.12 venv" -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Нужен Python 3.12: https://www.python.org/downloads/ (галочка 'py launcher')" }
}
$py = ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip -q

Write-Host "== PyTorch CPU (для голоса Silero)" -ForegroundColor Cyan
& $py -m pip install -q torch --index-url https://download.pytorch.org/whl/cpu

Write-Host "== Зависимости" -ForegroundColor Cyan
& $py -m pip install -q -r requirements.txt

Write-Host "== Модели (VAD, wake word, STT, голос)" -ForegroundColor Cyan
& $py -m assistant.setup_models
if ($LASTEXITCODE -ne 0) { throw "Не удалось скачать модели, запустите скрипт ещё раз" }

Write-Host "== Локальная LLM" -ForegroundColor Cyan
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    ollama pull qwen3:8b
    ollama pull qwen3-vl:4b-instruct   # screen help when Gemini is unavailable
} else {
    Write-Warning "Ollama не найдена. Установите https://ollama.com/download и выполните: ollama pull qwen3:8b; ollama pull qwen3-vl:4b-instruct"
}

if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
Write-Host ""
Write-Host "Готово. Впишите ключи GEMINI_API_KEY и GROQ_API_KEY в .env и запустите run.bat" -ForegroundColor Green
