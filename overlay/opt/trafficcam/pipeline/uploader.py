"""
uploader.py — unchanged from RPi version.
S3 upload with WiFi-first, 4G fallback, SD queue if both fail.
"""

import asyncio
import json
import logging
import os
import pickle
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))


class Uploader:
    def __init__(self, config: dict, upload_queue: asyncio.Queue,
                 modem=None):
        self._cfg = config
        self._q = upload_queue
        self._modem = modem
        srv = config["server"]
        self._bucket = srv["s3_bucket"]
        self._prefix = srv.get("s3_prefix", "trafficcam")
        self._max_retries = int(srv.get("max_retries", 3))
        self._queue_dir = Path(config["storage"]["upload_queue_dir"])
        self._cleanup = config["storage"].get("cleanup_after_upload", True)
        self._boto3 = None

    def _get_s3(self):
        if self._boto3 is None:
            import boto3
            srv = self._cfg["server"]
            self._boto3 = boto3.client(
                "s3",
                region_name=srv["aws_region"],
                aws_access_key_id=srv["aws_access_key_id"],
                aws_secret_access_key=srv["aws_secret_access_key"],
            )
        return self._boto3

    async def run(self):
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        # Retry any queued-to-disk chunks from prior run
        asyncio.create_task(self._retry_queued())
        while True:
            item = await self._q.get()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._upload_item, item)
            except Exception as e:
                log.error("Upload error: %s", e, exc_info=True)
                self._save_to_queue(item)
            finally:
                self._q.task_done()

    def _upload_item(self, item: dict):
        chunk_id = item["chunk_id"]
        sidecar_data = item["sidecar_data"]
        start_ts = datetime.fromisoformat(sidecar_data["start_timestamp"])
        date_ist = start_ts.astimezone(_IST).strftime("%Y-%m-%d")
        device_id = sidecar_data["device_id"]
        prefix = f"{self._prefix}/{device_id}/{date_ist}/{chunk_id}"

        s3 = self._get_s3()
        for local, key_suffix in [(item["video"], f"{chunk_id}.mp4"),
                                   (item["sidecar"], f"{chunk_id}.json")]:
            key = f"{prefix}/{key_suffix}"
            log.info("Uploading s3://%s/%s", self._bucket, key)
            for attempt in range(1, self._max_retries + 1):
                try:
                    s3.upload_file(local, self._bucket, key)
                    break
                except Exception as e:
                    log.warning("Upload attempt %d failed: %s", attempt, e)
                    if attempt == self._max_retries:
                        raise

        log.info("Upload complete: %s", chunk_id)
        if self._cleanup:
            for f in [item.get("video"), item.get("sidecar")]:
                if f:
                    try:
                        os.unlink(f)
                    except Exception:
                        pass

    def _save_to_queue(self, item: dict):
        qf = self._queue_dir / f"{item['chunk_id']}.pkl"
        with open(qf, "wb") as f:
            pickle.dump(item, f)
        log.info("Queued to disk: %s", qf.name)

    async def _retry_queued(self):
        await asyncio.sleep(30)
        while True:
            for qf in list(self._queue_dir.glob("*.pkl")):
                try:
                    with open(qf, "rb") as f:
                        item = pickle.load(f)
                    self._upload_item(item)
                    qf.unlink()
                    log.info("Retried queued upload: %s", qf.name)
                except Exception as e:
                    log.warning("Retry failed for %s: %s", qf.name, e)
            await asyncio.sleep(300)
