#!/usr/bin/env python3
"""Run any ipmitool command against a BMC over LAN.

Credentials default to redfish_manager.env (BMC_HOST/BMC_USER/BMC_PASS) and can
be overridden with -H/-U/-P. Everything after the wrapper flags is passed
straight through to ipmitool, so all ipmitool subcommands are supported.

Examples:
  python ipmi_cli.py sel list
  python ipmi_cli.py chassis status
  python ipmi_cli.py sensor list
  python ipmi_cli.py -H 192.168.0.47 -U root -P secret mc info
  python ipmi_cli.py -I lan raw 0x32 0xaa 0x00

Exit code mirrors ipmitool's own exit code.
"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.ipmi import (DEFAULT_INTERFACE, DEFAULT_TIMEOUT, IpmitoolNotFound,
                      PyghmiNotAvailable, run_ipmi)

ENV_PATH = Path(__file__).resolve().parent / "redfish_manager.env"


def main():
    load_dotenv(ENV_PATH)
    import os

    ap = argparse.ArgumentParser(
        description="Run any ipmitool command against a BMC (credentials default to redfish_manager.env)",
        usage="ipmi_cli.py [-H host] [-U user] [-P pass] [-I interface] [--timeout N] <ipmitool command...>",
    )
    ap.add_argument("-H", "--host", default=os.getenv("BMC_HOST"))
    ap.add_argument("-U", "--user", default=os.getenv("BMC_USER"))
    ap.add_argument("-P", "--password", default=os.getenv("BMC_PASS"))
    ap.add_argument("-I", "--interface", default=DEFAULT_INTERFACE)
    ap.add_argument("--backend", choices=("auto", "ipmitool", "pyghmi"), default="auto",
                    help="auto (default): ipmitool binary if present, else pure-Python pyghmi")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="ipmitool subcommand and arguments (e.g. sel list)")
    args = ap.parse_args()

    if not (args.host and args.user and args.password):
        ap.error("BMC host/user/password required (set them in redfish_manager.env or pass -H/-U/-P)")
    if not args.command:
        ap.error("no ipmitool command given (e.g. `ipmi_cli.py sel list`)")

    try:
        res = run_ipmi(args.host, args.user, args.password, args.command,
                       interface=args.interface, timeout=args.timeout, backend=args.backend)
    except (IpmitoolNotFound, PyghmiNotAvailable) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(127)

    # transparent pass-through: stdout to stdout, stderr to stderr, mirror exit code
    if res["stdout"]:
        sys.stdout.write(res["stdout"])
    if res["stderr"]:
        sys.stderr.write(res["stderr"])
    sys.exit(res["exit_code"] if res["exit_code"] is not None else 1)


if __name__ == "__main__":
    main()
