"""
main.py — Traffic camera async orchestrator for Luckfox Pico Ultra.

Wires together: ChunkRecorder → ChunkProcessor → Uploader
Background: GPSReader, ModemManager, HealthMonitor, ModemDialLoop
"""

import asyncio
import json
import logging
import os
import sys

# NOTE: No proxy settings here. Internet access is via the GSM modem (PPP).
# HTTPS_PROXY is only needed during development when routing through a host proxy.

# Ensure /opt/trafficcam is on the module path
sys.path.insert(0, "/opt/trafficcam")

from pipeline.camera    import ChunkRecorder
from pipeline.processor import ChunkProcessor
from pipeline.uploader  import Uploader
from pipeline.health    import HealthMonitor
from util.gps           import GPSReader
from util.modem         import ModemManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/trafficcam.log", mode="a"),
    ],
)
log = logging.getLogger("main")

CONFIG_PATH = "/opt/trafficcam/config.json"


def _ntp_sync() -> bool:
    """Try rdate NTP sync — used at startup before modem connects."""
    import subprocess as _sp
    for srv in ('time.cloudflare.com', 'pool.ntp.org', 'time.google.com'):
        try:
            r = _sp.run(['rdate', '-n', srv], capture_output=True, timeout=10)
            if r.returncode == 0:
                log.info('Clock synced via NTP (%s)', srv)
                return True
        except Exception:
            pass
    return False


async def _modem_loop(modem: ModemManager):
    """Dial modem at startup and sync clock whenever connectivity is restored."""
    loop = asyncio.get_running_loop()

    # Try NTP immediately via laptop USB internet (succeeds if laptop is attached)
    await asyncio.sleep(5)
    if not await loop.run_in_executor(None, _ntp_sync):
        log.info('NTP not yet reachable — will sync via modem after PPP connects')

    await asyncio.sleep(3)
    _clock_synced = False
    while True:
        if not modem.is_online():
            log.info('main: internet offline — dialling modem …')
            try:
                ok = await loop.run_in_executor(None, modem.ensure_connected)
                if ok and not _clock_synced:
                    synced = await loop.run_in_executor(None, modem.sync_time)
                    if synced:
                        _clock_synced = True
            except Exception as e:
                log.warning('main: modem dial error: %s', e)
        await asyncio.sleep(60)


async def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    raw_queue    = asyncio.Queue(maxsize=10)
    upload_queue = asyncio.Queue(maxsize=50)

    gps   = GPSReader(config)
    modem = ModemManager(config)
    health = HealthMonitor(config, gps=gps)

    gps.start()
    modem.start()

    recorder  = ChunkRecorder(config, raw_queue)
    processor = ChunkProcessor(config, raw_queue, upload_queue, gps, modem)
    uploader  = Uploader(config, upload_queue, modem=modem)

    await health.run()
    await recorder.start()

    await asyncio.gather(
        processor.run(),
        uploader.run(),
        _modem_loop(modem),         # <-- NEW: dial modem at startup
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")