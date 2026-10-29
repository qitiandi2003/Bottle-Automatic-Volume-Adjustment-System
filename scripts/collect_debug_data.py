"""Collect existing diagnostic images into a separate directory."""
import _bootstrap
from bottle_volume_control.diagnostics.collect import main

if __name__ == "__main__":
    raise SystemExit(main())
