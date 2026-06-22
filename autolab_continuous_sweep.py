#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
autolab_continuous_sweep.py

Headless continuous TMon8 + Keithley DMM6500 logger.

What changed from the checkpoint summary script
-----------------------------------------------
1. Checkpoints are now setpoint targets, not sparse measurement points.
2. The script records continuous temperature-voltage data during the entire wait/hold process.
3. There is no wait timeout. If the target temperature is never reached, it keeps logging until Ctrl+C.
4. The script only writes TMon8 SETP. It does NOT write OUTMODE/PID/MOUT/RANGE/ZONE.

Lab-tested TMon8 protocol
-------------------------
Read K temperature CH1: b"KRDG\\xa3\\xbf1"
Read setpoint loop1:   b"SETP\\xa3\\xbf1"
Write setpoint loop1:  b"SETP1,<target K>\\r\\n"

Configure Heat1 manually on the TMon8 first:
  Heat1 ON, closed loop, correct sensor channel, correct heater resistance,
  PID/control settings saved. This script only changes setpoint and logs.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pyvisa


Q_FULLWIDTH = b"\xa3\xbf"  # GBK full-width question mark: "？"


@dataclass
class Config:
    checkpoints: Path
    outdir: Path
    prefix: str
    tmon8_addr: str
    dmm_addr: str
    temp_channel: int
    setpoint_loop: int
    tolerance_k: float
    hold_s: float
    sample_interval_s: float
    voltage_samples: int
    voltage_interval_s: float
    dmm_nplc: float
    setpoint_settle_s: float
    setpoint_retries: int
    read_retry: int
    read_retry_delay_s: float
    continue_after_final: bool
    restore_initial_setpoint: bool
    dry_run: bool


def parse_numeric_checkpoints(path: Path) -> list[float]:
    text = path.read_text(encoding="utf-8-sig")
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    if not nums:
        raise ValueError(f"No numeric checkpoints found in {path}")
    return [float(x) for x in nums]


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def open_tmon8(addr: str):
    rm = pyvisa.ResourceManager()
    inst = rm.open_resource(addr)
    inst.baud_rate = 115200
    inst.timeout = 3000
    inst.write_termination = "\r\n"
    inst.read_termination = "\r\n"
    return rm, inst


def open_dmm6500(addr: str, nplc: float):
    rm = pyvisa.ResourceManager()
    dmm = rm.open_resource(addr)
    dmm.timeout = 5000
    dmm.write_termination = "\n"
    dmm.read_termination = "\n"
    idn = dmm.query("*IDN?").strip()
    dmm.write(":SENS:FUNC 'VOLT:DC'")
    dmm.write(":SENS:VOLT:DC:RANG:AUTO ON")
    dmm.write(f":SENS:VOLT:DC:NPLC {float(nplc)}")
    return rm, dmm, idn


def _safe_flush_read(inst) -> None:
    try:
        inst.flush(pyvisa.constants.VI_READ_BUF_DISCARD)
    except Exception:
        pass


def visa_write_read_raw(inst, cmd: bytes, delay_s: float, retries: int, retry_delay_s: float) -> bytes:
    last_error: Optional[Exception] = None
    for _ in range(retries):
        try:
            _safe_flush_read(inst)
            inst.write_raw(cmd)
            time.sleep(delay_s)
            return inst.read_raw()
        except Exception as e:
            last_error = e
            time.sleep(retry_delay_s)
    raise RuntimeError(f"Query failed after {retries} attempts for {cmd!r}: {last_error!r}")


def tmon8_idn(inst, cfg: Config) -> str:
    raw = visa_write_read_raw(inst, b"*IDN?\r\n", 0.3, cfg.read_retry, cfg.read_retry_delay_s)
    return raw.decode("ascii", errors="ignore").strip()


def tmon8_read_temp_k(inst, cfg: Config) -> float:
    cmd = b"KRDG" + Q_FULLWIDTH + str(cfg.temp_channel).encode("ascii")
    raw = visa_write_read_raw(inst, cmd, 0.25, cfg.read_retry, cfg.read_retry_delay_s)
    return float(raw.decode("ascii", errors="ignore").strip())


