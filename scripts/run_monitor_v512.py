#!/usr/bin/env python3
"""Launch the v5.1.1 monitor with the v5.1.2 runtime hotfix installed."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from scp_jp_hotfix_v512 import install


def load_core():
    path = Path(__file__).with_name("scp_jp_monitor.py")
    spec = importlib.util.spec_from_file_location("scp_jp_monitor_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load monitor core from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    core = install(load_core())
    return int(core.main())


if __name__ == "__main__":
    raise SystemExit(main())
