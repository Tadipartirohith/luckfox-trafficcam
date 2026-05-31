"""
uploader.py — S3 upload with boto3-first, modem AT-command fallback, SD queue.

boto3 path: requires a real network interface (USB internet / future WiFi).
Modem path: uses modem.http_put() via AT+QHTTPPUT with a pre-signed S3 URL.
  Throughput ≈ 11 KB/s (UART bottleneck); large video files are slow but work.
  JSON sidecars (<5 KB) upload in < 1 s; 30 MB MP4 ≈ 45 min.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import pickle
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))


def _s3_presign_put(region: str, bucket: str, key: str,
                    access_key: str, secret_key: str,
                    expires: int = 7200) -> str:
    """Generate a Sigv4 pre-signed PUT URL for S3 with the regional virtual-hosted endpoint.
    Works regardless of boto3 version — uses only hmac/hashlib.
    boto3 1.26 generates global-endpoint URLs which S3 rejects for non-us-east-1 regions."""
    now = datetime.utcnow()
    date_stamp = now.strftime('%Y%m%d')
    amz_date   = now.strftime('%Y%m%dT%H%M%SZ')
    host       = f'{bucket}.s3.{region}.amazonaws.com'
    path       = '/' + quote(key, safe='/-_.~')
    scope      = f'{date_stamp}/{region}/s3/aws4_request'
    credential = f'{access_key}/{scope}'

    query = (
        f'X-Amz-Algorithm=AWS4-HMAC-SHA256'
        f'&X-Amz-Credential={quote(credential, safe="")}'
        f'&X-Amz-Date={amz_date}'
        f'&X-Amz-Expires={expires}'
        f'&X-Amz-SignedHeaders=host'
    )

    canonical = f'PUT\n{path}\n{query}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD'
    canon_hash = hashlib.sha256(canonical.encode()).hexdigest()
    string_to_sign = f'AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{canon_hash}'

    def _sign(key_bytes, msg):
        return hmac.new(key_bytes, msg.encode(), hashlib.sha256).digest()

    signing_key = _sign(
        _sign(_sign(_sign(f'AWS4{secret_key}'.encode(), date_stamp), region), 's3'),
        'aws4_request',
    )
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f'https://{host}{path}?{query}&X-Amz-Signature={signature}'


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

    def _upload_via_modem(self, local: str, bucket: str, key: str) -> bool:
        """Upload one file to S3 via the modem's AT+QHTTPPUT using a pre-signed URL.
        Uses _s3_presign_put() to generate a regional virtual-hosted URL that works
        regardless of boto3 version — bypasses boto3 1.26's global-endpoint bug."""
        if not self._modem or not self._modem.is_bearer_active():
            return False
        try:
            srv = self._cfg['server']
            url = _s3_presign_put(
                region=srv['aws_region'],
                bucket=bucket,
                key=key,
                access_key=srv['aws_access_key_id'],
                secret_key=srv['aws_secret_access_key'],
                expires=7200,
            )
            ct = 'video/mp4' if key.endswith('.mp4') else 'application/json'
            data = Path(local).read_bytes()
            log.info('Modem upload s3://%s/%s (%d bytes via UART)', bucket, key, len(data))
            return self._modem.http_put(url, data, content_type=ct)
        except Exception as e:
            log.warning('Modem upload error: %s', e)
            return False

    def _upload_item(self, item: dict):
        chunk_id = item["chunk_id"]
        sidecar_data = item["sidecar_data"]
        start_ts = datetime.fromisoformat(sidecar_data["start_timestamp"])
        date_ist = start_ts.astimezone(_IST).strftime("%Y-%m-%d")
        device_id = sidecar_data["device_id"]
        prefix = f"{self._prefix}/{device_id}/{date_ist}/{chunk_id}"

        s3 = self._get_s3()
        video_key   = f"{prefix}/{chunk_id}.mp4"
        sidecar_key = f"{prefix}/{chunk_id}.json"

        # Sidecar first (<5 KB) — modem AT+QHTTPPUT fallback works
        log.info("Uploading s3://%s/%s", self._bucket, sidecar_key)
        sidecar_ok = False
        for attempt in range(1, self._max_retries + 1):
            try:
                s3.upload_file(item["sidecar"], self._bucket, sidecar_key)
                sidecar_ok = True
                break
            except Exception as e:
                log.warning("Sidecar attempt %d failed: %s", attempt, e)
        if not sidecar_ok:
            log.info("boto3 sidecar failed — trying modem AT-command path")
            sidecar_ok = self._upload_via_modem(
                item["sidecar"], self._bucket, sidecar_key)

        # Video — large file, no modem fallback (UART too slow)
        log.info("Uploading s3://%s/%s", self._bucket, video_key)
        video_ok = False
        for attempt in range(1, self._max_retries + 1):
            try:
                s3.upload_file(item["video"], self._bucket, video_key)
                video_ok = True
                break
            except Exception as e:
                log.warning("Video attempt %d failed: %s", attempt, e)
        if not video_ok:
            log.info("Video boto3 failed — no modem fallback; queuing bundle to SD")
            raise RuntimeError(f"Video upload failed for {chunk_id}")
        if not sidecar_ok:
            raise RuntimeError(f"All upload paths failed for sidecar {chunk_id}")

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
