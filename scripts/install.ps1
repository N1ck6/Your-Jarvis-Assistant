# Jarvis setup: everything a fresh clone needs. Safe to run again (it repairs and updates what is missing).
# Easiest: double-click install.bat in the project folder. Or from the project root:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1                 # asks about the cloned voice if an NVIDIA GPU is found
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -JarvisVoice    # + Jarvis's cloned voice (NVIDIA, ~5 GB)
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoJarvisVoice  # Silero voice only, no question
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoOllama       # no local models (cloud keys only)
#
# Missing programs are installed through winget, or downloaded from the official sites if winget is absent:
# Python 3.12 (python.org), Microsoft Visual C++ runtime, Ollama (ollama.com).
param([switch]$JarvisVoice, [switch]$NoJarvisVoice, [switch]$NoOllama)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location (Split-Path -Parent $PSScriptRoot)
$Root = (Get-Location).Path
$Tmp = Join-Path $env:TEMP "jarvis-setup"
New-Item -ItemType Directory -Force $Tmp | Out-Null

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   $text" -ForegroundColor Green }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Has-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }
# Runs a native command only to see whether it succeeds (exit code 0). Windows PowerShell turns a redirected
# stderr line into a terminating error under "Stop", so the check runs with "Continue".
function Probe([scriptblock]$block) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $block 2>&1 | Out-Null } catch { $global:LASTEXITCODE = 1 }
    $ErrorActionPreference = $old
    return ($LASTEXITCODE -eq 0)
}

function Winget-Install($id, $scope) {
    if (-not (Has-Winget)) { return $false }
    $wargs = @("install", "-e", "--id", $id, "--silent", "--accept-package-agreements", "--accept-source-agreements")
    if ($scope) { $wargs += @("--scope", $scope) }
    & winget @wargs | Out-Host
    Refresh-Path
    # 0 = installed; 0x8A15002B = already installed, nothing to upgrade
    return ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189)
}

function Download($url, $name) {
    $file = Join-Path $Tmp $name
    Write-Host "   Скачиваю $url"
    Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
    return $file
}

# ---------------------------------------------------------------- Python 3.12
function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Probe { py -3.12 -c "import sys" }) { return @("py", "-3.12") }
    }
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe")) {
        if (Test-Path $p) { return @($p) }
    }
    return $null
}

Step "Python 3.12"
$python = Find-Python
if (-not $python) {
    Write-Host "   Python 3.12 не найден, устанавливаю (для текущего пользователя)"
    if (-not (Winget-Install "Python.Python.3.12" "user")) {
        $exe = Download "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" "python-3.12.10-amd64.exe"
        Start-Process $exe -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1" -Wait
        Refresh-Path
    }
    $python = Find-Python
    if (-not $python) { throw "Не удалось установить Python 3.12. Установите вручную: https://www.python.org/downloads/release/python-31210/" }
}
Ok "Python: $($python -join ' ')"

# ---------------------------------------------------------------- Visual C++ runtime (onnxruntime, PyTorch, Qt)
Step "Microsoft Visual C++ Runtime"
$vc = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
if ($vc -and $vc.Installed -eq 1) {
    Ok "уже установлен"
} else {
    Write-Host "   Не найден, устанавливаю (Windows может спросить разрешение администратора)"
    if (-not (Winget-Install "Microsoft.VCRedist.2015+.x64" $null)) {
        $exe = Download "https://aka.ms/vs/17/release/vc_redist.x64.exe" "vc_redist.x64.exe"
        Start-Process $exe -ArgumentList "/install", "/quiet", "/norestart" -Wait
    }
}

# ---------------------------------------------------------------- the cloned voice: ask if an NVIDIA GPU is there
if (-not $JarvisVoice -and -not $NoJarvisVoice) {
    $vram = 0
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        try { $vram = [int]((& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1).Trim()) } catch { }
    }
    if ($vram -ge 6000) {
        Write-Host ""
        Write-Host "Найдена видеокарта NVIDIA ($vram МБ). Можно поставить клон голоса Джарвиса из фильма" -ForegroundColor Yellow
        Write-Host "(ещё ~5 ГБ загрузок, ~1,2 ГБ видеопамяти). Без него — голос Silero «Евгений»." -ForegroundColor Yellow
        try { $answer = Read-Host "Установить клон голоса? [y/N]" } catch { $answer = "" }
        $JarvisVoice = $answer -match "^(y|yes|д|да)$"
    }
}

