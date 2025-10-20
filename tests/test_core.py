# tests/test_engine.py
# ---------------------------------------------------------------------------
# Pytest unit tests for sqs_fargate_listener.core:
# - _env_int / _env_float helpers
# - VisibilityExtender behavior (stop early; heartbeat loop)
# - SqsListenerEngine: start/join, _recv mapping, _delete_batch chunking
# - _loop logic for batch and per_message modes with a fake SQS client
#
# Assumes your module is at sqs_fargate_listener/core.py.
# Adjust imports below if your package path differs.
# ---------------------------------------------------------------------------

import threading
import time

import src.sqs_fargate_listener.core as core
from src.sqs_fargate_listener.types import SqsMessage, BatchResult


# =========================
# Helpers & Test Doubles
# =========================

def make_raw_message(
    mid="m1",
    rh="rh1",
    body='{"ok":true}',
    attributes=None,
    msg_attributes=None,
):
    return {
        "MessageId": mid,
        "ReceiptHandle": rh,
        "Body": body,
        "Attributes": attributes or {"ApproximateReceiveCount": "1"},
        "MessageAttributes": msg_attributes or {},
    }


class FakeSQS:
    """
    Minimal fake SQS client to drive the engine without AWS.
    batches: list of lists; each inner list is the raw 'Messages' for that poll.
    """
    def __init__(self, batches=None):
        self.batches = list(batches or [])
        self.delete_calls = []     # list of (QueueUrl, Entries)
        self.change_vis_calls = [] # list of dict calls for visibility
        self.receive_calls = 0

    # API the engine uses:

    def receive_message(self, **kwargs):
        self.receive_calls += 1
        if self.batches:
            return {"Messages": self.batches.pop(0)}
        return {}  # no messages

    def delete_message_batch(self, QueueUrl, Entries):
        self.delete_calls.append((QueueUrl, list(Entries)))
        return {"Successful": [{"Id": e["Id"]} for e in Entries]}

    def change_message_visibility(self, **kwargs):
        self.change_vis_calls.append(kwargs)
        return {}


class FakeVisibilityExt(threading.Thread):
    """
    VisibilityExtender stand-in that doesn't sleep or call SQS.
    Records start/join and gives us access to the per-message stop_event passed in.
    """
    def __init__(self, sqs, queue_url, receipt_handle, vis_secs, max_extend, stop_event, msg_stop_event):
        super().__init__(daemon=True)
        self.sqs = sqs
        self.queue_url = queue_url
        self.rh = receipt_handle
        self.vis_secs = vis_secs
        self.max_extend = max_extend
        self.stop_event = stop_event
        self.msg_stop_event = msg_stop_event
        self.started = False
        self.joined = False

    def start(self):  # noqa: D401
        self.started = True
        # Do not actually create a new thread; behave as if started.
        # (Engine calls .join() with a short timeout — we'll just mark joined there.)
        return None

    def join(self, timeout=None):
        self.joined = True
        return None


# =========================
# Tests: env helpers
# =========================

def test_env_helpers(monkeypatch):
    monkeypatch.setenv("WAIT_TIME", "30")
    monkeypatch.setenv("IDLE_SLEEP_MAX", "1.25")

    assert core._env_int("WAIT_TIME", 20) == 30
    assert core._env_int("MISSING_INT", 7) == 7
    assert core._env_float("IDLE_SLEEP_MAX", 2.0) == 1.25
    assert core._env_float("MISSING_FLOAT", 3.5) == 3.5


# =========================
# Tests: VisibilityExtender
# =========================

