"""
util/modem.py  —  Quectel EC200U + carrier auto-detection.

Reads SIM carrier from IMSI (AT+CIMI) or operator name (AT+COPS), then sets:
  Jio    → APN = "jionet"          PDP type = "IPV4V6"  (Jio is IPv6-only on 4G)
  Airtel → APN = "airtelgprs.com"  PDP type = "IP"      (Airtel is IPv4)
  Vi     → APN = "www.viphone.co.in" PDP type = "IP"
  BSNL   → APN = "bsnlnet"         PDP type = "IP"
  Unknown→ uses config apn with "IP"

If config.json sets a non-empty apn, that APN is used but the PDP type
(IP vs IPV4V6) is still auto-detected from the SIM card.
IPv4V6 PPP also enables the +ipv6 pppd option for Jio.
"""

import logging
import re
import subprocess
import time
import threading
from typing import Optional, Tuple

import serial

log = logging.getLogger(__name__)

# ── Carrier database ──────────────────────────────────────────────────────
# IMSI prefix (first 5-6 digits = MCC+MNC) → (apn, pdp_type, name)
_PLMN = {
    '40440': ('jionet',             'IPV4V6', 'Jio'),
    '40450': ('jionet',             'IPV4V6', 'Jio'),
    '40585': ('jionet',             'IPV4V6', 'Jio'),
    '40410': ('airtelgprs.com',     'IP',     'Airtel'),
    '40445': ('airtelgprs.com',     'IP',     'Airtel'),
    '40449': ('airtelgprs.com',     'IP',     'Airtel'),
    '40470': ('airtelgprs.com',     'IP',     'Airtel'),
    '40487': ('airtelgprs.com',     'IP',     'Airtel'),
    '40492': ('airtelgprs.com',     'IP',     'Airtel'),
    '40494': ('airtelgprs.com',     'IP',     'Airtel'),
    '40420': ('www.viphone.co.in',  'IP',     'Vi'),
    '40427': ('www.viphone.co.in',  'IP',     'Vi'),
    '40467': ('www.viphone.co.in',  'IP',     'Vi'),
    '40471': ('bsnlnet',            'IP',     'BSNL'),
    '40474': ('bsnlnet',            'IP',     'BSNL'),
}
_NAME = {
    'jio':      ('jionet',            'IPV4V6', 'Jio'),
    'reliance': ('jionet',            'IPV4V6', 'Jio'),
    'airtel':   ('airtelgprs.com',    'IP',     'Airtel'),
    'vi ':      ('www.viphone.co.in', 'IP',     'Vi'),
    'vodafone': ('www.viphone.co.in', 'IP',     'Vi'),
    'idea':     ('www.viphone.co.in', 'IP',     'Vi'),
    'bsnl':     ('bsnlnet',           'IP',     'BSNL'),
}


