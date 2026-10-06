#!/usr/bin/env bash
# Runs inside the jarvis-piper container (see train.ps1).
#   /data/dataset  wavs/ + metadata.csv from make_voice_dataset.py (read-only)
#   /work          base checkpoint, cache, training runs, the exported voice
# train (default): fine-tune from the base voice, or continue from the last checkpoint of an earlier run.
# export: the best checkpoint (by the val_mos quality estimate) -> /work/ru_RU-jarvis-medium.onnx (+ .onnx.json), the file Jarvis uses.
set -euo pipefail
# The base checkpoints (rhasspy/piper-checkpoints, official) store their training paths as pathlib objects, which
# PyTorch >= 2.6 refuses to unpickle by default.
export TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1
MODE="${1:-train}"
EPOCHS="${EPOCHS:-100}"
BATCH="${BATCH:-16}"

BASE="$(ls /work/base/*.ckpt 2>/dev/null | head -n 1 || true)"   # not needed for export or resume
LATEST="$(ls -t /work/run/lightning_logs/version_*/checkpoints/last.ckpt 2>/dev/null | head -n 1 || true)"

if [ "$MODE" = "export" ]; then
    # The checkpoint the quality estimate (val_mos, higher is better) liked most; the last one if there is none.
    BEST="$(ls /work/run/lightning_logs/version_*/checkpoints/*val_mos=*.ckpt 2>/dev/null \
            | sed -E 's/.*val_mos=([0-9.]+)\.ckpt$/\1 &/' | sort -rn | head -n 1 | cut -d' ' -f2- || true)"
    CKPT="${BEST:-$LATEST}"
    if [ -z "$CKPT" ]; then echo "Нет чекпойнтов обучения в /work/run"; exit 1; fi
    echo "Экспорт: $CKPT"
    python3 -m piper.train.export_onnx --checkpoint "$CKPT" --output-file /work/ru_RU-jarvis-medium.onnx
    cp /work/config.json /work/ru_RU-jarvis-medium.onnx.json
    echo "Готово: ru_RU-jarvis-medium.onnx"
    exit 0
fi

# Lightning loads checkpoints with weights_only=True; the old base checkpoint keeps its training paths as pathlib
# objects (and a Namespace of arguments). Re-save it once with those turned into plain strings and dicts.
if [ -z "$LATEST" ] && [ -z "$BASE" ]; then echo "Нет базового голоса в /work/base (его скачивает train.ps1)"; exit 1; fi
if [ -n "$BASE" ] && [ ! -f /work/base/.converted ]; then
    echo "Подготовка базового чекпойнта..."
    python3 - "$BASE" <<'PY'
import argparse, pathlib, sys, torch
path = sys.argv[1]
def plain(x):
    if isinstance(x, pathlib.PurePath):
        return str(x)
    if isinstance(x, argparse.Namespace):
        return {k: plain(v) for k, v in vars(x).items()}
    if isinstance(x, dict):
        return {k: plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(plain(v) for v in x)
    return x
ckpt = torch.load(path, weights_only=False, map_location="cpu")
weights = ckpt.pop("state_dict")
ckpt = plain(ckpt)
ckpt["state_dict"] = weights
torch.save(ckpt, path)
torch.load(path, weights_only=True, map_location="cpu")  # the check Lightning will do
print("ok")
PY
    touch /work/base/.converted
fi

# First run: a warm start from the base voice (its weights, a fresh optimizer, epochs from 0). After a stop: resume
# our own last checkpoint. A larger -Epochs on a later run extends the training, a smaller one does not cut it.
if [ ! -f /work/target_epochs ] || [ "$EPOCHS" -gt "$(cat /work/target_epochs)" ]; then echo "$EPOCHS" > /work/target_epochs; fi
TARGET="$(cat /work/target_epochs)"
if [ -n "$LATEST" ]; then
    START=(--ckpt_path "$LATEST")
    echo "Продолжаю: $LATEST"
else
    START=(--model.warmstart_ckpt "$BASE")
    echo "Тёплый старт от базового голоса: $BASE"
fi
echo "Эпох всего: $TARGET (batch $BATCH)"

nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || { echo "Видеокарта в контейнере не видна"; exit 1; }

python3 -m piper.train fit \
    --data.voice_name "jarvis" \
    --data.csv_path /data/dataset/metadata.csv \
    --data.audio_dir /data/dataset/wavs \
    --model.sample_rate 22050 \
    --data.espeak_voice "ru" \
    --data.cache_dir /work/cache \
    --data.config_path /work/config.json \
    --data.batch_size "$BATCH" \
    --trainer.max_epochs "$TARGET" \
    --trainer.default_root_dir /work/run \
    "${START[@]}"
