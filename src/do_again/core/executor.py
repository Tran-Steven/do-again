from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .schema import OperatorError, atomic_json, expand_path, path_within, read_json, request_fingerprint


class LocalExecutor:
    def __init__(self, *, repo: Path, policy_path: Path, state_dir: Path):
        self.repo = repo.resolve()
        self.policy_path = policy_path.resolve()
        self.state_dir = state_dir.resolve()
        self.policy = read_json(self.policy_path)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cwd_roots = [
            expand_path(value, repo=self.repo)
            for value in self.policy.get("approved_cwd_roots", [])
        ]
        self.read_roots = [
            expand_path(value, repo=self.repo)
            for value in self.policy.get("read_roots", [])
        ]
        if self.state_dir not in self.cwd_roots:
            self.cwd_roots.append(self.state_dir)
        if self.state_dir not in self.read_roots:
            self.read_roots.append(self.state_dir)

    def authority_snapshot(self) -> dict[str, Any]:
        control = self._optional_json(self.repo / "automation/direct_chat_chatgpt_control.json")
        desired = self._optional_json(self.repo / "automation/control_plane_desired.json")
        lease = self._optional_json(self.repo / "automation/direct_chat_controller_lease.json")
        head = self._capture(["git", "-C", str(self.repo), "rev-parse", "HEAD"], timeout=15)
        branch = self._capture(
            ["git", "-C", str(self.repo), "branch", "--show-current"], timeout=15
        )
        dirty = self._capture(
            ["git", "-C", str(self.repo), "status", "--porcelain"], timeout=15
        )
        return {
            "repo_head": head["stdout"].strip() if head["returncode"] == 0 else None,
            "repo_branch": branch["stdout"].strip() if branch["returncode"] == 0 else None,
            "repo_dirty": bool(dirty["stdout"].strip()) if dirty["returncode"] == 0 else None,
            "generation": control.get("generation"),
            "control_state": control.get("state"),
            "revision": desired.get("revision"),
            "desired_mode": desired.get("mode"),
            "bundle_sha256": desired.get("bundle_sha256"),
            "lease_id": desired.get("ownership", {}).get("lease_id")
            or lease.get("lease_id")
            or lease.get("controller_lease_id"),
            "lease_epoch": desired.get("ownership", {}).get("epoch")
            or lease.get("epoch")
            or lease.get("controller_lease_epoch"),
        }

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request["operation"]
        if operation not in set(self.policy.get("allowed_operations", [])):
            raise OperatorError(f"operation is not allowed: {operation}")
        before = self.authority_snapshot()
        self._validate_expected(request.get("expected", {}), before)
        timeout = self._request_timeout(request)
        args = request.get("args", {})
        if operation == "status":
            result = self._status(timeout)
        elif operation == "read_file":
            result = self._read_file(args)
        elif operation == "run_tests":
            result = self._run_tests(args, timeout)
        elif operation == "repo_script":
            result = self._repo_script(args, timeout)
        elif operation == "control_plane_launcher":
            result = self._control_plane_launcher(request, args, timeout)
        elif operation == "deploy_cp1_bundle_at_stop":
            result = self._deploy_cp1_bundle_at_stop(request, timeout)
        elif operation == "scratch_script":
            result = self._scratch_script(request, args, timeout)
        elif operation == "self_update":
            result = self._self_update(request, args, timeout)
        elif operation == "artifact_get":
            result = self._artifact_get(args)
        elif operation == "artifact_put":
            result = self._artifact_put(request, args)
        elif operation == "capture_screenshot":
            result = self._capture_screenshot(request, args, timeout)
        elif operation == "full_process_list":
            result = self._full_process_list(timeout)
        elif operation == "extended_exec":
            result = self._extended_exec(args, timeout)
        elif operation == "process_signal":
            result = self._process_signal(args)
        elif operation == "launchctl_action":
            result = self._launchctl_action(args, timeout)
        else:
            raise OperatorError(f"unsupported operation: {operation}")
        after = self.authority_snapshot()
        return {
            "operation": operation,
            "request_fingerprint": request_fingerprint(request),
            "authority_before": before,
            "authority_after": after,
            "result": result,
        }

    def _optional_json(self, path: Path) -> dict[str, Any]:
        try:
            value = read_json(path)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _validate_expected(self, expected: dict[str, Any], actual: dict[str, Any]) -> None:
        aliases = {
            "state": "control_state",
            "mode": "desired_mode",
            "control_revision": "revision",
            "controller_lease_id": "lease_id",
            "controller_lease_epoch": "lease_epoch",
        }
        for key, wanted in expected.items():
            actual_key = aliases.get(key, key)
            if actual_key not in actual:
                raise OperatorError(f"unknown authority fence: {key}")
            observed = actual.get(actual_key)
            if observed != wanted:
                raise OperatorError(
                    f"authority fence mismatch for {key}: expected {wanted!r}, observed {observed!r}"
                )

    def _require_clean_main(self) -> None:
        snapshot = self.authority_snapshot()
        if snapshot.get("repo_branch") != "main":
            raise OperatorError("authority mutation requires local main checkout")
        if snapshot.get("repo_dirty"):
            raise OperatorError("authority mutation requires a clean repository")

    def _request_timeout(self, request: dict[str, Any]) -> float:
        limits = request.get("limits", {})
        requested = limits.get("timeout_seconds", self.policy.get("default_timeout_seconds", 120))
        try:
            value = float(requested)
        except (TypeError, ValueError) as exc:
            raise OperatorError("timeout_seconds must be numeric") from exc
        absolute = float(self.policy.get("absolute_timeout_seconds", 3600))
        if value <= 0 or value > absolute:
            raise OperatorError(f"timeout_seconds must be between 1 and {absolute:g}")
        return value

    def _safe_cwd(self, value: str | None) -> Path:
        path = self.repo if not value else expand_path(value, repo=self.repo)
        if not path_within(path, self.cwd_roots):
            raise OperatorError(f"cwd is outside approved roots: {path}")
        if not path.is_dir():
            raise OperatorError(f"cwd does not exist: {path}")
        return path

    def _shell_script_arg(self, path: Path, *, windows: bool | None = None) -> str:
        value = path.as_posix()
        is_windows = os.name == "nt" if windows is None else windows
        if is_windows and len(value) >= 3 and value[1:3] == ":/":
            return f"/{value[0].lower()}{value[2:]}"
        return value if is_windows else str(path)

    def _base_env(self, extra: dict[str, Any] | None = None) -> dict[str, str]:
        names = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMPDIR",
            "SSH_AUTH_SOCK",
            "GIT_SSH_COMMAND",
            "TERM",
        }
        env = {key: value for key, value in os.environ.items() if key in names}
        if extra:
            for key, value in extra.items():
                if not isinstance(key, str) or not key.replace("_", "").isalnum():
                    raise OperatorError(f"invalid environment variable name: {key!r}")
                env[key] = str(value)
        return env

    def _truncate(self, value: str) -> tuple[str, bool]:
        limit = int(self.policy.get("max_output_bytes", 262144))
        data = value.encode("utf-8", errors="replace")
        if len(data) <= limit:
            return value, False
        half = max(1, limit // 2)
        clipped = data[:half] + b"\n...<truncated>...\n" + data[-half:]
        return clipped.decode("utf-8", errors="replace"), True

    def _capture(
        self,
        argv: list[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        env: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd or self.repo),
                env=self._base_env(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise OperatorError(f"failed to start {argv[0]!r}: {exc}") from exc
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
        stdout, stdout_truncated = self._truncate(stdout or "")
        stderr, stderr_truncated = self._truncate(stderr or "")
        return {
            "argv": argv,
            "cwd": str(cwd or self.repo),
            "returncode": proc.returncode,
            "timed_out": timed_out,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }

    def _process_rows(self, text: str) -> list[dict[str, Any]]:
        rows = []
        for line in text.splitlines():
            parts = line.strip().split(None, 6)
            if len(parts) != 7:
                continue
            pid, ppid, elapsed, cpu, mem, rss, command = parts
            try:
                row = {
                    "pid": int(pid),
                    "ppid": int(ppid),
                    "elapsed": elapsed,
                    "cpu_percent": float(cpu),
                    "memory_percent": float(mem),
                    "rss_kib": int(rss),
                    "command": command,
                }
            except ValueError:
                continue
            rows.append(row)
        return rows

    def _process_summary(self, timeout: float) -> dict[str, Any]:
        result = self._capture(
            ["ps", "-axo", "pid=,ppid=,etime=,%cpu=,%mem=,rss=,command="],
            timeout=min(timeout, 30),
            cwd=self.repo,
        )
        if result["returncode"] != 0:
            return {"error": result["stderr"], "top_cpu": [], "top_memory": [], "relevant": []}
        rows = self._process_rows(result["stdout"])
        top_cpu = sorted(rows, key=lambda item: item["cpu_percent"], reverse=True)[:10]
        top_memory = sorted(rows, key=lambda item: item["rss_kib"], reverse=True)[:10]
        needles = (
            "direct_chat",
            "mac_operator",
            "persona",
            "llama",
            "ollama",
            "python",
            "next-server",
            "desktop-commander",
        )
        relevant = [
            row for row in rows
            if any(needle in row["command"].lower() for needle in needles)
        ][:30]
        return {
            "count": len(rows),
            "top_cpu": top_cpu,
            "top_memory": top_memory,
            "relevant": relevant,
        }

    def _memory_summary(self) -> dict[str, Any]:
        total = self._capture(["sysctl", "-n", "hw.memsize"], timeout=10, cwd=self.repo)
        vm = self._capture(["vm_stat"], timeout=10, cwd=self.repo)
        result = {
            "total_bytes": None,
            "page_size": None,
            "free_bytes": None,
            "active_bytes": None,
            "inactive_bytes": None,
            "wired_bytes": None,
            "compressed_bytes": None,
        }
        if total["returncode"] == 0:
            try:
                result["total_bytes"] = int(total["stdout"].strip())
            except ValueError:
                pass
        if vm["returncode"] != 0:
            return result
        page_size = 4096
        first = vm["stdout"].splitlines()[:1]
        if first and "page size of" in first[0]:
            try:
                page_size = int(first[0].split("page size of", 1)[1].split("bytes", 1)[0].strip())
            except ValueError:
                pass
        result["page_size"] = page_size
        values = {}
        for line in vm["stdout"].splitlines()[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            try:
                values[key.strip()] = int(value.strip().rstrip("."))
            except ValueError:
                continue
        mapping = {
            "free_bytes": "Pages free",
            "active_bytes": "Pages active",
            "inactive_bytes": "Pages inactive",
            "wired_bytes": "Pages wired down",
            "compressed_bytes": "Pages occupied by compressor",
        }
        for output_key, vm_key in mapping.items():
            if vm_key in values:
                result[output_key] = values[vm_key] * page_size
        return result

    def _disk_summary(self) -> dict[str, Any]:
        result = self._capture(["df", "-k", "/"], timeout=10, cwd=self.repo)
        if result["returncode"] != 0:
            return {"error": result["stderr"]}
        lines = [line for line in result["stdout"].splitlines() if line.strip()]
        if len(lines) < 2:
            return {"raw": result["stdout"]}
        parts = lines[-1].split()
        if len(parts) < 6:
            return {"raw": result["stdout"]}
        try:
            return {
                "filesystem": parts[0],
                "total_bytes": int(parts[1]) * 1024,
                "used_bytes": int(parts[2]) * 1024,
                "available_bytes": int(parts[3]) * 1024,
                "used_percent": parts[4],
                "mount": parts[-1],
            }
        except ValueError:
            return {"raw": result["stdout"]}

    def _compact_supervisor(self, value: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "status",
            "phase",
            "pid",
            "last_error",
            "last_exit_code",
            "last_generation_seen",
            "last_progress_utc",
            "active_failure_class",
            "autoheal_blocked_reason",
            "bundle_sha256",
            "updated_utc",
        )
        return {key: value.get(key) for key in keys if key in value}

    def _compact_observed(self, value: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "status",
            "mode",
            "generation",
            "revision",
            "bundle_sha256",
            "review_handoff_active",
            "checked_epoch",
        )
        result = {key: value.get(key) for key in keys if key in value}
        job = value.get("job")
        if isinstance(job, dict):
            result["job"] = {
                key: job.get(key)
                for key in ("proposal_id", "request_id", "run_id", "runner", "state", "status")
                if key in job
            }
        else:
            result["job"] = job
        return result

    def _compact_checker(self, value: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value.get(key)
            for key in (
                "status",
                "generation_recovery_tax",
                "pending_intake",
                "bundle_sha256",
                "checked_epoch",
            )
            if key in value
        }
        actors = value.get("actors")
        if isinstance(actors, dict):
            result["actors"] = {
                name: {
                    "registered": details.get("registered"),
                    "running": details.get("running"),
                }
                for name, details in actors.items()
                if isinstance(details, dict)
            }
        return result

    def _status(self, timeout: float) -> dict[str, Any]:
        supervisor = self._optional_json(
            Path.home() / ".direct_chat_chatgpt_bridge/supervisor_status.json"
        )
        observed = self._optional_json(
            Path.home() / ".direct_chat_control_plane/state/observed.json"
        )
        checker = self._optional_json(
            Path.home() / ".direct_chat_control_plane/state/checker.json"
        )
        return {
            "authority": self.authority_snapshot(),
            "supervisor": self._compact_supervisor(supervisor),
            "observed": self._compact_observed(observed),
            "checker": self._compact_checker(checker),
            "system": {
                "memory": self._memory_summary(),
                "disk": self._disk_summary(),
            },
            "processes": self._process_summary(timeout),
        }

    def _full_process_list(self, timeout: float) -> dict[str, Any]:
        return self._capture(
            ["ps", "-axo", "pid,ppid,etime,%cpu,%mem,rss,command"],
            timeout=min(timeout, 30),
            cwd=self.repo,
        )

    def _read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = expand_path(args.get("path", ""), repo=self.repo)
        if not path_within(path, self.read_roots):
            raise OperatorError(f"read path is outside approved roots: {path}")
        if not path.is_file():
            raise OperatorError(f"read path is not a file: {path}")
        max_bytes = int(args.get("max_bytes", self.policy.get("max_output_bytes", 262144)))
        max_bytes = max(1, min(max_bytes, int(self.policy.get("max_output_bytes", 262144))))
        mode = str(args.get("mode", "tail"))
        data = path.read_bytes()
        if mode == "head":
            data = data[:max_bytes]
        elif mode == "tail":
            data = data[-max_bytes:]
        else:
            raise OperatorError("read_file mode must be head or tail")
        return {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "content": data.decode("utf-8", errors="replace"),
        }

    def _run_tests(self, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        modules = args.get("modules")
        discover = bool(args.get("discover", False))
        if discover:
            start = str(args.get("start_directory", "control_plane/tests"))
            pattern = str(args.get("pattern", "test_*.py"))
            argv = ["python3", "-m", "unittest", "discover", "-s", start, "-p", pattern]
        else:
            if not isinstance(modules, list) or not modules:
                raise OperatorError("run_tests requires modules or discover=true")
            argv = ["python3", "-m", "unittest", *[str(value) for value in modules]]
        return self._capture(argv, timeout=timeout, cwd=self.repo)

    def _repo_script(self, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        relative = str(args.get("path", ""))
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise OperatorError("repo_script path must be repository-relative")
        prefixes = tuple(str(value) for value in self.policy.get("repo_script_prefixes", []))
        if not relative.startswith(prefixes):
            raise OperatorError(f"repo_script path is not approved: {relative}")
        script = (self.repo / relative).resolve()
        if not path_within(script, [self.repo]) or not script.is_file():
            raise OperatorError(f"repo_script does not exist: {relative}")
        script_args = args.get("argv", [])
        if not isinstance(script_args, list) or not all(isinstance(value, str) for value in script_args):
            raise OperatorError("repo_script argv must be a string list")
        if script.suffix == ".py":
            argv = ["python3", str(script), *script_args]
        elif script.suffix in {".sh", ".bash", ".zsh"}:
            script_arg = self._shell_script_arg(script)
            argv = ["bash" if script.suffix != ".zsh" else "zsh", script_arg, *script_args]
        else:
            if not os.access(script, os.X_OK):
                raise OperatorError("repo_script must be executable or use a supported script suffix")
            argv = [str(script), *script_args]
        return self._capture(
            argv,
            timeout=timeout,
            cwd=self._safe_cwd(args.get("cwd")),
            env=args.get("env"),
        )

    def _control_plane_launcher(
        self,
        request: dict[str, Any],
        args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        self._require_clean_main()
        subcommand = str(args.get("subcommand", ""))
        allowed = set(self.policy.get("allowed_launcher_subcommands", []))
        if subcommand not in allowed:
            raise OperatorError(f"launcher subcommand is not allowed: {subcommand}")
        expected = request.get("expected", {})
        if subcommand == "session-intake":
            if "generation" not in expected or not (
                "revision" in expected or "control_revision" in expected
            ):
                raise OperatorError("session-intake requires generation and revision authority fences")
            if "repo_head" not in expected:
                raise OperatorError("session-intake requires an exact repo_head fence")
        elif subcommand == "session-resolution":
            if "generation" not in expected:
                raise OperatorError("session-resolution requires a generation authority fence")
            if "repo_head" not in expected:
                raise OperatorError("session-resolution requires an exact repo_head fence")
        values = args.get("argv", [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise OperatorError("launcher argv must be a string list")
        materialized = {}
        json_files = args.get("json_files", {})
        if not isinstance(json_files, dict):
            raise OperatorError("launcher json_files must be an object")
        request_dir = self.state_dir / "request_files" / request["request_id"]
        for name, value in json_files.items():
            if (
                not isinstance(name, str)
                or not name
                or "/" in name
                or "\\" in name
                or name in {".", ".."}
            ):
                raise OperatorError(f"invalid launcher json file name: {name!r}")
            path = request_dir / f"{name}.json"
            atomic_json(path, value)
            materialized[name] = str(path)
        expanded_values = []
        for value in values:
            if value.startswith("@json:"):
                name = value.split(":", 1)[1]
                if name not in materialized:
                    raise OperatorError(f"unknown launcher json placeholder: {name}")
                expanded_values.append(materialized[name])
            else:
                expanded_values.append(value)
        values = expanded_values
        launcher = Path.home() / ".direct_chat_control_plane/launcher.py"
        if not launcher.is_file():
            raise OperatorError(f"launcher is missing: {launcher}")
        return self._capture(
            ["python3", str(launcher), subcommand, *values],
            timeout=timeout,
            cwd=self.repo,
            env=args.get("env"),
        )

    def _fast_forward_clean_main_to_origin(self, timeout: float) -> dict[str, Any]:
        """Reconcile a clean local main after a helper published commits via a clone."""
        self._require_clean_main()
        fetch = self._capture(
            ["git", "-C", str(self.repo), "fetch", "--quiet", "origin", "main"],
            timeout=min(timeout, 120),
            cwd=self.repo,
        )
        if fetch["returncode"] != 0:
            return {"returncode": fetch["returncode"], "stage": "fetch", "fetch": fetch}

        current_result = self._capture(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            timeout=15,
            cwd=self.repo,
        )
        remote_result = self._capture(
            ["git", "-C", str(self.repo), "rev-parse", "origin/main"],
            timeout=15,
            cwd=self.repo,
        )
        if current_result["returncode"] != 0 or remote_result["returncode"] != 0:
            return {
                "returncode": current_result["returncode"] or remote_result["returncode"],
                "stage": "resolve_heads",
                "current": current_result,
                "remote": remote_result,
            }
        current_head = current_result["stdout"].strip()
        remote_head = remote_result["stdout"].strip()
        if current_head == remote_head:
            return {
                "returncode": 0,
                "stage": "already_current",
                "previous_head": current_head,
                "remote_head": remote_head,
                "new_head": current_head,
            }

        ff_check = self._capture(
            ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", current_head, remote_head],
            timeout=15,
            cwd=self.repo,
        )
        if ff_check["returncode"] != 0:
            return {
                "returncode": ff_check["returncode"],
                "stage": "non_fast_forward",
                "previous_head": current_head,
                "remote_head": remote_head,
                "check": ff_check,
            }

        merge = self._capture(
            ["git", "-C", str(self.repo), "merge", "--ff-only", remote_head],
            timeout=min(timeout, 120),
            cwd=self.repo,
        )
        if merge["returncode"] != 0:
            return {
                "returncode": merge["returncode"],
                "stage": "fast_forward",
                "previous_head": current_head,
                "remote_head": remote_head,
                "merge": merge,
            }
        after = self._capture(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            timeout=15,
            cwd=self.repo,
        )
        return {
            "returncode": after["returncode"],
            "stage": "complete" if after["returncode"] == 0 else "verify",
            "previous_head": current_head,
            "remote_head": remote_head,
            "new_head": after["stdout"].strip() if after["returncode"] == 0 else None,
            "merge": merge,
        }

    def _deploy_cp1_bundle_at_stop(
        self,
        request: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        self._require_clean_main()
        before = self.authority_snapshot()
        if before.get("control_state") != "stop" or before.get("desired_mode") != "STOP":
            raise OperatorError("bundle deployment requires authoritative STOP")
        expected = request.get("expected", {})
        if "generation" not in expected or not (
            "revision" in expected or "control_revision" in expected
        ):
            raise OperatorError("bundle deployment requires generation and revision authority fences")
        if "repo_head" not in expected:
            raise OperatorError("bundle deployment requires an exact repo_head fence")
        destination = self.state_dir / "bundles" / request["request_id"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        build = self._capture(
            [
                "python3",
                str(self.repo / "control_plane/build_cp1_bundle.py"),
                "--out",
                str(destination),
            ],
            timeout=min(timeout, 300),
            cwd=self.repo,
        )
        if build["returncode"] != 0:
            return {"build": build, "retarget": None}
        bundle_sha = build["stdout"].strip().splitlines()[-1] if build["stdout"].strip() else ""
        if len(bundle_sha) != 64:
            raise OperatorError("bundle builder did not return a SHA-256")
        retarget = self._capture(
            [
                "python3",
                str(self.repo / "control_plane/retarget_bundle_at_stop.py"),
                "--repo",
                str(self.repo),
                "--bundle",
                str(destination),
                "--apply",
            ],
            timeout=timeout,
            cwd=self.repo,
        )
        local_sync = None
        if retarget["returncode"] == 0:
            local_sync = self._fast_forward_clean_main_to_origin(min(timeout, 120))
        return {
            "bundle_sha256": bundle_sha,
            "build": build,
            "retarget": retarget,
            "local_sync": local_sync,
            "local_sync_ok": bool(local_sync and local_sync.get("returncode") == 0),
        }

    def _artifact_payload(self, path: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
        maximum = int(max_bytes or self.policy.get("max_inline_artifact_bytes", 450000))
        size = path.stat().st_size
        if size > maximum:
            raise OperatorError(
                f"artifact is {size} bytes and exceeds inline maximum of {maximum} bytes"
            )
        data = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return {
            "path": str(path),
            "name": path.name,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime_type": mime_type,
            "base64": base64.b64encode(data).decode("ascii"),
        }

    def _artifact_get(self, args: dict[str, Any]) -> dict[str, Any]:
        path = expand_path(str(args.get("path", "")), repo=self.repo)
        if not path_within(path, self.read_roots):
            raise OperatorError(f"artifact path is outside approved roots: {path}")
        if not path.is_file():
            raise OperatorError(f"artifact path is not a file: {path}")
        requested = args.get("max_bytes")
        maximum = (
            min(int(requested), int(self.policy.get("max_inline_artifact_bytes", 450000)))
            if requested is not None
            else int(self.policy.get("max_inline_artifact_bytes", 450000))
        )
        return self._artifact_payload(path, max_bytes=maximum)

    def _artifact_put(
        self,
        request: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        if not name or name in {".", ".."} or Path(name).name != name:
            raise OperatorError("artifact_put name must be a simple file name")
        encoded = args.get("base64")
        if not isinstance(encoded, str) or not encoded:
            raise OperatorError("artifact_put base64 must be a non-empty string")
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise OperatorError("artifact_put base64 is invalid") from exc
        maximum = int(self.policy.get("max_inline_artifact_bytes", 450000))
        if len(data) > maximum:
            raise OperatorError(
                f"artifact_put exceeds inline maximum of {maximum} bytes"
            )
        directory = self.state_dir / "inbox" / request["request_id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return {
            "path": str(path),
            "name": name,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime_type": str(args.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"),
        }

    def _capture_screenshot(
        self,
        request: dict[str, Any],
        args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        directory = self.state_dir / "artifacts" / request["request_id"]
        directory.mkdir(parents=True, exist_ok=True)
        raw = directory / "screen.png"
        output = directory / "screen.jpg"
        capture_argv = ["/usr/sbin/screencapture", "-x"]
        display = args.get("display")
        if display is not None:
            try:
                display_number = int(display)
            except (TypeError, ValueError) as exc:
                raise OperatorError("capture_screenshot display must be an integer") from exc
            if display_number < 1:
                raise OperatorError("capture_screenshot display must be >= 1")
            capture_argv.extend(["-D", str(display_number)])
        capture_argv.append(str(raw))
        captured = self._capture(
            capture_argv,
            timeout=min(timeout, 30),
            cwd=self.repo,
        )
        if captured["returncode"] != 0 or not raw.is_file():
            detail = captured["stderr"] or captured["stdout"] or "no screenshot file produced"
            raise OperatorError(
                "screenshot capture failed; macOS Screen Recording permission may be required: "
                + detail.strip()
            )
        maximum = int(self.policy.get("max_inline_artifact_bytes", 450000))
        configured_width = int(self.policy.get("max_screenshot_width", 1440))
        configured_quality = int(self.policy.get("screenshot_jpeg_quality", 55))
        attempts = [
            (configured_width, configured_quality),
            (1200, min(configured_quality, 45)),
            (960, min(configured_quality, 35)),
        ]
        conversion = None
        for width, quality in attempts:
            conversion = self._capture(
                [
                    "/usr/bin/sips",
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    str(quality),
                    "-Z",
                    str(width),
                    str(raw),
                    "--out",
                    str(output),
                ],
                timeout=min(timeout, 30),
                cwd=self.repo,
            )
            if conversion["returncode"] == 0 and output.is_file() and output.stat().st_size <= maximum:
                break
        if not output.is_file():
            raise OperatorError("screenshot conversion failed")
        if output.stat().st_size > maximum:
            raise OperatorError(
                f"compressed screenshot still exceeds inline maximum of {maximum} bytes"
            )
        artifact = self._artifact_payload(output, max_bytes=maximum)
        artifact["source"] = "screenshot"
        artifact["display"] = display
        return artifact

    def _commit_json(self, commit: str, path: str, timeout: float = 15) -> dict[str, Any]:
        got = self._capture(
            ["git", "-C", str(self.repo), "show", f"{commit}:{path}"],
            timeout=min(timeout, 30),
            cwd=self.repo,
        )
        if got["returncode"] != 0:
            raise OperatorError(f"cannot read authority document at target commit: {path}")
        try:
            value = json.loads(got["stdout"])
        except Exception as exc:
            raise OperatorError(f"invalid authority JSON at target commit: {path}") from exc
        if not isinstance(value, dict):
            raise OperatorError(f"authority document is not an object at target commit: {path}")
        return value

    def _validate_stale_local_self_update_target(
        self,
        *,
        target: str,
        before: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if args.get("allow_stale_local_authority_reconcile") is not True:
            raise OperatorError("self_update requires authoritative STOP")
        proof = args.get("target_authority")
        if not isinstance(proof, dict):
            raise OperatorError("stale local self_update requires target_authority proof")

        control = self._commit_json(target, "automation/direct_chat_chatgpt_control.json")
        desired = self._commit_json(target, "automation/control_plane_desired.json")
        lease = self._commit_json(target, "automation/direct_chat_controller_lease.json")

        generation = control.get("generation")
        revision = desired.get("revision")
        lease_id = str(lease.get("lease_id") or "")
        lease_epoch = lease.get("epoch")
        if (
            control.get("state") != "stop"
            or desired.get("mode") != "STOP"
            or desired.get("generation") != generation
            or not isinstance(generation, int)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not lease_id
            or isinstance(lease_epoch, bool)
            or not isinstance(lease_epoch, int)
            or lease.get("state") != "active"
            or str(control.get("controller_lease_id") or "") != lease_id
            or int(control.get("controller_lease_epoch") or -1) != lease_epoch
            or str((desired.get("ownership") or {}).get("lease_id") or "") != lease_id
            or int((desired.get("ownership") or {}).get("epoch") or -1) != lease_epoch
        ):
            raise OperatorError("stale local self_update target is not coherent authoritative STOP")

        required = {
            "generation": generation,
            "revision": revision,
            "state": "stop",
            "mode": "STOP",
            "controller_lease_id": lease_id,
            "controller_lease_epoch": lease_epoch,
            "repo_head": target,
        }
        aliases = {"control_revision": "revision"}
        normalized = {aliases.get(k, k): v for k, v in proof.items()}
        if normalized != required:
            raise OperatorError("target_authority proof does not exactly match target STOP authority")
        local_generation = before.get("generation")
        if isinstance(local_generation, int) and generation < local_generation:
            raise OperatorError("stale local self_update target generation regresses local generation")
        return required

    def _self_update(
        self,
        request: dict[str, Any],
        args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        self._require_clean_main()
        before = self.authority_snapshot()
        local_is_stop = (
            before.get("control_state") == "stop"
            and before.get("desired_mode") == "STOP"
        )
        if (
            not local_is_stop
            and args.get("allow_stale_local_authority_reconcile") is not True
        ):
            raise OperatorError("self_update requires authoritative STOP")
        expected = request.get("expected", {})
        if "generation" not in expected or not (
            "revision" in expected or "control_revision" in expected
        ) or "repo_head" not in expected:
            raise OperatorError(
                "self_update requires generation, revision, and exact repo_head fences"
            )
        fetch = self._capture(
            ["git", "-C", str(self.repo), "fetch", "--quiet", "origin", "main"],
            timeout=min(timeout, 120),
            cwd=self.repo,
        )
        if fetch["returncode"] != 0:
            return {"returncode": fetch["returncode"], "stage": "fetch", "fetch": fetch}
        remote_result = self._capture(
            ["git", "-C", str(self.repo), "rev-parse", "origin/main"],
            timeout=15,
            cwd=self.repo,
        )
        if remote_result["returncode"] != 0:
            return {"returncode": remote_result["returncode"], "stage": "resolve_remote", "resolve": remote_result}
        remote_head = remote_result["stdout"].strip()
        target = str(args.get("target_commit") or remote_head).strip()
        if not local_is_stop:
            self._validate_stale_local_self_update_target(
                target=target,
                before=before,
                args=args,
            )
        target_check = self._capture(
            ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", target, remote_head],
            timeout=15,
            cwd=self.repo,
        )
        if target_check["returncode"] != 0:
            raise OperatorError("self_update target_commit is not contained in origin/main")
        current_head = str(before.get("repo_head") or "")
        ff_check = self._capture(
            ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", current_head, remote_head],
            timeout=15,
            cwd=self.repo,
        )
        if ff_check["returncode"] != 0:
            raise OperatorError("self_update would not be a fast-forward")

        preflight = self.state_dir / "preflight" / request["request_id"]
        if preflight.exists():
            shutil.rmtree(preflight)
        preflight.parent.mkdir(parents=True, exist_ok=True)
        add = self._capture(
            ["git", "-C", str(self.repo), "worktree", "add", "--detach", str(preflight), remote_head],
            timeout=min(timeout, 120),
            cwd=self.repo,
        )
        if add["returncode"] != 0:
            return {"returncode": add["returncode"], "stage": "preflight_worktree", "worktree": add}
        checks = []
        try:
            commands = [
                [
                    "python3",
                    "-m",
                    "py_compile",
                    "tools/mac_operator_agent.py",
                    "control_plane/mac_operator/schema.py",
                    "control_plane/mac_operator/executor.py",
                ],
                ["bash", "-n", "tools/install_mac_operator.sh", "tools/uninstall_mac_operator.sh"],
                ["python3", "-m", "unittest", "control_plane.tests.test_mac_operator"],
            ]
            for argv in commands:
                check = self._capture(
                    argv,
                    timeout=min(timeout, 300),
                    cwd=preflight,
                )
                checks.append(check)
                if check["returncode"] != 0 or check["timed_out"]:
                    return {
                        "returncode": check["returncode"] or 124,
                        "stage": "preflight_validation",
                        "preflight_head": remote_head,
                        "checks": checks,
                    }
        finally:
            self._capture(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(preflight)],
                timeout=60,
                cwd=self.repo,
            )

        merge = self._capture(
            ["git", "-C", str(self.repo), "merge", "--ff-only", remote_head],
            timeout=min(timeout, 120),
            cwd=self.repo,
        )
        if merge["returncode"] != 0:
            return {"returncode": merge["returncode"], "stage": "fast_forward", "merge": merge}
        after_head = self._capture(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            timeout=15,
            cwd=self.repo,
        )["stdout"].strip()
        return {
            "returncode": 0,
            "stage": "complete",
            "previous_head": current_head,
            "target_commit": target,
            "remote_head": remote_head,
            "new_head": after_head,
            "preflight_checks": checks,
            "stale_local_authority_reconciled": not local_is_stop,
            "restart_after_receipt": True,
        }

    def _scratch_script(
        self,
        request: dict[str, Any],
        args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        language = str(args.get("language", "")).strip().lower()
        if language not in {"python", "bash"}:
            raise OperatorError("scratch_script language must be python or bash")

        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            raise OperatorError("scratch_script content must be a non-empty string")

        data = content.encode("utf-8")
        maximum = int(self.policy.get("max_scratch_script_bytes", 65536))
        if len(data) > maximum:
            raise OperatorError(
                f"scratch_script exceeds maximum size of {maximum} bytes"
            )

        values = args.get("argv", [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise OperatorError("scratch_script argv must be a string list")

        cwd = self._safe_cwd(args.get("cwd"))
        directory = self.state_dir / "scratch" / request["request_id"]
        directory.mkdir(parents=True, exist_ok=True)

        suffix = ".py" if language == "python" else ".sh"
        path = directory / f"script{suffix}"
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)

        digest = hashlib.sha256(data).hexdigest()
        runner = "python3" if language == "python" else "bash"
        script_arg = self._shell_script_arg(path) if language == "bash" else str(path)
        result = self._capture(
            [runner, script_arg, *values],
            timeout=timeout,
            cwd=cwd,
            env=args.get("env"),
        )

        return {
            "language": language,
            "script_path": str(path),
            "script_sha256": digest,
            "script_bytes": len(data),
            **result,
        }

    def _extended_exec(self, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
            raise OperatorError("extended_exec argv must be a non-empty string list")
        binary = os.path.basename(argv[0])
        denied = set(self.policy.get("hard_denied_binaries", []))
        allowed = set(self.policy.get("extended_exec_binaries", []))
        if binary in denied or binary not in allowed:
            raise OperatorError(f"binary is not allowed for extended_exec: {binary}")
        executable = shutil.which(argv[0]) or shutil.which(binary)
        if not executable:
            raise OperatorError(f"binary is unavailable: {argv[0]}")
        normalized = [executable, *argv[1:]]
        cwd = self._safe_cwd(args.get("cwd"))
        self._validate_extended_argv(binary, normalized, cwd)
        return self._capture(
            normalized,
            timeout=timeout,
            cwd=cwd,
            env=args.get("env"),
        )

    def _validate_path_arguments(self, values: list[str], cwd: Path) -> None:
        for value in values:
            if not value or value.startswith("-"):
                continue
            candidate = Path(os.path.expanduser(value))
            check = None
            if candidate.is_absolute():
                check = candidate
            elif ".." in candidate.parts:
                check = cwd / candidate
            else:
                local = cwd / candidate
                if local.exists() or local.is_symlink():
                    check = local
            if check is not None and not path_within(check, self.read_roots):
                raise OperatorError(f"extended_exec path is outside approved roots: {value}")

    def _validate_extended_argv(self, binary: str, argv: list[str], cwd: Path) -> None:
        tail = argv[1:]
        if binary in {"bash", "zsh", "sh"}:
            if any(value in {"-c", "-s"} for value in tail):
                raise OperatorError("inline shell execution is not allowed")
            script = next((value for value in tail if not value.startswith("-")), None)
            if script is None:
                raise OperatorError("shell execution requires a script file")
            path = expand_path(script, repo=self.repo)
            if not path_within(path, self.cwd_roots) or not path.is_file():
                raise OperatorError("shell script must be inside an approved root")
        elif binary == "python3":
            if "-c" in tail or "-" in tail:
                raise OperatorError("inline Python execution is not allowed")
            if "-m" in tail:
                index = tail.index("-m")
                if index + 1 >= len(tail):
                    raise OperatorError("python -m requires a module")
                module = tail[index + 1]
                if module not in set(self.policy.get("python_allowed_modules", [])):
                    raise OperatorError(f"python module is not allowed: {module}")
            else:
                script = next((value for value in tail if not value.startswith("-")), None)
                if script:
                    path = expand_path(script, repo=self.repo)
                    if not path_within(path, self.cwd_roots) or not path.is_file():
                        raise OperatorError("Python script must be inside an approved root")
        elif binary == "git":
            if "-c" in tail:
                raise OperatorError("git -c is not allowed")
            values = list(tail)
            if values[:1] == ["-C"]:
                if len(values) < 3:
                    raise OperatorError("git -C requires path and subcommand")
                path = expand_path(values[1], repo=self.repo)
                if not path_within(path, self.cwd_roots):
                    raise OperatorError("git -C path is outside approved roots")
                values = values[2:]
            subcommand = next((value for value in values if not value.startswith("-")), "")
            if subcommand not in set(self.policy.get("git_allowed_subcommands", [])):
                raise OperatorError(f"git subcommand is not allowed: {subcommand}")
            joined = " ".join(values).lower()
            forbidden = (
                "--force",
                "--force-with-lease",
                "reset --hard",
                "clean -f",
                "clean -d",
                "push -f",
            )
            if any(value in joined for value in forbidden):
                raise OperatorError("destructive git option is not allowed")
        elif binary == "find":
            forbidden = {"-exec", "-execdir", "-delete", "-ok", "-okdir"}
            if any(value in forbidden for value in tail):
                raise OperatorError("mutating find actions are not allowed")
        elif binary == "sed":
            if "-i" in tail or any(value.startswith("-i") for value in tail):
                raise OperatorError("sed in-place editing is not allowed")
            self._validate_path_arguments(tail, cwd)
        elif binary in {
            "cat",
            "tail",
            "head",
            "grep",
            "rg",
            "find",
            "ls",
            "stat",
            "shasum",
            "wc",
            "sort",
            "uniq",
            "cut",
            "tr",
            "du",
        }:
            self._validate_path_arguments(tail, cwd)

    def _process_signal(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            pid = int(args.get("pid"))
        except (TypeError, ValueError) as exc:
            raise OperatorError("process_signal requires an integer pid") from exc
        if pid <= 100:
            raise OperatorError("refusing to signal low system pid")
        signal_name = str(args.get("signal", "TERM")).upper()
        if signal_name not in set(self.policy.get("process_signal_allowlist", [])):
            raise OperatorError(f"signal is not allowed: {signal_name}")
        command_result = self._capture(
            ["ps", "-p", str(pid), "-o", "command="],
            timeout=10,
            cwd=self.repo,
        )
        if command_result["returncode"] != 0 or not command_result["stdout"].strip():
            raise OperatorError(f"pid is not running: {pid}")
        expected_command = str(args.get("expected_command_contains", "")).strip()
        if signal_name == "KILL" and not expected_command:
            raise OperatorError("KILL requires expected_command_contains")
        if expected_command and expected_command not in command_result["stdout"]:
            raise OperatorError("process command does not match expected_command_contains")
        os.kill(pid, getattr(signal, f"SIG{signal_name}"))
        return {
            "pid": pid,
            "signal": signal_name,
            "command": command_result["stdout"].strip(),
        }

    def _launchctl_action(self, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        action = str(args.get("action", ""))
        allowed = set(self.policy.get("launchctl_allowed_subcommands", []))
        if action not in allowed:
            raise OperatorError(f"launchctl action is not allowed: {action}")
        prefixes = tuple(str(value) for value in self.policy.get("launchctl_label_prefixes", []))
        domain = str(args.get("domain", f"gui/{os.getuid()}"))
        if domain != f"gui/{os.getuid()}":
            raise OperatorError("launchctl domain must be the current user GUI domain")
        if action == "bootstrap":
            plist = expand_path(args.get("plist", ""), repo=self.repo)
            if not path_within(plist, self.cwd_roots) or not plist.is_file():
                raise OperatorError("launchctl bootstrap plist is not approved")
            argv = ["launchctl", "bootstrap", domain, str(plist)]
        else:
            label = str(args.get("label", "")).strip()
            if not label.startswith(prefixes):
                raise OperatorError("launchctl label is not approved")
            target = f"{domain}/{label}"
            argv = ["launchctl", action]
            if bool(args.get("kill", False)) and action == "kickstart":
                argv.append("-k")
            argv.append(target)
        return self._capture(argv, timeout=timeout, cwd=self.repo)

