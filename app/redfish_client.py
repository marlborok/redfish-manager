"""Async Redfish client with per-device capability discovery.

discover() walks the device's Redfish tree once and produces a "profile"
describing what the device actually supports (systems, chassis, sub-resources,
allowed reset types). snapshot(profile) then polls only what exists.
"""
import json
import re

import httpx

# system sub-resources worth recording in the profile
SYSTEM_SUBRESOURCES = (
    "Processors", "Memory", "Storage", "SimpleStorage",
    "EthernetInterfaces", "LogServices", "Bios", "SecureBoot",
)
CHASSIS_SUBRESOURCES = ("Thermal", "Power", "Sensors", "PCIeDevices")

# BIOS restore: keys containing these substrings are skipped (string format
# changes between BIOS versions cause false diffs — from MCT's restore script)
BIOS_RESTORE_IGNORE = (
    "CbsCmnCxlComponentErrorReporting",
    "CbsCmnFchSystemPwrFailShadow",
    "IPMI610",
)


def build_bios_patch(saved: dict, current: dict) -> tuple[dict, list[str]]:
    """Diff saved vs current BIOS attributes.

    Returns (patch, skipped): patch = keys in both whose values differ,
    with the saved values; skipped = saved keys missing from current BIOS.
    """
    patch, skipped = {}, []
    for key, saved_val in saved.items():
        if key == "MAPIDS":
            continue
        if key not in current:
            skipped.append(key)
            continue
        if any(ig in key for ig in BIOS_RESTORE_IGNORE):
            continue
        if current[key] != saved_val:
            patch[key] = saved_val
    return patch, skipped


def _ref(obj, key):
    v = obj.get(key)
    if isinstance(v, dict) and "@odata.id" in v:
        return v["@odata.id"]
    return None


