from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


def run(argv, *, cwd, env=None):
    subprocess.run([str(value) for value in argv], cwd=cwd, env=env, check=True)


def main():
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "package.json").read_text())["version"]
    wheel = root / "dist" / f"do_again-{version}-py3-none-any.whl"
    sdist = root / "dist" / f"do_again-{version}.tar.gz"
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm is required to verify both distributions")
    run([npm, "pack", "--ignore-scripts"], cwd=root)
    node_archive = root / f"do-again-{version}.tgz"
    required = {"do_again/browser/runtime.py", "do_again/browser/cdp.py", "do_again/browser/errors.py", "do_again/service/daemon.py", "do_again/default_policy.json", "do_again/platforms/process.py"}
    with zipfile.ZipFile(wheel) as archive:
        missing = required - set(archive.namelist())
        if missing:
            raise SystemExit(f"wheel missing runtime files: {sorted(missing)}")
    for path, prefix in [(sdist, f"do_again-{version}/src/"), (node_archive, "package/src/")]:
        with tarfile.open(path) as archive:
            missing = {prefix + name for name in required} - set(archive.getnames())
            if missing:
                raise SystemExit(f"{path.name} missing runtime files: {sorted(missing)}")
    with tempfile.TemporaryDirectory(prefix="do-again-artifacts-") as temporary:
        base = Path(temporary)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["DO_AGAIN_HOME"] = str(base / "home")
        for name, artifact in [("wheel", wheel), ("sdist", sdist)]:
            target = base / name
            venv.EnvBuilder(with_pip=True).create(target)
            scripts = target / ("Scripts" if os.name == "nt" else "bin")
            python = scripts / ("python.exe" if os.name == "nt" else "python")
            run([python, "-m", "pip", "install", artifact], cwd=base, env=env)
            run([scripts / ("do-again.exe" if os.name == "nt" else "do-again"), "--help"], cwd=base, env=env)
            for pattern in ["test_browser*.py", "test_cdp.py"]:
                run([python, "-m", "unittest", "discover", "-s", root / "tests", "-p", pattern], cwd=base, env=env)
        node_target = base / "npm"
        run([npm, "install", "--prefix", node_target, "--no-audit", "--no-fund", node_archive], cwd=base, env=env)
        launcher = node_target / "node_modules" / "do-again" / "bin" / "do-again.js"
        run([shutil.which("node"), launcher, "--help"], cwd=base, env=env)
    print("ARTIFACT_INSTALLS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
