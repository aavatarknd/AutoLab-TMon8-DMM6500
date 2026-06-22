#!/usr/bin/env python3
"""
Headless temperature checkpoint sweep for PHOTEC TMon8 + Keithley DMM6500.

Tested protocol from this lab setup:
- TMon8 read temperature channel 1: b"KRDG\xA3\xBF1"
- TMon8 read setpoint loop 1:    b"SETP\xA3\xBF1"
- TMon8 write setpoint loop 1:   b"SETP1,<temp_K>\r\n"
- DMM6500 voltage read:          :READ?

Input checkpoint file:
- CSV recommended. Any numeric cells are treated as temperature checkpoints in K.
- Examples:
    298,299,300,301
  or
    checkpoint_k
    298
    299
    300

Outputs:
- *_samples_long.csv: every voltage/temperature sample
- *_summary_long.csv: one row per checkpoint
- *_summary_wide.csv: checkpoints as columns, voltage as one row, plus extra rows
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import pyvisa
from pyvisa.errors import VisaIOError

T_MON8_Q = b"\xA3\xBF"  # GBK full-width question mark used by this TMon8 firmware


@dataclass
class SweepConfig:
    checkpoint_file: Path
    out_dir: Path
    tmon8_addr: str
    dmm_addr: str
    temp_channel: int
    setpoint_loop: int
    temp_tolerance_k: float
    hold_s: float
    max_wait_s: float
    temp_poll_s: float
    slope_window_s: float
    slope_tol_k_per_min: float
    voltage_samples: int
    voltage_interval_s: float
    dmm_nplc: float
    dmm_autorange: bool
    dmm_reset: bool
    min_k: float
    max_k: float
    restore_setpoint: bool
    dry_run: bool


def parse_numeric_cell(value: object) -> List[float]:
    """Extract numeric values from a cell-like object."""
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []

    # Split common separators, while still allowing scientific notation.
    pieces = re.split(r"[,;\t\s]+", text)
    nums: List[float] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        try:
            val = float(piece)
        except ValueError:
            continue
        if math.isfinite(val):
            nums.append(val)
    return nums


def load_checkpoints(path: Path) -> List[float]:
    """Load checkpoints from CSV/TXT, or XLSX if pandas is installed."""
    if not path.exists():
        raise FileNotFoundError(f"Checkpoint file not found: {path}")

    suffix = path.suffix.lower()
    checkpoints: List[float] = []

    if suffix in {".csv", ".txt"}:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                for cell in row:
                    checkpoints.extend(parse_numeric_cell(cell))

    elif suffix in {".xlsx", ".xls"}:
        try:
            import pandas as pd  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Excel input needs pandas. Easiest fix: save the Excel row as CSV, "
                "or install pandas in this venv with: python -m pip install pandas openpyxl"
            ) from exc
        df = pd.read_excel(path, header=None)
        for value in df.to_numpy().ravel():
            checkpoints.extend(parse_numeric_cell(value))

    else:
        raise ValueError(f"Unsupported checkpoint file type: {suffix}. Use .csv first.")

    if not checkpoints:
        raise ValueError("No numeric checkpoints found in input file.")
    return checkpoints


def check_temperature_limits(checkpoints: Sequence[float], min_k: float, max_k: float) -> None:
    bad = [x for x in checkpoints if x < min_k or x > max_k]
    if bad:
        raise ValueError(
            f"Checkpoint(s) outside safety limits [{min_k}, {max_k}] K: {bad}. "
            "Adjust --min-k/--max-k only if you are sure."
        )


def open_tmon8(rm: pyvisa.ResourceManager, addr: str, timeout_ms: int = 2000):
    inst = rm.open_resource(addr)
    inst.baud_rate = 115200
    inst.timeout = timeout_ms
    inst.write_termination = "\r\n"
    inst.read_termination = "\r\n"
    return inst


def open_dmm6500(rm: pyvisa.ResourceManager, addr: str, cfg: SweepConfig):
    inst = rm.open_resource(addr)
    inst.timeout = 10000
    inst.write_termination = "\n"
    inst.read_termination = "\n"

    print(f"DMM6500 IDN: {inst.query('*IDN?').strip()}")
    if cfg.dmm_reset:
        inst.write("*RST")
        time.sleep(0.5)
    inst.write(":SENS:FUNC 'VOLT:DC'")
    if cfg.dmm_autorange:
        inst.write(":SENS:VOLT:DC:RANG:AUTO ON")
    inst.write(f":SENS:VOLT:DC:NPLC {cfg.dmm_nplc}")
    return inst


def tmon8_write_setpoint(inst, loop: int, target_k: float) -> bytes:
    # Working command found experimentally: b"SETP1,297.80\r\n"
    cmd = f"SETP{loop},{target_k:.2f}\r\n".encode("ascii")

    try:
        inst.flush(pyvisa.constants.VI_READ_BUF_DISCARD)
    except Exception:
        pass

    inst.write_raw(cmd)
    return cmd


def tmon8_read_setpoint(inst, loop: int) -> float:
    cmd = b"SETP" + T_MON8_Q + str(loop).encode("ascii")
    inst.write_raw(cmd)
    time.sleep(0.2)
    return float(inst.read_raw().decode("ascii", errors="ignore").strip())


def tmon8_read_temperature(inst, channel: int) -> float:
    cmd = b"KRDG" + T_MON8_Q + str(channel).encode("ascii")
    inst.write_raw(cmd)
    time.sleep(0.1)
    return float(inst.read_raw().decode("ascii", errors="ignore").strip())


def dmm_read_voltage(inst) -> float:
    return float(inst.query(":READ?").strip())


def estimate_slope_k_per_min(history: Sequence[Tuple[float, float]], window_s: float) -> Optional[float]:
    if window_s <= 0 or len(history) < 2:
        return None
    t_last, temp_last = history[-1]
    # Keep points within the window.
    candidates = [(t, temp) for t, temp in history if t_last - t <= window_s]
    if len(candidates) < 2:
        return None
    t0, temp0 = candidates[0]
    dt = t_last - t0
    if dt <= 0:
        return None
    return (temp_last - temp0) / dt * 60.0


def wait_until_stable(tmon8, target_k: float, cfg: SweepConfig) -> Tuple[float, float, Optional[float]]:
    """Wait until temperature is inside target ± tolerance for cfg.hold_s seconds.

    If --slope-tol-k-per-min > 0, also require recent absolute slope to be <= that value.
    Returns: (final_temp_k, wait_elapsed_s, final_slope_k_per_min)
    """
    lower = target_k - cfg.temp_tolerance_k
    upper = target_k + cfg.temp_tolerance_k
    print(
        f"Waiting for {target_k:.3f} K: range [{lower:.3f}, {upper:.3f}] K, "
        f"hold {cfg.hold_s:.1f}s, max wait {cfg.max_wait_s:.1f}s"
    )

    t_start = time.monotonic()
    in_range_since: Optional[float] = None
    history: List[Tuple[float, float]] = []
    last_print = 0.0
    final_slope: Optional[float] = None

    while True:
        now = time.monotonic()
        elapsed = now - t_start
        if elapsed > cfg.max_wait_s:
            raise TimeoutError(
                f"Temperature did not stabilize at {target_k:.3f} K within {cfg.max_wait_s:.1f}s."
            )

        temp_k = tmon8_read_temperature(tmon8, cfg.temp_channel)
        history.append((now, temp_k))
        # Bound memory.
        history = [(t, temp) for t, temp in history if now - t <= max(cfg.slope_window_s, 60.0)]

        in_range = lower <= temp_k <= upper
        slope = estimate_slope_k_per_min(history, cfg.slope_window_s)
        final_slope = slope
        slope_ok = True
        if cfg.slope_tol_k_per_min > 0:
            slope_ok = slope is not None and abs(slope) <= cfg.slope_tol_k_per_min

        if in_range and slope_ok:
            if in_range_since is None:
                in_range_since = now
                print(f"  Entered range: temp={temp_k:.4f} K, slope={slope}")
            if now - in_range_since >= cfg.hold_s:
                print(f"  Stable enough: temp={temp_k:.4f} K after wait {elapsed:.1f}s")
                return temp_k, elapsed, final_slope
        else:
            in_range_since = None

        if elapsed - last_print >= 5.0:
            slope_text = "N/A" if slope is None else f"{slope:+.4f} K/min"
            print(f"  t={elapsed:7.1f}s  temp={temp_k:9.4f} K  slope={slope_text}")
            last_print = elapsed

        time.sleep(cfg.temp_poll_s)


def collect_voltage_samples(tmon8, dmm, target_k: float, cfg: SweepConfig) -> List[dict]:
    rows: List[dict] = []
    for i in range(cfg.voltage_samples):
        sample_time = datetime.now().isoformat(timespec="seconds")
        temp_k = tmon8_read_temperature(tmon8, cfg.temp_channel)
        voltage_v = dmm_read_voltage(dmm)
        rows.append(
            {
                "sample_index": i + 1,
                "timestamp": sample_time,
                "target_k": target_k,
                "actual_temp_k": temp_k,
                "voltage_v": voltage_v,
            }
        )
        print(f"    sample {i+1}/{cfg.voltage_samples}: T={temp_k:.4f} K, V={voltage_v:.9g} V")
        if i != cfg.voltage_samples - 1:
            time.sleep(cfg.voltage_interval_s)
    return rows


def mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else float("nan")


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


def write_outputs(out_dir: Path, prefix: str, samples: List[dict], summary: List[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    samples_path = out_dir / f"{prefix}_samples_long.csv"
    summary_path = out_dir / f"{prefix}_summary_long.csv"
    wide_path = out_dir / f"{prefix}_summary_wide.csv"

    if samples:
        with samples_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(samples[0].keys()))
            writer.writeheader()
            writer.writerows(samples)

    if summary:
        with summary_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            writer.writeheader()
            writer.writerows(summary)

        # Wide format: one row of checkpoints, one row of voltage, plus useful diagnostics.
        checkpoints = [row["target_k"] for row in summary]
        voltage_mean = [row["voltage_mean_v"] for row in summary]
        voltage_std = [row["voltage_std_v"] for row in summary]
        actual_temp_mean = [row["actual_temp_mean_k"] for row in summary]
        actual_temp_std = [row["actual_temp_std_k"] for row in summary]
        wait_s = [row["wait_s"] for row in summary]

        with wide_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["metric"] + [f"checkpoint_{i+1}" for i in range(len(summary))])
            writer.writerow(["target_k"] + checkpoints)
            writer.writerow(["voltage_mean_v"] + voltage_mean)
            writer.writerow(["voltage_std_v"] + voltage_std)
            writer.writerow(["actual_temp_mean_k"] + actual_temp_mean)
            writer.writerow(["actual_temp_std_k"] + actual_temp_std)
            writer.writerow(["wait_s"] + wait_s)

    print("\nOutput files:")
    print(f"  {samples_path}")
    print(f"  {summary_path}")
    print(f"  {wide_path}")


def run_sweep(cfg: SweepConfig) -> None:
    checkpoints = load_checkpoints(cfg.checkpoint_file)
    check_temperature_limits(checkpoints, cfg.min_k, cfg.max_k)
    print("Checkpoints K:", checkpoints)

    if cfg.dry_run:
        print("Dry run only. No instruments opened, no setpoints changed.")
        return

    prefix = datetime.now().strftime("sweep_%Y%m%d_%H%M%S")
    samples_all: List[dict] = []
    summary_rows: List[dict] = []

    rm = pyvisa.ResourceManager()
    tmon8 = None
    dmm = None
    original_setpoint: Optional[float] = None

    try:
        tmon8 = open_tmon8(rm, cfg.tmon8_addr)
        print(f"TMon8 IDN: {tmon8.query('*IDN?').strip()}")
        print(f"Initial temp ch{cfg.temp_channel}: {tmon8_read_temperature(tmon8, cfg.temp_channel):.4f} K")
        original_setpoint = tmon8_read_setpoint(tmon8, cfg.setpoint_loop)
        print(f"Initial setpoint loop{cfg.setpoint_loop}: {original_setpoint:.3f} K")

        dmm = open_dmm6500(rm, cfg.dmm_addr, cfg)

        for idx, target_k in enumerate(checkpoints, start=1):
            print("\n" + "=" * 80)
            print(f"Checkpoint {idx}/{len(checkpoints)}: target {target_k:.3f} K")
            t0 = time.monotonic()

            set_cmd = tmon8_write_setpoint(tmon8, cfg.setpoint_loop, target_k)
            print(f"Setpoint command sent: {set_cmd!r}")

            readback = None
            for attempt in range(1, 4):
                time.sleep(1.0)
                readback = tmon8_read_setpoint(tmon8, cfg.setpoint_loop)
                print(f"Setpoint readback attempt {attempt}/3: {readback:.3f} K")

                if abs(readback - target_k) <= max(0.05, cfg.temp_tolerance_k):
                    break

                # Retry write if readback did not change
                set_cmd = tmon8_write_setpoint(tmon8, cfg.setpoint_loop, target_k)
                print(f"Retry setpoint command sent: {set_cmd!r}")

            if readback is None or abs(readback - target_k) > max(0.05, cfg.temp_tolerance_k):
                raise RuntimeError(
                    f"Setpoint readback mismatch after retries: asked {target_k:.3f} K, got {readback:.3f} K"
                )

            final_temp, wait_s, final_slope = wait_until_stable(tmon8, target_k, cfg)
            sample_rows = collect_voltage_samples(tmon8, dmm, target_k, cfg)

            voltages = [float(row["voltage_v"]) for row in sample_rows]
            temps = [float(row["actual_temp_k"]) for row in sample_rows]
            elapsed_s = time.monotonic() - t0

            for row in sample_rows:
                row.update(
                    {
                        "checkpoint_index": idx,
                        "setpoint_readback_k": readback,
                        "wait_s": wait_s,
                    }
                )
            samples_all.extend(sample_rows)

            summary_row = {
                "checkpoint_index": idx,
                "target_k": target_k,
                "setpoint_readback_k": readback,
                "final_wait_temp_k": final_temp,
                "actual_temp_mean_k": mean(temps),
                "actual_temp_std_k": stdev(temps),
                "voltage_mean_v": mean(voltages),
                "voltage_std_v": stdev(voltages),
                "voltage_samples": len(voltages),
                "wait_s": wait_s,
                "elapsed_s": elapsed_s,
                "final_slope_k_per_min": "" if final_slope is None else final_slope,
            }
            summary_rows.append(summary_row)

            # Write after every checkpoint so partial data survives interruption.
            write_outputs(cfg.out_dir, prefix, samples_all, summary_rows)

    except KeyboardInterrupt:
        print("\nInterrupted by user. Writing partial results...")
        if summary_rows:
            write_outputs(cfg.out_dir, prefix, samples_all, summary_rows)
        raise
    finally:
        if cfg.restore_setpoint and tmon8 is not None and original_setpoint is not None:
            try:
                print(f"Restoring original TMon8 setpoint: {original_setpoint:.3f} K")
                tmon8_write_setpoint(tmon8, cfg.setpoint_loop, original_setpoint)
            except Exception as exc:
                print(f"Warning: failed to restore setpoint: {exc}")
        for inst in (dmm, tmon8):
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Headless temperature checkpoint sweep for TMon8 + Keithley DMM6500."
    )
    p.add_argument("--checkpoints", required=True, type=Path, help="CSV/TXT checkpoint file; XLSX optional if pandas installed.")
    p.add_argument("--out-dir", type=Path, default=Path("measurements_headless"), help="Output directory.")

    p.add_argument("--tmon8-addr", default="ASRL6::INSTR", help="TMon8 VISA resource, e.g. ASRL6::INSTR.")
    p.add_argument("--dmm-addr", default="USB0::0x05E6::0x6500::04429375::INSTR", help="DMM6500 VISA resource.")
    p.add_argument("--temp-channel", type=int, default=1, help="TMon8 KRDG channel.")
    p.add_argument("--setpoint-loop", type=int, default=1, help="TMon8 SETP loop.")

    p.add_argument("--tolerance-k", type=float, default=0.05, help="Allowed temperature error around checkpoint.")
    p.add_argument("--hold-s", type=float, default=20.0, help="Required continuous time inside tolerance before sampling.")
    p.add_argument("--max-wait-s", type=float, default=1800.0, help="Max wait per checkpoint.")
    p.add_argument("--temp-poll-s", type=float, default=1.0, help="Temperature polling interval while waiting.")
    p.add_argument("--slope-window-s", type=float, default=0.0, help="Recent window for optional slope stability check. 0 disables.")
    p.add_argument("--slope-tol-k-per-min", type=float, default=0.0, help="Require abs(temp slope) below this. 0 disables.")

    p.add_argument("--voltage-samples", type=int, default=5, help="Number of DMM voltage readings per checkpoint.")
    p.add_argument("--voltage-interval-s", type=float, default=0.5, help="Delay between DMM voltage samples.")
    p.add_argument("--dmm-nplc", type=float, default=1.0, help="DMM6500 NPLC integration setting.")
    p.add_argument("--no-dmm-autorange", action="store_true", help="Disable DMM voltage autorange.")
    p.add_argument("--dmm-reset", action="store_true", help="Send *RST to DMM6500 at startup.")

    p.add_argument("--min-k", type=float, default=250.0, help="Safety lower bound for checkpoints.")
    p.add_argument("--max-k", type=float, default=350.0, help="Safety upper bound for checkpoints.")
    p.add_argument("--restore-setpoint", action="store_true", help="Restore original TMon8 setpoint at exit.")
    p.add_argument("--dry-run", action="store_true", help="Parse checkpoints and exit without opening instruments.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = SweepConfig(
        checkpoint_file=args.checkpoints,
        out_dir=args.out_dir,
        tmon8_addr=args.tmon8_addr,
        dmm_addr=args.dmm_addr,
        temp_channel=args.temp_channel,
        setpoint_loop=args.setpoint_loop,
        temp_tolerance_k=args.tolerance_k,
        hold_s=args.hold_s,
        max_wait_s=args.max_wait_s,
        temp_poll_s=args.temp_poll_s,
        slope_window_s=args.slope_window_s,
        slope_tol_k_per_min=args.slope_tol_k_per_min,
        voltage_samples=args.voltage_samples,
        voltage_interval_s=args.voltage_interval_s,
        dmm_nplc=args.dmm_nplc,
        dmm_autorange=not args.no_dmm_autorange,
        dmm_reset=args.dmm_reset,
        min_k=args.min_k,
        max_k=args.max_k,
        restore_setpoint=args.restore_setpoint,
        dry_run=args.dry_run,
    )
    run_sweep(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