class ModemManager:
    def __init__(self, config: dict):
        mdm = config['modem']
        self._device          = mdm.get('device', '/dev/ttyS4')
        self._baud            = int(mdm.get('baud_rate', 115200))
        self._cfg_apn         = mdm.get('apn', '').strip()
        self._ppp_user        = mdm.get('ppp_user', '')
        self._ppp_password    = mdm.get('ppp_password', '')
        self._connect_timeout = int(mdm.get('connect_timeout_seconds', 60))
        self._ppp_proc        = None
        self._connected       = False
        self._lock            = threading.Lock()

        # Resolved at first connect attempt
        self._apn             = self._cfg_apn or 'airtelgprs.com'  # safe default
        self._pdp_type        = 'IP'
        self._carrier         = 'unknown'
        self._carrier_detected = False

    def start(self):
        log.info('ModemManager ready (device: %s, APN: auto-detect pending)',
                 self._device)

    # ── AT helper ────────────────────────────────────────────────────────────

    def _at(self, cmd: str, wait: float = 1.0) -> str:
        try:
            with serial.Serial(self._device, self._baud, timeout=5) as s:
                s.reset_input_buffer()
                s.write((cmd + '\r\n').encode())
                time.sleep(wait)
                return s.read(512).decode(errors='ignore')
        except Exception as e:
            log.debug('AT %s failed: %s', cmd, e)
            return ''

    # ── Carrier detection ─────────────────────────────────────────────────

    def _detect_carrier(self):
        """Probe SIM via AT commands; update self._apn / _pdp_type / _carrier."""
        try:
            # 1. IMSI → PLMN prefix lookup
            resp = self._at('AT+CIMI', wait=2.0)
            m = re.search(r'\n(\d{15})', resp)
            if m:
                imsi = m.group(1)
                for plen in (6, 5):
                    entry = _PLMN.get(imsi[:plen])
                    if entry:
                        self._apply(*entry, f'IMSI {imsi[:plen]}')
                        return

            # 2. Operator name fallback
            resp = self._at('AT+COPS?', wait=2.0)
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', resp)
            if m:
                op = m.group(1).lower()
                for key, entry in _NAME.items():
                    if key in op:
                        self._apply(*entry, f'operator "{m.group(1)}"')
                        return

            # 3. Config APN fallback
            apn = self._cfg_apn or 'airtelgprs.com'
            pdp = 'IPV4V6' if 'jio' in apn.lower() else 'IP'
            self._apply(apn, pdp, 'config-fallback', apn)

        except Exception as e:
            log.warning('Carrier detect error: %s', e)
        finally:
            self._carrier_detected = True

    def _apply(self, apn: str, pdp: str, carrier: str, source: str):
        if self._cfg_apn:          # honour explicit config APN
            apn = self._cfg_apn
        self._apn      = apn
        self._pdp_type = pdp
        self._carrier  = carrier
        log.info('Carrier detected: %s (via %s) → APN=%s  PDP=%s',
                 carrier, source, apn, pdp)

    # ── Connectivity ─────────────────────────────────────────────────────────

    def is_online(self) -> bool:
        try:
            r = subprocess.run(['ping', '-c', '1', '-W', '3', '8.8.8.8'],
                               capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def ensure_connected(self) -> bool:
        if self.is_online():
            return True
        if not self._carrier_detected:
            self._detect_carrier()
        return self._start_ppp()

    # ── PPP ───────────────────────────────────────────────────────────────────

    def _start_ppp(self) -> bool:
        with self._lock:
            if self._connected:
                return True

            apn = self._apn
            pdp = self._pdp_type

            log.info('Starting PPP: carrier=%s  APN=%s  PDP=%s',
                     self._carrier, apn, pdp)

            # Build chat dial command with correct PDP type
            # Jio needs IPV4V6; Airtel/others need IP
            chat = (
                f"'/usr/sbin/chat -v -t 60 "
                f"ABORT BUSY ABORT ERROR ABORT \"NO CARRIER\" "
                f"\"\" ATZ "
                f"OK \"AT+CGDCONT=1,\\\"{pdp}\\\",\\\"{apn}\\\"\" "
                f"OK ATD*99# CONNECT \"\"'"
            )

            peer_conf = (
                'noauth\ndefaultroute\nusepeerdns\nnoipdefault\n'
            )
            # Jio dual-stack: enable IPv6 in pppd
            if pdp == 'IPV4V6':
                peer_conf += '+ipv6\n'

            peer_conf += f'connect {chat}\n'

            peer_path = '/tmp/ppp_modem_peer'
            try:
                with open(peer_path, 'w') as f:
                    f.write(peer_conf)
            except Exception as e:
                log.error('Cannot write PPP peer config: %s', e)
                return False

            try:
                self._ppp_proc = subprocess.Popen(
                    ['pppd', self._device, str(self._baud), 'file', peer_path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                deadline = time.time() + self._connect_timeout
                while time.time() < deadline:
                    if self.is_online():
                        log.info('PPP connected (APN=%s, %s)', apn, pdp)
                        self._connected = True
                        return True
                    time.sleep(2)

                # Dual-stack timeout → retry IPv4 only
                if pdp == 'IPV4V6':
                    log.warning('Dual-stack PPP timed out — retrying IPv4 only')
                    self._stop_ppp()
                    self._pdp_type = 'IP'
                    return self._start_ppp()

                log.warning('PPP timed out (APN=%s)', apn)
                self._stop_ppp()
                return False

            except Exception as e:
                log.error('PPP start failed: %s', e)
                return False

    def _stop_ppp(self):
        if self._ppp_proc:
            try:
                self._ppp_proc.terminate()
                self._ppp_proc.wait(timeout=5)
            except Exception:
                pass
            self._ppp_proc = None
        self._connected = False

    # ── LBS location ─────────────────────────────────────────────────────────

    def get_location(self) -> Tuple[Optional[float], Optional[float]]:
        """Cell-tower LBS via AT+CLBS (only when PPP not active)."""
        if self._connected:
            return None, None
        try:
            resp = self._at('AT+CLBS=1,1', wait=3.0)
            m = re.search(r'\+CLBS:\s*\d+,\s*([\d.]+),\s*([\d.]+)', resp)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                log.info('LBS location: %.6f, %.6f', lat, lon)
                return lat, lon
        except Exception as e:
            log.warning('LBS failed: %s', e)
        return None, None