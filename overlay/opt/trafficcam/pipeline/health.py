"""
health.py — Luckfox Pico Ultra adaptation.

Key changes from RPi version:
- Hardware watchdog via /dev/watchdog (no systemd sd_notify on Buildroot SysV init)
- Thermal: try multiple /sys/class/thermal zones
- Same storage, GPS health checks
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

_THERMAL_ZONES = [
    "/sys/class/thermal/thermal_zone0/temp",
    "/sys/class/thermal/thermal_zone1/temp",
]


def _read_temp() -> float | None:
    for z in _THERMAL_ZONES:
        try:
            return float(Path(z).read_text().strip()) / 1000.0
        except Exception:
            continue
    return None


class HealthMonitor:
    def __init__(self, config: dict, gps=None):
        h = config["health"]
        self._max_temp = float(h.get("max_temp_celsius", 85.0))
        self._min_free_gb = float(h.get("min_free_sd_gb", 0.5))
        self._wdt_interval = int(h.get("watchdog_interval_seconds", 15))
        self._check_interval = int(h.get("health_check_interval_seconds", 30))
        self._gps_timeout = int(config["gps"].get("no_fix_timeout_seconds", 300))
        self._gps = gps
        self._raw_dir = Path(config["storage"]["raw_dir"])
        self._wdt_fd = None

    async def run(self):
        # Open hardware watchdog
        try:
            self._wdt_fd = open("/dev/watchdog", "w", buffering=1)
            log.info("Hardware watchdog opened at /dev/watchdog")
        except Exception as e:
            log.warning("Could not open /dev/watchdog: %s — running without HW watchdog", e)

        asyncio.create_task(self._watchdog_task())
        asyncio.create_task(self._health_check_task())

    async def _watchdog_task(self):
        """Keep hardware watchdog alive by writing to it periodically."""
        while True:
            if self._wdt_fd:
                try:
                    self._wdt_fd.write("1")
                    self._wdt_fd.flush()
                except Exception as e:
                    log.warning("Watchdog write failed: %s", e)
            await asyncio.sleep(self._wdt_interval)

    async def _health_check_task(self):
        gps_fix_since = None
        while True:
            await asyncio.sleep(self._check_interval)

            # Thermal check
            temp = _read_temp()
            if temp is not None:
                if temp >= self._max_temp:
                    log.critical("THERMAL SHUTDOWN: %.1f°C >= %.1f°C",
                                 temp, self._max_temp)
                    os.system("sync && halt")
                elif temp >= self._max_temp - 5:
                    log.warning("High temperature: %.1f°C", temp)

            # Storage check
            try:
                st = shutil.disk_usage(self._raw_dir)
                free_gb = st.free / (1024 ** 3)
                if free_gb < self._min_free_gb:
                    log.warning("Low storage: %.2f GB free (min %.2f GB)",
                                free_gb, self._min_free_gb)
                    self._purge_old_files()
            except Exception as e:
                log.warning("Storage check failed: %s", e)

            # GPS fix staleness
            if self._gps:
                fix = self._gps.get_fix()
                if fix and fix.valid:
                    gps_fix_since = time.time()
                else:
                    if gps_fix_since and (time.time() - gps_fix_since) > self._gps_timeout:
                        log.warning("GPS fix lost for >%ds", self._gps_timeout)

    def _purge_old_files(self):
        for d in [self._raw_dir.parent / "processed", self._raw_dir.parent / "queue"]:
            try:
                files = sorted(d.glob("*"), key=lambda p: p.stat().st_mtime)
                while files:
                    st = shutil.disk_usage(self._raw_dir)
                    if st.free / (1024 ** 3) >= self._min_free_gb * 2:
                        break
                    oldest = files.pop(0)
                    oldest.unlink(missing_ok=True)
                    log.info("Purged old file: %s", oldest.name)
            except Exception as e:
                log.warning("Purge failed in %s: %s", d, e)

    def close(self):
        if self._wdt_fd:
            # Write 'V' magic to cleanly disable watchdog on shutdown
            try:
                self._wdt_fd.write("V")
                self._wdt_fd.flush()
                self._wdt_fd.close()
            except Exception:
                pass
