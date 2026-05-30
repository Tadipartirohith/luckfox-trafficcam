"""
camera.py -- Luckfox Pico Ultra adaptation.

Runs trafficcam_recorder as a persistent subprocess.
Auto-restarts recorder if it exits.
Logs recorder output at WARNING so it appears in default INFO log.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

_RECORDER_BIN = "/opt/trafficcam/bin/trafficcam_recorder_new"


class ChunkRecorder:
    def __init__(self, config: dict, raw_queue: asyncio.Queue):
        cam  = config["camera"]
        stor = config["storage"]
        self._resolution  = cam["resolution"]
        self._fps         = int(cam.get("fps", 30))
        self._chunk_secs  = int(cam.get("chunk_duration_seconds", 60))
        self._bitrate     = int(cam.get("bitrate_kbps", 4000))
        self._vi_channel  = int(cam.get("vi_channel", 0))
        self._raw_dir     = Path(stor["raw_dir"])
        self._raw_queue   = raw_queue
        self._proc        = None
        self._cmd         = None

    async def start(self):
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        for f in self._raw_dir.glob("seg_*.h264"):
            f.unlink(missing_ok=True)

        width, height = self._resolution.split("x")
        self._cmd = [
            _RECORDER_BIN,
            "-w", width, "-h", height,
            "-f", str(self._fps),
            "-s", str(self._chunk_secs),
            "-b", str(self._bitrate),
            "-I", str(self._vi_channel),
            "-o", str(self._raw_dir),
        ]

        await self._launch()
        asyncio.create_task(self._poll_segments())
        asyncio.create_task(self._monitor_recorder())

    async def _launch(self):
        """Start (or restart) the recorder subprocess."""
        log.info("Starting camera recorder: %s", " ".join(self._cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._log_output(self._proc))
        log.info("Camera recorder PID %d started", self._proc.pid)

    async def _log_output(self, proc):
        """Drain recorder stdout+stderr; log at WARNING so always visible."""
        async def drain(stream, label):
            while True:
                line = await stream.readline()
                if not line:
                    break
                log.warning("[recorder-%s] %s", label,
                            line.decode("utf-8", errors="replace").rstrip())
        await asyncio.gather(
            drain(proc.stdout, "out"),
            drain(proc.stderr, "err"),
        )
        rc = await proc.wait()
        log.warning("Camera recorder PID %d exited rc=%d", proc.pid, rc)

    async def _monitor_recorder(self):
        """Restart recorder automatically if it exits."""
        while True:
            await asyncio.sleep(5)
            if self._proc and self._proc.returncode is not None:
                log.warning(
                    "Camera recorder dead (rc=%d) -- restarting in 5s ...",
                    self._proc.returncode,
                )
                await asyncio.sleep(5)
                for f in self._raw_dir.glob("seg_*.h264"):
                    f.unlink(missing_ok=True)
                await self._launch()

    async def _poll_segments(self):
        """
        Watch raw_dir for seg_NNNN.h264 files.
        When seg_N+1 appears, seg_N is complete -- enqueue it.
        """
        known_segs: dict = {}

        while True:
            await asyncio.sleep(1)
            try:
                seg_files = sorted(
                    self._raw_dir.glob("seg_*.h264"),
                    key=lambda p: self._seg_num(p) or 0,
                )
            except Exception as e:
                log.warning("Segment poll error: %s", e)
                continue

            nums = [self._seg_num(p) for p in seg_files
                    if self._seg_num(p) is not None]
            for p in seg_files:
                n = self._seg_num(p)
                if n is None:
                    continue
                if n not in known_segs:
                    known_segs[n] = time.time()

            if not nums:
                continue

            completed = [n for n in list(known_segs) if max(nums) > n]
            for n in sorted(completed):
                path = self._raw_dir / "seg_{:04d}.h264".format(n)
                if path.exists():
                    start_ts = known_segs.pop(n)
                    new_path = self._raw_dir / "raw_{:d}_{:04d}.h264".format(int(start_ts), n)
                    try:
                        path.rename(new_path)
                        await self._raw_queue.put(str(new_path))
                        log.info("Segment complete -> %s", new_path.name)
                    except Exception as e:
                        log.error("Failed to rename segment %s: %s", path, e)

    @staticmethod
    def _seg_num(path: Path):
        import re as _re
        m = _re.search(r"seg_(\d+)\.h264", path.name)
        return int(m.group(1)) if m else None

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            log.info("Stopping camera recorder (PID %d)", self._proc.pid)
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._proc.kill()