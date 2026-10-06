# Piper training environment (Linux + CUDA) for the light Jarvis voice. Built and run by scripts\piper\train.ps1.
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake ninja-build git espeak-ng \
 && rm -rf /var/lib/apt/lists/*

# piper1-gpl: the maintained Piper (GPL-3). A fixed commit keeps the build reproducible.
ARG PIPER_REF=main
RUN git clone https://github.com/OHF-Voice/piper1-gpl.git /piper \
 && cd /piper && git checkout ${PIPER_REF} \
 && pip install --no-cache-dir 'scikit-build<1' 'cmake>=3.18,<4' ninja \
 && pip install --no-cache-dir -e '.[train]' \
 && ./build_monotonic_align.sh \
 && python3 setup.py build_ext --inplace

# The training script (train_in_container.sh) is mounted at run time: editing it needs no rebuild.
WORKDIR /work
