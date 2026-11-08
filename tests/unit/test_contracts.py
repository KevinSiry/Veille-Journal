from datetime import UTC, date, datetime

from veille.clock import FixedClock, day_start_utc, local_day
from veille.config import Settings
from veille.contracts import ArticleDraft, ArticleStatus, RunStats, ThemeStats


def test_local_day_uses_paris_time() -> None:
    s = Settings()
    late = datetime(2026, 11, 7, 23, 30, tzinfo=UTC)  # 00:30 in Paris
    assert local_day(late, s.tz) == date(2026, 11, 8)
    assert day_start_utc(date(2026, 11, 8), s.tz) == datetime(2026, 11, 7, 23, 0, tzinfo=UTC)


def test_settings_parse_run_at(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("RUN_AT", "06:15")
    assert Settings().run_at.hour == 6


def test_run_stats_json() -> None:
    stats = RunStats(themes=(ThemeStats(theme_id=1, theme_name="x", kept=2),), duration_s=1.0)
    assert stats.to_json_dict()["themes"][0]["kept"] == 2
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    draft = ArticleDraft(
        1, local_day(clock.now(), Settings().tz), "t", "b", "m", clock.now(), ArticleStatus.EMPTY
    )
    assert draft.entry_count == 0