def tmon8_read_setpoint_k(inst, cfg: Config) -> float:
    cmd = b"SETP" + Q_FULLWIDTH + str(cfg.setpoint_loop).encode("ascii")
    raw = visa_write_read_raw(inst, cmd, 0.25, cfg.read_retry, cfg.read_retry_delay_s)
    return float(raw.decode("ascii", errors="ignore").strip())


def tmon8_write_setpoint_k(inst, cfg: Config, target_k: float) -> bytes:
    cmd = f"SETP{cfg.setpoint_loop},{target_k:.2f}\r\n".encode("ascii")
    _safe_flush_read(inst)
    inst.write_raw(cmd)
    return cmd


def robust_setpoint_write(inst, cfg: Config, target_k: float) -> float:
    final_readback = math.nan
    for attempt in range(1, cfg.setpoint_retries + 1):
        cmd = tmon8_write_setpoint_k(inst, cfg, target_k)
        print(f"Setpoint command attempt {attempt}/{cfg.setpoint_retries}: {cmd!r}")
        time.sleep(cfg.setpoint_settle_s)
        try:
            rb = tmon8_read_setpoint_k(inst, cfg)
            final_readback = rb
            print(f"Setpoint readback attempt {attempt}/{cfg.setpoint_retries}: {rb:.3f} K")
            if abs(rb - target_k) <= max(0.05, cfg.tolerance_k):
                return rb
        except Exception as e:
            print(f"Setpoint readback attempt {attempt}/{cfg.setpoint_retries} failed: {e!r}")

    print(
        f"WARNING: setpoint readback did not match after retries. "
        f"Asked {target_k:.3f} K, last readback {final_readback} K. Continuing anyway."
    )
    return final_readback


def dmm6500_read_voltage(dmm, cfg: Config) -> tuple[float, float, list[float]]:
    samples: list[float] = []
    for i in range(cfg.voltage_samples):
        raw = dmm.query(":READ?").strip()
        samples.append(float(raw))
        if i < cfg.voltage_samples - 1:
            time.sleep(cfg.voltage_interval_s)
    if len(samples) == 1:
        return samples[0], 0.0, samples
    return statistics.mean(samples), statistics.stdev(samples), samples


