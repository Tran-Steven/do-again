"""Read-only background Chrome focus preflight; no canary nonce or message."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from do_again.supervisor.canary_bootstrap import preflight_background_delivery


def main() -> int:
    result = preflight_background_delivery()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["state"] == "preflight_eligible" else 2


if __name__ == "__main__":
    raise SystemExit(main())
