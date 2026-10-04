"""Test wrapper: start a grandchild process, record its pid, then run the fake serve."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", "fake_serve_tree_grandchild"]
    )
    Path(os.environ["FAKE_OC_GRANDCHILD_PID_FILE"]).write_text(str(child.pid))
    runpy.run_path(str(Path(__file__).with_name("fake_opencode.py")), run_name="__main__")
