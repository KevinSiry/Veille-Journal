from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta, timezone

import pytest

from veille.db.store import (
    from_db_datetime,
    fts_query,
    pack_embedding,
    to_db_datetime,
    unpack_embedding,
)


def test_datetime_roundtrip_is_utc() -> None:
    paris = timezone(timedelta(hours=1))
    moment = datetime(2026, 11, 8, 7, 30, tzinfo=paris)
    stored = to_db_datetime(moment)
    assert stored == "2026-11-08T06:30:00.000000+00:00"
    back = from_db_datetime(stored)
    assert back == moment
    assert back.tzinfo == UTC


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_db_datetime(datetime(2026, 1, 1))


def test_from_db_datetime_assumes_utc_for_naive_text() -> None:
    assert from_db_datetime("2026-01-01T00:00:00").tzinfo == UTC


def test_embedding_is_float32_little_endian() -> None:
    blob = pack_embedding((1.0, -2.5))
    assert blob == struct.pack("<2f", 1.0, -2.5)
    assert unpack_embedding(blob) == (1.0, -2.5)
    assert pack_embedding(None) is None
    assert unpack_embedding(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("élection présidentielle", '"élection" "présidentielle"'),
        ('say "hello', '"say" "hello"'),
        ("AND OR NOT", '"AND" "OR" "NOT"'),
        ("clim*", '"clim"*'),
        ("( ) * \" - ''", None),
        ("", None),
        ("   ", None),
        ("l'IA", '"l\'IA"'),
    ],
)
def test_fts_query(raw: str, expected: str | None) -> None:
    assert fts_query(raw) == expected
