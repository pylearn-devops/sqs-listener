from __future__ import annotations
import os
import time
import random
import threading
import signal
from typing import Callable, Iterable, List, Optional, Dict, Any

import boto3
from botocore.config import Config

from .types import SqsMessage, BatchResult
from .logging_setup import get_logger

logger = get_logger("sqs_fargate_listener.engine")


def _env_int(name, default): 
    return int(os.environ.get(name, str(default)))


def _env_float(name, default):
    return float(os.environ.get(name, str(default)))


class VisibilityExtender(threading.Thread):
    def __init__(self, sqs, queue_url, receipt_handle, vis_secs, max_extend, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.sqs = sqs
        self.queue_url = queue_url
        self.rh = receipt_handle
        self.vis_secs = vis_secs
        self.max_extend = max_extend
        self.stop_event = stop_event
        self.elapsed = 0

    def run(self):
        interval = max(1, self.vis_secs // 2)
        while not self.stop_event.is_set() and self.elapsed < self.max_extend:
            time.sleep(interval)
            if self.stop_event.is_set():
                break
            try:
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=self.rh,
                    VisibilityTimeout=self.vis_secs
                )
                logger.debug("Extended visibility for RH=%s by %ds", self.rh[:12], self.vis_secs)
            except Exception as e:
                logger.warning("[visibility] extend failed for RH=%s: %s", self.rh[:12], e)
            self.elapsed += interval


class SqsListenerEngine:
    """
    Main engine: poll -> handle -> delete successes -> re-queue failures.

    Supports two handler shapes:

    Batch handler:
        def handler(messages: List[SqsMessage]) -> BatchResult

    Per-message handler:
        def handler(message: SqsMessage) -> bool
        (return True to delete; False/exception = retry)
    """

    def __init__(
        self,
        queue_url: str,
        handler: Callable,
        mode: str = "batch",               # "batch" | "per_message"
        wait_time: Optional[int] = None,
        batch_size: Optional[int] = None,
        visibility_secs: Optional[int] = None,
        max_extend: Optional[int] = None,
        worker_threads: Optional[int] = None,
        client_kwargs: Optional[Dict[str, Any]] = None,
    ):
        self.queue_url = queue_url
        self.handler = handler
        self.mode = mode

        self.sqs = boto3.client(
            "sqs",
            config=Config(
                retries={"max_attempts": 10, "mode": "adaptive"},
                user_agent_extra="sqs-fargate-listener/1.1"
            ),
            **(client_kwargs or {})
        )

        # Defaults with env overrides (decorator args take precedence in decorator -> engine binding)
        self.wait_time       = wait_time       if wait_time       is not None else _env_int("WAIT_TIME", 20)
        self.batch_size      = batch_size      if batch_size      is not None else _env_int("BATCH_SIZE", 10)
        self.visibility_secs = visibility_secs if visibility_secs is not None else _env_int("VISIBILITY_SECS", 60)
        self.max_extend      = max_extend      if max_extend      is not None else _env_int("MAX_EXTEND", 900)
        self.worker_threads  = worker_threads  if worker_threads  is not None else _env_int("WORKER_THREADS", 4)
        self.idle_sleep_max  = _env_float("IDLE_SLEEP_MAX", 2.0)

        self.stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

        logger.info(
            "Configured listener: mode=%s wait_time=%s batch_size=%s visibility=%ss threads=%s queue=%s",
            self.mode, self.wait_time, self.batch_size, self.visibility_secs,
            self.worker_threads, self.queue_url
        )

    def start(self):
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        for i in range(self.worker_threads):
            t = threading.Thread(target=self._loop, daemon=True, name=f"sqs-poller-{i}")
            t.start()
            self._threads.append(t)
        logger.info("Spawned %d poller thread(s).", self.worker_threads)

    def join(self):
        for t in self._threads:
            while t.is_alive() and not self.stop_event.is_set():
                t.join(timeout=0.2)

    def _handle_stop(self, *_):
        logger.info("Stop signal received; draining…")
        self.stop_event.set()

    def _recv(self) -> List[SqsMessage]:
        resp = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=self.batch_size,
            WaitTimeSeconds=self.wait_time,
            VisibilityTimeout=self.visibility_secs,
            AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
            MessageAttributeNames=["All"],
        )
        msgs = resp.get("Messages", [])
        if msgs:
            logger.debug("Received %d message(s).", len(msgs))
        return [
            SqsMessage(
                message_id=m["MessageId"],
                receipt_handle=m["ReceiptHandle"],
                body=m["Body"],
                attributes=m.get("Attributes", {}),
                md=m
            ) for m in msgs
        ]

    def _delete_batch(self, receipt_handles: Iterable[str]):
        handles = list(receipt_handles)
        if not handles:
            return
        total = 0
        for i in range(0, len(handles), 10):
            chunk = handles[i:i+10]
            try:
                self.sqs.delete_message_batch(
                    QueueUrl=self.queue_url,
                    Entries=[{"Id": str(j), "ReceiptHandle": rh} for j, rh in enumerate(chunk)]
                )
                total += len(chunk)
            except Exception as e:
                logger.error("[delete] batch delete failed: %s", e)
        if total:
            logger.info("Deleted %d message(s).", total)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                batch = self._recv()
                if not batch:
                    time.sleep(random.random() * self.idle_sleep_max)
                    continue

                # Start visibility extenders for each message
                extenders = {}
                for m in batch:
                    ext = VisibilityExtender(
                        self.sqs, self.queue_url, m.receipt_handle,
                        self.visibility_secs, self.max_extend, self.stop_event
                    )
                    ext.start()
                    extenders[m.receipt_handle] = ext

                try:
                    if self.mode == "batch":
                        try:
                            result = self.handler(batch)
                            if not isinstance(result, BatchResult):
                                raise TypeError("Batch handler must return BatchResult")
                        except Exception as e:
                            # Whole-batch failure → nothing deleted
                            logger.error("[handler] batch error: %s", e, exc_info=True)
                            continue
                        failed = set(result.failed_receipt_handles)
                        ok = [m.receipt_handle for m in batch if m.receipt_handle not in failed]
                        if failed:
                            logger.warning("Batch processed with failures: ok=%d failed=%d", len(ok), len(failed))
                        else:
                            logger.info("Batch processed successfully: ok=%d", len(ok))
                        self._delete_batch(ok)
                    else:
                        ok = []
                        failed = 0
                        for m in batch:
                            try:
                                if bool(self.handler(m)):
                                    ok.append(m.receipt_handle)
                                else:
                                    failed += 1
                            except Exception as e:
                                failed += 1
                                logger.error("[handler] error for %s: %s", m.message_id, e, exc_info=True)
                        logger.info("Per-message processed: ok=%d failed=%d", len(ok), failed)
                        self._delete_batch(ok)
                finally:
                    # extenders exit on their next sleep tick when stop_event is set; best-effort
                    pass
            except Exception as e:
                logger.error("[loop] error: %s", e, exc_info=True)
                time.sleep(1)
