"""Read only the two supported settings; never execute dotenv content."""

import os
from pathlib import Path


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_settings(path=None):
    local = {}
    path = Path(path) if path is not None else ENV_FILE
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"):
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                local[key] = value
    return {
        "DEEPSEEK_API_KEY": os.environ.get("DEEPSEEK_API_KEY") or local.get("DEEPSEEK_API_KEY", ""),
        "DEEPSEEK_MODEL": os.environ.get("DEEPSEEK_MODEL") or local.get("DEEPSEEK_MODEL") or "deepseek-flash",
    }
