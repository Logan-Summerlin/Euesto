"""Structural checks for the workflow invariants that make a hang diagnosable.

A workflow that omits a step ceiling, the collect step, or the log artifact turns any hang into
an anonymous job cancellation, which is how the Linux validation job lost the hardening the
Windows release job already had (docs/TESTING.md, "Required checks"). These checks read the
workflow files as text: the dev lock has no YAML parser, and the structural tests already scan
files rather than load them.
"""

from __future__ import annotations

import re
from pathlib import Path

import scripts.validate as validate

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
# A step ceiling must leave the harness at least this much time to kill, reap, and report.
HARNESS_MARGIN_SECONDS = 60
# A job ceiling must leave this much time for checkout, bootstrap, and artifact uploads.
JOB_MARGIN_SECONDS = 600
PYTEST_TIERS = frozenset({"collect", "fast", "slow", "docker"})
TIER_CALL = re.compile(r"scripts/validate\.py\s+([a-z][a-z0-9-]*)")
LOGGED_STEP = "scripts/validation-step.sh"
# Security assertions belong in tests/docker/ (one authoritative copy), not in YAML.
FORBIDDEN_IN_WORKFLOWS = (
    "assert ",
    "network_mode",
    "no-new-privileges",
    "docker.sock",
    "cap_drop",
)


def _minutes(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*", value)
    return int(match.group(1)) if match else None


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def parse(text: str) -> dict[str, dict]:
    """Split a workflow into jobs, their ceiling, and their steps (the YAML subset we use)."""
    jobs: dict[str, dict] = {}
    job: dict | None = None
    step: dict | None = None
    in_jobs = False
    block_indent: int | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block_indent is not None:
            if indent > block_indent:
                continue
            block_indent = None
        body = raw.strip()
        if indent == 0:
            in_jobs = body == "jobs:"
            job = step = None
            continue
        if not in_jobs:
            continue
        if indent == 2 and body.endswith(":"):
            job = {"name": body[:-1], "minutes": None, "steps": []}
            jobs[body[:-1]] = job
            step = None
            continue
        if job is None:
            continue
        key, _, value = body.partition(":")
        key = key.strip()
        value = value.strip()
        if indent == 4 and not body.startswith("- "):
            step = None
            if key == "timeout-minutes":
                job["minutes"] = _minutes(value)
            continue
        if indent >= 6 and body.startswith("- "):
            body = body[2:]
            key, _, value = body.partition(":")
            key = key.strip()
            value = value.strip()
            step = {"text": body, "minutes": None}
            job["steps"].append(step)
        if step is None:
            continue
        if step["text"] != body:
            step["text"] += "\n" + body
        if key == "timeout-minutes":
            step["minutes"] = _minutes(value)
        elif value in {"|", "|-", ">", ">-"}:
            block_indent = indent
    return jobs


def tiers(step: dict) -> list[str]:
    return TIER_CALL.findall(step["text"])


def tiered_steps() -> list[tuple[str, str, dict]]:
    found: list[tuple[str, str, dict]] = []
    for path in workflow_files():
        for name, job in parse(path.read_text(encoding="utf-8")).items():
            for step in job["steps"]:
                if tiers(step):
                    found.append((path.name, name, step))
    return found


def test_the_workflows_run_at_least_one_validation_tier() -> None:
    assert workflow_files(), "the workflows are missing"
    assert tiered_steps(), "no workflow runs scripts/validate.py"


def test_every_validation_step_has_a_ceiling() -> None:
    missing = [
        f"{name}:{job}: {step['text'].splitlines()[0]}"
        for name, job, step in tiered_steps()
        if step["minutes"] is None
    ]
    assert not missing, f"validation steps without timeout-minutes: {missing}"


def test_every_validation_step_runs_through_the_logging_wrapper() -> None:
    unwrapped = [
        f"{name}:{job}: {step['text'].splitlines()[0]}"
        for name, job, step in tiered_steps()
        if LOGGED_STEP not in step["text"]
    ]
    assert not unwrapped, (
        f"validation steps must run through {LOGGED_STEP} so the log and heartbeat survive: "
        f"{unwrapped}"
    )


def test_collection_runs_before_the_pytest_tiers_in_the_same_job() -> None:
    problems = []
    for path in workflow_files():
        for name, job in parse(path.read_text(encoding="utf-8")).items():
            steps = job["steps"]
            first_tier = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if PYTEST_TIERS.intersection(tiers(step))
                ),
                None,
            )
            if first_tier is None:
                continue
            collects = [
                index
                for index, step in enumerate(steps)
                if "collect" in tiers(step) or "--collect-only" in step["text"]
            ]
            if not collects or min(collects) > first_tier:
                problems.append(f"{path.name}:{name}: no collect step before the pytest tiers")
    assert not problems, problems


