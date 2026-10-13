from __future__ import annotations

import json
from pathlib import Path

from ..core.schema import OperatorError
from .isolated_cycle import TEST_COMMANDS
from .isolated_planner import _digest, _read_source, plan_isolated_request
from .isolated_receipts import _load_verified_receipt, report_isolated_receipt
from .isolated_worker import create_isolated_worker


def _published_identity(*, repo: Path, state_dir: Path, goal: str,
                        files: list[str], test_command: list[str]) -> str | None:
    head, sources = _read_source(repo, files)
    basis = {
        "project": str(repo),
        "head": head,
        "goal": goal.strip(),
        "files": [
            {key: item[key] for key in ("path", "before_sha256")}
            for item in sources
        ],
        "test_command": test_command,
    }
    digest = _digest(basis)
    request_id = "isolated-" + digest[:24]
    journal = state_dir / "planning-intents" / (request_id + ".json")
    if not journal.exists() and not journal.is_symlink():
        return None
    if journal.is_symlink() or not journal.is_file():
        raise OperatorError("existing planning identity is not an ordinary file")
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise OperatorError("existing planning identity is unreadable") from exc
    if (not isinstance(record, dict) or record.get("request_id") != request_id
            or record.get("state") != "published"
            or record.get("source_head") != head
            or record.get("goal_sha256") != digest):
        raise OperatorError("existing planning attempt is incomplete or uncertain")
    return request_id


def run_isolated_once(*, repo: Path, control_worktree: Path,
                      state_dir: Path, branch: str, image: str,
                      goal: str, files: list[str],
                      test_command: list[str],
                      chat_repo: Path | None = None) -> dict:
    if (not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 2000
            or not isinstance(test_command, list)
            or tuple(test_command) not in TEST_COMMANDS):
        raise OperatorError("isolated flow requires an approved goal and fixed test suite")
    repo = Path(repo).resolve()
    state_dir = Path(state_dir).resolve()
    existing = _published_identity(
        repo=repo, state_dir=state_dir, goal=goal,
        files=files, test_command=test_command)
    if existing is None:
        planned = plan_isolated_request(
            repo=repo, control_worktree=control_worktree,
            state_dir=state_dir, branch=branch, image=image,
            goal=goal, files=files, test_command=test_command,
            chat_repo=chat_repo)
        request_id = planned["request_id"]
    else:
        request_id = existing
    agent = create_isolated_worker(
        repo=repo, control_worktree=control_worktree,
        state_dir=state_dir, branch=branch, image=image)
    if agent.run(once=True) != 0:
        raise OperatorError("isolated worker scan failed; do not retry the model send")
    receipt = _load_verified_receipt(agent, request_id)
    if receipt is None:
        return {
            "state": "awaiting_receipt", "request_id": request_id,
            "planning_reused": existing is not None,
            "chatgpt_acknowledged": False, "production_deployed": False,
        }
    acknowledged = agent.state_dir / "receipt-reports" / (request_id + ".json")
    if acknowledged.exists() or acknowledged.is_symlink():
        if acknowledged.is_symlink() or not acknowledged.is_file():
            raise OperatorError("existing receipt acknowledgment is unsafe")
        try:
            record = json.loads(acknowledged.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise OperatorError("existing receipt acknowledgment is unreadable") from exc
        if (not isinstance(record, dict)
                or record.get("state") != "acknowledged"
                or record.get("request_id") != request_id):
            raise OperatorError("previous receipt delivery outcome is uncertain")
        return {
            "state": "completed" if receipt["state"] == "succeeded" else "failed",
            "request_id": request_id, "receipt_state": receipt["state"],
            "planning_reused": True, "chatgpt_acknowledged": True,
            "production_deployed": False, "automatic_merge": False,
        }
    feedback = report_isolated_receipt(
        repo=repo, control_worktree=control_worktree,
        state_dir=state_dir, branch=branch, image=image,
        request_id=request_id, chat_repo=chat_repo)
    if feedback["state"] != "acknowledged":
        return {
            "state": "awaiting_receipt", "request_id": request_id,
            "planning_reused": existing is not None,
            "chatgpt_acknowledged": False, "production_deployed": False,
        }
    return {
        "state": "completed" if receipt["state"] == "succeeded" else "failed",
        "request_id": request_id, "receipt_state": receipt["state"],
        "planning_reused": existing is not None,
        "chatgpt_acknowledged": True,
        "production_deployed": False, "automatic_merge": False,
    }
