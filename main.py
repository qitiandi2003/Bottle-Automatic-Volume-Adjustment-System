"""Compatibility entry point for running directly from this checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bottle_volume_control.app import cli

if __name__ == "__main__":
    raise SystemExit(cli())
