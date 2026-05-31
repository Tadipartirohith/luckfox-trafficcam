"""
util/modem.py  —  Quectel EC200U carrier + AT-command bearer management.

PPP over UART is NOT used: after ATD*99# the EC200U-CN goes completely silent
on the UART (even LCP echo gets no response).  Instead we:
  1. Activate the PDP bearer with AT+CGACT=1,1  → real internet IP
  2. Check connectivity with AT+CGPADDR=1 (Airtel blocks ICMP on PDP context)
  3. Upload files via AT+QHTTPPUT with pre-signed S3 URLs
  4. Sync time via AT+QLTS=2

All HTTP operations keep ONE serial port open for the entire command sequence
(QHTTPCFG → QHTTPURL → QHTTPPUT) to avoid modem state-machine issues from
repeatedly opening/closing the port.  A reentrant lock (RLock) serialises
all serial-port access so the modem loop and the uploader thread don't race.

UART throughput at 115200 baud ≈ 11 KB/s.  Large video uploads should be
queued and deferred; small files (<500 KB) upload in ~45 s.
"""

import datetime
import logging
import re
import subprocess
import time
import threading
from typing import Optional, Tuple

import serial

log = logging.getLogger(__name__)

# ── Carrier database ──────────────────────────────────────────────────────
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

_SSL_CTX = 1          # Quectel SSL context index used for all HTTPS calls
_HTTP_GET_TIMEOUT = 60  # seconds for GET operations


