# Light Jarvis voice: fine-tunes Piper on the dataset from make_voice_dataset.py inside Docker (Linux + your GPU).
#   powershell -ExecutionPolicy Bypass -File scripts\piper\train.ps1            # train (continues after a stop)
#   powershell -ExecutionPolicy Bypass -File scripts\piper\train.ps1 -Export    # latest checkpoint -> .onnx for Jarvis
# Options: -Epochs 100 (~8-9 h on an RTX 4060; a larger number later continues the training), -Batch 16 (lower if video memory runs out), -Dataset <folder>,
#          -Work <folder> (checkpoints and the result), -Rebuild (rebuild the Docker image).
# Stop at any time with Ctrl+C: the next run continues from the last saved epoch.
param([switch]$Export, [int]$Epochs = 100, [int]$Batch = 16, [string]$Dataset = "", [string]$Work = "",
      [switch]$Rebuild)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Dataset) { $Dataset = Join-Path $Root "data\voice_dataset" }
if (-not $Work) { $Work = Join-Path $Root "data\piper" }
$Image = "jarvis-piper"
$BaseUrl = "https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/main/ru/ru_RU/ruslan/medium/epoch%3D2436-step%3D1724372.ckpt"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }

# ---------------------------------------------------------------- Docker
Step "Docker"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker не найден. Установите Docker Desktop: https://www.docker.com/products/docker-desktop/" }
$old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
docker info *> $null; $up = $LASTEXITCODE -eq 0
$ErrorActionPreference = $old
if (-not $up) {
    $desktop = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $desktop)) { throw "Docker не запущен. Запустите Docker Desktop и повторите." }
    Write-Host "   Запускаю Docker Desktop..."
    Start-Process $desktop
    for ($i = 0; $i -lt 90 -and -not $up; $i++) {
        Start-Sleep 2
        $ErrorActionPreference = "Continue"; docker info *> $null; $up = $LASTEXITCODE -eq 0; $ErrorActionPreference = $old
    }
    if (-not $up) { throw "Docker Desktop не запустился за 3 минуты" }
}

$ErrorActionPreference = "Continue"; docker image inspect $Image *> $null; $have = $LASTEXITCODE -eq 0; $ErrorActionPreference = $old
if ($Rebuild -or -not $have) {
    Step "Образ $Image (один раз, ~10 ГБ, 10–20 минут)"
    docker build -t $Image (Join-Path $PSScriptRoot ".")
    if ($LASTEXITCODE -ne 0) { throw "Образ не собрался" }
}

# ---------------------------------------------------------------- data
New-Item -ItemType Directory -Force $Work, (Join-Path $Work "base") | Out-Null
if (-not $Export) {
    if (-not (Test-Path (Join-Path $Dataset "metadata.csv"))) {
        throw "Нет набора: $Dataset\metadata.csv. Сначала: .venv\Scripts\python scripts\make_voice_dataset.py"
    }
    # The file keeps its original name: the container reads the base epoch from it.
    $base = Join-Path $Work "base\epoch=2436-step=1724372.ckpt"
    if (-not (Test-Path $base)) {
        Step "Базовый голос Piper ruslan (845 МБ)"
        curl.exe -L --fail --retry 5 -C - -o "$base.part" $BaseUrl
        if ($LASTEXITCODE -ne 0) { throw "Не скачался базовый голос, запустите ещё раз (докачает)" }
        Move-Item "$base.part" $base
    }
}

# ---------------------------------------------------------------- run
$mode = "train"
if ($Export) { $mode = "export"; Step "Экспорт в ONNX" } else { Step "Обучение (Ctrl+C — остановить, повторный запуск продолжит)" }
$dockerArgs = @("run", "--rm", "--gpus", "all", "--shm-size", "8g",
                "-e", "EPOCHS=$Epochs", "-e", "BATCH=$Batch",
                "-v", "${Dataset}:/data/dataset:ro", "-v", "${Work}:/work", "-v", "${PSScriptRoot}:/scripts:ro",
                "--entrypoint", "bash", $Image,
                "-c", "tr -d '\r' < /scripts/train_in_container.sh > /tmp/train.sh && bash /tmp/train.sh $mode")
& docker @dockerArgs
if ($LASTEXITCODE -ne 0) { throw "Контейнер завершился с ошибкой (код $LASTEXITCODE)" }

if ($Export) {
    $voices = Join-Path $Root "models\piper"
    New-Item -ItemType Directory -Force $voices | Out-Null
    Copy-Item (Join-Path $Work "ru_RU-jarvis-medium.onnx"), (Join-Path $Work "ru_RU-jarvis-medium.onnx.json") $voices -Force
    Write-Host ""
    Write-Host "Готово: $voices\ru_RU-jarvis-medium.onnx" -ForegroundColor Green
}