class RedfishClient:
    def __init__(self, host: str, username: str, password: str, timeout: float = 20.0):
        self.base = f"https://{host}"
        self._client = httpx.AsyncClient(
            base_url=self.base,
            auth=(username, password),
            verify=False,
            timeout=timeout,
        )

    async def close(self):
        await self._client.aclose()

    async def get(self, path: str) -> dict:
        r = await self._client.get(path)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, body: dict) -> httpx.Response:
        r = await self._client.post(path, json=body)
        r.raise_for_status()
        return r

    # ------------------------------------------------------------------
    # Discovery: walk the tree, build a device profile
    # ------------------------------------------------------------------

    async def _collection_members(self, path: str) -> list[str]:
        col = await self.get(path)
        return [m["@odata.id"] for m in col.get("Members", [])]

    async def _discover_log_services(self, resource: dict, owner: str) -> list[dict]:
        """Collect log services (SEL, event/audit logs) under a resource."""
        out = []
        ls_ref = _ref(resource, "LogServices")
        if not ls_ref:
            return out
        for path in await self._collection_members(ls_ref):
            svc = await self.get(path)
            clear = ((svc.get("Actions") or {}).get("#LogService.ClearLog") or {})
            out.append({
                "owner": owner,
                "path": path,
                "id": svc.get("Id"),
                "name": svc.get("Name"),
                "entries": _ref(svc, "Entries"),
                "clear_target": clear.get("target"),
                "max_records": svc.get("MaxNumberOfRecords"),
            })
        return out

    async def discover(self) -> dict:
        root = await self.get("/redfish/v1/")
        profile = {
            "vendor": root.get("Vendor"),
            "product": root.get("Product"),
            "redfish_version": root.get("RedfishVersion"),
            "supports_expand": bool(
                (root.get("ProtocolFeaturesSupported") or {}).get("ExpandQuery")
            ),
            "systems": [],
            "chassis": [],
            "managers": [],
            "log_services": [],
        }

        for path in await self._collection_members(_ref(root, "Systems") or "/redfish/v1/Systems"):
            sys = await self.get(path)
            entry = {
                "path": path,
                "id": sys.get("Id"),
                "resources": {k: _ref(sys, k) for k in SYSTEM_SUBRESOURCES if _ref(sys, k)},
                "reset_target": None,
                "reset_types": [],
            }
            action = (sys.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
            entry["reset_target"] = action.get("target")
            if "ResetType@Redfish.AllowableValues" in action:
                entry["reset_types"] = action["ResetType@Redfish.AllowableValues"]
            elif action.get("@Redfish.ActionInfo"):
                info = await self.get(action["@Redfish.ActionInfo"])
                for p in info.get("Parameters", []):
                    if p.get("Name") == "ResetType":
                        entry["reset_types"] = p.get("AllowableValues", [])
            profile["systems"].append(entry)
            profile["log_services"] += await self._discover_log_services(
                sys, f"System {sys.get('Id')}")

        if _ref(root, "Chassis"):
            for path in await self._collection_members(_ref(root, "Chassis")):
                ch = await self.get(path)
                profile["chassis"].append({
                    "path": path,
                    "id": ch.get("Id"),
                    "resources": {k: _ref(ch, k) for k in CHASSIS_SUBRESOURCES if _ref(ch, k)},
                })
                profile["log_services"] += await self._discover_log_services(
                    ch, f"Chassis {ch.get('Id')}")

        if _ref(root, "Managers"):
            for path in await self._collection_members(_ref(root, "Managers")):
                mgr = await self.get(path)
                profile["managers"].append({
                    "path": path,
                    "id": mgr.get("Id"),
                    "model": mgr.get("Model"),
                    "firmware_version": mgr.get("FirmwareVersion"),
                })
                profile["log_services"] += await self._discover_log_services(
                    mgr, f"BMC {mgr.get('Id')}")

        return profile

    # ------------------------------------------------------------------
    # Snapshot: poll according to the profile
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_system(sys: dict) -> dict:
        status = sys.get("Status") or {}
        return {
            "id": sys.get("Id"),
            "manufacturer": (sys.get("Manufacturer") or "").strip(),
            "model": (sys.get("Model") or "").strip(),
            "serial": (sys.get("SerialNumber") or "").strip(),
            "bios_version": sys.get("BiosVersion"),
            "power_state": sys.get("PowerState"),
            "health": status.get("HealthRollup") or status.get("Health"),
            "cpu": {
                "count": (sys.get("ProcessorSummary") or {}).get("Count"),
                "model": ((sys.get("ProcessorSummary") or {}).get("Model") or "").strip(),
            },
            "memory_gib": (sys.get("MemorySummary") or {}).get("TotalSystemMemoryGiB"),
        }

    async def snapshot(self, profile: dict) -> dict:
        snap = {"systems": [], "chassis": []}

        for s in profile.get("systems", []):
            data = await self.get(s["path"])
            snap["systems"].append(self._normalize_system(data))

        for c in profile.get("chassis", []):
            res = c.get("resources", {})
            entry = {"id": c["id"], "temperatures": [], "fans": [], "psus": [], "sensors": []}

            if res.get("Thermal"):
                thermal = await self.get(res["Thermal"])
                entry["temperatures"] = [
                    {
                        "name": t.get("Name"),
                        "celsius": t.get("ReadingCelsius"),
                        "health": (t.get("Status") or {}).get("Health"),
                        "upper_critical": t.get("UpperThresholdCritical"),
                    }
                    for t in thermal.get("Temperatures", [])
                    if t.get("ReadingCelsius") is not None
                ]
                entry["fans"] = [
                    {
                        "name": f.get("Name"),
                        "rpm": f.get("Reading"),
                        "health": (f.get("Status") or {}).get("Health"),
                    }
                    for f in thermal.get("Fans", [])
                    if f.get("Reading") is not None
                ]

            if res.get("Power"):
                power = await self.get(res["Power"])
                entry["psus"] = [
                    {
                        "name": p.get("Name"),
                        "watts": p.get("PowerInputWatts") or p.get("PowerOutputWatts"),
                        "health": (p.get("Status") or {}).get("Health"),
                    }
                    for p in power.get("PowerSupplies", [])
                ]

            if res.get("Sensors"):
                path = res["Sensors"]
                if profile.get("supports_expand"):
                    col = await self.get(path + "?$expand=.")
                    members = col.get("Members", [])
                else:
                    members = [await self.get(p) for p in await self._collection_members(path)]
                entry["sensors"] = [
                    {
                        "name": m.get("Name"),
                        "reading": m.get("Reading"),
                        "units": m.get("ReadingUnits"),
                        "type": m.get("ReadingType"),
                        "health": (m.get("Status") or {}).get("Health"),
                    }
                    for m in members
                    if m.get("Reading") is not None
                ]

            snap["chassis"].append(entry)

        return snap

    async def system_reset(self, target: str, reset_type: str) -> httpx.Response:
        return await self.post(target, {"ResetType": reset_type})

    # ------------------------------------------------------------------
    # Log services (SEL, event/audit logs)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_log_entry(m: dict) -> dict:
        msg = m.get("Message") or ""
        # IPMI SEL messages pack the useful info into "Event_Type : xxx,"
        ev = re.search(r"Event_Type\s*:\s*([^,]+)", msg)
        return {
            "id": m.get("Id"),
            "created": m.get("Created"),
            "severity": m.get("Severity"),
            "sensor_type": m.get("SensorType"),
            "event": ev.group(1).strip() if ev else None,
            "entry_code": m.get("EntryCode"),
            "message": msg,
        }

    async def log_entries(self, entries_path: str, limit: int = 100) -> dict:
        """Fetch the newest `limit` entries of a log service, newest first."""
        probe = await self.get(f"{entries_path}?$top=1")
        total = probe.get("Members@odata.count") or 0
        skip = max(0, total - limit)
        # AMI rejects $skip=0 with 400 — only send it when actually skipping
        query = f"?$top={limit}" + (f"&$skip={skip}" if skip else "")
        data = await self.get(entries_path + query)
        members = [self._normalize_log_entry(m) for m in data.get("Members", [])]
        members.reverse()
        return {"total": total, "entries": members}

    async def clear_log(self, clear_target: str) -> httpx.Response:
        return await self.post(clear_target, {})

    # ------------------------------------------------------------------
    # BIOS firmware update (AMI MegaRAC-specific upload endpoint)
    # ------------------------------------------------------------------

    async def bios_attributes(self, bios_path: str = "/redfish/v1/Systems/Self/Bios") -> dict:
        """Current BIOS settings, minus the AMI-internal MAPIDS blob."""
        data = await self.get(bios_path)
        attrs = data.get("Attributes", {})
        attrs.pop("MAPIDS", None)
        return attrs

    async def firmware_version(self, target: str = "BIOS") -> str | None:
        data = await self.get(f"/redfish/v1/UpdateService/FirmwareInventory/{target}")
        return data.get("Version")

    async def upload_firmware(self, filename: str, content: bytes,
                              target: str = "BIOS", image_type: str = "BIOS") -> str:
        """POST an image to the AMI upload endpoint; return the created task id."""
        update_params = json.dumps(
            {"Targets": [f"/redfish/v1/UpdateService/FirmwareInventory/{target}"]}
        )
        oem_params = json.dumps({"ImageType": image_type})
        r = await self._client.post(
            "/redfish/v1/UpdateService/upload",
            files=[
                ("UpdateFile", (filename, content, "application/octet-stream")),
                ("UpdateParameters", (None, update_params, "application/json")),
                ("OemParameters", (None, oem_params, "application/json")),
            ],
            timeout=600.0,
        )
        body = r.text
        # older SPX: "A new task /redfish/v1/TaskService/Tasks/<id> ..."
        m = re.search(r"A new task /redfish/v1/TaskService/Tasks/(\d+)", body)
        if not m:
            # SPX 13.8: {"@odata.id": ".../TaskService/Tasks/<id>", ...}
            m = re.search(r'"@odata\.id"\s*:\s*"[^"]*/(\d+)"', body)
        if not m:
            raise RuntimeError(f"no task id in upload response (HTTP {r.status_code}): {body[:300]}")
        return m.group(1)

    async def patch_bios_settings(self, patch: dict,
                                  sd_path: str = "/redfish/v1/Systems/Self/Bios/SD") -> httpx.Response:
        """PATCH pending BIOS settings; takes effect after a host power reset."""
        r = await self._client.patch(
            sd_path,
            json={"Attributes": patch},
            headers={"If-Match": "*", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r

    async def update_task_status(self, task_id: str) -> dict:
        task = await self.get(f"/redfish/v1/TaskService/Tasks/{task_id}")
        try:
            us = await self.get("/redfish/v1/UpdateService")
            percentage = ((us.get("Oem") or {}).get("AMIUpdateService") or {}).get(
                "FlashPercentage") or us.get("FlashPercentage")
        except httpx.HTTPError:
            percentage = None
        return {
            "state": task.get("TaskState"),
            "status": task.get("TaskStatus"),
            "percent_complete": task.get("PercentComplete"),
            "flash_percentage": percentage,
        }