class ModemManager:
    def __init__(self, config: dict):
        mdm = config['modem']
        self._device           = mdm.get('device', '/dev/ttyS4')
        self._baud             = int(mdm.get('baud_rate', 115200))
        self._cfg_apn          = mdm.get('apn', '').strip()
        self._connect_timeout  = int(mdm.get('connect_timeout_seconds', 60))
        # RLock: reentrant so http_put (which holds lock) can call _at (which also locks)
        self._lock             = threading.RLock()
        self._bearer_active    = False

        self._apn              = self._cfg_apn or 'airtelgprs.com'
        self._pdp_type         = 'IP'
        self._carrier          = 'unknown'
        self._carrier_detected = False

    def start(self):
        log.info('ModemManager ready (device: %s, APN: auto-detect pending)',
                 self._device)

    # ── AT helper (opens/closes port per command) ─────────────────────────────

    def _at(self, cmd: str, wait: float = 1.0, bufsize: int = 512) -> str:
        """Send AT command; poll for response.  Acquires _lock (RLock)."""
        with self._lock:
            try:
                with serial.Serial(self._device, self._baud, timeout=2.0) as s:
                    s.reset_input_buffer()
                    s.write((cmd + '\r\n').encode())
                    if wait <= 3.0:
                        time.sleep(wait)
                        return s.read(bufsize).decode(errors='ignore')
                    buf = bytearray()
                    deadline = time.time() + wait
                    while time.time() < deadline:
                        chunk = s.read(min(bufsize - len(buf), 512))
                        if chunk:
                            buf.extend(chunk)
                            text = bytes(buf)
                            if (b'\r\nOK\r\n' in text or b'\r\nERROR\r\n' in text
                                    or b'+CME ERROR' in text
                                    or b'+QHTTPGET:' in text
                                    or b'+QHTTPREAD:' in text
                                    or b'+QPING:' in text
                                    or b'CONNECT\r\n' in text):
                                break
                        else:
                            time.sleep(0.1)
                    return bytes(buf).decode(errors='ignore')
            except Exception as e:
                log.debug('AT %s failed: %s', cmd, e)
                return ''

    # ── AT helper (uses already-open port, no lock) ───────────────────────────

    @staticmethod
    def _scmd(s, cmd: str, wait_for: str = None, timeout: float = 8.0) -> str:
        """Send AT command on an open serial port; wait for expected string or timeout.
        Called from within methods that already hold _lock and have s open."""
        s.reset_input_buffer()
        s.write((cmd + '\r\n').encode())
        buf = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = s.read(256)
            if chunk:
                buf += chunk
                text = buf
                if wait_for and wait_for.encode() in text:
                    break
                if (b'\r\nOK\r\n' in text or b'\r\nERROR\r\n' in text
                        or b'+CME ERROR' in text or b'+CMS ERROR' in text):
                    break
        return buf.decode(errors='ignore')

    # ── Carrier detection ─────────────────────────────────────────────────────

    def _detect_carrier(self):
        try:
            resp = self._at('AT+CIMI', wait=2.0)
            m = re.search(r'\n(\d{15})', resp)
            if m:
                imsi = m.group(1)
                for plen in (6, 5):
                    entry = _PLMN.get(imsi[:plen])
                    if entry:
                        self._apply(*entry, f'IMSI {imsi[:plen]}')
                        return

            resp = self._at('AT+COPS?', wait=2.0)
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', resp)
            if m:
                op = m.group(1).lower()
                for key, entry in _NAME.items():
                    if key in op:
                        self._apply(*entry, f'operator "{m.group(1)}"')
                        return

            apn = self._cfg_apn or 'airtelgprs.com'
            pdp = 'IPV4V6' if 'jio' in apn.lower() else 'IP'
            self._apply(apn, pdp, 'config-fallback', apn)
        except Exception as e:
            log.warning('Carrier detect error: %s', e)
        finally:
            self._carrier_detected = True

    def _apply(self, apn: str, pdp: str, carrier: str, source: str):
        if self._cfg_apn:
            apn = self._cfg_apn
        self._apn      = apn
        self._pdp_type = pdp
        self._carrier  = carrier
        log.info('Carrier: %s (via %s) → APN=%s PDP=%s', carrier, source, apn, pdp)

    # ── Network registration ──────────────────────────────────────────────────

    def _init_modem(self) -> bool:
        log.info('Initialising modem...')
        self._at('AT+QCFG="nwscanmode",0,1', wait=1.0)
        log.info('Waiting for registration (up to 60s)...')
        for _ in range(30):
            resp = self._at('AT+CEREG?', wait=1.0)
            if ',1' in resp or ',5' in resp:
                log.info('LTE registered')
                return True
            resp2 = self._at('AT+CREG?', wait=1.0)
            if ',1' in resp2 or ',5' in resp2:
                log.info('GSM registered')
                return True
            time.sleep(2)
        log.warning('Modem not registered after 60s')
        return False

    # ── Bearer (AT+CGACT) ─────────────────────────────────────────────────────

    def _activate_bearer(self) -> bool:
        # _lock already held by caller (ensure_connected) or acquired here
        with self._lock:
            self._at('AT+CGACT=0,2', wait=3.0)

            r_cfg = self._at(
                f'AT+CGDCONT=1,"{self._pdp_type}","{self._apn}"', wait=1.5)
            log.debug('CGDCONT: %s', r_cfg.strip())

            r_act = self._at('AT+CGACT=1,1', wait=8.0)
            if 'ERROR' in r_act and 'CME ERROR' not in r_act:
                log.warning('CGACT=1,1 failed: %s', r_act.strip())
                return False

            r_addr = self._at('AT+CGPADDR=1', wait=2.0)
            m = re.search(r'\+CGPADDR:\s*1,"([^"]+)"', r_addr)
            if not m or m.group(1) in ('0.0.0.0', ''):
                log.warning('Bearer has no IP after CGACT: %s', r_addr.strip())
                return False

            log.info('Bearer active — device IP: %s', m.group(1))
            self._bearer_active = True
            return True

    def is_bearer_active(self) -> bool:
        r = self._at('AT+CGPADDR=1', wait=2.0)
        m = re.search(r'\+CGPADDR:\s*1,"([^"]+)"', r)
        if m and m.group(1) not in ('0.0.0.0', ''):
            return True
        self._bearer_active = False
        return False

    # ── Connectivity ──────────────────────────────────────────────────────────

    def is_online(self) -> bool:
        """Return True if bearer is up and has an IP address.
        Skips QPING — Airtel blocks ICMP to 8.8.8.8 on PDP context."""
        if not self._lock.acquire(blocking=True, timeout=3.0):
            return self._bearer_active
        try:
            r_addr = self._at('AT+CGPADDR=1', wait=2.0)
            m = re.search(r'\+CGPADDR:\s*1,"([^"]+)"', r_addr)
            online = bool(m and m.group(1) not in ('0.0.0.0', ''))
            self._bearer_active = online
            return online
        finally:
            self._lock.release()

    def ensure_connected(self) -> bool:
        if self.is_bearer_active():
            return True
        if not self._carrier_detected:
            self._detect_carrier()
        if not self._init_modem():
            log.warning('Modem not registered — bearer activation skipped')
            return False
        ok = self._activate_bearer()
        if ok:
            self.sync_time()
        return ok

    # ── AT-command HTTP (single open port, held under _lock) ──────────────────

    def http_get(self, url: str) -> Optional[bytes]:
        """HTTP(S) GET using modem's embedded client.  Returns body bytes or None."""
        with self._lock:
            if not self.is_bearer_active():
                log.warning('http_get: bearer not active')
                return None
            try:
                with serial.Serial(self._device, self._baud, timeout=2.0) as s:
                    self._scmd(s, 'AT+QHTTPCFG="contextid",1')
                    self._scmd(s, 'AT+QHTTPCFG="requestheader",0')
                    self._scmd(s, 'AT+QHTTPCFG="responseheader",0')
                    self._scmd(s, f'AT+QSSLCFG="sslversion",{_SSL_CTX},4')
                    self._scmd(s, f'AT+QSSLCFG="ciphersuite",{_SSL_CTX},0xFFFF')
                    self._scmd(s, f'AT+QSSLCFG="seclevel",{_SSL_CTX},0')
                    self._scmd(s, f'AT+QHTTPCFG="sslctxid",{_SSL_CTX}')

                    r = self._scmd(s, f'AT+QHTTPURL={len(url)},30',
                                   wait_for='CONNECT', timeout=12)
                    if 'CONNECT' not in r:
                        log.warning('http_get QHTTPURL no CONNECT: %s', r.strip()[:80])
                        return None
                    s.write(url.encode())
                    ok_r = b''
                    for _ in range(8):
                        ok_r += s.read(128)
                        if b'OK' in ok_r:
                            break

                    r2 = self._scmd(s, f'AT+QHTTPGET={_HTTP_GET_TIMEOUT}',
                                    wait_for='+QHTTPGET:', timeout=_HTTP_GET_TIMEOUT + 5)
                    m = re.search(r'\+QHTTPGET: 0,\d+,(\d+)', r2)
                    if not m:
                        log.warning('http_get GET failed: %s', r2.strip()[:80])
                        return None
                    body_len = int(m.group(1))
                    if body_len == 0:
                        return b''

                    r3 = self._scmd(s, 'AT+QHTTPREAD=10',
                                    wait_for='+QHTTPREAD:', timeout=15)
                    if 'CONNECT\r\n' in r3:
                        start = r3.index('CONNECT\r\n') + len('CONNECT\r\n')
                        end   = r3.rfind('\r\nOK')
                        if end > start:
                            return r3[start:end].strip().encode()
                    return r3.strip().encode(errors='replace')
            except Exception as e:
                log.error('http_get error: %s', e)
                return None

    def http_put(self, url: str, data: bytes,
                 content_type: str = 'application/octet-stream') -> bool:
        """Upload bytes to URL via HTTP(S) PUT.
        Holds _lock for the entire operation so the modem loop can't race.
        Throughput ≈ 11 KB/s (UART bottleneck)."""
        with self._lock:
            if not self.is_bearer_active():
                log.info('http_put: bearer inactive — reactivating')
                if not self._activate_bearer():
                    log.warning('http_put: bearer reactivation failed')
                    return False

            uart_seconds = max(60, int(len(data) / 10000) + 30)
            total_wait   = uart_seconds + 105

            try:
                with serial.Serial(self._device, self._baud, timeout=2.0) as s:
                    self._scmd(s, 'AT+QHTTPSTOP', timeout=3)
                    self._scmd(s, 'AT+QHTTPCFG="contextid",1')
                    self._scmd(s, 'AT+QHTTPCFG="requestheader",0')
                    self._scmd(s, 'AT+QHTTPCFG="responseheader",0')
                    self._scmd(s, f'AT+QSSLCFG="sslversion",{_SSL_CTX},4')
                    self._scmd(s, f'AT+QSSLCFG="ciphersuite",{_SSL_CTX},0xFFFF')
                    self._scmd(s, f'AT+QSSLCFG="seclevel",{_SSL_CTX},0')
                    self._scmd(s, f'AT+QHTTPCFG="sslctxid",{_SSL_CTX}')

                    r_url = self._scmd(s, f'AT+QHTTPURL={len(url)},30',
                                       wait_for='CONNECT', timeout=12)
                    if 'CONNECT' not in r_url:
                        log.warning('http_put QHTTPURL no CONNECT: %s', r_url.strip()[:80])
                        return False
                    s.write(url.encode())
                    ok_r = b''
                    for _ in range(8):
                        ok_r += s.read(128)
                        if b'OK' in ok_r:
                            break
                    log.debug('QHTTPURL OK: %s', ok_r.decode(errors='ignore')[:30])

                    r_put = self._scmd(s, f'AT+QHTTPPUT={len(data)},{uart_seconds}',
                                       wait_for='CONNECT', timeout=15)
                    if 'CONNECT' not in r_put:
                        log.warning('http_put QHTTPPUT no CONNECT: %s', r_put.strip()[:80])
                        return False

                    log.info('Sending %d bytes via UART (≈ %ds)…', len(data), uart_seconds)
                    for i in range(0, len(data), 1024):
                        s.write(data[i:i + 1024])

                    resp_buf = b''
                    deadline = time.time() + total_wait
                    while time.time() < deadline:
                        chunk = s.read(256)
                        if chunk:
                            resp_buf += chunk
                            if b'+QHTTPPUT:' in resp_buf:
                                break

                    resp = resp_buf.decode(errors='ignore')
                    log.debug('QHTTPPUT response: %s', resp.strip()[:120])
                    m = re.search(r'\+QHTTPPUT:\s*(\d+),(\d+)', resp)
                    if m and m.group(1) == '0' and m.group(2).startswith('2'):
                        log.info('http_put: success HTTP %s (%d bytes)',
                                 m.group(2), len(data))
                        return True
                    log.warning('http_put failed: %s', resp.strip()[:120])
                    return False
            except Exception as e:
                log.error('http_put error: %s', e)
                return False

    # ── Time sync ─────────────────────────────────────────────────────────────

    def sync_time(self) -> bool:
        try:
            resp = self._at('AT+QLTS=2', wait=2.0)
            m = re.search(
                r'\+QLTS:\s*"(\d+)/(\d+)/(\d+),(\d+):(\d+):(\d+)([+-]\d+)',
                resp)
            if not m:
                log.debug('AT+QLTS=2 no usable response: %r', resp)
                return False
            yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hr, mi, sc = int(m.group(4)), int(m.group(5)), int(m.group(6))
            tz_qh      = int(m.group(7))
            local_dt   = datetime.datetime(yr, mo, dy, hr, mi, sc)
            utc_dt     = local_dt - datetime.timedelta(minutes=tz_qh * 15)
            epoch      = int(utc_dt.timestamp())
            result     = subprocess.run(['date', '-u', '-s', f'@{epoch}'],
                                        capture_output=True, timeout=5)
            if result.returncode == 0:
                log.info('Clock synced via modem: %s UTC', utc_dt.isoformat())
                return True
        except Exception as e:
            log.warning('sync_time error: %s', e)
        return False

    # ── LBS location ──────────────────────────────────────────────────────────

    def get_location(self) -> Tuple[Optional[float], Optional[float]]:
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
