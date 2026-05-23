from datetime import datetime, timezone
from typing import Any

import pytest

from vortexec.core.types import Diff, Side
from vortexec.venues.base import SequenceGapError
from vortexec.venues.binance import _Aligner, _parse_diff_message


def _msg(U: int, u: int) -> dict[str, Any]:
    return {"U": U, "u": u}


def test_parse_diff_message_emits_bids_then_asks() -> None:
    data = {
        "e": "depthUpdate",
        "E": 1700000000000,
        "s": "BTCUSDT",
        "U": 100,
        "u": 105,
        "b": [["100.50", "1.5"], ["100.40", "0.0"]],
        "a": [["100.60", "0.0"], ["100.70", "2.5"]],
    }
    ts = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert _parse_diff_message(data) == [
        Diff(side=Side.BUY, price=100.50, quantity=1.5, timestamp=ts),
        Diff(side=Side.BUY, price=100.40, quantity=0.0, timestamp=ts),
        Diff(side=Side.SELL, price=100.60, quantity=0.0, timestamp=ts),
        Diff(side=Side.SELL, price=100.70, quantity=2.5, timestamp=ts),
    ]


def test_parse_diff_message_handles_empty_sides() -> None:
    data = {"E": 1700000000000, "b": [], "a": []}
    assert _parse_diff_message(data) == []


def test_parse_diff_message_preserves_zero_quantity() -> None:
    # Zero qty means "remove this level" — the parser keeps it; book.apply_diff decides.
    data = {"E": 1700000000000, "b": [["100.00", "0.0"]], "a": []}
    diffs = _parse_diff_message(data)
    assert len(diffs) == 1
    assert diffs[0].quantity == 0.0
    assert diffs[0].side is Side.BUY


def test_parse_diff_message_timestamp_uses_ms_event_time() -> None:
    data = {"E": 1700000000123, "b": [["100.0", "1.0"]], "a": []}
    diffs = _parse_diff_message(data)
    expected = datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=timezone.utc)
    assert diffs[0].timestamp == expected


def test_aligner_skips_messages_fully_before_snapshot() -> None:
    aligner = _Aligner(last_update_id=100)
    assert aligner.should_emit(_msg(U=95, u=99)) is False
    assert aligner.should_emit(_msg(U=90, u=100)) is False  # u == snap_id also skipped


def test_aligner_accepts_first_message_bridging_snap_plus_one() -> None:
    aligner = _Aligner(last_update_id=100)
    # 95 <= 101 <= 105 — message spans the boundary
    assert aligner.should_emit(_msg(U=95, u=105)) is True


def test_aligner_accepts_first_message_starting_exactly_at_snap_plus_one() -> None:
    aligner = _Aligner(last_update_id=100)
    assert aligner.should_emit(_msg(U=101, u=105)) is True


def test_aligner_raises_when_first_message_starts_after_snap_plus_one() -> None:
    aligner = _Aligner(last_update_id=100)
    with pytest.raises(SequenceGapError):
        aligner.should_emit(_msg(U=102, u=110))


def test_aligner_accepts_contiguous_sequence() -> None:
    aligner = _Aligner(last_update_id=100)
    assert aligner.should_emit(_msg(U=101, u=105)) is True
    assert aligner.should_emit(_msg(U=106, u=110)) is True
    assert aligner.should_emit(_msg(U=111, u=115)) is True


def test_aligner_raises_on_mid_stream_gap() -> None:
    aligner = _Aligner(last_update_id=100)
    aligner.should_emit(_msg(U=101, u=105))
    with pytest.raises(SequenceGapError):
        aligner.should_emit(_msg(U=107, u=110))  # expected U=106
