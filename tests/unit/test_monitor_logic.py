"""Decision logic of the production monitor (scripts/monitor/monitor.py): trading days, freshness windows,
thresholds and what happens to the monitor issue. No network."""
import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "monitor" / "monitor.py"
_spec = importlib.util.spec_from_file_location("ab_monitor", _PATH)
monitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitor)

ET = ZoneInfo("America/New_York")


def et(*args) -> datetime:
    return datetime(*args, tzinfo=ET).astimezone(timezone.utc)


# --- NYSE calendar -----------------------------------------------------------------------------------

class TestCalendar:
    def test_weekends_and_holidays_are_not_trading_days(self):
        assert monitor.is_trading_day(date(2026, 10, 13))          # Tuesday
        assert not monitor.is_trading_day(date(2026, 10, 17))      # Saturday
        assert not monitor.is_trading_day(date(2026, 11, 26))      # Thanksgiving
        assert not monitor.is_trading_day(date(2026, 12, 25))      # Christmas
        assert not monitor.is_trading_day(date(2027, 7, 5))        # Independence Day observed

    def test_regular_session_boundaries(self):
        assert not monitor.market_is_open(et(2026, 10, 13, 9, 29))
        assert monitor.market_is_open(et(2026, 10, 13, 9, 30))
        assert monitor.market_is_open(et(2026, 10, 13, 15, 59))
        assert not monitor.market_is_open(et(2026, 10, 13, 16, 0))

    def test_early_close_and_holiday(self):
        assert monitor.market_is_open(et(2026, 11, 27, 12, 59))    # day after Thanksgiving
        assert not monitor.market_is_open(et(2026, 11, 27, 13, 0))
        assert not monitor.market_is_open(et(2026, 11, 26, 11, 0))  # Thanksgiving

    def test_session_uses_eastern_time_across_dst(self):
        # Nov 2, 2026 is after the DST change: the open is 14:30 UTC, not 13:30 UTC
        assert not monitor.market_is_open(datetime(2026, 11, 2, 14, 29, tzinfo=timezone.utc))
        assert monitor.market_is_open(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc))


# --- Thresholds ---------------------------------------------------------------------------------------

class TestThresholds:
    def test_health(self):
        assert monitor.eval_health(200, 0.4).ok
        assert not monitor.eval_health(200, 5.1).ok
        assert not monitor.eval_health(503, 0.2).ok
        assert not monitor.eval_health(0, 20.0).ok          # no response

    def test_home_needs_the_headline(self):
        assert monitor.eval_home(200, "<h1>Receipts for market calls.</h1>").ok
        assert monitor.eval_home(200, "<h1>Practice trading stocks. Free.</h1>").ok
        assert monitor.eval_home(200, "<p>Receipts for every trade</p>").ok
        assert not monitor.eval_home(200, "<h1>Something else</h1>").ok
        assert not monitor.eval_home(500, "").ok

    def test_digest_speed(self):
        assert monitor.eval_digest_speed(200, 2.9).ok
        assert not monitor.eval_digest_speed(200, 3.0).ok
        assert not monitor.eval_digest_speed(502, 0.1).ok

    def test_mcp_tool_count(self):
        assert monitor.eval_mcp("Alpha Bazaar Arena", 24).ok
        assert not monitor.eval_mcp("Alpha Bazaar Arena", 23).ok
        assert not monitor.eval_mcp(None, 24).ok
        assert not monitor.eval_mcp(None, None, "initialize HTTP 500").ok

    def test_leaderboard(self):
        assert monitor.eval_leaderboard(200, {"leaderboard": [{"rank": 1}]}).ok
        assert not monitor.eval_leaderboard(200, {"leaderboard": []}).ok
        assert not monitor.eval_leaderboard(404, None).ok

    def test_forced_probe_fails_on_anything_but_200(self):
        assert not monitor.eval_forced("https://example.invalid/x", 404).ok
        assert monitor.eval_forced("https://example.invalid/x", 200).ok


# --- Freshness ------------------------------------------------------------------------------------------