def append_rows(path: Path, fieldnames: list[str], rows: Iterable[dict]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Continuous TMon8 temperature vs DMM6500 voltage logger.")
    parser.add_argument("--checkpoints", type=Path, required=True, help="CSV/TXT containing target temperatures in K.")
    parser.add_argument("--outdir", type=Path, default=Path("measurements_continuous"))
    parser.add_argument("--prefix", default="continuous_sweep")

    parser.add_argument("--tmon8-addr", default="ASRL6::INSTR")
    parser.add_argument("--dmm-addr", default="USB0::0x05E6::0x6500::04429375::INSTR")
    parser.add_argument("--temp-channel", type=int, default=1)
    parser.add_argument("--setpoint-loop", type=int, default=1)

    parser.add_argument("--tolerance-k", type=float, default=0.05)
    parser.add_argument("--hold-s", type=float, default=30.0)
    parser.add_argument("--sample-interval-s", type=float, default=2.0, help="Continuous logging interval.")
    parser.add_argument("--voltage-samples", type=int, default=1, help="DMM samples averaged per log row.")
    parser.add_argument("--voltage-interval-s", type=float, default=0.2)
    parser.add_argument("--dmm-nplc", type=float, default=1.0)

    parser.add_argument("--setpoint-settle-s", type=float, default=1.0)
    parser.add_argument("--setpoint-retries", type=int, default=3)
    parser.add_argument("--read-retry", type=int, default=3)
    parser.add_argument("--read-retry-delay-s", type=float, default=0.5)

    parser.add_argument("--continue-after-final", action="store_true")
    parser.add_argument("--restore-initial-setpoint", action="store_true")
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    cfg = Config(**vars(args))

    checkpoints = parse_numeric_checkpoints(cfg.checkpoints)
    print(f"Checkpoints K: {checkpoints}")
    print("Mode: continuous logging, no timeout while waiting. Press Ctrl+C to stop.")

    if cfg.dry_run:
        print("Dry run only. No instruments opened, no setpoints changed.")
        return 0

    cfg.outdir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp_for_file()
    continuous_path = cfg.outdir / f"{cfg.prefix}_{stamp}_continuous.csv"
    summary_path = cfg.outdir / f"{cfg.prefix}_{stamp}_summary.csv"

    continuous_fields = [
        "wall_time_iso", "elapsed_s", "checkpoint_index", "target_k",
        "actual_temp_k", "temp_error_k", "setpoint_readback_k",
        "voltage_mean_v", "voltage_std_v", "voltage_samples",
        "in_range", "hold_elapsed_s", "phase",
    ]
    summary_fields = [
        "checkpoint_index", "target_k", "reached", "wait_elapsed_s",
        "hold_s", "final_temp_k", "final_voltage_mean_v",
        "final_voltage_std_v", "sample_count",
    ]

    tmon_rm = dmm_rm = None
    tmon = dmm = None
    initial_setpoint: Optional[float] = None
    start_all = time.monotonic()

    try:
        tmon_rm, tmon = open_tmon8(cfg.tmon8_addr)
        print("TMon8 IDN:", tmon8_idn(tmon, cfg))
        print(f"Initial temp ch{cfg.temp_channel}: {tmon8_read_temp_k(tmon, cfg):.4f} K")
        initial_setpoint = tmon8_read_setpoint_k(tmon, cfg)
        print(f"Initial setpoint loop{cfg.setpoint_loop}: {initial_setpoint:.3f} K")

        dmm_rm, dmm, dmm_idn = open_dmm6500(cfg.dmm_addr, cfg.dmm_nplc)
        print("DMM6500 IDN:", dmm_idn)

        for idx, target_k in enumerate(checkpoints, start=1):
            print("\n" + "=" * 80)
            print(f"Checkpoint {idx}/{len(checkpoints)}: target {target_k:.3f} K")
            robust_setpoint_write(tmon, cfg, target_k)

            target_start = time.monotonic()
            hold_start: Optional[float] = None
            reached = False
            sample_count = 0
            final_temp = math.nan
            final_v_mean = math.nan
            final_v_std = math.nan

            print(
                f"Continuous logging for target {target_k:.3f} K, "
                f"range [{target_k - cfg.tolerance_k:.3f}, {target_k + cfg.tolerance_k:.3f}] K. "
                f"No timeout."
            )

            while True:
                now = time.monotonic()
                elapsed_all = now - start_all
                elapsed_target = now - target_start

                try:
                    temp_k = tmon8_read_temp_k(tmon, cfg)
                except Exception as e:
                    print(f"WARNING: temp read failed: {e!r}. Continuing.")
                    time.sleep(cfg.sample_interval_s)
                    continue

                try:
                    setp_rb = tmon8_read_setpoint_k(tmon, cfg)
                except Exception:
                    setp_rb = math.nan

                try:
                    v_mean, v_std, v_samples = dmm6500_read_voltage(dmm, cfg)
                except Exception as e:
                    print(f"WARNING: DMM read failed: {e!r}. Continuing.")
                    v_mean, v_std, v_samples = math.nan, math.nan, []

                err = temp_k - target_k
                in_range = abs(err) <= cfg.tolerance_k

                if in_range:
                    if hold_start is None:
                        hold_start = time.monotonic()
                        print(f"Entered range at t={elapsed_target:.1f}s: temp={temp_k:.4f} K")
                    hold_elapsed = time.monotonic() - hold_start
                    phase = "holding"
                else:
                    hold_start = None
                    hold_elapsed = 0.0
                    phase = "waiting"

                row = {
                    "wall_time_iso": datetime.now().isoformat(timespec="seconds"),
                    "elapsed_s": f"{elapsed_all:.3f}",
                    "checkpoint_index": idx,
                    "target_k": f"{target_k:.5f}",
                    "actual_temp_k": f"{temp_k:.5f}",
                    "temp_error_k": f"{err:.5f}",
                    "setpoint_readback_k": "" if math.isnan(setp_rb) else f"{setp_rb:.5f}",
                    "voltage_mean_v": "" if math.isnan(v_mean) else f"{v_mean:.12g}",
                    "voltage_std_v": "" if math.isnan(v_std) else f"{v_std:.12g}",
                    "voltage_samples": ";".join(f"{x:.12g}" for x in v_samples),
                    "in_range": int(in_range),
                    "hold_elapsed_s": f"{hold_elapsed:.3f}",
                    "phase": phase,
                }
                append_rows(continuous_path, continuous_fields, [row])
                sample_count += 1

                print(
                    f"  t={elapsed_target:8.1f}s  T={temp_k:9.4f} K  "
                    f"target={target_k:8.3f} K  V={v_mean: .8g} V  "
                    f"phase={phase} hold={hold_elapsed:5.1f}s"
                )

                final_temp, final_v_mean, final_v_std = temp_k, v_mean, v_std

                if hold_elapsed >= cfg.hold_s:
                    reached = True
                    print(
                        f"Checkpoint {target_k:.3f} K completed: "
                        f"held within ±{cfg.tolerance_k:.3f} K for {cfg.hold_s:.1f}s."
                    )
                    break

                time.sleep(cfg.sample_interval_s)  # no timeout

            summary_row = {
                "checkpoint_index": idx,
                "target_k": f"{target_k:.5f}",
                "reached": int(reached),
                "wait_elapsed_s": f"{time.monotonic() - target_start:.3f}",
                "hold_s": f"{cfg.hold_s:.3f}",
                "final_temp_k": f"{final_temp:.5f}",
                "final_voltage_mean_v": f"{final_v_mean:.12g}",
                "final_voltage_std_v": f"{final_v_std:.12g}",
                "sample_count": sample_count,
            }
            append_rows(summary_path, summary_fields, [summary_row])

        if cfg.continue_after_final:
            idx = len(checkpoints)
            target_k = checkpoints[-1]
            print("\nFinal checkpoint completed. Continuing to log until Ctrl+C.")
            while True:
                temp_k = tmon8_read_temp_k(tmon, cfg)
                try:
                    setp_rb = tmon8_read_setpoint_k(tmon, cfg)
                except Exception:
                    setp_rb = math.nan
                v_mean, v_std, v_samples = dmm6500_read_voltage(dmm, cfg)
                err = temp_k - target_k
                row = {
                    "wall_time_iso": datetime.now().isoformat(timespec="seconds"),
                    "elapsed_s": f"{time.monotonic() - start_all:.3f}",
                    "checkpoint_index": idx,
                    "target_k": f"{target_k:.5f}",
                    "actual_temp_k": f"{temp_k:.5f}",
                    "temp_error_k": f"{err:.5f}",
                    "setpoint_readback_k": "" if math.isnan(setp_rb) else f"{setp_rb:.5f}",
                    "voltage_mean_v": "" if math.isnan(v_mean) else f"{v_mean:.12g}",
                    "voltage_std_v": "" if math.isnan(v_std) else f"{v_std:.12g}",
                    "voltage_samples": ";".join(f"{x:.12g}" for x in v_samples),
                    "in_range": int(abs(err) <= cfg.tolerance_k),
                    "hold_elapsed_s": "",
                    "phase": "final_continuous",
                }
                append_rows(continuous_path, continuous_fields, [row])
                print(f"  T={temp_k:.4f} K  target={target_k:.3f} K  V={v_mean:.8g} V")
                time.sleep(cfg.sample_interval_s)

    except KeyboardInterrupt:
        print("\nCtrl+C received. Stopping safely.")
    finally:
        if cfg.restore_initial_setpoint and tmon is not None and initial_setpoint is not None:
            try:
                cmd = tmon8_write_setpoint_k(tmon, cfg, initial_setpoint)
                print(f"Restored initial setpoint with {cmd!r}")
            except Exception as e:
                print(f"WARNING: failed to restore initial setpoint: {e!r}")

        print("\nOutput files:")
        print(" ", continuous_path)
        print(" ", summary_path)

        for obj in (dmm, dmm_rm, tmon, tmon_rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
