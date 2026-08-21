from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config


def path() -> Path:
    return config.audit_path()


def append(
    *,
    action: str,
    key: str,
    ok: bool,
    before: Any = None,
    after: Any = None,
    result: Any = None,
    error: str | None = None,
) -> dict:
    """Append one row and return it — callers surface it as `--json` output."""
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "key": key,
        "ok": ok,
        "before": before,
        "after": after,
    }
    if result is not None:
        row["result"] = result
    if error is not None:
        row["error"] = error

    p = path()
    new = not p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
    if new:
        os.chmod(p, 0o600)
    return row