def test_every_tiered_workflow_uploads_its_logs_unconditionally() -> None:
    for path in workflow_files():
        text = path.read_text(encoding="utf-8")
        jobs = parse(text)
        if not any(tiers(step) for job in jobs.values() for step in job["steps"]):
            continue
        uploads = [
            step
            for job in jobs.values()
            for step in job["steps"]
            if "validation-logs" in step["text"]
        ]
        assert uploads, f"{path.name} must upload validation-logs"
        for step in uploads:
            assert "if: always()" in step["text"], (
                f"{path.name} must upload validation-logs with if: always()"
            )


def test_every_workflow_cancels_superseded_runs() -> None:
    for path in workflow_files():
        text = path.read_text(encoding="utf-8")
        assert "concurrency:" in text, f"{path.name} needs a concurrency group"
        assert "cancel-in-progress: true" in text, (
            f"{path.name} must cancel superseded runs instead of queueing a duplicate"
        )


def test_every_job_keeps_room_below_its_ceiling() -> None:
    problems = []
    for path in workflow_files():
        for name, job in parse(path.read_text(encoding="utf-8")).items():
            if job["minutes"] is None:
                problems.append(f"{path.name}:{name}: no job timeout-minutes")
                continue
            budget = sum(step["minutes"] or 0 for step in job["steps"]) * 60
            if job["minutes"] * 60 < budget + JOB_MARGIN_SECONDS:
                problems.append(
                    f"{path.name}:{name}: {job['minutes']} min ceiling cannot hold "
                    f"{budget // 60} min of steps plus {JOB_MARGIN_SECONDS // 60} min of setup"
                )
    assert not problems, problems


def test_each_step_ceiling_leaves_the_harness_time_to_report() -> None:
    problems = []
    for name, job, step in tiered_steps():
        for tier in tiers(step):
            assert tier in validate.TIER_TIMEOUTS, f"{name}:{job}: unknown tier {tier}"
            deadline = validate.TIER_TIMEOUTS[tier]
            ceiling = (step["minutes"] or 0) * 60
            needed = (deadline + HARNESS_MARGIN_SECONDS) // 60
            if ceiling < deadline + HARNESS_MARGIN_SECONDS:
                problems.append(
                    f"{name}:{job}: {tier} needs at least {needed} min "
                    f"for its {deadline}s deadline"
                )
    assert not problems, problems


def test_docker_tests_report_below_the_docker_tier_deadline() -> None:
    deadline = validate.TIER_TIMEOUTS["docker"]
    problems = []
    for path in sorted((ROOT / "tests" / "docker").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in (r"pytest\.mark\.timeout\((\d+)\)", r"timeout=(\d+)"):
            for match in re.finditer(pattern, text):
                seconds = int(match.group(1))
                if seconds >= deadline:
                    problems.append(f"{path.name}: {seconds}s is not below {deadline}s")
    assert not problems, problems


def test_security_assertions_live_in_the_docker_tests_not_in_yaml() -> None:
    problems = []
    for path in workflow_files():
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_IN_WORKFLOWS:
            if needle in text:
                problems.append(f"{path.name}: {needle!r} belongs in tests/docker/")
    assert not problems, problems


def test_compile_check_runs_once() -> None:
    duplicated = []
    for path in workflow_files():
        if "compileall" in path.read_text(encoding="utf-8"):
            duplicated.append(path.name)
    assert not duplicated, (
        f"the compile tier owns the file list; drop compileall from {duplicated}"
    )


if __name__ == "__main__":  # pragma: no cover - lets the file self-check without pytest
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("workflow budgets checked")
