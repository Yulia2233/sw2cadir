from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> None:
    for script in sorted(ROOT.glob("*/model.py")):
        print(f"==> {script.relative_to(ROOT)}")
        subprocess.run([sys.executable, str(script)], check=True)


if __name__ == "__main__":
    main()