def test_visibility_extender_exits_immediately_if_msg_already_stopped(monkeypatch):
    fake_sqs = FakeSQS()
    stop = threading.Event()
    msg_stop = threading.Event()
    msg_stop.set()  # simulate handler already finished

    ve = core.VisibilityExtender(
        fake_sqs, "q", "rh", vis_secs=2, max_extend=10, stop_event=stop, msg_stop_event=msg_stop
    )
    # Patch Event.wait to avoid real sleeping by returning immediately
    class FastEvent(threading.Event):
        def wait(self, timeout=None):  # type: ignore[override]
            return True  # behave as if signaled
    ve.msg_stop_event = FastEvent()
    ve.msg_stop_event.set()

    ve.start()
    ve.join(timeout=0.2)

    # Should have made zero ChangeMessageVisibility calls
    assert fake_sqs.change_vis_calls == []


def test_visibility_extender_heartbeats_until_max_extend(monkeypatch):
    fake_sqs = FakeSQS()
    stop = threading.Event()

    # Use a custom Event that returns immediately (no sleep)
    class FastEvent(threading.Event):
        def wait(self, timeout=None):  # type: ignore[override]
            # do not block; indicate not signaled
            return False

    msg_stop = FastEvent()

    # Set vis_secs=2 -> interval=1; max_extend=3 -> loop increments elapsed by 1 three times
    ve = core.VisibilityExtender(
        fake_sqs, "q", "rh", vis_secs=2, max_extend=3, stop_event=stop, msg_stop_event=msg_stop
    )
    ve.start()
    ve.join(timeout=0.5)

    # Expect ~3 heartbeats
    assert len(fake_sqs.change_vis_calls) == 3
    for call in fake_sqs.change_vis_calls:
        assert call["QueueUrl"] == "q"
        assert call["ReceiptHandle"] == "rh"
        assert call["VisibilityTimeout"] == 2


# =========================
# Tests: Engine basics
# =========================

def test_engine_start_spawns_threads_and_sets_handlers(monkeypatch):
    # Avoid touching real signal handlers in test env
    monkeypatch.setattr(core.signal, "signal", lambda *a, **k: None)

    # Patch boto3.client to avoid creating a real client
    fake_sqs = FakeSQS()
    monkeypatch.setattr(core.boto3, "client", lambda *a, **kw: fake_sqs)

    eng = core.SqsListenerEngine(
        queue_url="q",
        handler=lambda _: None,
        mode="batch",
        worker_threads=2,
    )
    eng.start()
    try:
        assert len(eng._threads) == 2
        for t in eng._threads:
            assert t.is_alive()
    finally:
        # stop and join
        eng.stop_event.set()
        eng.join()


def test__recv_maps_messages(monkeypatch):
    fake_batch = [make_raw_message(mid="mX", rh="rhX", body='{"k": "v"}')]
    fake_sqs = FakeSQS(batches=[fake_batch])
    monkeypatch.setattr(core.boto3, "client", lambda *a, **kw: fake_sqs)

    eng = core.SqsListenerEngine(queue_url="q", handler=lambda _: None)
    msgs = eng._recv()
    assert len(msgs) == 1
    m = msgs[0]
    assert isinstance(m, SqsMessage)
    assert m.message_id == "mX"
    assert m.receipt_handle == "rhX"
    assert m.json == {"k": "v"}


def test__delete_batch_chunks_in_tens(monkeypatch):
    fake_sqs = FakeSQS()
    monkeypatch.setattr(core.boto3, "client", lambda *a, **kw: fake_sqs)

    eng = core.SqsListenerEngine(queue_url="q", handler=lambda _: None)
    handles = [f"rh{i}" for i in range(23)]
    eng._delete_batch(handles)

    # Should call delete in chunks: 10, 10, 3
    sizes = [len(entries) for (_q, entries) in fake_sqs.delete_calls]
    assert sizes == [10, 10, 3]
    assert all(q == "q" for (q, _entries) in fake_sqs.delete_calls)


# =========================
# Tests: Engine _loop logic
# =========================

