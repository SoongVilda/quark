"""The game harness as a package. tests/run.py is its only entry point."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
