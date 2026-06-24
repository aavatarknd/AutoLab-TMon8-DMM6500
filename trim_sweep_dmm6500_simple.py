#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
trim_sweep_dmm6500_simple.py

Minimal output:
- Sends STM32 command: Trim:<trim>\r\n
- Measures DMM6500 voltage
- Console output only: trim,voltage_mean_v
- CSV output only: trim,voltage_mean_v
"""

from __future__ import annotations

import argparse
import csv
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pyvisa
import serial


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def build_command(fmt: str, trim: int) -> bytes:
    text = fmt.format(trim=trim)
    text = text.encode("utf-8").decode("unicode_escape")
    return text.encode("ascii")


def open_stm32_serial(port: str, baud: int, timeout: float) -> serial.Serial:
    ser = serial.Serial(port=port, baudrate=baud, timeout=timeout)
    time.sleep(0.2)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception:
        pass
    return ser


def open_dmm6500(addr: str, nplc: float):
    rm = pyvisa.ResourceManager()
    dmm = rm.open_resource(addr)
    dmm.timeout = 5000
    dmm.write_termination = "\n"
    dmm.read_termination = "\n"
    dmm.write(":SENS:FUNC 'VOLT:DC'")
    dmm.write(":SENS:VOLT:DC:RANG:AUTO ON")
    dmm.write(f":SENS:VOLT:DC:NPLC {float(nplc)}")
    return rm, dmm


def read_voltage_mean(dmm, samples: int, interval_s: float) -> float:
    values = []
    for i in range(samples):
        values.append(float(dmm.query(":READ?").strip()))
        if i < samples - 1:
            time.sleep(interval_s)
    return statistics.mean(values)


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal STM32 TRIM sweep + DMM6500 voltage logger.")
    parser.add_argument("--stm32-port", required=True, help="STM32 serial COM port, e.g. COM5.")
    parser.add_argument("--dmm-addr", required=True, help="DMM6500 VISA address.")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--serial-timeout-s", type=float, default=1.0)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=255)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--cmd-format", default=r"Trim:{trim}\r\n")
    parser.add_argument("--response-wait-s", type=float, default=0.1)
    parser.add_argument("--settle-s", type=float, default=1.0)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--sample-interval-s", type=float, default=0.1)
    parser.add_argument("--dmm-nplc", type=float, default=1.0)
    parser.add_argument("--outdir", type=Path, default=Path("measurements_trim"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.step == 0:
        raise ValueError("--step cannot be 0")

    stop_exclusive = args.stop + (1 if args.step > 0 else -1)
    trims = list(range(args.start, stop_exclusive, args.step))
    if not trims:
        raise ValueError("Empty trim range. Check --start/--stop/--step.")

    args.outdir.mkdir(parents=True, exist_ok=True)
    output = args.output or (args.outdir / f"trim_voltage_{timestamp_for_file()}.csv")

    if args.dry_run:
        print("trim,voltage_mean_v")
        for trim in trims:
            print(f"{trim},DRY_RUN")
        print(f"\nSaved: {output}")
        return 0

    ser: Optional[serial.Serial] = None
    dmm = None
    dmm_rm = None

    try:
        ser = open_stm32_serial(args.stm32_port, args.baud, args.serial_timeout_s)
        dmm_rm, dmm = open_dmm6500(args.dmm_addr, args.dmm_nplc)

        with output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["trim", "voltage_mean_v"])
            writer.writeheader()
            print("trim,voltage_mean_v")

            for trim in trims:
                cmd = build_command(args.cmd_format, trim)
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass

                ser.write(cmd)
                ser.flush()

                # Read and discard STM32 response so the buffer does not grow.
                time.sleep(args.response_wait_s)
                try:
                    ser.read_all()
                except Exception:
                    pass

                time.sleep(args.settle_s)
                voltage_mean = read_voltage_mean(dmm, args.samples, args.sample_interval_s)

                writer.writerow({"trim": trim, "voltage_mean_v": f"{voltage_mean:.12g}"})
                f.flush()
                print(f"{trim},{voltage_mean:.12g}")

    except KeyboardInterrupt:
        print("\nCtrl+C received. Stopping safely.")
    finally:
        try:
            if dmm is not None:
                dmm.close()
        except Exception:
            pass
        try:
            if dmm_rm is not None:
                dmm_rm.close()
        except Exception:
            pass
        try:
            if ser is not None and ser.is_open:
                ser.close()
        except Exception:
            pass
        print(f"\nSaved: {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
