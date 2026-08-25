#!/usr/bin/env python3
"""Capture immutable half-page screenshots for newly observed all-time lows."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

MIN_WIDTH = 1_000
MIN_HEIGHT = 700
MIN_PNG_BYTES = 10_000


def validate_request(request: dict) -> None:
    """Reject any screenshot request that could escape the intended source/root."""
    parsed = urlsplit(str(request.get("url", "")))
    if (parsed.scheme != "https" or parsed.netloc != "www.refurbed.se"
            or not parsed.path.startswith("/p/")):
        raise ValueError("price proof requires an HTTPS Refurbed product URL")

    screenshot = Path(str(request.get("screenshot", "")))
    if (screenshot.is_absolute() or not screenshot.parts
            or screenshot.parts[0] != "price-proofs"
            or ".." in screenshot.parts or screenshot.suffix.lower() != ".png"):
        raise ValueError("screenshot must be a PNG inside price-proofs/")

    if not isinstance(request.get("key"), str) or not request["key"]:
        raise ValueError("price proof requires a comparison key")
    if not isinstance(request.get("price"), int):
        raise ValueError("price proof requires an integer price")
    if not isinstance(request.get("seen_at"), str) or not request["seen_at"]:
        raise ValueError("price proof requires an observation timestamp")


def png_dimensions(path: Path) -> tuple[int, int]:
    """Read width and height from a PNG IHDR without third-party packages."""
    with path.open("rb") as handle:
        header = handle.read(24)
    if (len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n"
            or header[12:16] != b"IHDR"):
        raise ValueError(f"Chrome did not produce a valid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def _matching_record(state: dict, request: dict) -> dict:
    record = state.get("price_lows", {}).get(request["key"])
    if not record:
        raise ValueError(f"price-low record disappeared for {request['key']}")
    for field in ("price", "url", "seen_at"):
        if record.get(field) != request[field]:
            raise ValueError(
                f"price-low record changed before proof capture: {request['key']} {field}"
            )
    return record


def _capture_one(request: dict, root: Path, chrome_bin: str) -> Path:
    target = (root / request["screenshot"]).resolve()
    proof_root = (root / "price-proofs").resolve()
    if proof_root not in target.parents:
        raise ValueError("screenshot must remain inside price-proofs/")
    target.parent.mkdir(parents=True, exist_ok=True)

    command = [
        chrome_bin,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--window-size=1440,1200",
        "--force-device-scale-factor=1",
        "--virtual-time-budget=8000",
        f"--screenshot={target}",
        request["url"],
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout).strip()[-1_000:]
        raise RuntimeError(f"Chrome screenshot failed ({result.returncode}): {detail}")
    if not target.exists() or target.stat().st_size < MIN_PNG_BYTES:
        target.unlink(missing_ok=True)
        raise RuntimeError("Chrome screenshot was missing or suspiciously small")
    width, height = png_dimensions(target)
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Chrome screenshot was too small: {width}x{height}")
    return target


def capture_all(
    state_path: Path,
    requests_path: Path,
    root: Path,
    chrome_bin: str,
) -> int:
    """Capture every queued proof, then atomically attach paths to state."""
    if not requests_path.exists():
        return 0
    requests = json.loads(requests_path.read_text(encoding="utf-8"))
    if not isinstance(requests, list):
        raise ValueError("price proof request manifest must be a JSON list")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    root = root.resolve()

    for request in requests:
        validate_request(request)
        record = _matching_record(state, request)
        _capture_one(request, root, chrome_bin)
        record["screenshot"] = request["screenshot"]

    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(state_path)
    return len(requests)


def find_chrome() -> str:
    configured = os.environ.get("CHROME_BIN")
    if configured:
        return configured
    for candidate in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(candidate)
        if found:
            return found
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac_chrome.exists():
        return str(mac_chrome)
    raise RuntimeError("No Chrome/Chromium executable found for price-proof capture")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument(
        "--requests", type=Path, default=Path("price_proof_requests.json")
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()

    if not args.requests.exists():
        print("No new historical-low screenshot to capture.")
        return 0
    count = capture_all(
        state_path=args.state,
        requests_path=args.requests,
        root=args.root,
        chrome_bin=find_chrome(),
    )
    print(f"Captured {count} historical-low screenshot proof{'s' if count != 1 else ''}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
