from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, List
import os

from .core import SqsListenerEngine
from .logging_setup import get_logger

_logger = get_logger("sqs_fargate_listener")

@dataclass
class _ListenerSpec:
    queue_url: str
    handler: Callable
    mode: str
    opts: Dict[str, Any]

_REGISTRY: list[_ListenerSpec] = []


def sqs_listener(
    queue_url: Optional[str] = None,
    *,
    mode: str = "batch",                   # "batch" or "per_message"
    wait_time: Optional[int] = None,
    batch_size: Optional[int] = None,
    visibility_secs: Optional[int] = None,
    max_extend: Optional[int] = None,
    worker_threads: Optional[int] = None,
    **extra_opts: Any,
):
    """
    Decorator to register an SQS handler. If queue_url is None, falls back to
    env var QUEUE_URL or raises at decoration time.
    """
    def _decorator(func: Callable):
        q = queue_url or os.environ.get("QUEUE_URL")
        if not q:
            raise ValueError("queue_url is required (or set QUEUE_URL env var).")
        spec = _ListenerSpec(
            queue_url=q,
            handler=func,
            mode=mode,
            opts={
                "wait_time": wait_time,
                "batch_size": batch_size,
                "visibility_secs": visibility_secs,
                "max_extend": max_extend,
                "worker_threads": worker_threads,
                **extra_opts,
            },
        )
        _REGISTRY.append(spec)
        _logger.info("Registered listener for queue=%s mode=%s handler=%s", q, mode, func.__name__)
        return func
    return _decorator


def run_listeners(block: bool = True):
    """
    Instantiate and start all registered listeners. If block=True, join threads.
    """
    if not _REGISTRY:
        raise RuntimeError("No listeners registered. Did you decorate a function with @sqs_listener?")

    _logger.info("Starting %d SQS listener(s)…", len(_REGISTRY))
    engines: List[SqsListenerEngine] = []
    for spec in _REGISTRY:
        eng = SqsListenerEngine(
            queue_url=spec.queue_url,
            handler=spec.handler,
            mode=spec.mode,
            **{k: v for k, v in spec.opts.items() if v is not None},
        )
        eng.start()
        engines.append(eng)
    _logger.info("All listeners started.")

    if block:
        for eng in engines:
            eng.join()
        _logger.info("All listeners stopped.")
