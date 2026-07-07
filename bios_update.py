#!/usr/bin/env python3
"""BIOS firmware update via Redfish for MCT/AMI MegaRAC BMC (OOB).

Python port of the BIOS path in mct_redfish_update_preserve_bios.sh (v2.05):
  1. check BMC alive + auth
  2. (--preserve) save current BIOS Attributes to bios_setting_prev.json
  3. show current BIOS version
  4. POST image to /redfish/v1/UpdateService/upload (AMI multipart form)
  5. poll TaskService task + FlashPercentage until Completed/Exception

Usage:
  python bios_update.py <BMC_IP> <USER> <PASSWORD> <image_file> [--preserve] [--yes]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
import urllib3

urllib3.disable_warnings()

FW_TARGET = "BIOS"
POLL_INTERVAL = 2       # seconds, same as the shell script
MAX_NET_RETRIES = 10    # consecutive unreadable task states before giving up


def log(msg: str):
    print(f"[INFO] {msg}", flush=True)


def err(msg: str):
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)


def check_bmc_alive(client: httpx.Client) -> None:
    """mc_selftest equivalent: retry GET /UpdateService up to 10 times."""
    for i in range(1, 11):
        try:
            r = client.get("/redfish/v1/UpdateService")
            if r.status_code == 200:
                log(f"BMC connection success ({time.strftime('%F %T')})")
                return
        except httpx.HTTPError:
            pass
        err(f"BMC connection fail! (attempt {i}/10)")
        time.sleep(2)
    err("BMC connection failure!!! Exit Update!!!")
    sys.exit(1)


def check_auth(client: httpx.Client) -> None:
    r = client.get("/redfish/v1/Chassis/Self")
    if r.status_code != 200:
        err(f"User/Password Authentication failed (HTTP {r.status_code})")
        sys.exit(1)
    log("Authentication successful")


def preserve_bios_settings(client: httpx.Client, out_path: Path) -> None:
    """Save current BIOS Attributes (minus MAPIDS) before the update."""
    log(f"Preserving {FW_TARGET} settings for firmware update...")
    r = client.get("/redfish/v1/Systems/Self/Bios")
    r.raise_for_status()
    attrs = r.json().get("Attributes", {})
    attrs.pop("MAPIDS", None)
    out_path.write_text(json.dumps({"Attributes": attrs}, indent=2))
    log(f"Saved {FW_TARGET} settings to {out_path}")


def show_bios_version(client: httpx.Client) -> str | None:
    r = client.get(f"/redfish/v1/UpdateService/FirmwareInventory/{FW_TARGET}")
    version = r.json().get("Version") if r.status_code == 200 else None
    print()
    print("########################################")
    print(f"Current {FW_TARGET} FW Version: {version}")
    print("########################################")
    print()
    return version


def upload_image(client: httpx.Client, image: Path) -> str:
    """POST the image to the AMI upload endpoint; return the task id."""
    update_params = json.dumps(
        {"Targets": [f"/redfish/v1/UpdateService/FirmwareInventory/{FW_TARGET}"]}
    )
    oem_params = json.dumps({"ImageType": FW_TARGET})

    log("Uploading image ....")
    with image.open("rb") as fh:
        r = client.post(
            "/redfish/v1/UpdateService/upload",
            files=[
                ("UpdateFile", (image.name, fh, "application/octet-stream")),
                ("UpdateParameters", (None, update_params, "application/json")),
                ("OemParameters", (None, oem_params, "application/json")),
            ],
            timeout=600.0,  # BIOS images are tens of MB; give the upload time
        )
    body = r.text
    if not body:
        err("BIOS firmware update failure (empty response)")
        sys.exit(1)

    # older SPX: "A new task /redfish/v1/TaskService/Tasks/<id> ..."
    m = re.search(r"A new task /redfish/v1/TaskService/Tasks/(\d+)", body)
    if not m:
        # SPX 13.8: {"@odata.id": ".../TaskService/Tasks/<id>", ...}
        m = re.search(r'"@odata\.id"\s*:\s*"[^"]*/(\d+)"', body)
    if not m:
        err("Could not find the Task ID from the following response payload:")
        print(body, file=sys.stderr)
        sys.exit(1)

    task_id = m.group(1)
    log(f"TaskID: {task_id}")
    return task_id


def monitor_task(client: httpx.Client, task_id: str) -> str:
    """Poll task state + FlashPercentage until Completed/Exception."""
    log(f"Monitoring TaskID: {task_id} ==> {FW_TARGET} Firmware Update Status....")
    time.sleep(5)
    net_retries = 0
    task_state = None
    while net_retries <= MAX_NET_RETRIES:
        try:
            task = client.get(f"/redfish/v1/TaskService/Tasks/{task_id}").json()
            task_state = task.get("TaskState")
            us = client.get("/redfish/v1/UpdateService").json()
            percentage = (us.get("Oem", {}).get("AMIUpdateService", {})
                          .get("FlashPercentage")
                          or us.get("FlashPercentage"))
        except (httpx.HTTPError, ValueError):
            task_state, percentage = None, None

        if task_state in ("Completed", "Exception"):
            print(">>>>> Updating (Flash Percentage: 100%)")
            print("\n###################\n####### DONE ######\n###################\n")
            break
        elif task_state is None:
            print("\n###### Network is not stable ######\n")
            net_retries += 1
        else:
            net_retries = 0
            if percentage is not None:
                print(f">>>>> Updating (Flash Percentage: {percentage}, "
                      f"TaskState: {task_state})")
            else:
                print(f">>>>> Updating (TaskState: {task_state})")
        time.sleep(POLL_INTERVAL)
    return task_state


def main():
    ap = argparse.ArgumentParser(description="BIOS update via Redfish (MCT/AMI BMC, OOB)")
    ap.add_argument("bmc_ip")
    ap.add_argument("user")
    ap.add_argument("password")
    ap.add_argument("image", type=Path, help="BIOS image file")
    ap.add_argument("--preserve", "-s", action="store_true",
                    help="save current BIOS Attributes to bios_setting_prev.json first")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="skip the confirmation prompt")
    args = ap.parse_args()

    if not args.image.is_file():
        err(f"File: {args.image} NOT found...")
        sys.exit(1)
    log(f"File: {args.image} found... ({args.image.stat().st_size / 1e6:.1f} MB)")

    client = httpx.Client(
        base_url=f"https://{args.bmc_ip}",
        auth=(args.user, args.password),
        verify=False,
        timeout=20.0,
    )

    check_bmc_alive(client)
    check_auth(client)

    if args.preserve:
        preserve_bios_settings(client, Path("bios_setting_prev.json"))
    else:
        log("Do Not Preserve BIOS settings...")

    show_bios_version(client)

    if not args.yes:
        answer = input(f"Flash {args.image.name} to {FW_TARGET} on {args.bmc_ip}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            log("Aborted by user.")
            sys.exit(0)

    task_id = upload_image(client, args.image)
    task_state = monitor_task(client, task_id)

    print("##############################################################")
    if task_state == "Completed":
        log(f"{FW_TARGET} firmware update completed!!!")
    else:
        err(f"{FW_TARGET} firmware update failed!!!! (final state: {task_state})")
    print("##############################################################")
    client.close()
    sys.exit(0 if task_state == "Completed" else 1)


if __name__ == "__main__":
    main()
