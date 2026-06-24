from __future__ import annotations

import sys
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("AEGIS_ALPHA_PROJECT_ROOT", str(ROOT))
os.environ.setdefault("AEGIS_ALPHA_ENV_FILE", str(ROOT / ".env.local"))
os.environ.setdefault("AEGIS_ALPHA_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("AEGIS_ALPHA_DB_PATH", str(ROOT / "data" / "aegis_alpha.db"))
os.environ.setdefault("AEGIS_ALPHA_RUNNER_STATUS_PATH", str(ROOT / "data" / "runner_status.json"))

from aegis_alpha.config import load_project_env

load_project_env()

from aegis_alpha.mcp.server import main


if __name__ == "__main__":
    raise SystemExit(main())
