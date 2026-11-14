"""Single runtime source for the app/plugin release version."""
from pathlib import Path


_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"
APP_VERSION = _VERSION_FILE.read_text(encoding="utf-8").strip()

if not APP_VERSION:
    raise RuntimeError("VERSION file must contain a release version")
