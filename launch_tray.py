from __future__ import annotations

import subprocess
import sys

from runtime_paths import app_dir, pythonw_path


def main() -> None:
    subprocess.Popen(
        [str(pythonw_path()), str(app_dir() / "tray_app.py")],
        cwd=str(app_dir()),
    )


if __name__ == "__main__":
    main()
