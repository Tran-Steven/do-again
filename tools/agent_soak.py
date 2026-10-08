#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from do_again.core.agent import Agent
from do_again.core.schema import atomic_json, request_fingerprint, utc_now


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * p)))
    return ordered[index]


def make_agent(root: Path, executions: list[str]) -> Agent:
    repo = root / "repo"
    control = root / "control"
    state = root / "state"
    policy = root / "policy.json"
    repo.mkdir(exist_ok=True)
    atomic_json(
        policy,
        {
            "schema_version": 1,
            "max_request_ttl_seconds": 3600,
            "allowed_operations": ["status"],
        },
    )
    agent = Agent(
        repo=repo,
        control_worktree=control,
        branch="operator-control",
        policy_path=policy,
        state_dir=state,
    )
    agent.publish_json = lambda relative, value, message: atomic_json(control / relative, value)
    agent.acquire_remote_claim = lambda request: (
        atomic_json(
            control / agent.claim_relative(request["request_id"]),
            {
                "request_id": request["request_id"],
                "request_fingerprint": request_fingerprint(request),
            },
        )
        or True
    )

    def execute(request):
        executions.append(str(request["request_id"]))
        return {
            "request_fingerprint": request_fingerprint(request),
            "result": {"returncode": 0},
        }

    agent.executor.execute = Mock(side_effect=execute)
    return agent


def run_soak(count: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        executions: list[str] = []
        agent = make_agent(root, executions)
        latencies_ms: list[float] = []
        paths: list[Path] = []

        for index in range(count):
            now = utc_now()
            request = {
                "schema_version": 1,
                "request_id": f"soak-request-{index:06d}",
                "operation": "status",
                "issued_at_utc": now.isoformat(),
                "expires_at_utc": (now + timedelta(minutes=10)).isoformat(),
                "args": {},
                "expected": {},
                "limits": {},
            }
            path = agent.requests_dir / f"{request['request_id']}.json"
            atomic_json(path, request)
            paths.append(path)
            started = time.perf_counter()
            changed = agent.process_path(path)
            latencies_ms.append((time.perf_counter() - started) * 1000.0)
            if not changed:
                raise RuntimeError(f"new request was not processed: {request['request_id']}")

        # Duplicate delivery must be a no-op.
        for path in paths:
            if agent.process_path(path):
                raise RuntimeError(f"duplicate request changed state: {path.stem}")

        # Restarted agent must also observe terminal receipts and not re-execute.
        restarted = make_agent(root, executions)
        for path in paths:
            if restarted.process_path(path):
                raise RuntimeError(f"restart replay changed state: {path.stem}")

        unique_executions = len(set(executions))
        if len(executions) != count or unique_executions != count:
            raise RuntimeError(
                f"exact-once failure: executions={len(executions)} unique={unique_executions} expected={count}"
            )

        return {
            "scope": "local Agent.process_path only; excludes Git remote, browser, ChatGPT, and network latency",
            "requests": count,
            "executions": len(executions),
            "unique_executions": unique_executions,
            "duplicate_replays": count,
            "restart_replays": count,
            "exact_once": True,
            "latency_ms": {
                "min": round(min(latencies_ms), 3),
                "median": round(statistics.median(latencies_ms), 3),
                "p95": round(percentile(latencies_ms, 0.95), 3),
                "max": round(max(latencies_ms), 3),
                "mean": round(statistics.fmean(latencies_ms), 3),
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an isolated Do Again local-agent soak benchmark.")
    parser.add_argument("--requests", type=int, default=250)
    parser.add_argument("--json-out")
    args = parser.parse_args()
    if args.requests < 1 or args.requests > 10000:
        parser.error("--requests must be between 1 and 10000")
    result = run_soak(args.requests)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
