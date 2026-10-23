"""Structural check: every workflow is valid YAML, a valid workflow, and runnable.

A workflow file that GitHub cannot parse, or whose schema is wrong, does not appear in the
Actions tab and has no *Run workflow* button — the failure mode is silence, not an error the
contributor sees. This file parses each workflow itself (the dev lock has no YAML library) and
asserts the part of GitHub's schema that decides whether a workflow is listed and runnable:

- it parses as block YAML, with no duplicate keys;
- every key is one GitHub defines, and every job and step is complete;
- the workflow has a name (otherwise the Actions tab lists it by file path);
- it can be dispatched by hand (``workflow_dispatch``), so a maintainer can always run it;
- a ``push`` filter names both common default-branch names, because a filter that misses the
  repository's default branch silently stops validation after a merge.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
# A `push` filter must match the default branch. The repository does not record its name, so
# both conventional spellings are listed; an unmatched filter disables the workflow after every
# merge, with no error anywhere.
DEFAULT_BRANCHES = ("main", "master")
TOP_LEVEL_KEYS = {
    "name",
    "run-name",
    "on",
    "permissions",
    "env",
    "defaults",
    "concurrency",
    "jobs",
}
JOB_KEYS = {
    "name",
    "permissions",
    "needs",
    "if",
    "runs-on",
    "environment",
    "concurrency",
    "outputs",
    "env",
    "defaults",
    "steps",
    "timeout-minutes",
    "strategy",
    "continue-on-error",
    "container",
    "services",
    "uses",
    "with",
    "secrets",
}
STEP_KEYS = {
    "id",
    "if",
    "name",
    "uses",
    "run",
    "working-directory",
    "shell",
    "with",
    "env",
    "continue-on-error",
    "timeout-minutes",
}
TRIGGERS = {
    "branch_protection_rule",
    "check_run",
    "check_suite",
    "create",
    "delete",
    "deployment",
    "deployment_status",
    "discussion",
    "discussion_comment",
    "fork",
    "gollum",
    "issue_comment",
    "issues",
    "label",
    "merge_group",
    "milestone",
    "page_build",
    "project",
    "project_card",
    "project_column",
    "public",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_target",
    "push",
    "registry_package",
    "release",
    "repository_dispatch",
    "schedule",
    "status",
    "watch",
    "workflow_call",
    "workflow_dispatch",
    "workflow_run",
}
MAX_TIMEOUT_MINUTES = 360
BLOCK_MARKERS = {"|", "|-", "|+", ">", ">-", ">+"}


class WorkflowError(Exception):
    """A workflow file that GitHub would reject or hide."""


class _Line:
    __slots__ = ("number", "indent", "text", "raw")

    def __init__(self, number: int, indent: int, text: str, raw: str) -> None:
        self.number = number
        self.indent = indent
        self.text = text
        self.raw = raw


def _tokenize(source: str, name: str) -> list[_Line]:
    lines: list[_Line] = []
    block_indent: int | None = None
    for number, raw in enumerate(source.splitlines(), 1):
        if "\t" in raw:
            raise WorkflowError(f"{name}:{number}: tab character")
        indent = len(raw) - len(raw.lstrip(" "))
        text = raw.strip()
        if block_indent is not None:
            # Block scalar content is opaque to YAML; only its indentation matters.
            if not text or indent > block_indent:
                continue
            block_indent = None
        if not text or text.startswith("#"):
            continue
        lines.append(_Line(number, indent, text, raw))
    return lines


def _split_key(text: str, name: str, number: int) -> tuple[str, str | None]:
    quote = text[:1]
    if quote in {'"', "'"}:
        end = text.find(quote, 1)
        if end == -1:
            raise WorkflowError(f"{name}:{number}: unterminated quoted key")
        rest = text[end + 1 :].lstrip()
        if not rest.startswith(":"):
            return text, None
        return text[1:end], rest[1:].strip()
    for position, character in enumerate(text):
        if character == ":" and (position + 1 == len(text) or text[position + 1] in " \t"):
            return text[:position].strip(), text[position + 1 :].strip()
    return text, None


class _Parser:
    def __init__(self, lines: list[_Line], name: str) -> None:
        self.lines = lines
        self.name = name
        self.index = 0

    def peek(self) -> _Line | None:
        return self.lines[self.index] if self.index < len(self.lines) else None

    def block(self, indent: int):
        line = self.peek()
        if line is None or line.indent < indent:
            return None
        if line.text == "-" or line.text.startswith("- "):
            return self.sequence(line.indent)
        return self.mapping(line.indent)

    def sequence(self, indent: int) -> list:
        items: list = []
        while True:
            line = self.peek()
            if (
                line is None
                or line.indent != indent
                or not (line.text == "-" or line.text.startswith("- "))
            ):
                break
            body = line.text[1:].strip()
            self.index += 1
            if not body:
                items.append(self.block(indent + 1))
                continue
            key, value = _split_key(body, self.name, line.number)
            if value is None:
                items.append(body)
                continue
            mapping: dict = {}
            self.entry(mapping, key, value, line, indent + 2)
            self.extend(mapping, indent + 2)
            items.append(mapping)
        return items

    def entry(self, mapping: dict, key: str, value: str, line: _Line, indent: int) -> None:
        if key in mapping:
            raise WorkflowError(f"{self.name}:{line.number}: duplicate key {key!r}")
        if value in BLOCK_MARKERS:
            mapping[key] = self.scalar(line, indent)
        elif value == "":
            following = self.peek()
            mapping[key] = (
                self.block(following.indent)
                if following is not None and following.indent > indent
                else None
            )
        else:
            mapping[key] = value

    def scalar(self, line: _Line, indent: int) -> str:
        collected: list[str] = []
        while True:
            following = self.peek()
            if following is None or following.indent <= indent:
                break
            collected.append(following.raw.strip())
            self.index += 1
        return "\n".join(collected)

    def extend(self, mapping: dict, indent: int) -> dict:
        while True:
            line = self.peek()
            if line is None or line.indent != indent:
                break
            if line.text == "-" or line.text.startswith("- "):
                raise WorkflowError(f"{self.name}:{line.number}: sequence item in a mapping")
            key, value = _split_key(line.text, self.name, line.number)
            if value is None:
                raise WorkflowError(f"{self.name}:{line.number}: expected 'key: value'")
            self.index += 1
            self.entry(mapping, key, value, line, indent)
        return mapping

    def mapping(self, indent: int) -> dict:
        mapping: dict = {}
        self.extend(mapping, indent)
        return mapping


def parse_workflow(path: Path) -> dict:
    """Parse one workflow file into plain dicts, lists, and strings."""
    lines = _tokenize(path.read_text(encoding="utf-8"), path.name)
    if not lines:
        raise WorkflowError(f"{path.name}: empty")
    parser = _Parser(lines, path.name)
    document = parser.block(lines[0].indent)
    if parser.index != len(lines):
        line = lines[parser.index]
        raise WorkflowError(f"{path.name}:{line.number}: unexpected indentation")
    if not isinstance(document, dict):
        raise WorkflowError(f"{path.name}: top level must be a mapping")
    return document


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def _minutes(value: object) -> int | None:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def audit(path: Path) -> list[str]:
    """Everything that stops GitHub from listing, running, or dispatching this workflow."""
    problems: list[str] = []
    try:
        document = parse_workflow(path)
    except WorkflowError as exc:
        return [str(exc)]
    unknown = set(document) - TOP_LEVEL_KEYS
    if unknown:
        problems.append(f"{path.name}: unknown top-level keys {sorted(unknown)}")
    if not isinstance(document.get("name"), str) or not document["name"].strip():
        problems.append(f"{path.name}: a workflow needs a name in the Actions tab")
    triggers = document.get("on")
    if isinstance(triggers, dict):
        unknown = set(triggers) - TRIGGERS
        if unknown:
            problems.append(f"{path.name}: unknown triggers {sorted(unknown)}")
    elif isinstance(triggers, str):
        if triggers not in TRIGGERS:
            problems.append(f"{path.name}: unknown trigger {triggers!r}")
    else:
        problems.append(f"{path.name}: on: must name a trigger")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return problems + [f"{path.name}: no jobs"]
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            problems.append(f"{path.name}:{job_name}: job is not a mapping")
            continue
        unknown = set(job) - JOB_KEYS
        if unknown:
            problems.append(f"{path.name}:{job_name}: unknown job keys {sorted(unknown)}")
        if "runs-on" not in job and "uses" not in job:
            problems.append(f"{path.name}:{job_name}: no runs-on")
        if "uses" in job and "steps" in job:
            problems.append(f"{path.name}:{job_name}: has both uses: and steps:")
        needs = job.get("needs")
        for dependency in [needs] if isinstance(needs, str) else _as_list(needs):
            if dependency not in jobs:
                problems.append(f"{path.name}:{job_name}: needs unknown job {dependency}")
        minutes = _minutes(job.get("timeout-minutes"))
        if job.get("timeout-minutes") is not None and not (
            minutes and 0 < minutes <= MAX_TIMEOUT_MINUTES
        ):
            problems.append(f"{path.name}:{job_name}: bad timeout-minutes")
        steps = _as_list(job.get("steps"))
        if "uses" not in job and not steps:
            problems.append(f"{path.name}:{job_name}: no steps")
        for position, step in enumerate(steps, 1):
            label = f"{path.name}:{job_name}: step {position}"
            if not isinstance(step, dict):
                problems.append(f"{label}: not a mapping")
                continue
            unknown = set(step) - STEP_KEYS
            if unknown:
                problems.append(f"{label}: unknown keys {sorted(unknown)}")
            if ("uses" in step) == ("run" in step):
                problems.append(f"{label}: needs exactly one of uses: and run:")
            if "with" in step and "uses" not in step:
                problems.append(f"{label}: with: without uses:")
            if "shell" in step and "run" not in step:
                problems.append(f"{label}: shell: without run:")
            minutes = _minutes(step.get("timeout-minutes"))
            if step.get("timeout-minutes") is not None and not (
                minutes and 0 < minutes <= MAX_TIMEOUT_MINUTES
            ):
                problems.append(f"{label}: bad timeout-minutes")
    return problems


def test_every_workflow_is_a_valid_workflow() -> None:
    assert workflow_files(), "the workflows are missing"
    problems: list[str] = []
    for path in workflow_files():
        problems.extend(audit(path))
    assert not problems, problems


def test_every_workflow_has_its_own_name() -> None:
    """Two workflows with one name are indistinguishable in the Actions tab."""
    seen: dict[str, str] = {}
    duplicates = []
    for path in workflow_files():
        name = parse_workflow(path).get("name")
        if name in seen:
            duplicates.append(f"{path.name} and {seen[name]} are both named {name!r}")
        else:
            seen[name] = path.name
    assert not duplicates, duplicates


def test_every_workflow_can_be_run_by_hand() -> None:
    """A maintainer must be able to dispatch it, or a stuck workflow cannot be retried."""
    missing = []
    for path in workflow_files():
        triggers = parse_workflow(path).get("on")
        names = set(triggers) if isinstance(triggers, dict) else {triggers}
        if "workflow_dispatch" not in names:
            missing.append(path.name)
    assert not missing, f"workflows without workflow_dispatch (not runnable by hand): {missing}"


def test_every_push_trigger_covers_the_default_branch() -> None:
    """A `push` filter that misses the default branch disables the workflow silently."""
    problems = []
    for path in workflow_files():
        triggers = parse_workflow(path).get("on")
        if not isinstance(triggers, dict) or "push" not in triggers:
            continue
        push = triggers["push"]
        if not isinstance(push, dict) or "branches" not in push:
            continue
        branches = set(_as_list(push["branches"]))
        missing = set(DEFAULT_BRANCHES) - branches
        if missing:
            problems.append(f"{path.name}: push filter misses {sorted(missing)}")
    assert not problems, problems


SCAN_WORKFLOW = "vulnerability-scan.yml"


def test_the_image_scan_runs_on_commits_and_not_only_on_a_schedule() -> None:
    """A weekly-only scan reports a bad image days after the commit that shipped it."""
    workflow = WORKFLOWS / SCAN_WORKFLOW
    assert workflow.is_file(), "the image scan workflow is missing"
    triggers = parse_workflow(workflow).get("on")
    problems = []
    if not isinstance(triggers, dict):
        problems.append("on: must be a mapping")
    else:
        for trigger in ("schedule", "workflow_dispatch"):
            if trigger not in triggers:
                problems.append(f"keep the {trigger} trigger")
        push = triggers.get("push")
        if not isinstance(push, dict):
            problems.append("the scan must run on a pushed commit")
        else:
            branches = set(_as_list(push.get("branches")))
            for branch in sorted(set(DEFAULT_BRANCHES) - branches):
                problems.append(f"the push filter misses {branch}")
            if not _as_list(push.get("tags")):
                problems.append("a release tag must run the scan too")
    assert not problems, f"{workflow.name}: {problems}"


if __name__ == "__main__":  # pragma: no cover - lets the file self-check without pytest
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("workflow syntax checked")
