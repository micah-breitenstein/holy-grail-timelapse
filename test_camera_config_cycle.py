#!/usr/bin/env python3
"""Cycle camera ISO/shutter/f-stop values and verify what actually sticks.

This script is intended for debugging gphoto2 set-config behavior where a value
may be accepted but the camera keeps a different current value.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class ProbeResult:
    key: str
    requested: str
    ok: bool
    current: Optional[str]
    matched: bool
    message: str


def run_gphoto(args: List[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gphoto2", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_current_from_get_config(output: str) -> Optional[str]:
    match = re.search(r"^Current:\s*(.+?)\s*$", output, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip('"')


def parse_choices_from_get_config(output: str) -> List[str]:
    choices: List[str] = []
    for line in output.splitlines():
        line = line.strip()
        match = re.match(r"^Choice:\s+\d+\s+(.+)$", line)
        if match:
            choices.append(match.group(1).strip())
    return choices


def canonical_iso(value: str) -> str:
    token = value.strip()
    match = re.search(r"\d+", token)
    return str(int(match.group(0))) if match else token


def canonical_shutter(value: str) -> str:
    seconds = shutter_seconds(value)
    if seconds is None:
        return value.strip().lower().replace("sec", "").replace("s", "").strip()
    if abs(seconds - round(seconds)) < 1e-9:
        return str(int(round(seconds)))
    return f"{seconds:.9g}"


def shutter_seconds(value: str) -> Optional[float]:
    token = value.strip().lower().replace("sec", "").replace("s", "").strip()
    if token == "1/1":
        token = "1"
    if "/" in token:
        a, b = token.split("/", 1)
        try:
            num = float(a)
            den = float(b)
            if den == 0.0:
                return None
            return num / den
        except ValueError:
            return None
    try:
        return float(token)
    except ValueError:
        return None


def canonical_fstop(value: str) -> str:
    token = value.strip().lower().replace("f/", "")
    try:
        # Normalize 2.0 -> 2 and preserve decimals when needed.
        num = float(token)
        if abs(num - round(num)) < 1e-9:
            return str(int(round(num)))
        return f"{num:g}"
    except ValueError:
        return token


def canonical_value(key: str, value: str) -> str:
    if key == "iso":
        return canonical_iso(value)
    if key == "shutterspeed":
        return canonical_shutter(value)
    if key == "f-number":
        return canonical_fstop(value)
    return value.strip()


def resolve_requested_token(key: str, requested: str, choices: List[str]) -> str:
    if not choices:
        return requested

    if key == "iso":
        req = canonical_iso(requested)
        for token in choices:
            if canonical_iso(token) == req:
                return token
        return requested

    if key == "f-number":
        req = canonical_fstop(requested)
        for token in choices:
            if canonical_fstop(token) == req:
                return token
        return requested

    if key == "shutterspeed":
        req_s = shutter_seconds(requested)
        if req_s is None:
            return requested

        for token in choices:
            token_low = token.strip().lower()
            if token_low in ("bulb", "0/0"):
                continue
            tok_s = shutter_seconds(token)
            if tok_s is None:
                continue
            if abs(tok_s - req_s) <= 1e-9:
                return token
        return requested

    return requested


def get_config_state(key: str, timeout: int) -> Tuple[Optional[str], List[str], str]:
    result = run_gphoto(["--get-config", key], timeout=timeout)
    text = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        return None, [], text
    current = parse_current_from_get_config(result.stdout)
    choices = parse_choices_from_get_config(result.stdout)
    return current, choices, text


def set_and_verify(key: str, requested: str, send_value: str, timeout: int, settle_s: float) -> ProbeResult:
    set_result = run_gphoto(["--set-config", f"{key}={send_value}"], timeout=timeout)
    set_text = (set_result.stdout + "\n" + set_result.stderr).strip()
    if set_result.returncode != 0:
        return ProbeResult(key, requested, False, None, False, f"sent={send_value} | {set_text}")

    if settle_s > 0:
        time.sleep(settle_s)

    current, _, get_text = get_config_state(key, timeout)
    if current is None:
        return ProbeResult(key, requested, False, None, False, get_text)

    req_norm = canonical_value(key, requested)
    cur_norm = canonical_value(key, current)
    matched = req_norm == cur_norm
    if matched:
        message = f"matched (sent={send_value})"
    else:
        message = f"mismatch (sent={send_value}, requested={req_norm}, current={cur_norm})"
    return ProbeResult(key, requested, True, current, matched, message)


def parse_csv_tokens(raw: str) -> List[str]:
    return [token.strip() for token in raw.split(",") if token.strip()]


def print_probe_header(key: str, current: Optional[str], choices: List[str], show_choices: bool) -> None:
    print("\n" + "=" * 70)
    print(f"Testing {key}")
    print(f"Initial current: {current if current else 'unknown'}")
    if show_choices:
        print(f"Available choices ({len(choices)}):")
        print(", ".join(choices))


def run_probe(
    key: str,
    values: List[str],
    timeout: int,
    settle_s: float,
    delay_s: float,
    show_choices: bool,
) -> List[ProbeResult]:
    current, choices, raw = get_config_state(key, timeout)
    if current is None:
        print("\n" + "=" * 70)
        print(f"Testing {key}")
        print("Unable to read config:")
        print(raw)
        return []

    print_probe_header(key, current, choices, show_choices)
    results: List[ProbeResult] = []

    for idx, value in enumerate(values, start=1):
        send_value = resolve_requested_token(key=key, requested=value, choices=choices)
        if send_value != value:
            print(f"[{idx}/{len(values)}] set {key}={value} (sending {send_value})")
        else:
            print(f"[{idx}/{len(values)}] set {key}={value}")
        result = set_and_verify(
            key=key,
            requested=value,
            send_value=send_value,
            timeout=timeout,
            settle_s=settle_s,
        )
        results.append(result)

        if result.ok:
            current_text = result.current if result.current is not None else "unknown"
            print(f"  -> current: {current_text} | {result.message}")
        else:
            print("  -> failed")
            print(f"  -> {result.message}")

        if delay_s > 0:
            time.sleep(delay_s)

    return results


def summarize(all_results: Dict[str, List[ProbeResult]]) -> int:
    print("\n" + "#" * 70)
    print("Summary")
    print("#" * 70)

    total_failures = 0
    for key, results in all_results.items():
        if not results:
            print(f"{key}: no results")
            continue

        failed = sum(1 for r in results if not r.ok)
        mismatched = sum(1 for r in results if r.ok and not r.matched)
        matched = sum(1 for r in results if r.ok and r.matched)

        print(f"{key}: matched={matched}, mismatched={mismatched}, failed={failed}")

        for r in results:
            if r.ok and not r.matched:
                print(
                    f"  mismatch: requested={r.requested}, current={r.current}"
                )
            if not r.ok:
                print(f"  failed: requested={r.requested}")
        total_failures += failed + mismatched

    return total_failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cycle camera config values and verify readback.")
    parser.add_argument(
        "--iso-values",
        default="100,200,400,800,1600,3200",
        help="Comma-separated ISO values to test.",
    )
    parser.add_argument(
        "--shutter-values",
        default="1/1,1,1/2,1/3,1/4,1/5,1/8,1/15,1/30",
        help="Comma-separated shutter values to test.",
    )
    parser.add_argument(
        "--fstop-values",
        default="1.4,1.6,1.8,2,2.8,4,5.6,8,11,16",
        help="Comma-separated f-stop values to test.",
    )
    parser.add_argument(
        "--targets",
        default="iso,shutterspeed,f-number",
        help="Comma-separated config keys to probe: iso, shutterspeed, f-number.",
    )
    parser.add_argument("--timeout", type=int, default=20, help="gphoto2 command timeout seconds.")
    parser.add_argument("--settle-seconds", type=float, default=0.2, help="Wait after set-config before readback.")
    parser.add_argument("--delay-seconds", type=float, default=0.2, help="Wait between value tests.")
    parser.add_argument(
        "--show-choices",
        action="store_true",
        help="Print all available camera-reported choices before each probe.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    iso_values = parse_csv_tokens(args.iso_values)
    shutter_values = parse_csv_tokens(args.shutter_values)
    fstop_values = parse_csv_tokens(args.fstop_values)
    targets = [t.strip() for t in parse_csv_tokens(args.targets)]

    key_values: Dict[str, List[str]] = {
        "iso": iso_values,
        "shutterspeed": shutter_values,
        "f-number": fstop_values,
    }

    all_results: Dict[str, List[ProbeResult]] = {}
    for key in targets:
        if key not in key_values:
            print(f"Skipping unknown target: {key}")
            continue
        all_results[key] = run_probe(
            key=key,
            values=key_values[key],
            timeout=int(args.timeout),
            settle_s=max(0.0, float(args.settle_seconds)),
            delay_s=max(0.0, float(args.delay_seconds)),
            show_choices=bool(args.show_choices),
        )

    failures = summarize(all_results)
    return 1 if failures > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
