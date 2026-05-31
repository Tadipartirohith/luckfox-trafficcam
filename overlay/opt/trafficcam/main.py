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


async def _modem_loop(modem: ModemManager):
    """
    Dial the 4G modem at startup and sync the clock once after first connection.
    Without this, the modem only dials when the uploader has a file to
    upload — but if the camera has never produced a file the modem never
    comes online.  This loop fixes that chicken-and-egg problem.
    """
    await asyncio.sleep(8)          # let GPS / health initialise first
    loop = asyncio.get_running_loop()
    _time_synced = False
    while True:
        connected = modem.is_online()
        if not connected:
            log.info("main: internet offline — dialling modem …")
            try:
                connected = await loop.run_in_executor(None, modem.ensure_connected)
            except Exception as e:
                log.warning("main: modem dial error: %s", e)
        if connected and not _time_synced:
            try:
                await loop.run_in_executor(None, modem.sync_time)
                _time_synced = True
            except Exception as e:
                log.warning("main: sync_time error: %s", e)
        await asyncio.sleep(60)     # re-check every minute


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