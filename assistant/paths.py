import sys
from pathlib import Path

# Source checkout: the project folder. Installed build (PyInstaller): the folder of Jarvis.exe,
# where config/, data/ and models/ live next to the program.
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
MODELS_DIR = ROOT / "models"
LOG_DIR = DATA_DIR / "logs"
