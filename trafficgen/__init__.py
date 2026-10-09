from pathlib import Path

__version__ = Path(__file__).resolve().parents[1].joinpath("version.txt").read_text().strip()
