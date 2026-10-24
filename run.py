"""无需安装即可运行的入口：python run.py <command>"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rail.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
