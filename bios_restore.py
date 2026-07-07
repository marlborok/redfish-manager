#!/usr/bin/env python3
"""Restore BIOS settings via Redfish for MCT/AMI MegaRAC BMC.

Python port of mct_restore_bios_setting.sh:
  1. auth check
  2. GET current BIOS Attributes (minus MAPIDS)
  3. diff saved config vs current; keys whose values differ are restored
     with the saved values (keys missing from either side are skipped,
     matching the shell script's diff behavior)
  4. drop ignore-list keys (known to change string format across versions)
  5. PATCH the differences to /redfish/v1/Systems/Self/Bios/SD (expects 204)
  6. a host power reset/cycle is required for the settings to take effect

Usage:
  python bios_restore.py <BMC_IP> <USER> <PASSWORD> [config_file] [--yes]
  (config_file defaults to bios_setting_prev.json)
"""
import argparse
import json
import sys
from pathlib import Path

import httpx
import urllib3

urllib3.disable_warnings()

BIOS_PATH = "/redfish/v1/Systems/Self/Bios"
BIOS_SD_PATH = "/redfish/v1/Systems/Self/Bios/SD"

# keys containing these substrings are skipped (string format changes
# between BIOS versions cause false diffs — same list as the shell script)
IGNORE_LIST = (
    "CbsCmnCxlComponentErrorReporting",
    "CbsCmnFchSystemPwrFailShadow",
    "IPMI610",
)


def info(msg: str):
    print(f"[INFO] {msg}", flush=True)


def error(msg: str):
    print(f"Error! {msg}", file=sys.stderr, flush=True)


def check_auth(client: httpx.Client):
    r = client.get("/redfish/v1/Chassis/Self")
    if r.status_code != 200:
        error(f"User/Password Authentication failed (HTTP {r.status_code})")
        sys.exit(1)
    info("Authentication successful")


def get_current_attributes(client: httpx.Client) -> dict:
    info("Get current BIOS setting ...")
    r = client.get(BIOS_PATH)
    r.raise_for_status()
    attrs = r.json().get("Attributes", {})
    attrs.pop("MAPIDS", None)
    return attrs


def build_patch(saved: dict, current: dict) -> dict:
    """Keys present in both sides whose values differ → restore saved value."""
    patch = {}
    for key, saved_val in saved.items():
        if key == "MAPIDS" or key not in current:
            continue
        if any(ig in key for ig in IGNORE_LIST):
            continue
        if current[key] != saved_val:
            patch[key] = saved_val
    return patch


def push_settings(client: httpx.Client, patch: dict):
    info("Push the setting to BMC ...")
    r = client.patch(
        BIOS_SD_PATH,
        json={"Attributes": patch},
        headers={"If-Match": "*", "Content-Type": "application/json"},
    )
    if r.status_code not in (200, 204):
        error(f"PATCH failed (HTTP {r.status_code}):")
        print(r.text, file=sys.stderr)
        sys.exit(1)
    info(f"Settings pushed (HTTP {r.status_code}).")
    info("Please do a power reset or cycle to take effect")


def main():
    ap = argparse.ArgumentParser(description="Restore BIOS settings via Redfish (MCT/AMI BMC)")
    ap.add_argument("bmc_ip")
    ap.add_argument("user")
    ap.add_argument("password")
    ap.add_argument("config_file", nargs="?", default="bios_setting_prev.json",
                    type=Path, help="saved settings JSON (default: bios_setting_prev.json)")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if not args.config_file.is_file():
        error(f"BIOS config file not found: {args.config_file}")
        sys.exit(1)
    saved = json.loads(args.config_file.read_text()).get("Attributes", {})
    if not saved:
        error(f"No Attributes found in {args.config_file}")
        sys.exit(1)

    print("===============================================")
    print(f"BMC IP       : {args.bmc_ip}")
    print(f"USER         : {args.user}")
    print(f"BIOS SETTING : {args.config_file} ({len(saved)} attributes)")
    print("===============================================")

    client = httpx.Client(
        base_url=f"https://{args.bmc_ip}",
        auth=(args.user, args.password),
        verify=False,
        timeout=30.0,
    )

    check_auth(client)
    current = get_current_attributes(client)
    patch = build_patch(saved, current)

    skipped = [k for k in saved if k != "MAPIDS" and k not in current]
    if skipped:
        info(f"Skipped {len(skipped)} keys not present on current BIOS: "
             f"{', '.join(skipped[:10])}{' ...' if len(skipped) > 10 else ''}")

    if not patch:
        info("Current BIOS settings already match the saved config. Nothing to restore.")
        client.close()
        return

    info(f"{len(patch)} attribute(s) differ and will be restored:")
    for key, val in sorted(patch.items()):
        print(f"  {key}: {json.dumps(current[key])} -> {json.dumps(val)}")

    if not args.yes:
        answer = input(f"Push these {len(patch)} setting(s) to {args.bmc_ip}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            info("Aborted by user.")
            client.close()
            return

    push_settings(client, patch)
    client.close()


if __name__ == "__main__":
    main()
