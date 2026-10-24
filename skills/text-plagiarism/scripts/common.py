"""Small shared helpers for the CLIs: UTF-8 stdout, JSON output, error exit."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def fail(code: str, detail: str | None = None) -> int:
    out = {"error": code}
    if detail:
        out["detail"] = detail
    emit(out)
    return 2
