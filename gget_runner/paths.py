"""Read bundled resources separately from persistent user files."""
from pathlib import Path
import sys

RESOURCE_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else RESOURCE_ROOT
