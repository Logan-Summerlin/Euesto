from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.protocol import PROTOCOL_VERSION, protocol_is_compatible


def main() -> int:
    parser = argparse.ArgumentParser(description="Check desktop/gateway protocol compatibility")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    request = urllib.request.Request(
        f"{args.url.rstrip('/')}/v1/status",
        headers={"Authorization": f"Bearer {args.token}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        status = json.load(response)
    remote = str(status.get("protocol_version") or "")
    print(f"desktop={PROTOCOL_VERSION} gateway={remote}")
    return 0 if protocol_is_compatible(remote) else 2


if __name__ == "__main__":
    sys.exit(main())
