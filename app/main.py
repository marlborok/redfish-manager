"""Redfish 管理介面 — FastAPI 後端(discovery 驅動)."""
import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

import urllib3
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .redfish_client import RedfishClient, build_bios_patch

urllib3.disable_warnings()

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "redfish_manager.env")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))


def make_client(device: dict) -> RedfishClient:
    return RedfishClient(device["host"], device["username"], device["password"])


async def poll_device(conn, device: dict):
    """Discover (once) then snapshot a single device."""
    client = make_client(device)
    try:
        profile = db.get_profile(conn, device["id"])
        if not profile:
            profile = await client.discover()
            db.save_profile(conn, device["id"], profile)
        snap = await client.snapshot(profile)
        db.save_snapshot(conn, device["id"], snap, None)
    except Exception as e:
        # ConnectTimeout and friends have an empty str(e); fall back to the type
        msg = str(e) or type(e).__name__
        db.save_snapshot(conn, device["id"], None, msg)
    finally:
        await client.close()


async def collector_loop():
    while True:
        conn = db.connect()
        try:
            for dev in db.list_devices(conn):
                await poll_device(conn, db.get_device(conn, dev["id"]))
        finally:
            conn.close()
        await asyncio.sleep(POLL_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    host, user, pw = os.getenv("BMC_HOST"), os.getenv("BMC_USER"), os.getenv("BMC_PASS")
    if host and user and pw:
        db.ensure_device(conn, host, host, user, pw)
    conn.close()
    task = asyncio.create_task(collector_loop())
    yield
    task.cancel()


app = FastAPI(title="Redfish Manager", lifespan=lifespan)


@app.get("/api/devices")
def api_devices():
    conn = db.connect()
    try:
        out = []
        for dev in db.list_devices(conn):
            out.append({
                **dev,
                "profile": db.get_profile(conn, dev["id"]),
                "snapshot": db.latest_snapshot(conn, dev["id"]),
            })
        return out
    finally:
        conn.close()


class DeviceBody(BaseModel):
    name: str
    host: str
    username: str
    password: str


@app.post("/api/devices")
async def api_add_device(body: DeviceBody):
    conn = db.connect()
    try:
        device_id = db.ensure_device(conn, body.name, body.host, body.username, body.password)
        await poll_device(conn, db.get_device(conn, device_id))
        snap = db.latest_snapshot(conn, device_id)
        if snap and not snap["ok"]:
            raise HTTPException(502, f"已加入但連線失敗:{snap['error']}")
        return {"id": device_id}
    finally:
        conn.close()


@app.post("/api/devices/{device_id}/rediscover")
async def api_rediscover(device_id: int):
    conn = db.connect()
    try:
        dev = db.get_device(conn, device_id)
        if not dev:
            raise HTTPException(404, "device not found")
        client = make_client(dev)
        try:
            profile = await client.discover()
        finally:
            await client.close()
        db.save_profile(conn, device_id, profile)
        return profile
    finally:
        conn.close()


class ResetBody(BaseModel):
    system_id: str
    reset_type: str


@app.post("/api/devices/{device_id}/reset")
async def api_reset(device_id: int, body: ResetBody):
    conn = db.connect()
    dev = db.get_device(conn, device_id)
    profile = db.get_profile(conn, device_id) if dev else None
    conn.close()
    if not dev:
        raise HTTPException(404, "device not found")
    if not profile:
        raise HTTPException(409, "device not discovered yet")

    system = next((s for s in profile["systems"] if s["id"] == body.system_id), None)
    if not system or not system.get("reset_target"):
        raise HTTPException(404, "system or reset action not found")
    if system["reset_types"] and body.reset_type not in system["reset_types"]:
        raise HTTPException(400, f"reset_type must be one of {system['reset_types']}")

    client = make_client(dev)
    try:
        r = await client.system_reset(system["reset_target"], body.reset_type)
        return {"status": r.status_code}
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Log services (SEL, event/audit logs)
# ---------------------------------------------------------------------------


def _log_service_or_404(device_id: int, service_path: str) -> dict:
    conn = db.connect()
    profile = db.get_profile(conn, device_id)
    conn.close()
    svc = next((s for s in (profile or {}).get("log_services", [])
                if s["path"] == service_path), None)
    if not svc:
        raise HTTPException(404, "log service not found on this device")
    return svc


@app.get("/api/devices/{device_id}/logs")
def api_log_services(device_id: int):
    _get_device_or_404(device_id)
    conn = db.connect()
    profile = db.get_profile(conn, device_id)
    conn.close()
    return (profile or {}).get("log_services", [])


@app.get("/api/devices/{device_id}/logs/entries")
async def api_log_entries(device_id: int, service: str, limit: int = 100):
    dev = _get_device_or_404(device_id)
    svc = _log_service_or_404(device_id, service)
    if not svc.get("entries"):
        raise HTTPException(400, "this log service has no entries collection")
    client = make_client(dev)
    try:
        return await client.log_entries(svc["entries"], limit=min(limit, 300))
    except Exception as e:
        raise HTTPException(502, f"讀取日誌失敗:{e}")
    finally:
        await client.close()


class ClearLogBody(BaseModel):
    service: str


@app.post("/api/devices/{device_id}/logs/clear")
async def api_log_clear(device_id: int, body: ClearLogBody):
    dev = _get_device_or_404(device_id)
    svc = _log_service_or_404(device_id, body.service)
    if not svc.get("clear_target"):
        raise HTTPException(400, "this log service does not support ClearLog")
    act = _log_activity(device_id, "clearlog", "act_clearlog_start", svc["name"])
    client = make_client(dev)
    try:
        await client.clear_log(svc["clear_target"])
    except Exception as e:
        _finish_activity(act, "failed", "act_clearlog_failed", svc["name"], str(e))
        raise HTTPException(502, f"清除日誌失敗:{e}")
    finally:
        await client.close()
    _finish_activity(act, "success", "act_clearlog_success", svc["name"])
    return {"cleared": svc["name"]}


# ---------------------------------------------------------------------------
# BIOS firmware update (AMI MegaRAC upload flow)
# ---------------------------------------------------------------------------

BACKUP_DIR = ROOT / "bios_backups"


def _get_device_or_404(device_id: int) -> dict:
    conn = db.connect()
    dev = db.get_device(conn, device_id)
    conn.close()
    if not dev:
        raise HTTPException(404, "device not found")
    return dev


def _log_activity(device_id: int, kind: str, code: str, *params,
                  status: str = "running", task_id: str | None = None) -> int:
    conn = db.connect()
    try:
        return db.add_activity(conn, device_id, kind, code, list(params), status, task_id)
    finally:
        conn.close()


def _finish_activity(activity_id: int, status: str, code: str, *params,
                     task_id: str | None = None):
    conn = db.connect()
    try:
        db.update_activity(conn, activity_id, status, code, list(params), task_id)
    finally:
        conn.close()


def _save_backup(host: str, attrs: dict) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"{host}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps({"Attributes": attrs}, indent=2))
    return path


def _backup_path_or_400(name: str) -> Path:
    path = BACKUP_DIR / Path(name).name  # strip any directory components
    if not path.is_file():
        raise HTTPException(404, f"backup file not found: {path.name}")
    return path


@app.get("/api/activities")
def api_activities():
    conn = db.connect()
    try:
        return db.list_activities(conn)
    finally:
        conn.close()


@app.post("/api/devices/{device_id}/bios/backup")
async def api_bios_backup(device_id: int):
    """Save current BIOS Attributes to a JSON file (pre-update preserve)."""
    dev = _get_device_or_404(device_id)
    act = _log_activity(device_id, "backup", "act_backup_start")
    client = make_client(dev)
    try:
        attrs = await client.bios_attributes()
    except Exception as e:
        _finish_activity(act, "failed", "act_backup_failed", str(e))
        raise HTTPException(502, f"備份失敗:{e}")
    finally:
        await client.close()
    path = _save_backup(dev["host"], attrs)
    _finish_activity(act, "success", "act_backup_success", len(attrs), path.name)
    return {"backup_file": path.name, "attribute_count": len(attrs)}


@app.get("/api/devices/{device_id}/bios/backups")
def api_bios_backups(device_id: int):
    dev = _get_device_or_404(device_id)
    if not BACKUP_DIR.is_dir():
        return []
    files = sorted(BACKUP_DIR.glob(f"{dev['host']}_*.json"), reverse=True)
    return [{"name": f.name, "size_kb": round(f.stat().st_size / 1024)} for f in files]


class RestoreBody(BaseModel):
    backup_file: str


@app.post("/api/devices/{device_id}/bios/restore/preview")
async def api_bios_restore_preview(device_id: int, body: RestoreBody):
    """Read-only diff of a backup vs current BIOS settings."""
    dev = _get_device_or_404(device_id)
    saved = json.loads(_backup_path_or_400(body.backup_file).read_text()).get("Attributes", {})
    client = make_client(dev)
    try:
        current = await client.bios_attributes()
    finally:
        await client.close()
    patch, skipped = build_bios_patch(saved, current)
    return {
        "differences": [
            {"key": k, "current": current[k], "saved": v}
            for k, v in sorted(patch.items())
        ],
        "skipped": skipped,
        "total_saved": len(saved),
    }


@app.post("/api/devices/{device_id}/bios/restore")
async def api_bios_restore(device_id: int, body: RestoreBody):
    """Diff a backup vs current settings and PATCH the differences to Bios/SD."""
    dev = _get_device_or_404(device_id)
    saved = json.loads(_backup_path_or_400(body.backup_file).read_text()).get("Attributes", {})
    act = _log_activity(device_id, "restore", "act_restore_start", body.backup_file)
    client = make_client(dev)
    try:
        current = await client.bios_attributes()
        patch, skipped = build_bios_patch(saved, current)
        if not patch:
            _finish_activity(act, "success", "act_restore_noop")
            return {"restored": 0, "skipped": len(skipped), "message": "設定與備份一致,無需還原"}
        await client.patch_bios_settings(patch)
    except HTTPException:
        raise
    except Exception as e:
        _finish_activity(act, "failed", "act_restore_failed", str(e))
        raise HTTPException(502, f"還原失敗:{e}")
    finally:
        await client.close()
    _finish_activity(act, "success", "act_restore_success", len(patch), body.backup_file)
    return {"restored": len(patch), "skipped": len(skipped),
            "keys": sorted(patch), "message": "還原完成,主機重開機後生效"}


@app.post("/api/devices/{device_id}/bios/update")
async def api_bios_update(device_id: int,
                          file: UploadFile = File(...),
                          preserve: bool = Form(False)):
    """Upload a BIOS image to the BMC; returns the BMC task id to poll."""
    dev = _get_device_or_404(device_id)
    content = await file.read()
    if not content:
        raise HTTPException(400, "empty file")

    # BIOS flashing requires the host to be powered off
    conn = db.connect()
    profile = db.get_profile(conn, device_id)
    conn.close()
    system_path = (profile or {}).get("systems", [{}])[0].get("path", "/redfish/v1/Systems/Self")
    precheck = make_client(dev)
    try:
        power_state = (await precheck.get(system_path)).get("PowerState")
    finally:
        await precheck.close()
    if power_state != "Off":
        _log_activity(device_id, "update", "act_update_rejected", file.filename, power_state,
                      status="failed")
        raise HTTPException(409, f"主機電源狀態為 {power_state},BIOS 更新前請先將主機關機")

    act = _log_activity(device_id, "update", "act_update_start",
                        file.filename, round(len(content) / 1e6, 1))
    backup_file = None
    client = make_client(dev)
    try:
        if preserve:
            attrs = await client.bios_attributes()
            backup_file = _save_backup(dev["host"], attrs).name
        current_version = await client.firmware_version("BIOS")
        try:
            task_id = await client.upload_firmware(file.filename, content)
        except RuntimeError as e:
            _finish_activity(act, "failed", "act_update_upload_failed", str(e))
            raise HTTPException(502, str(e))
        if backup_file:
            _finish_activity(act, "running", "act_update_uploaded_bak",
                             file.filename, task_id, backup_file, task_id=task_id)
        else:
            _finish_activity(act, "running", "act_update_uploaded",
                             file.filename, task_id, task_id=task_id)
        return {
            "task_id": task_id,
            "current_version": current_version,
            "backup_file": backup_file,
            "size_mb": round(len(content) / 1e6, 1),
        }
    except HTTPException:
        raise
    except Exception as e:
        _finish_activity(act, "failed", "act_update_failed", str(e))
        raise HTTPException(502, f"更新失敗:{e}")
    finally:
        await client.close()


@app.get("/api/devices/{device_id}/bios/task/{task_id}")
async def api_bios_task(device_id: int, task_id: str):
    dev = _get_device_or_404(device_id)
    client = make_client(dev)
    try:
        status = await client.update_task_status(task_id)
    except Exception as e:
        # BMC may drop connections briefly during the flash — report, don't 500
        return {"state": None, "error": str(e)}
    finally:
        await client.close()

    # close out the running update activity when the task reaches a final state
    if status.get("state") in ("Completed", "Exception"):
        conn = db.connect()
        try:
            act = db.find_running_update_by_task(conn, device_id, task_id)
            if act:
                if status["state"] == "Completed":
                    db.update_activity(conn, act, "success",
                                       "act_update_task_complete", [task_id])
                else:
                    db.update_activity(conn, act, "failed",
                                       "act_update_task_failed", [task_id])
        finally:
            conn.close()
    return status


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
