from pathlib import Path

_root_version = Path(__file__).resolve().parents[1] / "version.txt"
__version__ = (_root_version if _root_version.exists() else Path(__file__).with_name("version.txt")).read_text().strip()
