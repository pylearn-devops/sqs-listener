# tests/test_decorator.py
# -------------------------------------------------------------------
# Pytest unit tests for the sqs_fargate_listener.decorator module.
# These tests validate:
# - decorator parameter/env handling
# - registry population
# - run_listeners() engine instantiation and start/join behavior
# - option filtering (None values are dropped)
#
# Adjust the import path below if your package name differs.
# -------------------------------------------------------------------

import pytest

# Import module under test
import src.sqs_fargate_listener.decorator as dec


# ---------------------------
# Fixtures & test doubles
# ---------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure the internal registry is clean between tests."""
    # dec._REGISTRY is a module-level list
    dec._REGISTRY.clear()
    yield
    dec._REGISTRY.clear()


@pytest.fixture
def fake_engine_cls(monkeypatch):
    """
    Patch SqsListenerEngine used by the decorator module with a spy class
    that records constructor args and method calls.
    """
    created = []

    class FakeEngine:
        def __init__(self, *, queue_url, handler, mode, **opts):
            # record args for assertions
            self.kwargs = {
                "queue_url": queue_url,
                "handler": handler,
                "mode": mode,
                **opts,
            }
            self.started = False
            self.joined = False
            created.append(self)

        def start(self):
            self.started = True

        def join(self):
            self.joined = True

    monkeypatch.setattr(dec, "SqsListenerEngine", FakeEngine)
    return created  # return list to allow assertions in tests


# ---------------------------
# Helper(s)
# ---------------------------

def define_noop_handler():
    def handler(*args, **kwargs):
        return None
    return handler


# ---------------------------
# Tests: decorator basics
# ---------------------------

def test_decorator_raises_when_no_queue_url_and_no_env(monkeypatch):
    monkeypatch.delenv("QUEUE_URL", raising=False)

    with pytest.raises(ValueError) as e:
        @dec.sqs_listener()  # no queue_url, no env -> error at decoration time
        def h(_): ...
    assert "queue_url is required" in str(e.value)


def test_decorator_uses_env_queue_url_when_not_provided(monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "http://localstack:4566/000000000000/test-queue")

    @dec.sqs_listener()  # picks from env
    def handler(_): ...

    assert len(dec._REGISTRY) == 1
    spec = dec._REGISTRY[0]
    assert spec.queue_url == "http://localstack:4566/000000000000/test-queue"
    assert spec.handler is handler
    assert spec.mode == "batch"  # default


def test_decorator_allows_explicit_queue_url_over_env(monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "http://should/not/use")
    @dec.sqs_listener(queue_url="http://explicit:4566/000000000000/test-queue", mode="per_message")
    def handler(_): ...
    assert len(dec._REGISTRY) == 1
    spec = dec._REGISTRY[0]
    assert spec.queue_url == "http://explicit:4566/000000000000/test-queue"
    assert spec.mode == "per_message"


def test_decorator_returns_original_function(monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "q")
    def f(_): return "ok"
    wrapped = dec.sqs_listener()(f)
    assert wrapped is f
    assert wrapped("x") == "ok"


def test_multiple_decorators_register_multiple_listeners(monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "q1")

    @dec.sqs_listener()
    def h1(_): ...

    # Switch queue for second one
    monkeypatch.setenv("QUEUE_URL", "q2")

    @dec.sqs_listener(mode="per_message")
    def h2(_): ...

    assert len(dec._REGISTRY) == 2
    assert dec._REGISTRY[0].queue_url == "q1"
    assert dec._REGISTRY[1].queue_url == "q2"
    assert dec._REGISTRY[0].handler is h1
    assert dec._REGISTRY[1].handler is h2


# ---------------------------
# Tests: option propagation
# ---------------------------

def test_options_propagate_and_none_are_dropped(monkeypatch, fake_engine_cls):
    monkeypatch.setenv("QUEUE_URL", "q")

    @dec.sqs_listener(
        mode="batch",
        wait_time=15,
        batch_size=7,
        visibility_secs=45,
        max_extend=300,
        worker_threads=2,
        extra_a="A",           # extra opts should propagate too
        extra_b=0,
        none_opt=None,         # should be filtered out before engine init
    )
    def h(_): ...

    # Run (non-blocking) to instantiate engines
    dec.run_listeners(block=False)

    assert len(fake_engine_cls) == 1
    eng = fake_engine_cls[0]
    kw = eng.kwargs

    # core args
    assert kw["queue_url"] == "q"
    assert kw["handler"] is h
    assert kw["mode"] == "batch"

    # options passed (and not None)
    assert kw["wait_time"] == 15
    assert kw["batch_size"] == 7
    assert kw["visibility_secs"] == 45
    assert kw["max_extend"] == 300
    assert kw["worker_threads"] == 2

    # extras
    assert kw["extra_a"] == "A"
    assert kw["extra_b"] == 0

    # ensure none_opt was filtered
    assert "none_opt" not in kw


# ---------------------------
# Tests: run_listeners behavior
# ---------------------------

def test_run_listeners_raises_when_empty_registry():
    with pytest.raises(RuntimeError) as e:
        dec.run_listeners(block=False)
    assert "No listeners registered" in str(e.value)


def test_run_listeners_starts_engines_no_block(monkeypatch, fake_engine_cls):
    monkeypatch.setenv("QUEUE_URL", "q")

    @dec.sqs_listener()
    def h(_): ...

    dec.run_listeners(block=False)

    assert len(fake_engine_cls) == 1
    eng = fake_engine_cls[0]
    assert eng.started is True
    assert eng.joined is False  # block=False => no join


def test_run_listeners_starts_and_joins_when_block_true(monkeypatch, fake_engine_cls):
    monkeypatch.setenv("QUEUE_URL", "q")

    @dec.sqs_listener()
    def h(_): ...

    dec.run_listeners(block=True)

    assert len(fake_engine_cls) == 1
    eng = fake_engine_cls[0]
    assert eng.started is True
    assert eng.joined is True


def test_run_listeners_multiple_handlers(monkeypatch, fake_engine_cls):
    monkeypatch.setenv("QUEUE_URL", "q1")

    @dec.sqs_listener()
    def h1(_): ...

    monkeypatch.setenv("QUEUE_URL", "q2")

    @dec.sqs_listener(mode="per_message", worker_threads=1)
    def h2(_): ...

    dec.run_listeners(block=False)

    assert len(fake_engine_cls) == 2
    q_urls = {e.kwargs["queue_url"] for e in fake_engine_cls}
    assert q_urls == {"q1", "q2"}
    modes = {e.kwargs["mode"] for e in fake_engine_cls}
    assert modes == {"batch", "per_message"}
    assert all(e.started for e in fake_engine_cls)
    assert not any(e.joined for e in fake_engine_cls)


# ---------------------------
# Tests: logging side-effects (smoke)
# ---------------------------

def test_logger_is_callable_and_module_has_logger():
    # Just ensure the module logger exists and is callable for info()
    assert hasattr(dec, "_logger")
    assert hasattr(dec._logger, "info")
    # We won't assert log output in unit tests here to avoid coupling.
