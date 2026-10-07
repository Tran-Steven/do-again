from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))

    python_version = pyproject["project"]["version"]
    npm_version = package["version"]
    if python_version != npm_version:
        raise SystemExit(
            f"Version mismatch: pyproject.toml={python_version!r}, package.json={npm_version!r}"
        )

    expected_tag = f"v{python_version}"
    if args.tag and args.tag != expected_tag:
        raise SystemExit(f"Tag {args.tag!r} does not match release version {expected_tag!r}")

    print(f"release_version={python_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
