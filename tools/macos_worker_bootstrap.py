"""Installed immutable operator-side entrypoint; not a legacy runtime copy."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("/Library/Application Support/DoAgainSupervisor/current")


def main() -> int:
    # The launched interpreter and imported module identity are independently
    # attested by immutable_worker before any Git, browser, or daemon work.
    sys.path.insert(0, str(ROOT / "package"))
    from do_again.supervisor.immutable_worker import main as worker_main
    return worker_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
