#!/usr/bin/env python3
"""Capture immutable half-page screenshots for newly observed all-time lows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from refurbed_watch import parse_product_price, parse_variant_title

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


def _expected_configuration(request: dict) -> tuple[str, float, float]:
    try:
        _, chip, ram, ssd = request["key"].rsplit("|", 3)
        return chip, float(ram), float(ssd)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("price proof comparison key is malformed") from exc


def validate_rendered_page(request: dict, page_html: str) -> dict:
    """Require the rendered product page to prove the requested specs and price."""
    expected_chip, expected_ram, expected_ssd = _expected_configuration(request)
    _, chip, ram, ssd = parse_variant_title(page_html)
    price = parse_product_price(page_html)
    expected = (expected_chip, expected_ram, expected_ssd, request["price"])
    actual = (chip, ram, ssd, price)
    if actual != expected:
        raise ValueError(
            "rendered Refurbed page does not match historical-low request: "
            f"expected {expected}, got {actual}"
        )
    return {
        "version": 1,
        "chip": chip,
        "ram_gb": ram,
        "ssd_gb": ssd,
        "price": price,
    }


def _dump_rendered_dom(request: dict, chrome_bin: str) -> str:
    command = [
        chrome_bin,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--virtual-time-budget=8000",
        "--dump-dom",
        request["url"],
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 or "<title" not in result.stdout.lower():
        detail = (result.stderr or result.stdout).strip()[-1_000:]
        raise RuntimeError(
            f"Chrome could not inspect rendered product page ({result.returncode}): {detail}"
        )
    return result.stdout


def _capture_one(request: dict, root: Path, chrome_bin: str) -> tuple[Path, dict]:
    target = (root / request["screenshot"]).resolve()
    proof_root = (root / "price-proofs").resolve()
    if proof_root not in target.parents:
        raise ValueError("screenshot must remain inside price-proofs/")
    target.parent.mkdir(parents=True, exist_ok=True)

    validation = validate_rendered_page(
        request, _dump_rendered_dom(request, chrome_bin)
    )

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
    # Recheck after capture so a redirect or rapidly changed offer cannot leave
    # a PNG attached to configuration data from a different page load.
    validate_rendered_page(request, _dump_rendered_dom(request, chrome_bin))
    validation["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    return target, validation


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
        _, validation = _capture_one(request, root, chrome_bin)
        record["screenshot"] = request["screenshot"]
        record["proof_validation"] = validation

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
