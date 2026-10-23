"""The scheduled vulnerability scan must scan every image a release manifest publishes.

The scan downloads the manifest of the newest *successful* `Build Windows release` run, so the
manifest can come from a release older than this checkout's schema: schema 2 predates the egress
proxy image, and reading `manifest["egress_proxy"]` in workflow YAML therefore failed with a
`KeyError` instead of scanning anything. The reference list lives in
`scripts/release_image_refs.py` so that tolerance is covered here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release_image_refs import SCHEMA_IMAGES, ManifestError, references

ROOT = Path(__file__).resolve().parents[2]
SCAN_WORKFLOW = ROOT / ".github" / "workflows" / "vulnerability-scan.yml"
RUNNER_TARGETS = ("base", "python", "node", "polyglot")
DIGEST = "sha256:" + "a" * 64
TAG_ONLY = "ghcr.io/example/gateway:v1.1.0"
SHORT_DIGEST = "ghcr.io/example/egress-proxy@sha256:" + "a" * 63


def pinned(name: str) -> str:
    return f"ghcr.io/example/{name}:v1.1.0@{DIGEST}"


def manifest(schema_version: int, *, egress_proxy: bool = True, runners: bool = True) -> dict:
    data: dict = {
        "schema_version": schema_version,
        "gateway": pinned("gateway"),
        "executor": pinned("executor"),
    }
    if egress_proxy:
        data["egress_proxy"] = pinned("egress-proxy")
    if runners:
        data["runners"] = {target: pinned(f"runner-{target}") for target in RUNNER_TARGETS}
    return data


def runner_references() -> list[str]:
    return [pinned(f"runner-{target}") for target in RUNNER_TARGETS]


def without_schema() -> dict:
    data = manifest(3)
    del data["schema_version"]
    return data


def test_the_current_schema_scans_every_published_image() -> None:
    found = references(manifest(3))

    assert found == [
        pinned("gateway"),
        pinned("executor"),
        pinned("egress-proxy"),
        *runner_references(),
    ]


@pytest.mark.parametrize(
    ("schema_version", "expected"),
    [
        # Schema 2 shipped the runner images and no egress proxy: this is the shape that made
        # the scan fail with a KeyError.
        (2, [pinned("gateway"), pinned("executor"), *runner_references()]),
        # The oldest manifest publishes the gateway and the executor only.
        (1, [pinned("gateway"), pinned("executor")]),
    ],
)
def test_an_older_release_is_still_scanned(schema_version: int, expected: list[str]) -> None:
    data = manifest(schema_version, egress_proxy=False, runners=schema_version > 1)

    assert references(data) == expected


def test_a_schema_newer_than_the_scan_is_refused() -> None:
    with pytest.raises(ManifestError, match="newer than this scan"):
        references(manifest(max(SCHEMA_IMAGES) + 1))


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(without_schema(), id="no-schema-version"),
        pytest.param({"schema_version": "3"}, id="schema-version-not-an-int"),
        pytest.param([], id="not-an-object"),
        pytest.param(None, id="null"),
        # A known schema that lost a key it declares is corrupt, not an older release.
        pytest.param(manifest(3, egress_proxy=False), id="schema-3-without-egress-proxy"),
        pytest.param(manifest(3, runners=False), id="schema-3-without-runners"),
        pytest.param(manifest(2, runners=False), id="schema-2-without-runners"),
        pytest.param(manifest(3, egress_proxy=False, runners=False), id="gateway-only"),
    ],
)
def test_a_manifest_that_cannot_be_scanned_is_refused(data: object) -> None:
    with pytest.raises(ManifestError):
        references(data)


def test_every_scanned_reference_must_be_digest_pinned() -> None:
    data = manifest(3)
    data["executor"] = TAG_ONLY

    with pytest.raises(ManifestError, match="executor is not a digest-pinned"):
        references(data)


def test_a_short_digest_is_not_a_digest_pin() -> None:
    data = manifest(3)
    data["egress_proxy"] = SHORT_DIGEST

    with pytest.raises(ManifestError, match="egress_proxy is not a digest-pinned"):
        references(data)


def test_a_runner_reference_must_be_digest_pinned_and_is_named() -> None:
    data = manifest(3)
    data["runners"] = {**data["runners"], "base": "ghcr.io/example/runner-base:v1.1.0"}

    with pytest.raises(ManifestError, match="runners.base is not a digest-pinned"):
        references(data)


def test_empty_runners_are_refused_rather_than_scanned_vacuously() -> None:
    data = manifest(3)
    data["runners"] = {}

    with pytest.raises(ManifestError, match="non-empty"):
        references(data)


def test_the_scan_workflow_reads_the_manifest_through_this_script() -> None:
    """Inline key access in YAML is what a later schema breaks; the script owns it instead."""
    text = SCAN_WORKFLOW.read_text(encoding="utf-8")

    assert "scripts/release_image_refs.py" in text
    assert "manifest[" not in text


def test_the_script_prints_one_reference_per_line(tmp_path: Path) -> None:
    manifest_path = tmp_path / "release-images.json"
    manifest_path.write_text(json.dumps(manifest(3)), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "scripts/release_image_refs.py", str(manifest_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == references(manifest(3))
    assert result.stderr == ""


def test_the_script_names_the_unscannable_manifest_and_exits_non_zero(tmp_path: Path) -> None:
    manifest_path = tmp_path / "release-images.json"
    manifest_path.write_text(json.dumps(manifest(2, runners=False)), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "scripts/release_image_refs.py", str(manifest_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr
    assert "runners" in result.stderr
