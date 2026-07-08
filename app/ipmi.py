"""IPMI wrapper core — shared by the CLI and (future) web endpoint.

Two interchangeable backends behind one `run_ipmi()` interface:

- **ipmitool** — shells out to the ipmitool binary. Most faithful (full CLI
  surface, exact output), but requires the binary to be installed.
- **pyghmi** — pure-Python IPMI 2.0 over the network. No external binary, works
  on Windows, returns structured data. Covers the common subcommands plus raw;
  vendor-specific ipmitool subcommands may not be implemented.

`backend="auto"` (default) prefers ipmitool when present, else falls back to
pyghmi. Both paths are injection-safe (arg lists, no shell) and keep the
password out of the process list.
"""
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_CLI_PATH = Path(__file__).resolve().parent.parent / "ipmi_cli.py"

DEFAULT_INTERFACE = "lanplus"   # IPMI 2.0; use "lan" for legacy 1.5
DEFAULT_TIMEOUT = 30

# pyghmi's session layer is not thread-safe and keeps global per-BMC session
# state; concurrent calls from the threadpool can deadlock. Serialize them.
_pyghmi_lock = threading.Lock()


class IpmitoolNotFound(RuntimeError):
    """Raised when the ipmitool binary is not available on the host."""


class PyghmiNotAvailable(RuntimeError):
    """Raised when the pyghmi backend is requested but not importable."""


class SubcommandNotSupported(RuntimeError):
    """Raised when the pyghmi backend has no mapping for a subcommand."""


# ---------------------------------------------------------------------------
# availability probes
# ---------------------------------------------------------------------------

def ipmitool_available() -> bool:
    return shutil.which("ipmitool") is not None


def pyghmi_available() -> bool:
    try:
        import pyghmi.ipmi.command  # noqa: F401
        return True
    except ImportError:
        return False


def _result(ok, exit_code, stdout, stderr, command) -> dict:
    return {"ok": ok, "exit_code": exit_code, "stdout": stdout,
            "stderr": stderr, "command": command}


# ---------------------------------------------------------------------------
# backend: ipmitool binary
# ---------------------------------------------------------------------------

def build_command(host: str, user: str, args, interface: str = DEFAULT_INTERFACE) -> list[str]:
    """Build the ipmitool argv. Password is NOT included (passed via env)."""
    return ["ipmitool", "-I", interface, "-H", host, "-U", user, "-E", *args]


def _run_ipmitool(host, user, password, args, *, interface, timeout) -> dict:
    if not ipmitool_available():
        raise IpmitoolNotFound("ipmitool not found in PATH; please install it on this host")
    cmd = build_command(host, user, args, interface)
    env = {**os.environ, "IPMI_PASSWORD": password}
    printable = " ".join(cmd)  # safe: password is in env, not argv
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _result(False, None, "", f"ipmitool timed out after {timeout}s", printable)
    return _result(proc.returncode == 0, proc.returncode, proc.stdout, proc.stderr, printable)


# ---------------------------------------------------------------------------
# backend: pyghmi (pure Python)
# ---------------------------------------------------------------------------

def _fmt_power(c) -> str:
    return f"Chassis Power is {c.get_power().get('powerstate')}"


# ipmitool power verb -> pyghmi set_power state
_POWER_MAP = {"on": "on", "off": "off", "cycle": "boot",
              "reset": "reset", "soft": "shutdown", "diag": "diag"}


