"""
util/gps.py — NMEA GPS reader.

Identical logic to RPi version; CLOCAL fix is kept (harmless on UARTs that
don't need it, critical on those that do).  Device is configurable (/dev/ttyS3
on Luckfox Pico Ultra with UART3 wired to GPS module).
"""

import logging
import termios
import threading
import time
from dataclasses import dataclass
from typing import Optional

import serial

log = logging.getLogger(__name__)


@dataclass
class GPSFix:
    lat: float
    lon: float
    valid: bool


def _nmea_checksum_ok(sentence: str) -> bool:
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    data, checksum = sentence[1:].rsplit("*", 1)
    calc = 0
    for c in data:
        calc ^= ord(c)
    try:
        return calc == int(checksum.strip(), 16)
    except ValueError:
        return False


class GPSReader:
    def __init__(self, config: dict):
        gps_cfg = config["gps"]
        self._device = gps_cfg.get("device", "/dev/ttyS3")
        self._baud = int(gps_cfg.get("baud_rate", 9600))
        self._fix: Optional[GPSFix] = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)

    def start(self):
        self._thread.start()
        log.info("GPS reader started on %s @ %d baud", self._device, self._baud)

    def get_fix(self) -> Optional[GPSFix]:
        with self._lock:
            return self._fix

    def get_coordinates(self):
        fix = self.get_fix()
        if fix and fix.valid:
            return fix.lat, fix.lon
        return None, None

    def _reader_loop(self):
        was_fixed = False
        consecutive_errors = 0
        while True:
            try:
                ser = serial.Serial(
                    self._device, self._baud, timeout=2,
                    xonxoff=False, rtscts=False, dsrdtr=False,
                )
                # CLOCAL: ignore DCD/modem-control lines (needed on UART without
                # hardware handshake lines asserted, prevents empty reads)
                attrs = termios.tcgetattr(ser.fileno())
                attrs[2] |= termios.CLOCAL
                attrs[2] &= ~termios.CRTSCTS
                termios.tcsetattr(ser.fileno(), termios.TCSANOW, attrs)

                consecutive_errors = 0
                log.info("GPS serial opened: %s", self._device)
                while True:
                    try:
                        raw = ser.readline().decode("ascii", errors="ignore")
                    except serial.SerialException:
                        log.warning("GPS serial read error; reopening")
                        break

                    # Strip garbage before first '$'
                    dollar = raw.find("$")
                    if dollar < 0:
                        continue
                    sentence = raw[dollar:].strip()
                    if not sentence or not _nmea_checksum_ok(sentence):
                        continue

                    parts = sentence.split(",")
                    if parts[0] in ("$GPRMC", "$GNRMC"):
                        if len(parts) >= 7 and parts[2] == "A":
                            lat = self._parse_coord(parts[3], parts[4])
                            lon = self._parse_coord(parts[5], parts[6].split("*")[0])
                            if lat is not None and lon is not None:
                                with self._lock:
                                    new_fix = GPSFix(lat=lat, lon=lon, valid=True)
                                    if not was_fixed:
                                        log.info("GPS fix acquired: %.6f, %.6f", lat, lon)
                                    else:
                                        log.debug("GPS fix: %.6f, %.6f", lat, lon)
                                    was_fixed = True
                                    self._fix = new_fix
                        else:
                            with self._lock:
                                if was_fixed:
                                    log.warning("GPS fix lost")
                                was_fixed = False
                                self._fix = GPSFix(lat=0.0, lon=0.0, valid=False)

            except serial.SerialException as e:
                consecutive_errors += 1
                if consecutive_errors == 1:
                    log.warning(
                        "GPS unavailable (%s) -- retrying every 30s "
                        "(further messages suppressed)", self._device)
                else:
                    log.debug("GPS retry %d: %s", consecutive_errors, e)
                time.sleep(30)
            except Exception as e:
                log.error("GPS unexpected error: %s", e, exc_info=True)
                time.sleep(30)

    @staticmethod
    def _parse_coord(value: str, direction: str) -> Optional[float]:
        if not value or not direction:
            return None
        try:
            deg = float(value[:2]) if direction in "NS" else float(value[:3])
            minutes = float(value[2:]) if direction in "NS" else float(value[3:])
            decimal = deg + minutes / 60.0
            if direction in "SW":
                decimal = -decimal
            return decimal
        except (ValueError, IndexError):
            return None