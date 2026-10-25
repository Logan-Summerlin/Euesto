"""Print the digest-pinned image references that a release manifest lists.

`docker/release-images.json` is published by the release workflow and its schema grows with the
image set: schema 2 added the four runner images and schema 3 added the egress proxy. The
image scan (`.github/workflows/vulnerability-scan.yml`) reads the manifest of the newest
*successful* release, which can predate the schema in this checkout, so a key that an older
schema does not declare is not an error. A schema newer than this script understands is refused
instead: an image key it does not know would be published and never scanned.

Usage: python scripts/release_image_refs.py docker/release-images.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# The image keys each manifest schema declares. The form is read from `schema_version`, not
# guessed from the keys that happen to be present, so a manifest of a known schema that lost a
# key fails loudly instead of being scanned partially.
SCHEMA_IMAGES: dict[int, tuple[str, ...]] = {
    1: ("gateway", "executor"),
    2: ("gateway", "executor", "runners"),
    3: ("gateway", "executor", "egress_proxy", "runners"),
}
# The schema version `src/runtime_manager.py` accepts; a newer manifest is not scannable here.
LATEST_SCHEMA = max(SCHEMA_IMAGES)
RUNNERS_KEY = "runners"
# A digest-pinned reference: a name, a tag, and the immutable digest the scan must use.
DIGEST_PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_:@-]*@sha256:[0-9a-f]{64}$")


class ManifestError(RuntimeError):
    """The manifest cannot be turned into a scan list."""


def _image(where: str, value: object) -> str:
    if not isinstance(value, str) or not DIGEST_PINNED.match(value):
        raise ManifestError(f"{where} is not a digest-pinned image reference: {value!r}")
    return value


def references(manifest: object) -> list[str]:
    """Every image reference the manifest publishes, in manifest order, deduplicated."""
    if not isinstance(manifest, dict):
        raise ManifestError("the release manifest must be a JSON object")
    schema = manifest.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ManifestError(f"the release manifest has no integer schema_version: {schema!r}")
    if schema not in SCHEMA_IMAGES:
        raise ManifestError(
            f"schema {schema} is newer than this scan understands (schema {LATEST_SCHEMA}); "
            "add its image keys to scripts/release_image_refs.py so they are scanned"
        )
    found: list[str] = []
    for key in SCHEMA_IMAGES[schema]:
        if key not in manifest:
            raise ManifestError(f"schema {schema} requires a {key!r} entry")
        if key != RUNNERS_KEY:
            found.append(_image(key, manifest[key]))
            continue
        runners = manifest[key]
        if not isinstance(runners, dict) or not runners:
            raise ManifestError(f"{key} must be a non-empty object of image references")
        for name, reference in runners.items():
            found.append(_image(f"runners.{name}", reference))
    return list(dict.fromkeys(found))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_image_refs.py <release-images.json>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    try:
        found = references(manifest)
    except ManifestError as exc:
        print(f"error: {path}: {exc}", file=sys.stderr)
        return 2
    for reference in found:
        print(reference)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