class TestFreshness:
    def test_digest_freshness_only_counts_during_market_hours(self):
        now = et(2026, 10, 13, 11, 0)
        fresh = {"generated_at": (now - timedelta(minutes=29)).isoformat().replace("+00:00", "Z")}
        stale = {"generated_at": (now - timedelta(minutes=31)).isoformat().replace("+00:00", "Z")}
        assert monitor.eval_digest_fresh(fresh, now).ok
        assert not monitor.eval_digest_fresh(stale, now).ok
        assert not monitor.eval_digest_fresh({}, now).ok
        evening = et(2026, 10, 13, 20, 0)
        result = monitor.eval_digest_fresh(stale, evening)
        assert result.ok and result.skipped

    def test_naive_timestamps_are_utc(self):
        now = et(2026, 10, 13, 11, 0)
        naive = (now - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        assert monitor.eval_digest_fresh({"generated_at": naive}, now).ok

    def test_pulse_freshness(self):
        now = et(2026, 10, 13, 14, 0)
        assert monitor.eval_pulse_fresh({"as_of": (now - timedelta(minutes=14)).isoformat()}, now).ok
        assert not monitor.eval_pulse_fresh({"as_of": (now - timedelta(minutes=16)).isoformat()}, now).ok
        assert monitor.eval_pulse_fresh({}, et(2026, 10, 17, 14, 0)).skipped    # Saturday

    def test_price_freshness_from_health_data(self):
        def data(age_minutes, count=480):
            return {"prices": {"latest_update_age_seconds": age_minutes * 60, "updated_last_30_min": count}}
        now = et(2026, 10, 13, 14, 0)
        assert monitor.eval_prices_fresh(data(29), now).ok
        stale = monitor.eval_prices_fresh(data(31, count=0), now)
        assert not stale.ok and "31 min old" in stale.detail
        assert not monitor.eval_prices_fresh({"prices": {"error": "price query failed"}}, now).ok
        assert not monitor.eval_prices_fresh({"prices": {"latest_update_age_seconds": None}}, now).ok
        assert not monitor.eval_prices_fresh({}, now).ok
        # Overnight prices are hours old until the first post-open writes: skipped until 9:50 ET
        assert monitor.eval_prices_fresh(data(900), et(2026, 10, 13, 9, 49)).skipped
        assert not monitor.eval_prices_fresh(data(900), et(2026, 10, 13, 9, 50)).ok
        assert monitor.eval_prices_fresh(data(900), et(2026, 10, 13, 16, 30)).skipped   # after the close
        assert monitor.eval_prices_fresh(data(900), et(2026, 11, 26, 12, 0)).skipped    # Thanksgiving

    def test_price_check_is_registered(self):
        assert monitor.check_prices in monitor.CHECKS

    def test_brief_is_due_at_ten_on_trading_days(self):
        brief = {"brief": {"date": "2026-10-13", "text": "The market...", "kind": "ai"}}
        assert monitor.eval_brief({}, et(2026, 10, 13, 9, 59)).skipped
        assert monitor.eval_brief(brief, et(2026, 10, 13, 10, 5)).ok
        assert not monitor.eval_brief({"brief": None}, et(2026, 10, 13, 10, 5)).ok
        stale = {"brief": {"date": "2026-10-12", "text": "Yesterday"}}
        assert not monitor.eval_brief(stale, et(2026, 10, 13, 10, 5)).ok
        assert monitor.eval_brief({}, et(2026, 11, 26, 12, 0)).skipped         # Thanksgiving

    def test_insider_moves_due_by_noon(self):
        assert monitor.eval_insider({"moves": []}, et(2026, 10, 13, 11, 59)).skipped
        assert not monitor.eval_insider({"moves": []}, et(2026, 10, 13, 12, 0)).ok
        assert monitor.eval_insider({"moves": [{"ticker": "PAGS"}]}, et(2026, 10, 13, 12, 0)).ok
        assert monitor.eval_insider({"moves": []}, et(2026, 10, 18, 13, 0)).skipped   # Sunday


# --- Issue decisions ---------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 13, 18, 7, tzinfo=timezone.utc)


def failing(*names):
    return [monitor.fail(n, f"{n} broke") for n in names] + [monitor.ok("api health", "0.3s")]


class TestDecide:
    def test_all_good_and_no_issue_does_nothing(self):
        assert monitor.decide(None, [monitor.ok("api health", "0.3s")], NOW).kind == "noop"

    def test_skipped_checks_never_alert(self):
        results = [monitor.skip("daily brief", "due at 10:00 ET"), monitor.ok("mcp", "24 tools")]
        assert monitor.decide(None, results, NOW).kind == "noop"

    def test_first_failure_opens_one_issue_with_state(self):
        action = monitor.decide(None, failing("digest"), NOW, "https://run/1")
        assert action.kind == "create"
        assert "digest" in action.title and "1 check failing" in action.title
        assert "First seen: 2026-10-13 18:07 UTC" in action.body and "https://run/1" in action.body
        assert monitor.parse_state(action.body) == {"first_seen": "2026-10-13 18:07 UTC", "failing": ["digest"]}

    def test_still_failing_updates_and_keeps_first_seen_without_a_comment(self):
        created = monitor.decide(None, failing("digest"), NOW)
        issue = {"number": 7, "body": created.body}
        later = NOW + timedelta(hours=2)
        action = monitor.decide(issue, failing("digest"), later)
        assert action.kind == "update"
        assert "First seen: 2026-10-13 18:07 UTC" in action.body
        assert "Last seen: 2026-10-13 20:07 UTC" in action.body
        assert action.comment == ""                                   # nothing new: no notification

    def test_new_failure_while_open_adds_a_comment(self):
        issue = {"number": 7, "body": monitor.decide(None, failing("digest"), NOW).body}
        action = monitor.decide(issue, failing("digest", "mcp"), NOW + timedelta(hours=1))
        assert action.kind == "update"
        assert action.comment.startswith("Newly failing: mcp")
        assert monitor.parse_state(action.body)["failing"] == ["digest", "mcp"]

    def test_recovery_comments_and_closes(self):
        issue = {"number": 7, "body": monitor.decide(None, failing("digest"), NOW).body}
        action = monitor.decide(issue, [monitor.ok("digest", "0.2s")], NOW + timedelta(hours=3), "https://run/9")
        assert action.kind == "close"
        assert "digest failing since 2026-10-13 18:07 UTC" in action.comment and "https://run/9" in action.comment

    def test_issue_without_state_still_updates(self):
        action = monitor.decide({"number": 3, "body": "edited by hand"}, failing("skill.md"), NOW)
        assert action.kind == "update"
        assert "First seen: 2026-10-13 18:07 UTC" in action.body
        assert action.comment.startswith("Newly failing: skill.md")

    @pytest.mark.parametrize("body", [None, "", "<!-- monitor-state {broken json} -->"])
    def test_parse_state_is_robust(self, body):
        assert monitor.parse_state(body) == {}


def test_run_checks_retries_failures_once(monkeypatch):
    calls = {"flaky": 0}

    def flaky(now):
        calls["flaky"] += 1
        return [monitor.ok("flaky", "ok") if calls["flaky"] > 1 else monitor.fail("flaky", "blip")]

    def steady(now):
        return [monitor.ok("steady", "ok")]

    monkeypatch.setattr(monitor, "CHECKS", [flaky, steady])
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    results = monitor.run_checks(NOW, retry_delay=1)
    assert calls["flaky"] == 2
    assert all(r.ok for r in results)


class TestArenaConsistency:
    ROW = {"username": "a", "wins": 11, "losses": 7, "closed_wins": 4, "closed_losses": 2, "open_signals": 12,
           "signals": 18, "ranked": True, "rank": 1, "avg_return": 0.7}

    def payload(self, *rows, basis=None):
        season = {"min_signals": 10, **({"scoring_basis": basis} if basis else {})}
        return {"season": season, "standings": list(rows)}

    def test_a_consistent_board_passes(self):
        second = dict(self.ROW, username="b", rank=2, avg_return=0.5)
        assert monitor.eval_arena_consistency(200, self.payload(self.ROW, second)).ok

    def test_closed_plus_open_must_add_up(self):
        waiting = dict(self.ROW, open_signals=13)  # one trade waiting for the next open isn't scored yet
        assert monitor.eval_arena_consistency(200, self.payload(waiting)).ok
        bad = dict(self.ROW, open_signals=5)
        r = monitor.eval_arena_consistency(200, self.payload(bad))
        assert not r.ok and "closed" in r.detail

    def test_closed_only_scores_closed_trades(self):
        row = dict(self.ROW, wins=4, losses=2, signals=10)
        assert monitor.eval_arena_consistency(200, self.payload(row, basis="closed_only")).ok
        assert not monitor.eval_arena_consistency(200, self.payload(self.ROW, basis="closed_only")).ok

    def test_ranked_below_minimum_and_rank_order(self):
        low = dict(self.ROW, signals=3)
        assert not monitor.eval_arena_consistency(200, self.payload(low)).ok
        worse_first = dict(self.ROW, avg_return=0.1)
        better_second = dict(self.ROW, username="b", rank=2, avg_return=0.9)
        assert not monitor.eval_arena_consistency(200, self.payload(worse_first, better_second)).ok

    def test_old_rows_without_new_fields_pass(self):
        row = {k: v for k, v in self.ROW.items() if k not in ("closed_wins", "closed_losses", "open_signals")}
        assert monitor.eval_arena_consistency(200, self.payload(row)).ok
        assert not monitor.eval_arena_consistency(503, None).ok
