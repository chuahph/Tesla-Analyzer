"""The receiver's spool: never block the car, never lose a batch, never reorder.

The bridge runs on a box nobody looks at, and the failure it guards against is
the one no other part of this system covers. The car buffers what it cannot
send and replays it in order, so the car-to-receiver link repairs itself; once
the receiver has the records the car will never send them again, and a batch
dropped between here and the app is gone for good.

zmq is not installed in this environment and is not needed to test any of
that, so it is stubbed at import. Nothing below touches a socket.
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("zmq", types.SimpleNamespace(
    Context=object, SUB=1, POLLIN=1, SUBSCRIBE=2, Poller=object))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "telemetry"))

import bridge  # noqa: E402


@pytest.fixture(autouse=True)
def _spool_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "SPOOL_PATH", str(tmp_path / "spool.jsonl"))
    monkeypatch.setattr(bridge, "SPOOL_DRAIN_MAX", 5)
    monkeypatch.setattr(bridge, "SPOOL_MAX_BATCHES", 100)
    return tmp_path / "spool.jsonl"


def _posts(monkeypatch, outcomes):
    """Stub post() with a scripted sequence of results, recording what it saw."""
    seen, results = [], list(outcomes)

    def fake_post(records):
        seen.append(records)
        return results.pop(0) if results else True

    monkeypatch.setattr(bridge, "post", fake_post)
    return seen


def test_a_batch_the_app_will_not_take_is_kept_not_dropped(monkeypatch, _spool_file):
    seen = _posts(monkeypatch, [False])
    bridge.send([{"vin": "A", "createdAt": "1"}])
    assert seen == [[{"vin": "A", "createdAt": "1"}]]
    assert json.loads(_spool_file.read_text().strip()) == [{"vin": "A", "createdAt": "1"}]


def test_the_spool_goes_out_before_the_live_batch(monkeypatch, _spool_file):
    """Order is the whole point.

    Spooled records are older than live ones and the app builds a trip from a
    stream it reads in order. Send the new batch first and the recovered one
    arrives looking like the odometer running backwards — the exact state the
    trip machine refuses, and it would count them as replays and discard them.
    """
    _posts(monkeypatch, [False])
    bridge.send([{"n": 1}])                       # stuck

    seen = _posts(monkeypatch, [True, True])
    bridge.send([{"n": 2}])                       # app is back
    assert seen == [[{"n": 1}], [{"n": 2}]], "older batch must go first"
    assert not _spool_file.exists()


def test_a_live_batch_queues_behind_a_drain_that_failed(monkeypatch, _spool_file):
    """If the old one still will not go, the new one must not overtake it."""
    _posts(monkeypatch, [False])
    bridge.send([{"n": 1}])

    seen = _posts(monkeypatch, [False])
    bridge.send([{"n": 2}])
    assert seen == [[{"n": 1}]], "the live batch is not even attempted"
    lines = [json.loads(l) for l in _spool_file.read_text().splitlines()]
    assert lines == [[{"n": 1}], [{"n": 2}]]


def test_a_partial_drain_keeps_the_rest_in_order(monkeypatch, _spool_file):
    for i in range(4):
        _posts(monkeypatch, [False])
        bridge.send([{"n": i}])

    # Two get through, the third is refused: the last two must survive intact.
    _posts(monkeypatch, [True, True, False])
    assert bridge.drain() is False
    lines = [json.loads(l) for l in _spool_file.read_text().splitlines()]
    assert lines == [[{"n": 2}], [{"n": 3}]]


def test_a_rejected_batch_is_not_spooled(monkeypatch, _spool_file):
    """post() returns True for a 4xx on purpose.

    A wrong key or a missing endpoint will not fix itself, and spooling it
    would build a backlog that can never drain — and that backlog would then
    block every live batch behind it for ever.
    """
    _posts(monkeypatch, [True])
    bridge.send([{"n": 1}])
    assert not _spool_file.exists()


def test_the_spool_is_bounded_and_keeps_the_newest(monkeypatch, _spool_file):
    monkeypatch.setattr(bridge, "SPOOL_MAX_BATCHES", 3)
    _posts(monkeypatch, [False] * 6)
    for i in range(5):
        bridge.send([{"n": i}])
    lines = [json.loads(l) for l in _spool_file.read_text().splitlines()]
    assert lines == [[{"n": 2}], [{"n": 3}], [{"n": 4}]]


def test_an_unreadable_spool_line_is_discarded_rather_than_blocking(monkeypatch,
                                                                   _spool_file):
    """One corrupt line must not wedge every batch behind it for ever."""
    _spool_file.write_text('not json\n' + json.dumps([{"n": 1}]) + "\n")
    seen = _posts(monkeypatch, [True])
    assert bridge.drain() is True
    assert seen == [[{"n": 1}]]
    assert not _spool_file.exists()
