#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
trim_sweep_dmm6500.py

Sweep STM32 TRIM code through serial port and measure voltage with Keithley DMM6500.

Default STM32 command:
    Trim:<trim>\r\n

Examples:
    # Dry-run: do not open instruments
    python trim_sweep_dmm6500.py --dry-run --stm32-port COM6 --dmm-addr "USB0::0x05E6::0x6500::04429375::INSTR"

    # Small test first
    python trim_sweep_dmm6500.py --stm32-port COM6 --dmm-addr "USB0::0x05E6::0x6500::04429375::INSTR" --start 0 --stop 3

    # Full sweep 0-255
    python trim_sweep_dmm6500.py --stm32-port COM6 --dmm-addr "USB0::0x05E6::0x6500::04429375::INSTR" --start 0 --stop 255 --step 1
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

    idn = dmm.query("*IDN?").strip()
    dmm.write(":SENS:FUNC 'VOLT:DC'")
    dmm.write(":SENS:VOLT:DC:RANG:AUTO ON")
    dmm.write(f":SENS:VOLT:DC:NPLC {float(nplc)}")

    return rm, dmm, idn


def read_dmm_voltage_samples(dmm, samples: int, interval_s: float) -> tuple[float, float, list[float]]:
    values: list[float] = []
    for i in range(samples):
        raw = dmm.query(":READ?").strip()
        values.append(float(raw))
        if i < samples - 1:
            time.sleep(interval_s)

    if len(values) == 1:
        return values[0], 0.0, values
    return statistics.mean(values), statistics.stdev(values), values


def read_stm32_response(ser: serial.Serial, wait_s: float) -> str:
    time.sleep(wait_s)
    try:
        data = ser.read_all()
    except Exception:
        data = b""
    return data.decode("ascii", errors="replace").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep STM32 TRIM 0-255 and measure voltage using DMM6500.")
    parser.add_argument("--stm32-port", required=True, help="STM32 serial COM port, e.g. COM6.")
    parser.add_argument("--dmm-addr", required=True, help="DMM6500 VISA address, e.g. USB0::...::INSTR.")

    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--serial-timeout-s", type=float, default=1.0)

    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=255)
    parser.add_argument("--step", type=int, default=1)

    parser.add_argument(
        "--cmd-format",
        default=r"Trim:{trim}\r\n",
        help=r'STM32 command format. Use {trim}. Default: "Trim:{trim}\r\n"',
    )
    parser.add_argument("--response-wait-s", type=float, default=0.2)
    parser.add_argument("--settle-s", type=float, default=0.5)

    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--sample-interval-s", type=float, default=0.1)
    parser.add_argument("--dmm-nplc", type=float, default=1.0)

    parser.add_argument("--outdir", type=Path, default=Path("measurements_trim"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-serial-response", action="store_true")

    args = parser.parse_args()

    if args.step == 0:
        raise ValueError("--step cannot be 0")

    if args.start < 0 or args.stop > 255:
        print("WARNING: TRIM is usually 0-255. You requested outside this range.")

    stop_exclusive = args.stop + (1 if args.step > 0 else -1)
    trims = list(range(args.start, stop_exclusive, args.step))
    if not trims:
        raise ValueError("Empty trim range. Check --start/--stop/--step.")

    print(f"Trim codes: {trims[:10]}{' ...' if len(trims) > 10 else ''}")
    print(f"Total points: {len(trims)}")
    print(f"STM32 command example for first point: {build_command(args.cmd_format, trims[0])!r}")

    if args.dry_run:
        print("Dry run only. No instruments opened.")
        return 0

    args.outdir.mkdir(parents=True, exist_ok=True)
    output = args.output or (args.outdir / f"trim_sweep_{timestamp_for_file()}.csv")

    ser: Optional[serial.Serial] = None
    dmm = None
    dmm_rm = None

    fieldnames = [
        "wall_time_iso",
        "elapsed_s",
        "trim",
        "command_ascii",
        "stm32_response",
        "voltage_mean_v",
        "voltage_std_v",
        "voltage_samples_v",
    ]

    start_time = time.monotonic()

    try:
        print(f"Opening STM32 serial: {args.stm32_port} @ {args.baud}")
        ser = open_stm32_serial(args.stm32_port, args.baud, args.serial_timeout_s)
        print("STM32 serial opened.")

        print(f"Opening DMM6500: {args.dmm_addr}")
        dmm_rm, dmm, idn = open_dmm6500(args.dmm_addr, args.dmm_nplc)
        print("DMM6500 IDN:", idn)

        with output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for idx, trim in enumerate(trims, start=1):
                cmd = build_command(args.cmd_format, trim)

                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass

                print("=" * 70)
                print(f"[{idx}/{len(trims)}] TRIM={trim}, send {cmd!r}")

                ser.write(cmd)
                ser.flush()

                response = ""
                if not args.no_serial_response:
                    response = read_stm32_response(ser, args.response_wait_s)

                time.sleep(args.settle_s)

                v_mean, v_std, v_samples = read_dmm_voltage_samples(dmm, args.samples, args.sample_interval_s)

                row = {
                    "wall_time_iso": datetime.now().isoformat(timespec="seconds"),
                    "elapsed_s": f"{time.monotonic() - start_time:.3f}",
                    "trim": trim,
                    "command_ascii": cmd.decode("ascii", errors="replace").replace("\r", "\\r").replace("\n", "\\n"),
                    "stm32_response": response,
                    "voltage_mean_v": f"{v_mean:.12g}",
                    "voltage_std_v": f"{v_std:.12g}",
                    "voltage_samples_v": ";".join(f"{v:.12g}" for v in v_samples),
                }
                writer.writerow(row)
                f.flush()

                print(f"  response={response!r}")
                print(f"  voltage_mean={v_mean:.12g} V, std={v_std:.3g} V")

    except KeyboardInterrupt:
        print("\nCtrl+C received. Stopping safely.")
    finally:
        print("\nOutput file:")
        print(" ", output)

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
