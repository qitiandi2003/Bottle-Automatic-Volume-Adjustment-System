"""Keep generated files outside the source tree."""
import os
from pathlib import Path


def data_dir() -> Path:
    """Use BVC_DATA_DIR, or var/ under the current working directory."""
    return Path(os.environ.get("BVC_DATA_DIR", "var")).expanduser().resolve()


def logs_dir() -> Path:
    return data_dir() / "logs"


def images_dir() -> Path:
    return data_dir() / "images"
