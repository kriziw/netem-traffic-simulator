from pathlib import Path

# Single release-managed version file; the update worker validates release tags against it.
__version__ = Path(__file__).with_name("version.txt").read_text().strip()