# ---------------------------------------------------------------- venv and packages
Step "Виртуальное окружение .venv"
$py = Join-Path $Root ".venv\Scripts\python.exe"
$fresh = $true
if (Test-Path $py) {
    $fresh = -not (Probe { & $py -c "import sys; assert sys.version_info[:2] == (3, 12)" })
    if ($fresh) { Write-Host "   .venv повреждено или другой версии Python, создаю заново"; Remove-Item -Recurse -Force ".venv" }
}
if ($fresh) {
    $exe = $python[0]; $rest = @($python | Select-Object -Skip 1)
    & $exe @rest -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать .venv" }
}
& $py -m pip install --upgrade pip -q

if ($JarvisVoice) {
    Step "PyTorch с CUDA (голос Джарвиса на видеокарте)"
    & $py -m pip install -q "torch==2.11.0" "torchaudio==2.11.0" --index-url https://download.pytorch.org/whl/cu130
} else {
    Step "PyTorch для процессора (голос Silero)"
    if (-not (Probe { & $py -c "import torch" })) { & $py -m pip install -q torch --index-url https://download.pytorch.org/whl/cpu }
}
if ($LASTEXITCODE -ne 0) { throw "PyTorch не установился, проверьте интернет и запустите установку ещё раз" }

Step "Библиотеки Python"
& $py -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Библиотеки не установились, запустите установку ещё раз" }

if ($JarvisVoice) {
    Step "Клон голоса Джарвиса (ESpeech-TTS-1 / F5-TTS)"
    & $py -m pip install -q "f5-tts==1.1.22" --no-deps
    & $py -m pip install -q -r requirements-voice.txt
    if ($LASTEXITCODE -ne 0) { throw "Клон голоса не установился" }
}

Step "Модели: VAD, кодовое слово, распознавание, голос, реплики Джарвиса (~600 МБ)"
& $py -m assistant.setup_models
if ($LASTEXITCODE -ne 0) { throw "Не удалось скачать модели, запустите установку ещё раз" }
if ($JarvisVoice) {
    & $py -c "from assistant.config import save_override; save_override('tts.voice', 'clone:jarvis-remaster')"
    Ok "голос по умолчанию — клон Джарвиса (модель ~1,3 ГБ скачается при первом запуске)"
}

# ---------------------------------------------------------------- Ollama: the local model (offline answers, command router)
function Ollama-Exe {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $p = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $p) { return $p }
    return $null
}
function Ollama-Up {
    try { Invoke-RestMethod "http://127.0.0.1:11434/api/version" -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

if ($NoOllama) {
    Write-Warning "Без Ollama: нужны ключи GROQ_API_KEY / GEMINI_API_KEY, иначе Джарвис понимает только готовые команды"
} else {
    Step "Ollama (локальная модель)"
    $ollama = Ollama-Exe
    if (-not $ollama) {
        Write-Host "   Ollama не найдена, устанавливаю"
        if (-not (Winget-Install "Ollama.Ollama" $null)) {
            $exe = Download "https://ollama.com/download/OllamaSetup.exe" "OllamaSetup.exe"
            Start-Process $exe -ArgumentList "/VERYSILENT", "/NORESTART" -Wait
            Refresh-Path
        }
        $ollama = Ollama-Exe
    }
    if (-not $ollama) {
        Write-Warning "Ollama не установилась. Поставьте вручную: https://ollama.com/download и запустите install.bat ещё раз"
    } else {
        if (-not (Ollama-Up)) {
            $app = Join-Path (Split-Path $ollama) "ollama app.exe"
            if (Test-Path $app) { Start-Process $app } else { Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden }
            for ($i = 0; $i -lt 30 -and -not (Ollama-Up); $i++) { Start-Sleep 1 }
        }
        Write-Host "   qwen3:8b (~5 ГБ) — ответы и понимание свободных фраз"
        & $ollama pull qwen3:8b
        Write-Host "   qwen3-vl:4b-instruct (~3 ГБ) — помощь с экраном без облака"
        & $ollama pull qwen3-vl:4b-instruct
        if ($LASTEXITCODE -ne 0) { Write-Warning "Модели Ollama не скачались. Позже: ollama pull qwen3:8b" }
    }
}

# ---------------------------------------------------------------- done
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
Set-Content -Path ".venv\.jarvis-installed" -Value (Get-Date -Format s) -Encoding ascii
Write-Host ""
Write-Host "Готово. Запуск: run.bat (Джарвис появится в трее). Скажите: «Джарвис, который час?»" -ForegroundColor Green
Write-Host "Ключи GROQ_API_KEY и GEMINI_API_KEY (бесплатные) — в трее: Настройки -> ИИ." -ForegroundColor Green