def test_loop_batch_mode_success_deletes_ok_and_stops_extenders(monkeypatch):
    # Engine should:
    # - receive 1 batch with 2 messages
    # - run handler which marks one failed
    # - stop extenders, join them, and delete only the OK message
    # - then see no more messages and we stop the loop
    fake_msgs = [
        make_raw_message(mid="m1", rh="rh1"),
        make_raw_message(mid="m2", rh="rh2"),
    ]
    fake_sqs = FakeSQS(batches=[fake_msgs, []])
    monkeypatch.setattr(core.boto3, "client", lambda *a, **kw: fake_sqs)
    # Use Fake VisibilityExtender to avoid threading/sleep
    created_exts = []
    def _fake_ext_factory(*args, **kwargs):
        ext = FakeVisibilityExt(*args, **kwargs)
        created_exts.append(ext)
        return ext
    monkeypatch.setattr(core, "VisibilityExtender", _fake_ext_factory)

    # handler returns one failed RH; also stop engine after processing
    def handler(batch):
        # mark m2 failed
        nonlocal_eng.stop_event.set()
        return BatchResult(failed_receipt_handles=["rh2"])

    nonlocal_eng = core.SqsListenerEngine(queue_url="q", handler=handler, mode="batch")
    # Make the idle backoff zero so the loop doesn't sleep forever on empty
    nonlocal_eng.idle_sleep_max = 0.0

    # Run loop in a background thread and wait briefly
    t = threading.Thread(target=nonlocal_eng._loop, daemon=True)
    t.start()
    t.join(timeout=1.0)

    # Assert delete called only for rh1
    assert len(fake_sqs.delete_calls) == 1
    (_q, entries) = fake_sqs.delete_calls[0]
    rhs = sorted(e["ReceiptHandle"] for e in entries)
    assert rhs == ["rh1"]

    # Extenders created for both messages, started and then joined
    assert len(created_exts) == 2
    assert all(ext.started for ext in created_exts)
    assert all(ext.joined for ext in created_exts)


def test_loop_per_message_mode_deletes_only_truthy_and_handles_exceptions(monkeypatch):
    # 3 messages: True -> delete, False -> keep, Exception -> keep
    fake_msgs = [
        make_raw_message(mid="m1", rh="rh1"),
        make_raw_message(mid="m2", rh="rh2"),
        make_raw_message(mid="m3", rh="rh3"),
    ]
    fake_sqs = FakeSQS(batches=[fake_msgs, []])
    monkeypatch.setattr(core.boto3, "client", lambda *a, **kw: fake_sqs)

    created_exts = []
    monkeypatch.setattr(core, "VisibilityExtender",
                        lambda *a, **k: created_exts.append(FakeVisibilityExt(*a, **k)) or created_exts[-1])

    def handler(msg):
        # behavior depends on message id
        if msg.receipt_handle == "rh1":
            return True
        if msg.receipt_handle == "rh2":
            return False
        raise RuntimeError("boom")

    eng = core.SqsListenerEngine(queue_url="q", handler=handler, mode="per_message")
    eng.idle_sleep_max = 0.0

    # Stop after first loop
    def stop_after_short_delay():
        # Give loop a moment to run
        time.sleep(0.05)
        eng.stop_event.set()

    stopper = threading.Thread(target=stop_after_short_delay, daemon=True)
    stopper.start()

    t = threading.Thread(target=eng._loop, daemon=True)
    t.start()
    t.join(timeout=2.0)

    # Only rh1 should be deleted
    assert len(fake_sqs.delete_calls) == 1
    (_q, entries) = fake_sqs.delete_calls[0]
    rhs = sorted(e["ReceiptHandle"] for e in entries)
    assert rhs == ["rh1"]

    # Extenders for 3 messages should be started and joined
    assert len(created_exts) == 3
    assert all(ext.started and ext.joined for ext in created_exts)


def test_handle_stop_sets_flag(monkeypatch):
    # Avoid mutating real signal handlers
    monkeypatch.setattr(core.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(core.boto3, "client", lambda *a, **kw: FakeSQS())

    eng = core.SqsListenerEngine(queue_url="q", handler=lambda _: None)
    # Simulate signal handler call
    eng._handle_stop()
    assert eng.stop_event.is_set()
