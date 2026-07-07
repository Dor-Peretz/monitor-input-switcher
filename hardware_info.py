"""Windows monitor hardware IDs and Dell driver lookup."""

from __future__ import annotations

import re
import subprocess
import webbrowser
from dataclasses import dataclass

from ddc_monitor import MonitorInfo

DELL_MONITOR_DRIVERS: dict[str, dict[str, str]] = {
    "DELF122": {
        "model": "Dell P2721Q",
        "driver_url": "https://www.dell.com/support/home/drivers/driversdetails?driverid=m52f1",
        "notes": "Install the Dell monitor INF, then enable DDC/CI in the monitor OSD.",
    },
    "DELF13B": {
        "model": "Dell P2723QE",
        "driver_url": "https://www.dell.com/support/home/drivers/driversdetails?driverid=HWX61",
        "notes": "Enable DDC/CI under Menu → Others in the monitor OSD.",
    },
    "DEL41B3": {
        "model": "Dell U2720Q",
        "driver_url": "https://www.dell.com/support/home/drivers/driversdetails?driverid=CRXHT",
        "notes": "Enable DDC/CI under Menu → Others in the monitor OSD.",
    },
}

DELL_DISPLAY_MANAGER_URL = (
    "https://www.dell.com/support/home/drivers/driversdetails?driverid=g0v75"
)


@dataclass
class PnpMonitor:
    name: str
    device_id: str
    hardware_id: str
    manufacturer: str
    uses_generic_driver: bool
    detected_model: str
    driver_url: str | None
    driver_notes: str


def _hardware_id_from_device_id(device_id: str) -> str:
    match = re.search(r"DISPLAY\\([^\\]+)\\", device_id, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _query_pnp_monitors() -> list[PnpMonitor]:
    script = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.DeviceID -like 'DISPLAY\\*' } | "
        "Select-Object Name, Manufacturer, DeviceID | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    import json

    if not result.stdout.strip():
        return []

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    if isinstance(payload, dict):
        payload = [payload]

    monitors: list[PnpMonitor] = []
    for item in payload:
        name = str(item.get("Name") or "Unknown monitor")
        device_id = str(item.get("DeviceID") or "")
        manufacturer = str(item.get("Manufacturer") or "")
        hardware_id = _hardware_id_from_device_id(device_id)
        driver_info = DELL_MONITOR_DRIVERS.get(hardware_id, {})
        uses_generic = "generic pnp monitor" in name.lower()
        monitors.append(
            PnpMonitor(
                name=name,
                device_id=device_id,
                hardware_id=hardware_id,
                manufacturer=manufacturer,
                uses_generic_driver=uses_generic,
                detected_model=driver_info.get("model", ""),
                driver_url=driver_info.get("driver_url"),
                driver_notes=driver_info.get("notes", ""),
            )
        )
    return monitors


def _name_overlap(a: str, b: str) -> bool:
    a_lower = a.lower()
    b_lower = b.lower()
    if a_lower in b_lower or b_lower in a_lower:
        return True
    for token in ("p2723qe", "u2720q", "p2721q", "dell"):
        if token in a_lower and token in b_lower:
            return True
    return False


def match_pnp_to_monitor(monitor: MonitorInfo, pnp_devices: list[PnpMonitor]) -> PnpMonitor | None:
    for pnp in pnp_devices:
        if _name_overlap(monitor.description, pnp.name):
            return pnp

    if "generic" in monitor.description.lower():
        generic_devices = [p for p in pnp_devices if p.uses_generic_driver]
        if len(generic_devices) == 1:
            return generic_devices[0]
    return None


def enrich_monitor(monitor: MonitorInfo, pnp_devices: list[PnpMonitor]) -> dict:
    pnp = match_pnp_to_monitor(monitor, pnp_devices)
    if pnp is None:
        return {
            "pnp_name": "",
            "hardware_id": "",
            "detected_model": "",
            "driver_url": None,
            "driver_notes": "",
            "uses_generic_driver": False,
        }

    return {
        "pnp_name": pnp.name,
        "hardware_id": pnp.hardware_id,
        "detected_model": pnp.detected_model or pnp.name,
        "driver_url": pnp.driver_url,
        "driver_notes": pnp.driver_notes,
        "uses_generic_driver": pnp.uses_generic_driver,
    }


def controllable_reason(monitor: MonitorInfo, extra: dict) -> str:
    if monitor.controllable:
        if extra.get("uses_generic_driver"):
            return (
                "DDC/CI available, but Windows reports a generic driver. "
                "Install the Dell driver for best results."
            )
        return "Ready — DDC/CI input switching available."

    if not monitor.handle:
        if extra.get("uses_generic_driver") and extra.get("driver_url"):
            model = extra.get("detected_model") or "this monitor"
            return (
                f"Windows is using a generic driver for {model}. "
                "Install the Dell monitor driver and enable DDC/CI in the OSD."
            )
        if "generic pnp" in monitor.description.lower():
            return (
                "No DDC/CI handle — may be a laptop internal screen, "
                "which cannot switch video inputs."
            )
        return "No DDC/CI handle from Windows. Enable DDC/CI in the monitor OSD."

    return "DDC/CI handle present but input control is unavailable."


def get_pnp_monitors() -> list[PnpMonitor]:
    return _query_pnp_monitors()


def open_driver_page(url: str | None) -> None:
    if url:
        webbrowser.open(url)


def open_device_manager_monitors() -> None:
    subprocess.Popen(["mmc", "devmgmt.msc"])
