# PyInstaller spec: a folder build of Jarvis (Silero voice, CPU). Built by scripts/build.ps1.
# The cloned GPU voice is not included (it needs CUDA PyTorch, ~3 GB); install from source for it.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

hidden = (collect_submodules("assistant") + collect_submodules("onnx_asr") + collect_submodules("pymorphy3")
          + ["pymorphy3_dicts_ru", "win32timezone", "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
             "uvicorn.lifespan.on"])
datas = (collect_data_files("assistant", includes=["voicelab/static/*", "ui/icon.svg"])
         + collect_data_files("pymorphy3_dicts_ru") + collect_data_files("onnx_asr") + collect_data_files("vosk")
         + collect_data_files("num2words"))

a = Analysis([str(ROOT / "assistant" / "__main__.py")], pathex=[str(ROOT)], hiddenimports=hidden, datas=datas,
             excludes=["f5_tts", "silero_stress", "torchaudio", "matplotlib", "tkinter", "pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Jarvis", console=False,
          icon=str(ROOT / "assistant" / "ui" / "jarvis.ico"), version=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Jarvis")
