# One-time setup: Python venv, dependencies, models, Ollama models.
# Run from the project root:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1                # Silero voice, CPU
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -JarvisVoice   # + Jarvis's cloned voice (NVIDIA GPU)
param([switch]$JarvisVoice)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "== Python 3.12 venv" -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Нужен Python 3.12: https://www.python.org/downloads/ (галочка 'py launcher')" }
}
$py = ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip -q

if ($JarvisVoice) {
    Write-Host "== PyTorch с CUDA (голос Джарвиса на видеокарте)" -ForegroundColor Cyan
    & $py -m pip install -q "torch==2.11.0" "torchaudio==2.11.0" --index-url https://download.pytorch.org/whl/cu130
} else {
    Write-Host "== PyTorch CPU (голос Silero)" -ForegroundColor Cyan
    & $py -m pip install -q torch --index-url https://download.pytorch.org/whl/cpu
}

Write-Host "== Зависимости" -ForegroundColor Cyan
& $py -m pip install -q -r requirements.txt

if ($JarvisVoice) {
    Write-Host "== Клон голоса Джарвиса (ESpeech-TTS-1 / F5-TTS)" -ForegroundColor Cyan
    & $py -m pip install -q "f5-tts==1.1.22" --no-deps
    & $py -m pip install -q -r requirements-voice.txt
}

Write-Host "== Модели (VAD, wake word, STT, голос, реплики Джарвиса)" -ForegroundColor Cyan
& $py -m assistant.setup_models
if ($LASTEXITCODE -ne 0) { throw "Не удалось скачать модели, запустите скрипт ещё раз" }

Write-Host "== Локальные модели" -ForegroundColor Cyan
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    ollama pull qwen3:8b
    ollama pull qwen3-vl:4b-instruct   # screen help when Gemini is unavailable
} else {
    Write-Warning "Ollama не найдена. Установите https://ollama.com/download и выполните: ollama pull qwen3:8b; ollama pull qwen3-vl:4b-instruct"
}

if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
Write-Host ""
Write-Host "Готово. Запустите run.bat, ключи GROQ_API_KEY и GEMINI_API_KEY можно вписать в трее -> Настройки -> ИИ" -ForegroundColor Green
