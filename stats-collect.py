#!/usr/bin/env python3
"""Emit one JSON line of system stats for the blitz.stats bar widget.

Usage: stats-collect.py <fast|temp|disk|net|gpu>

  fast - CPU % (1s /proc/stat delta) + RAM % and sizes + NIC RX/TX rates
         sampled across the same 1s window; takes ~1s to run
  temp - CPU package temperature via `sensors -j` (k10temp/coretemp/zenpower)
  disk - root filesystem usage via df
  net  - active interface name + link speed in Mbps; when the default route
         rides a virtual device (wireguard, bridges, veths...) the physical
         NIC underneath is reported instead, since tunnels have no link speed
  gpu  - GPU utilization + temperature via nvidia-smi
"""

import json
import os
import re
import subprocess
import sys
import time

GiB = 1048576.0  # /proc/meminfo is in kB


def emit(payload):
    print(json.dumps(payload))


def cpu_sample():
    fields = [int(v) for v in open("/proc/stat").readline().split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    return idle, sum(fields)


def nic_counters(iface):
    if not iface:
        return 0, 0
    try:
        base = f"/sys/class/net/{iface}/statistics"
        return int(open(f"{base}/rx_bytes").read()), int(open(f"{base}/tx_bytes").read())
    except OSError:
        return 0, 0


def section_fast():
    iface = resolve_iface()
    idle1, total1 = cpu_sample()
    rx1, tx1 = nic_counters(iface)
    time.sleep(1.0)
    idle2, total2 = cpu_sample()
    rx2, tx2 = nic_counters(iface)
    dt = total2 - total1
    cpu = round(100.0 * (1.0 - (idle2 - idle1) / dt)) if dt > 0 else 0

    info = {}
    for line in open("/proc/meminfo"):
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0])
    total = info["MemTotal"]
    used = total - info.get("MemAvailable", 0)
    emit({
        "cpu": max(0, min(100, cpu)),
        "memPct": round(100.0 * used / total),
        "memUsedGB": round(used / GiB, 1),
        "memTotalGB": round(total / GiB, 1),
        "iface": iface,
        "rxBytesPerSec": max(0, rx2 - rx1),
        "txBytesPerSec": max(0, tx2 - tx1),
    })


CPU_CHIPS = ("k10temp", "coretemp", "zenpower", "cpu_thermal")


def section_temp():
    try:
        chips = json.loads(subprocess.check_output(["sensors", "-j"], text=True, stderr=subprocess.DEVNULL))
    except Exception:
        emit({"tempC": None})
        return
    readings = []
    for chip, block in chips.items():
        if not chip.startswith(CPU_CHIPS) or not isinstance(block, dict):
            continue
        for sensor in block.values():
            if not isinstance(sensor, dict):
                continue
            for key, value in sensor.items():
                if key.startswith("temp") and key.endswith("_input") and isinstance(value, (int, float)):
                    readings.append(value)
    emit({"tempC": round(max(readings)) if readings else None})


def section_disk():
    out = subprocess.check_output(
        ["df", "-B1", "--output=used,size,pcent", "/"], text=True
    ).splitlines()[1].split()
    emit({
        "diskPct": int(out[2].rstrip("%")),
        "diskUsedGB": round(int(out[0]) / GiB, 0),
        "diskTotalGB": round(int(out[1]) / GiB, 0),
    })


VIRTUAL_PREFIXES = ("wg", "virbr", "br-", "veth", "tailscale", "docker", "lo", "zt", "ppp", "tap", "tun")


def link_speed(iface):
    try:
        speed = int(open(f"/sys/class/net/{iface}/speed").read().strip())
        return speed if speed > 0 else None
    except (OSError, ValueError):
        return None


def physical_iface_with_carrier():
    for iface in sorted(os.listdir("/sys/class/net")):
        if iface.startswith(VIRTUAL_PREFIXES):
            continue
        try:
            if int(open(f"/sys/class/net/{iface}/carrier").read().strip()) != 1:
                continue
        except (OSError, ValueError):
            continue
        if link_speed(iface):
            return iface
    return None


def resolve_iface():
    """The physical NIC actually carrying traffic: follow the default route,
    and when it rides a virtual device report the carrier-backed NIC instead."""
    iface = None
    try:
        route = subprocess.check_output(
            ["ip", "route", "get", "1.1.1.1"], text=True, stderr=subprocess.DEVNULL
        )
        match = re.search(r"\bdev (\S+)", route)
        if match:
            iface = match.group(1)
    except Exception:
        pass

    if iface and not iface.startswith(VIRTUAL_PREFIXES) and link_speed(iface):
        return iface
    return physical_iface_with_carrier()


def section_net():
    iface = resolve_iface()
    emit({"iface": iface, "mbps": link_speed(iface) if iface else None})


def section_gpu():
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        util, temp = out.splitlines()[0].split(",")
        emit({"gpuPct": int(util.strip()), "gpuTempC": int(temp.strip())})
    except Exception:
        emit({"gpuPct": None, "gpuTempC": None})


SECTIONS = {
    "fast": section_fast,
    "temp": section_temp,
    "disk": section_disk,
    "net": section_net,
    "gpu": section_gpu,
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SECTIONS:
        print(f"usage: {sys.argv[0]} <{'|'.join(SECTIONS)}>", file=sys.stderr)
        return 2
    try:
        SECTIONS[sys.argv[1]]()
    except Exception as exc:  # a dead widget section must never hang the bar
        print(f"stats-collect: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
