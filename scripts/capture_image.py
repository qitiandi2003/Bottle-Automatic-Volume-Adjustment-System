"""Capture one camera image; requires the native SDK and a connected camera."""
import _bootstrap
from bottle_volume_control.camera.capture import main

if __name__ == "__main__":
    raise SystemExit(main())
