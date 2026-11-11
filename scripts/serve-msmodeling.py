"""[Role] Start the service with its project-local Python dependencies.
[Invariants] Explicit engine and Python paths take precedence over defaults.
[Timeouts & Locks] One exec handoff; no waiting or locks.
[Side-Effect Policy] Change only this process environment, never global Python.
"""

import argparse
from pathlib import Path
import os
import sys


ROOT = Path(__file__).resolve().parents[1]
if not os.environ.get("LLM_ANALYSIS_SERVICE_ENV"):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--msmodeling-path", type=Path, default=ROOT / ".local/msmodeling")
    parser.add_argument("--python", type=Path)
    args, _ = parser.parse_known_args()
    candidates = [args.python] if args.python else [args.msmodeling_path / ".venv/Scripts/python.exe", args.msmodeling_path / ".venv/bin/python"]
    python = next((p for p in candidates if p.is_file()), None)
    if python is None:
        raise SystemExit("请先按照 docs/msmodeling-integration.md 安装项目环境")
    os.environ["LLM_ANALYSIS_SERVICE_ENV"] = "1"
    os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(ROOT))
from msmodeling_bridge.server import main

main()
