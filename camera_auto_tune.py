#!/usr/bin/env python3
"""
Capture -> analyze -> auto-adjust loop using gphoto2 and camera_ai_analyzer.

Workflow per iteration:
1) Capture and download one frame via gphoto2 --capture-image-and-download --keep
2) Resolve downloaded filename
3) Analyze frame and compute recommended next settings
4) Apply gphoto2 --set-config changes
5) Stop when exposure converges, otherwise repeat
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Tuple

from camera_ai_analyzer import (
    DEFAULT_FSTOP_STOPS,
    DEFAULT_ISO_STOPS,
    DEFAULT_SHUTTER_STOPS,
    AnalysisMetrics,
    clamp,
    nearest_stop,
    move_stop_toward,
    analyze_image,
    format_shutter,
    parse_shutter,
    parse_numeric_list,
    read_capture_settings_from_exif,
    recommend_settings,
)


KEEPER_SEQUENCE_LOCK = threading.Lock()
KEEPER_NEXT_INDEX_BY_DIR: dict[str, int] = {}
KEEPER_NAME_RE = re.compile(r"^timelapse_(\d{6})\.[A-Za-z0-9]+$")


def suppress_macos_camera_daemons() -> None:
    # macOS daemons can preemptively claim the camera USB/PTP interface.
    # Kill them before each gphoto command to improve capture reliability.
    for daemon in ("PTPCamera", "ptpcamerad", "icdd", "mscamerad"):
        subprocess.run(
            ["pkill", "-9", daemon],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(0.15)


def start_macos_daemon_suppressor(interval_s: float = 0.2):
    def _worker():
        while True:
            for daemon in ("PTPCamera", "ptpcamerad", "icdd", "mscamerad"):
                subprocess.run(
                    ["pkill", "-9", daemon],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            time.sleep(max(0.05, float(interval_s)))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def run_gphoto(args: Sequence[str], timeout: int = 45) -> subprocess.CompletedProcess[str]:
    suppress_macos_camera_daemons()
    cmd = ["gphoto2", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        timeout_msg = f"gphoto2 command timed out after {timeout}s: {' '.join(cmd)}"
        stdout_text = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr_text = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        combined_stderr = "\n".join(part for part in [stderr_text, timeout_msg] if part).strip()
        return subprocess.CompletedProcess(cmd, returncode=124, stdout=stdout_text, stderr=combined_stderr)


def parse_downloaded_filename(output_text: str) -> Optional[Path]:
    # gphoto2 typically emits lines like:
    # "Saving file as capt_MJB09396.JPG"
    match = re.search(r"Saving file as\s+(.+)$", output_text, flags=re.MULTILINE)
    if not match:
        return None
    raw_name = match.group(1).strip().strip('"')
    return Path(raw_name)


def newest_image_in_dir(workdir: Path) -> Optional[Path]:
    exts = {".jpg", ".jpeg", ".png", ".arw", ".cr2", ".cr3", ".nef", ".dng"}
    candidates = [p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def apply_setting(name: str, value: str, retries: int = 2, retry_seconds: float = 1.0) -> Tuple[bool, str]:
    attempt = 0
    max_attempts = max(1, int(retries) + 1)
    last_message = ""

    while attempt < max_attempts:
        attempt += 1
        result = run_gphoto(["--set-config", f"{name}={value}"], timeout=20)
        combined = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0:
            return True, combined

        last_message = combined
        transient = is_transient_capture_error(combined) or ("could not claim the usb device" in combined.lower())
        if transient and attempt < max_attempts:
            time.sleep(max(0.0, float(retry_seconds)))
            continue

        break

    return False, last_message


def read_current_fstop() -> Tuple[Optional[float], str]:
    result = run_gphoto(["--get-config", "f-number"], timeout=20)
    combined = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        return None, combined

    match = re.search(r"^Current:\s*(?:f/)?([0-9]+(?:\.[0-9]+)?)\s*$", result.stdout, flags=re.MULTILINE)
    if not match:
        match = re.search(r"Current:\s*(?:f/)?([0-9]+(?:\.[0-9]+)?)", combined)
    if not match:
        return None, combined

    try:
        return float(match.group(1)), combined
    except Exception:
        return None, combined


def apply_fstop_with_backcheck(
    previous_fstop: float,
    requested_fstop: float,
    fstop_stops: Sequence[float],
) -> Tuple[bool, float, str]:
    ok, msg = apply_setting("f-number", f"{requested_fstop}")
    if not ok:
        return False, requested_fstop, msg

    actual_fstop, read_msg = read_current_fstop()
    if actual_fstop is None:
        return True, requested_fstop, ""

    # Brightening means moving to a smaller f-number (e.g. 16 -> 14 -> 13).
    if requested_fstop < previous_fstop and actual_fstop < (requested_fstop - 1e-6):
        retry_ok, retry_msg = apply_setting("f-number", f"{requested_fstop}")
        if not retry_ok:
            combined_error = "\n".join(part for part in [msg, read_msg, retry_msg] if part).strip()
            return False, actual_fstop, combined_error

        retried_actual_fstop, retried_read_msg = read_current_fstop()
        if retried_actual_fstop is not None:
            actual_fstop = retried_actual_fstop
            read_msg = retried_read_msg

        if actual_fstop < (requested_fstop - 1e-6):
            note = (
                f"Requested f/{requested_fstop} but camera remained at f/{actual_fstop}; "
                "intermediate stop appears unavailable."
            )
            nearest_known = min(fstop_stops, key=lambda v: abs(float(v) - actual_fstop))
            return True, float(nearest_known), note

        note = f"Camera initially skipped past f/{requested_fstop}, retry succeeded at f/{actual_fstop}."
        nearest_known = min(fstop_stops, key=lambda v: abs(float(v) - actual_fstop))
        return True, float(nearest_known), note

    nearest_known = min(fstop_stops, key=lambda v: abs(float(v) - actual_fstop))
    return True, float(nearest_known), ""


def converged(metrics: AnalysisMetrics, deadband_ev: float) -> bool:
    return (
        abs(metrics.delta_ev) <= deadband_ev
        and metrics.highlight_clip_pct <= 1.0
        and metrics.shadow_clip_pct <= 2.0
    )


def dashboard_recommends_optimized(metrics: AnalysisMetrics, deadband_ev: float) -> bool:
    # Keep this aligned with the dashboard recommendation logic.
    highlight_darken_pct = 2.5
    shadow_brighten_pct = 2.0

    if metrics.delta_ev > deadband_ev:
        return metrics.highlight_clip_pct >= highlight_darken_pct
    if metrics.delta_ev < -deadband_ev:
        return False
    if metrics.highlight_clip_pct >= highlight_darken_pct:
        return False
    if metrics.shadow_clip_pct >= shadow_brighten_pct:
        return False
    return True


DAY_APERTURE_BIAS_TARGET_FSTOP = 5.6
DAY_APERTURE_BIAS_FAST_SHUTTER_S = 1.0 / 1000.0


def next_higher_fstop(current_fstop: float, fstop_stops: Sequence[float]) -> Optional[float]:
    ordered = sorted({float(v) for v in fstop_stops})
    for value in ordered:
        if value > current_fstop * (1.0 + 1e-9):
            return value
    return None


def should_apply_day_aperture_bias(
    *,
    startup_tune_pending: bool,
    effective_lock_aperture: bool,
    current_iso: int,
    iso_min: int,
    current_shutter_s: float,
    current_fstop: float,
) -> bool:
    if startup_tune_pending or effective_lock_aperture:
        return False
    if current_iso > iso_min:
        return False
    if current_shutter_s > DAY_APERTURE_BIAS_FAST_SHUTTER_S:
        return False
    return current_fstop < (DAY_APERTURE_BIAS_TARGET_FSTOP - 1e-6)


def append_reject_log(
    log_path: Path,
    image_name: str,
    metrics: AnalysisMetrics,
    current_iso: int,
    current_shutter_s: float,
    current_fstop: float,
    note: str,
) -> None:
    is_new = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        if is_new:
            writer.writerow(
                [
                    "timestamp",
                    "image",
                    "iso",
                    "shutter",
                    "fstop",
                    "delta_ev",
                    "median_luma",
                    "highlight_clip_pct",
                    "shadow_clip_pct",
                    "blur_score",
                    "note",
                ]
            )
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                image_name,
                current_iso,
                format_shutter(current_shutter_s),
                current_fstop,
                f"{metrics.delta_ev:.4f}",
                f"{metrics.median_luma:.4f}",
                f"{metrics.highlight_clip_pct:.4f}",
                f"{metrics.shadow_clip_pct:.4f}",
                f"{metrics.blur_score:.4f}",
                note,
            ]
        )


def copy_keeper_image(image_path: Path, keep_dir: Path) -> Path:
    keep_dir.mkdir(parents=True, exist_ok=True)

    suffix = image_path.suffix if image_path.suffix else ".jpg"
    key = str(keep_dir.resolve())

    with KEEPER_SEQUENCE_LOCK:
        next_index = KEEPER_NEXT_INDEX_BY_DIR.get(key)
        if next_index is None:
            max_seen = 0
            for entry in keep_dir.iterdir():
                if not entry.is_file():
                    continue
                match = KEEPER_NAME_RE.match(entry.name)
                if not match:
                    continue
                try:
                    value = int(match.group(1))
                except Exception:
                    continue
                if value > max_seen:
                    max_seen = value
            next_index = max_seen + 1

        dst = keep_dir / f"timelapse_{next_index:06d}{suffix}"
        while dst.exists():
            next_index += 1
            dst = keep_dir / f"timelapse_{next_index:06d}{suffix}"

        KEEPER_NEXT_INDEX_BY_DIR[key] = next_index + 1

    shutil.copy2(image_path, dst)
    return dst


def is_transient_capture_error(output_text: str) -> bool:
    text = (output_text or "").lower()
    markers = [
        "ptp timeout",
        "timeout reading from or writing to the port",
        "could not capture image",
        "could not capture",
        "io-library",
    ]
    return any(marker in text for marker in markers)


def next_stop_value(current: float, stops: Sequence[float], brighten: bool) -> Optional[float]:
    ordered = sorted({float(v) for v in stops})
    if brighten:
        for value in ordered:
            if value > current * (1.0 + 1e-9):
                return value
        return None
    for value in reversed(ordered):
        if value < current * (1.0 - 1e-9):
            return value
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto-tune camera exposure using iterative captures.")
    parser.add_argument("--workdir", default=".", help="Directory where captures are downloaded.")
    parser.add_argument("--max-iterations", type=int, default=6, help="Max capture/adjust iterations.")
    parser.add_argument(
        "--keep-on-camera",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Keep each captured image on camera storage after download.",
    )
    parser.add_argument(
        "--capture-timeout",
        type=int,
        default=90,
        help="Timeout in seconds for each gphoto2 capture/download command.",
    )
    parser.add_argument("--duration-minutes", type=float, default=None, help="Optional run duration in minutes.")
    parser.add_argument("--interval-seconds", type=int, default=0, help="Seconds between captures (0 = no enforced interval).")
    parser.add_argument(
        "--startup-tune-shot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use the first timed capture as a calibration frame and do not save it to keep-dir.",
    )
    parser.add_argument(
        "--startup-max-iterations",
        type=int,
        default=0,
        help="Maximum fast calibration attempts at launch before switching cadence (0 = unlimited until calibrated).",
    )
    parser.add_argument(
        "--startup-max-step-ev",
        type=float,
        default=1.0,
        help="Max EV step during startup calibration mode for faster convergence.",
    )
    parser.add_argument(
        "--startup-priority",
        choices=["low-iso", "fast-brighten"],
        default="low-iso",
        help=(
            "Startup tuning priority: 'low-iso' keeps ISO as low as possible and adjusts shutter first; "
            "'fast-brighten' raises ISO first to converge faster in very dark scenes."
        ),
    )
    parser.add_argument(
        "--startup-retry-seconds",
        type=float,
        default=5.0,
        help="Seconds between startup calibration attempts when frame is not yet optimal.",
    )
    parser.add_argument(
        "--capture-retry-seconds",
        type=float,
        default=5.0,
        help="Seconds to wait before retrying after a transient capture transport error.",
    )
    parser.add_argument(
        "--max-consecutive-capture-failures",
        type=int,
        default=6,
        help="Abort after this many consecutive transient capture failures.",
    )
    parser.add_argument(
        "--reject-log",
        default="reject_candidates.csv",
        help="CSV file to log frames that are not yet optimal.",
    )
    parser.add_argument(
        "--keep-dir",
        default="timelapse",
        help="Directory where optimal keeper frames are copied.",
    )
    parser.add_argument(
        "--keep-mode",
        choices=["optimal", "all"],
        default="optimal",
        help="Frame copy policy for keep-dir: 'optimal' copies only converged frames, 'all' copies every frame.",
    )
    parser.add_argument(
        "--stop-on-optimal",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Exit immediately once an optimal frame is reached.",
    )

    parser.add_argument("--target-luma", type=float, default=118.0)
    parser.add_argument("--deadband-ev", type=float, default=0.15)
    parser.add_argument(
        "--startup-deadband-ev",
        type=float,
        default=0.20,
        help="Wider EV deadband used only during startup calibration to avoid oscillation.",
    )
    parser.add_argument("--max-step-ev", type=float, default=1.0)
    parser.add_argument(
        "--aggressive-breakout-ev",
        type=float,
        default=1.0,
        help="If |deltaEV| exceeds this, use aggressive EV step for faster convergence.",
    )
    parser.add_argument(
        "--aggressive-max-step-ev",
        type=float,
        default=1.0,
        help="Max EV step while in aggressive convergence mode.",
    )
    parser.add_argument(
        "--settled-max-step-ev",
        type=float,
        default=0.125,
        help="Max EV step after calibration has settled; lower values reduce visible brightness jumps.",
    )
    parser.add_argument(
        "--settled-breakout-ev",
        type=float,
        default=0.5,
        help=(
            "If |deltaEV| exceeds this in settled mode, temporarily bypass smoothing "
            "and use normal max-step-ev to recover from rapid light changes."
        ),
    )
    parser.add_argument("--blur-min", type=float, default=120.0)
    parser.add_argument(
        "--iso-strategy",
        choices=["last", "first-when-blur-low"],
        default="last",
        help="ISO adjustment strategy. 'last' minimizes ISO changes for timelapse.",
    )

    parser.add_argument("--shutter-min", default="1/1000")
    parser.add_argument("--shutter-max", default="1/30")
    parser.add_argument("--iso-min", type=int, default=100)
    parser.add_argument("--iso-max", type=int, default=51200)
    parser.add_argument("--lock-aperture", action="store_true", default=False)
    parser.add_argument(
        "--startup-set-baseline",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Set startup baseline camera values before first capture (lowest ISO and max f-stop by default).",
    )
    parser.add_argument(
        "--startup-iso",
        type=int,
        default=None,
        help="Optional startup ISO override. Defaults to --iso-min.",
    )
    parser.add_argument(
        "--startup-fstop",
        type=float,
        default=None,
        help="Optional startup f-stop override. Defaults to maximum value in --fstop-stops.",
    )
    parser.add_argument(
        "--startup-shutter",
        default=None,
        help="Optional startup shutter override (e.g. 1/30).",
    )

    parser.add_argument("--iso-stops", default=None)
    parser.add_argument("--shutter-stops", default=None)
    parser.add_argument("--fstop-stops", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    # Keep macOS camera daemons from grabbing the USB interface during long runs.
    start_macos_daemon_suppressor(interval_s=0.2)

    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.exists() or not workdir.is_dir():
        print(f"error: invalid --workdir: {workdir}", file=sys.stderr)
        return 2

    try:
        shutter_min_s = parse_shutter(args.shutter_min)
        shutter_max_s = parse_shutter(args.shutter_max)
    except Exception as exc:
        print(f"error: invalid shutter bounds: {exc}", file=sys.stderr)
        return 2

    if shutter_min_s > shutter_max_s:
        print("error: --shutter-min must be faster/smaller than --shutter-max", file=sys.stderr)
        return 2

    iso_stops_override = parse_numeric_list(args.iso_stops, as_int=True)
    shutter_stops_override = parse_numeric_list(args.shutter_stops, as_int=False)
    fstop_stops_override = parse_numeric_list(args.fstop_stops, as_int=False)

    iso_stops = iso_stops_override if iso_stops_override else [float(v) for v in DEFAULT_ISO_STOPS]
    shutter_stops = shutter_stops_override if shutter_stops_override else list(DEFAULT_SHUTTER_STOPS)
    fstop_stops = fstop_stops_override if fstop_stops_override else list(DEFAULT_FSTOP_STOPS)

    if args.startup_set_baseline:
        startup_iso = args.startup_iso if args.startup_iso is not None else int(args.iso_min)
        startup_fstop = args.startup_fstop if args.startup_fstop is not None else float(max(fstop_stops))
        startup_shutter = args.startup_shutter

        print(f"Applying startup baseline ISO: {startup_iso}")
        ok, msg = apply_setting("iso", str(startup_iso))
        print("Set startup ISO:", "ok" if ok else "failed")
        if not ok and msg:
            print(msg)

        print(f"Applying startup baseline f-stop: f/{startup_fstop}")
        ok, msg = apply_setting("f-number", f"{startup_fstop}")
        print("Set startup f-stop:", "ok" if ok else "failed")
        if not ok and msg:
            print(msg)

        if startup_shutter:
            print(f"Applying startup baseline shutter: {startup_shutter}")
            ok, msg = apply_setting("shutterspeed", str(startup_shutter))
            print("Set startup shutter:", "ok" if ok else "failed")
            if not ok and msg:
                print(msg)

    last_iso: Optional[int] = None
    last_shutter_s: Optional[float] = None
    last_fstop: Optional[float] = None
    # Oscillation detection: track last N (iso, shutter, fstop) tuples applied.
    settings_history: list = []
    OSCILLATION_WINDOW = 4
    reject_log_path = (workdir / args.reject_log).resolve()
    keep_dir_path = (workdir / args.keep_dir).resolve()

    calibration_only_mode = args.duration_minutes is not None and args.duration_minutes <= 0
    timed_mode = args.duration_minutes is not None and args.duration_minutes > 0
    if timed_mode:
        end_time = time.time() + (args.duration_minutes * 60.0)
    else:
        end_time = None
    startup_tune_pending = calibration_only_mode or (timed_mode and bool(args.startup_tune_shot))
    startup_attempts = 0
    consecutive_capture_failures = 0
    settled_mode = False
    severe_highlight_clip_pct = 6.0

    next_capture_at = time.time()
    iteration = 0
    first_capture_pending = True

    print("Starting auto-tune loop...")
    while True:
        if end_time is not None and time.time() >= end_time:
            print("Run duration reached. Stopping.")
            break

        if not timed_mode and not calibration_only_mode and iteration >= args.max_iterations:
            print("Reached max iterations. More adjustment may still be needed.")
            break

        now = time.time()
        if not first_capture_pending and now < next_capture_at:
            wait_s = next_capture_at - now
            print(f"Waiting {wait_s:.1f}s before next capture...")
            time.sleep(wait_s)

        iteration += 1
        if timed_mode:
            print(f"\nIteration {iteration} (timed mode)")
        else:
            print(f"\nIteration {iteration}/{args.max_iterations}")

        before_capture_files = {p.name for p in workdir.iterdir() if p.is_file()}
        print("Starting capture...")
        capture_args = ["--capture-image-and-download"]
        if args.keep_on_camera:
            capture_args.append("--keep")
        cap = run_gphoto(capture_args, timeout=args.capture_timeout)
        first_capture_pending = False
        cap_output = (cap.stdout + "\n" + cap.stderr).strip()
        if cap.returncode != 0:
            print("Capture failed:")
            print(cap_output)
            if is_transient_capture_error(cap_output):
                consecutive_capture_failures += 1
                if args.max_consecutive_capture_failures > 0 and consecutive_capture_failures >= args.max_consecutive_capture_failures:
                    print(
                        "error: too many consecutive transient capture failures; aborting.",
                        file=sys.stderr,
                    )
                    return 1
                wait_s = max(0.0, float(args.capture_retry_seconds))
                next_capture_at = time.time() + wait_s
                print(f"Transient camera transport error. Retrying capture in {wait_s:.1f}s...")
                continue
            return 1

        consecutive_capture_failures = 0

        image_path = parse_downloaded_filename(cap_output)
        if image_path is None:
            after_capture_files = [p for p in workdir.iterdir() if p.is_file()]
            new_candidates = [
                p for p in after_capture_files
                if p.name not in before_capture_files
                and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".arw", ".cr2", ".cr3", ".nef", ".dng"}
            ]
            if new_candidates:
                image_path = max(new_candidates, key=lambda p: p.stat().st_mtime)
            else:
                image_path = newest_image_in_dir(workdir)
        elif not image_path.is_absolute():
            image_path = workdir / image_path

        if image_path is None or not image_path.exists():
            print("error: could not resolve downloaded filename", file=sys.stderr)
            print("gphoto2 output:")
            print(cap_output)
            if is_transient_capture_error(cap_output):
                consecutive_capture_failures += 1
                if args.max_consecutive_capture_failures > 0 and consecutive_capture_failures >= args.max_consecutive_capture_failures:
                    print(
                        "error: too many consecutive transient capture failures; aborting.",
                        file=sys.stderr,
                    )
                    return 1
                wait_s = max(0.0, float(args.capture_retry_seconds))
                next_capture_at = time.time() + wait_s
                print(f"Transient capture/transfer error. Retrying in {wait_s:.1f}s...")
                continue
            return 1

        print(f"Captured: {image_path.name}")

        suppress_keeper_this_frame = startup_tune_pending and timed_mode
        if startup_tune_pending:
            startup_attempts += 1
        copied_this_iteration = False
        if args.keep_mode == "all" and not suppress_keeper_this_frame:
            copied_path = copy_keeper_image(image_path, keep_dir_path)
            copied_this_iteration = True
            print(f"Keeper saved: {copied_path.name}")

        metrics = analyze_image(image_path=image_path, target_luma=args.target_luma)
        exif_iso, exif_shutter_s, exif_fstop = read_capture_settings_from_exif(image_path)

        current_iso = exif_iso if exif_iso is not None else last_iso
        current_shutter_s = exif_shutter_s if exif_shutter_s is not None else last_shutter_s
        current_fstop = exif_fstop if exif_fstop is not None else last_fstop

        if current_iso is None or current_shutter_s is None:
            print("error: missing ISO or shutter metadata and no previous value to fall back to", file=sys.stderr)
            return 1

        effective_lock_aperture = bool(args.lock_aperture)
        if current_fstop is None:
            current_fstop = 2.8
            effective_lock_aperture = True

        print(
            "Current settings: "
            f"ISO={current_iso}, shutter={format_shutter(current_shutter_s)}, f/{current_fstop}"
        )
        print(
            "Metrics: "
            f"deltaEV={metrics.delta_ev:.3f}, "
            f"median={metrics.median_luma:.1f}, "
            f"high_clip={metrics.highlight_clip_pct:.3f}%, "
            f"shadow_clip={metrics.shadow_clip_pct:.3f}%"
        )

        if suppress_keeper_this_frame:
            print("Startup tune shot: using this frame to calibrate; not saving to keep-dir.")

        active_deadband_ev = args.startup_deadband_ev if startup_tune_pending else args.deadband_ev
        if startup_tune_pending:
            # Startup should settle quickly to usable exposure and then switch to cadence.
            # Strict clip constraints can keep startup stuck in calibration indefinitely.
            optimal_now = abs(metrics.delta_ev) <= active_deadband_ev
        else:
            optimal_now = converged(metrics, active_deadband_ev)

        if args.stop_on_optimal and dashboard_recommends_optimized(metrics, active_deadband_ev):
            if not copied_this_iteration:
                copied_path = copy_keeper_image(image_path, keep_dir_path)
                copied_this_iteration = True
                print(f"Keeper saved: {copied_path.name}")
            print("Dashboard recommendation is Optimized. Stopping by --stop-on-optimal.")
            return 0

        if startup_tune_pending and optimal_now:
            if calibration_only_mode or args.stop_on_optimal:
                startup_tune_pending = False
                if not copied_this_iteration:
                    copied_path = copy_keeper_image(image_path, keep_dir_path)
                    print(f"Keeper saved: {copied_path.name}")
                if args.stop_on_optimal and not calibration_only_mode:
                    print("Optimal frame reached. Stopping by --stop-on-optimal.")
                else:
                    print("Calibration complete.")
                return 0
            startup_tune_pending = False
            print(
                "Startup calibration complete (luma settled). Taking first keeper shot now, "
                "then switching to normal timed capture cadence."
            )
            continue

        if not optimal_now:
            append_reject_log(
                log_path=reject_log_path,
                image_name=image_path.name,
                metrics=metrics,
                current_iso=current_iso,
                current_shutter_s=current_shutter_s,
                current_fstop=current_fstop,
                note="not_optimal",
            )
            print(f"Logged non-optimal frame to {reject_log_path.name}")

        if optimal_now and not timed_mode:
            if not copied_this_iteration:
                copied_path = copy_keeper_image(image_path, keep_dir_path)
                print(f"Keeper saved: {copied_path.name}")
            print("Adjusted, all set.")
            return 0
        day_aperture_bias_pending = False
        if optimal_now and timed_mode:
            day_aperture_bias_pending = should_apply_day_aperture_bias(
                startup_tune_pending=startup_tune_pending,
                effective_lock_aperture=effective_lock_aperture,
                current_iso=int(current_iso),
                iso_min=int(args.iso_min),
                current_shutter_s=float(current_shutter_s),
                current_fstop=float(current_fstop),
            )
            if not copied_this_iteration and not suppress_keeper_this_frame:
                copied_path = copy_keeper_image(image_path, keep_dir_path)
                print(f"Keeper saved: {copied_path.name}")
                settled_mode = True
            if args.stop_on_optimal:
                if suppress_keeper_this_frame and not copied_this_iteration:
                    copied_path = copy_keeper_image(image_path, keep_dir_path)
                    print(f"Keeper saved: {copied_path.name}")
                print("Optimal frame reached. Stopping by --stop-on-optimal.")
                return 0
            if args.interval_seconds > 0:
                next_capture_at = time.time() + args.interval_seconds
            print("Frame is optimal. Continuing timed capture loop.")
            if day_aperture_bias_pending:
                print(
                    "Day aperture bias: rebalancing exposure toward higher f-stop "
                    "for deeper depth of field."
                )

        if (timed_mode or calibration_only_mode) and not optimal_now:
            if startup_tune_pending:
                wait_s = max(0.0, float(args.startup_retry_seconds))
                next_capture_at = time.time() + wait_s
                print(f"Startup calibration in progress. Retrying in {wait_s:.1f}s.")
            elif (
                settled_mode
                and abs(metrics.delta_ev) <= args.settled_breakout_ev
                and args.interval_seconds > 0
            ):
                next_capture_at = time.time() + args.interval_seconds
                print(
                    "Post-settle smoothing active. Keeping cadence and applying small exposure nudges "
                    "on the next timed frame."
                )
            elif args.interval_seconds > 0:
                print("Frame is not optimal. Retrying immediately until a keeper is found.")

        if optimal_now and timed_mode and not day_aperture_bias_pending:
            continue

        if startup_tune_pending:
            active_max_step_ev = args.startup_max_step_ev
        elif abs(metrics.delta_ev) >= args.aggressive_breakout_ev:
            # When far off target, take bigger jumps (2-3 stop style moves)
            # and then naturally fall back to smoother behavior near target.
            active_max_step_ev = max(args.max_step_ev, args.aggressive_max_step_ev)
        elif settled_mode and abs(metrics.delta_ev) <= args.settled_breakout_ev:
            active_max_step_ev = min(args.max_step_ev, args.settled_max_step_ev)
        else:
            active_max_step_ev = args.max_step_ev

        # When startup frames are near-black, allow larger aperture jumps so we
        # reach usable exposure quickly instead of walking one f-stop at a time.
        if startup_tune_pending:
            if metrics.median_luma <= 2.0 or metrics.delta_ev >= 4.0:
                startup_aperture_step_budget = 4
            elif metrics.delta_ev >= 2.0:
                startup_aperture_step_budget = 3
            else:
                startup_aperture_step_budget = 2
        else:
            startup_aperture_step_budget = 1

        rec = recommend_settings(
            metrics=metrics,
            iso=current_iso,
            shutter_s=current_shutter_s,
            fstop=current_fstop,
            iso_stops=iso_stops,
            shutter_stops=shutter_stops,
            fstop_stops=fstop_stops,
            shutter_min_s=shutter_min_s,
            shutter_max_s=shutter_max_s,
            iso_min=args.iso_min,
            iso_max=args.iso_max,
            deadband_ev=active_deadband_ev,
            max_step_ev=active_max_step_ev,
            lock_aperture=effective_lock_aperture,
            # Disable blur-based shutter speed-up during startup to prevent oscillation.
            blur_min=(-1.0 if startup_tune_pending else args.blur_min),
            prefer_iso_first_when_blur_low=(
                (startup_tune_pending and args.startup_priority == "fast-brighten")
                or (args.iso_strategy == "first-when-blur-low")
            ),
            force_iso_first_when_brightening=(
                startup_tune_pending and args.startup_priority == "fast-brighten"
            ),
            force_aperture_first_when_brightening=False,
            prefer_aperture_last=False,
            max_aperture_stop_steps=startup_aperture_step_budget,
        )

        if day_aperture_bias_pending:
            target_fstop = float(DAY_APERTURE_BIAS_TARGET_FSTOP)
            next_f = next_higher_fstop(float(current_fstop), fstop_stops)
            if next_f is not None:
                if next_f > target_fstop:
                    next_f = target_fstop
                nearest_target = min(fstop_stops, key=lambda v: abs(float(v) - next_f))
                next_f = float(nearest_target)

                if next_f > float(current_fstop) and not effective_lock_aperture:
                    aperture_ev = -2.0 * math.log2(max(next_f, 1e-9) / max(float(current_fstop), 1e-9))
                    compensating_shutter = float(current_shutter_s) * (2.0 ** (-aperture_ev))
                    compensating_shutter = clamp(compensating_shutter, shutter_min_s, shutter_max_s)
                    compensating_shutter = nearest_stop(compensating_shutter, shutter_stops)

                    rec.action = "day_aperture_bias"
                    rec.reason = "daylight depth-of-field bias"
                    rec.suggested_iso = int(current_iso)
                    rec.suggested_fstop = float(next_f)
                    rec.suggested_shutter_s = float(compensating_shutter)
                    rec.applied_ev_step = 0.0

        print(
            "Recommend: "
            f"action={rec.action}, "
            f"ISO={rec.suggested_iso}, "
            f"shutter={format_shutter(rec.suggested_shutter_s)}, "
            f"f/{rec.suggested_fstop}, "
            f"ev_step={rec.applied_ev_step:.3f}"
        )

        if startup_tune_pending and not optimal_now and abs(rec.applied_ev_step) < 1e-6:
            bounded_shutter_stops = [s for s in shutter_stops if shutter_min_s <= s <= shutter_max_s]
            if metrics.delta_ev > active_deadband_ev:
                if metrics.highlight_clip_pct >= severe_highlight_clip_pct:
                    print(
                        "Startup nudge skipped: brighten request conflicts with severe highlight clipping."
                    )
                else:
                    nudged_shutter = next_stop_value(current_shutter_s, bounded_shutter_stops, brighten=True)
                    if nudged_shutter is not None:
                        rec.suggested_shutter_s = nudged_shutter
                        print(
                            "Startup nudge: forcing brighter shutter stop "
                            f"{format_shutter(nudged_shutter)}"
                        )
                    else:
                        if args.startup_priority == "low-iso" and not effective_lock_aperture:
                            current_f = float(current_fstop)
                            lower_f_candidates = sorted([f for f in fstop_stops if f < current_f])
                            if lower_f_candidates:
                                rec.suggested_fstop = float(lower_f_candidates[-1])
                                print(f"Startup nudge: forcing wider aperture f/{rec.suggested_fstop}")
                        if rec.suggested_fstop == current_fstop:
                            nudged_iso = next_stop_value(float(current_iso), iso_stops, brighten=True)
                            if nudged_iso is not None and nudged_iso <= float(args.iso_max):
                                rec.suggested_iso = int(round(nudged_iso))
                                print(f"Startup nudge: forcing brighter ISO stop {rec.suggested_iso}")
            elif metrics.delta_ev < -active_deadband_ev or (
                metrics.highlight_clip_pct > 1.0 and metrics.delta_ev <= 0.0
            ):
                nudged_shutter = next_stop_value(current_shutter_s, bounded_shutter_stops, brighten=False)
                if nudged_shutter is not None:
                    rec.suggested_shutter_s = nudged_shutter
                    print(
                        "Startup nudge: forcing darker shutter stop "
                        f"{format_shutter(nudged_shutter)}"
                    )
                else:
                    nudged_iso = next_stop_value(float(current_iso), iso_stops, brighten=False)
                    if nudged_iso is not None and nudged_iso >= float(args.iso_min):
                        rec.suggested_iso = int(round(nudged_iso))
                        print(f"Startup nudge: forcing darker ISO stop {rec.suggested_iso}")

        changed = False
        if rec.suggested_iso != current_iso:
            print(f"Applying: gphoto2 --set-config iso={rec.suggested_iso}")
            ok, msg = apply_setting("iso", str(rec.suggested_iso))
            print("Set ISO:", "ok" if ok else "failed")
            if not ok:
                print(msg)
                return 1
            changed = True

        shutter_text = format_shutter(rec.suggested_shutter_s)
        current_shutter_text = format_shutter(current_shutter_s)
        if shutter_text != current_shutter_text:
            print(f"Applying: gphoto2 --set-config shutterspeed={shutter_text}")
            ok, msg = apply_setting("shutterspeed", shutter_text)
            print("Set shutter:", "ok" if ok else "failed")
            if not ok:
                print(msg)
                return 1
            changed = True

        if not effective_lock_aperture and abs(rec.suggested_fstop - current_fstop) > 1e-6:
            print(f"Applying: gphoto2 --set-config f-number={rec.suggested_fstop}")
            ok, applied_fstop, msg = apply_fstop_with_backcheck(
                previous_fstop=float(current_fstop),
                requested_fstop=float(rec.suggested_fstop),
                fstop_stops=fstop_stops,
            )
            print("Set f-stop:", "ok" if ok else "failed")
            if not ok:
                print(msg)
                return 1
            rec.suggested_fstop = float(applied_fstop)
            if msg:
                print(msg)
            changed = True

        if not changed:
            print("No setting changes needed.")
            if startup_tune_pending and not optimal_now:
                print("Startup calibration stalled at stop boundary. Forcing one-stop adjustment.")
                forced_change = False
                bounded_shutter_stops = [s for s in shutter_stops if shutter_min_s <= s <= shutter_max_s]

                if metrics.delta_ev > args.deadband_ev and metrics.highlight_clip_pct < severe_highlight_clip_pct:
                    forced_shutter = next_stop_value(current_shutter_s, bounded_shutter_stops, brighten=True)
                    if forced_shutter is not None:
                        forced_shutter_text = format_shutter(forced_shutter)
                        print(f"Applying: gphoto2 --set-config shutterspeed={forced_shutter_text}")
                        ok, msg = apply_setting("shutterspeed", forced_shutter_text)
                        print("Set shutter:", "ok" if ok else "failed")
                        if not ok:
                            print(msg)
                            return 1
                        rec.suggested_shutter_s = forced_shutter
                        forced_change = True
                    else:
                        if args.startup_priority == "low-iso" and not effective_lock_aperture:
                            current_f = float(current_fstop)
                            lower_f_candidates = sorted([f for f in fstop_stops if f < current_f])
                            if lower_f_candidates:
                                forced_f = float(lower_f_candidates[-1])
                                print(f"Applying: gphoto2 --set-config f-number={forced_f}")
                                ok, applied_fstop, msg = apply_fstop_with_backcheck(
                                    previous_fstop=float(current_fstop),
                                    requested_fstop=float(forced_f),
                                    fstop_stops=fstop_stops,
                                )
                                print("Set f-stop:", "ok" if ok else "failed")
                                if not ok:
                                    print(msg)
                                    return 1
                                rec.suggested_fstop = float(applied_fstop)
                                if msg:
                                    print(msg)
                                forced_change = True
                        if not forced_change:
                            forced_iso = next_stop_value(float(current_iso), iso_stops, brighten=True)
                            if forced_iso is not None and forced_iso <= float(args.iso_max):
                                forced_iso_i = int(round(forced_iso))
                                print(f"Applying: gphoto2 --set-config iso={forced_iso_i}")
                                ok, msg = apply_setting("iso", str(forced_iso_i))
                                print("Set ISO:", "ok" if ok else "failed")
                                if not ok:
                                    print(msg)
                                    return 1
                                rec.suggested_iso = forced_iso_i
                                forced_change = True
                else:
                    forced_shutter = next_stop_value(current_shutter_s, bounded_shutter_stops, brighten=False)
                    if forced_shutter is not None:
                        forced_shutter_text = format_shutter(forced_shutter)
                        print(f"Applying: gphoto2 --set-config shutterspeed={forced_shutter_text}")
                        ok, msg = apply_setting("shutterspeed", forced_shutter_text)
                        print("Set shutter:", "ok" if ok else "failed")
                        if not ok:
                            print(msg)
                            return 1
                        rec.suggested_shutter_s = forced_shutter
                        forced_change = True
                    else:
                        forced_iso = next_stop_value(float(current_iso), iso_stops, brighten=False)
                        if forced_iso is not None and forced_iso >= float(args.iso_min):
                            forced_iso_i = int(round(forced_iso))
                            print(f"Applying: gphoto2 --set-config iso={forced_iso_i}")
                            ok, msg = apply_setting("iso", str(forced_iso_i))
                            print("Set ISO:", "ok" if ok else "failed")
                            if not ok:
                                print(msg)
                                return 1
                            rec.suggested_iso = forced_iso_i
                            forced_change = True

                if forced_change:
                    changed = True

            if not optimal_now and not changed and abs(rec.applied_ev_step) < 1e-6:
                if startup_tune_pending:
                    startup_tune_pending = False
                    print(
                        "Startup calibration is bounded by camera limits. "
                        "Switching to normal timed capture cadence."
                    )
                    if args.interval_seconds > 0:
                        next_capture_at = time.time() + args.interval_seconds
                    continue

            if timed_mode and not startup_tune_pending and not optimal_now and not changed and abs(rec.applied_ev_step) < 1e-6:
                if not copied_this_iteration:
                    copied_path = copy_keeper_image(image_path, keep_dir_path)
                    print(f"Keeper saved (bounded exposure): {copied_path.name}")
                    settled_mode = True
                if args.interval_seconds > 0:
                    next_capture_at = time.time() + args.interval_seconds
                print(
                    "Exposure is bounded by camera limits; accepting best-possible frame "
                    "and continuing normal timed cadence."
                )
                continue

            if not timed_mode:
                print("Adjusted, all set.")
                return 0

        last_iso = rec.suggested_iso
        last_shutter_s = rec.suggested_shutter_s
        last_fstop = rec.suggested_fstop

        # Oscillation detection: if we keep alternating between the same two
        # setting combinations, force an aperture step to break the loop.
        settings_history.append((rec.suggested_iso, rec.suggested_shutter_s, rec.suggested_fstop))
        if len(settings_history) > OSCILLATION_WINDOW:
            settings_history.pop(0)
        if len(settings_history) >= OSCILLATION_WINDOW:
            unique_states = set(settings_history)
            if len(unique_states) <= 2 and not effective_lock_aperture:
                print(
                    "Oscillation detected: stuck between the same settings. "
                    "Forcing one f-stop step to break out."
                )
                # Step aperture one stop in the direction that matches the
                # current recommendation (darken → higher f-stop, else lower).
                if rec.action in ('darken', 'speed_up_shutter'):
                    # Higher f-number = darker (smaller aperture)
                    f_candidate = next_stop_value(rec.suggested_fstop, fstop_stops, brighten=False)
                else:
                    # Lower f-number = brighter (wider aperture)
                    f_candidate = next_stop_value(rec.suggested_fstop, fstop_stops, brighten=True)
                if f_candidate is not None and f_candidate != rec.suggested_fstop:
                    print(f"Oscillation break: f/{rec.suggested_fstop} -> f/{f_candidate}")
                    ok, msg = apply_setting('f-number', str(f_candidate))
                    if ok:
                        last_fstop = f_candidate
                    settings_history.clear()

        if startup_tune_pending and args.startup_max_iterations > 0 and startup_attempts >= args.startup_max_iterations:
            startup_tune_pending = False
            print(
                "Startup calibration attempt limit reached. "
                "Taking first keeper shot now, then switching to normal timed capture cadence."
            )

    print("Auto-tune run complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
