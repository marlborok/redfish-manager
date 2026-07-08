"""ipmitool wrapper core — shared by the CLI and (future) web endpoint.

Design goals:
- Injection-safe: the ipmitool command is always passed as an argument list
  and executed without a shell.
- Credential-safe: the password is passed via the IPMI_PASSWORD environment
  variable (ipmitool's -E flag), never on the command line where it would be
  visible in the process list.
- Reusable: callers supply host/user/password explicitly and get a structured
  result back, so both a CLI (prints it) and a web endpoint (serializes it)
  can share this module unchanged.
"""
import os
import shutil
import subprocess

DEFAULT_INTERFACE = "lanplus"   # IPMI 2.0; use "lan" for legacy 1.5
DEFAULT_TIMEOUT = 30


class IpmitoolNotFound(RuntimeError):
    """Raised when the ipmitool binary is not available on the host."""


def ipmitool_available() -> bool:
    return shutil.which("ipmitool") is not None


def build_command(host: str, user: str, args, interface: str = DEFAULT_INTERFACE) -> list[str]:
    """Build the ipmitool argv. Password is NOT included (passed via env)."""
    return ["ipmitool", "-I", interface, "-H", host, "-U", user, "-E", *args]


def run_ipmi(host: str, user: str, password: str, args,
             *, interface: str = DEFAULT_INTERFACE, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run an ipmitool command against a BMC over LAN.

    `args` is the ipmitool subcommand as a list, e.g. ["sel", "list"] or
    ["raw", "0x32", "0xaa", "0x00"]. Returns a structured result dict; raises
    IpmitoolNotFound if the binary is missing (a deployment error).
    """
    if not ipmitool_available():
        raise IpmitoolNotFound("ipmitool not found in PATH; please install it on this host")

    cmd = build_command(host, user, args, interface)
    env = {**os.environ, "IPMI_PASSWORD": password}
    # the command string is safe to echo — the password lives only in env
    printable = " ".join(cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": f"ipmitool timed out after {timeout}s", "command": printable}
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "command": printable,
    }
