"""独立接口体检脚本：python scripts/probe.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rail.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["probe", *sys.argv[1:]]))