def _pyghmi_dispatch(c, args) -> str:
    """Map an ipmitool-style arg list to pyghmi calls, return text output."""
    a = list(args)
    head = a[0] if a else ""

    # raw <netfn> <cmd> [data...]
    if head == "raw":
        netfn = int(a[1], 0)
        cmd = int(a[2], 0)
        data = bytes(int(x, 0) for x in a[3:])
        resp = c.xraw_command(netfn=netfn, command=cmd, data=data)
        return " ".join(f"{b:02x}" for b in resp["data"])

    # power / chassis power
    if head == "power" or (head == "chassis" and len(a) > 1 and a[1] == "power"):
        verb = a[-1]
        if verb in ("status", "power", "chassis"):
            return _fmt_power(c)
        if verb in _POWER_MAP:
            c.set_power(_POWER_MAP[verb])
            return f"Chassis Power Control: {verb}"
        raise SubcommandNotSupported(f"power verb '{verb}' not supported")

    # chassis status / chassis bootdev
    if head == "chassis":
        sub = a[1] if len(a) > 1 else "status"
        if sub == "status":
            return _fmt_power(c)
        if sub == "bootdev":
            if len(a) > 2:
                c.set_bootdev(a[2])
                return f"bootdev set to {a[2]}"
            return f"bootdev: {c.get_bootdev()}"
        if sub == "identify":
            c.set_identify(on=(a[2] != "0") if len(a) > 2 else True)
            return "Chassis identify updated"
        raise SubcommandNotSupported(f"chassis {sub} not supported")

    # mc info (decode Get Device ID)
    if head == "mc" and len(a) > 1 and a[1] == "info":
        d = c.raw_command(netfn=0x06, command=0x01)["data"]
        mfg = d[6] | (d[7] << 8) | (d[8] << 16)
        prod = d[9] | (d[10] << 8)
        return ("Device ID                 : {}\n"
                "Firmware Revision         : {}.{:x}\n"
                "IPMI Version              : {}.{}\n"
                "Manufacturer ID           : {}\n"
                "Product ID                : {}").format(
            d[0], d[2], d[3], d[4] & 0x0f, d[4] >> 4, mfg, prod)

    # sel list / elist
    if head == "sel" and (len(a) < 2 or a[1] in ("list", "elist")):
        lines = []
        for e in c.get_event_log():
            state = "Deasserted" if e.get("deassertion") else "Asserted"
            lines.append(f"{e.get('record_id')} | {e.get('component')} | "
                         f"{e.get('component_type')} | {state}")
        return "\n".join(lines) or "SEL has no entries"

    # sdr / sensor list
    if head in ("sdr", "sensor"):
        lines = []
        for s in c.get_sensor_data():
            val = getattr(s, "value", None)
            units = getattr(s, "units", "") or ""
            states = ",".join(getattr(s, "states", []) or []) or "ok"
            reading = f"{val} {units}".strip() if val is not None else "na"
            lines.append(f"{getattr(s, 'name', '?'):24} | {reading:16} | {states}")
        return "\n".join(lines) or "no sensors"

    # fru
    if head == "fru":
        out = []
        for name, data in c.get_inventory():
            out.append(f"[{name}]")
            if isinstance(data, dict):
                out += [f"  {k}: {v}" for k, v in data.items() if v not in (None, "")]
        return "\n".join(out) or "no FRU data"

    raise SubcommandNotSupported(
        f"subcommand '{' '.join(a)}' is not implemented in the pyghmi backend; "
        f"use the ipmitool binary or a 'raw' command")


def _run_pyghmi(host, user, password, args, *, timeout) -> dict:
    if not pyghmi_available():
        raise PyghmiNotAvailable("pyghmi not installed; run `pip install pyghmi`")
    from pyghmi.ipmi import command
    printable = "pyghmi: " + " ".join(args)
    # serialize: pyghmi's session layer is not thread-safe. pyghmi caches and
    # reuses per-BMC sessions internally, so we deliberately do NOT log out.
    with _pyghmi_lock:
        try:
            c = command.Command(bmc=host, userid=user, password=password)
            out = _pyghmi_dispatch(c, args)
        except SubcommandNotSupported as e:
            return _result(False, 1, "", str(e), printable)
        except Exception as e:
            return _result(False, 1, "", f"{type(e).__name__}: {e}", printable)
    return _result(True, 0, out + ("\n" if out and not out.endswith("\n") else ""), "", printable)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def run_ipmi(host: str, user: str, password: str, args,
             *, interface: str = DEFAULT_INTERFACE, timeout: int = DEFAULT_TIMEOUT,
             backend: str = "auto") -> dict:
    """Run an IPMI command against a BMC.

    `args` is the ipmitool-style subcommand as a list, e.g. ["sel", "list"] or
    ["raw", "0x32", "0xaa", "0x00"]. `backend`: "auto" | "ipmitool" | "pyghmi".
    Returns a structured result dict.
    """
    args = list(args)
    if backend == "ipmitool":
        return _run_ipmitool(host, user, password, args, interface=interface, timeout=timeout)
    if backend == "pyghmi":
        return _run_pyghmi(host, user, password, args, timeout=timeout)
    # auto: prefer the faithful binary, fall back to pure Python
    if ipmitool_available():
        return _run_ipmitool(host, user, password, args, interface=interface, timeout=timeout)
    if pyghmi_available():
        return _run_pyghmi(host, user, password, args, timeout=timeout)
    raise IpmitoolNotFound(
        "no IPMI backend available: install ipmitool or `pip install pyghmi`")


def run_ipmi_isolated(host: str, user: str, password: str, args,
                      *, interface: str = DEFAULT_INTERFACE, timeout: int = DEFAULT_TIMEOUT,
                      backend: str = "auto") -> dict:
    """Run one IPMI command in a fresh subprocess (via ipmi_cli.py).

    Preferred for long-lived servers: each command gets its own process, so the
    IPMI session is cleanly released on exit. This avoids pyghmi's in-process
    session reuse exhausting the BMC's session slots. Credentials are passed to
    the child via environment variables, never on its command line.
    """
    cmd = [sys.executable, str(_CLI_PATH), "--backend", backend,
           "-I", interface, "--timeout", str(timeout), *args]
    env = {**os.environ, "BMC_HOST": host, "BMC_USER": user, "BMC_PASS": password}
    # resolve which backend the child will actually pick (parent shares this host)
    effective = backend
    if backend == "auto":
        effective = "ipmitool" if ipmitool_available() else "pyghmi"
    printable = f"{effective}: " + " ".join(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        return _result(False, None, "", f"timed out after {timeout}s", printable)
    return _result(proc.returncode == 0, proc.returncode, proc.stdout, proc.stderr, printable)
