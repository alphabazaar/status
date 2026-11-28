"""Alpha Bazaar production monitor, run by .github/workflows/monitor.yml (stdlib only, no installs).

Checks public production endpoints for uptime and data freshness, then keeps ONE GitHub issue labeled
`monitor` in step with the result: opened on the first failure (GitHub emails the repo owner), updated while
anything still fails, commented and closed on recovery. No secrets are needed for the checks themselves;
GITHUB_TOKEN is only used for the issue.

Environment:
  GITHUB_TOKEN, GITHUB_REPOSITORY   issue writes (without a token the run only reports)
  MONITOR_DRY_RUN=1                 report only, never touch the issue
  MONITOR_FAIL_URL                  test only: an extra check against a URL that should fail (proves the alert path)
  MONITOR_RUN_URL                   link to this run, shown in the issue
  MONITOR_API_BASE / MONITOR_WWW_BASE   override the production URLs

The decision logic (trading days, freshness windows, thresholds, issue actions) is pure functions, covered by
tests/unit/test_monitor_logic.py.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

API_BASE = os.getenv("MONITOR_API_BASE", "https://api.alphabazaar.ai").rstrip("/")
WWW_BASE = os.getenv("MONITOR_WWW_BASE", "https://www.alphabazaar.ai").rstrip("/")
USER_AGENT = "AlphaBazaar-Monitor/1.0 (+https://github.com/alphabazaar/status)"
ISSUE_LABEL = "monitor"
STATE_MARKER = "monitor-state"

EASTERN = ZoneInfo("America/New_York")
REGULAR_OPEN = dt_time(9, 30)
REGULAR_CLOSE = dt_time(16, 0)
EARLY_CLOSE = dt_time(13, 0)

# NYSE full-day holidays and 1 p.m. early closes, 2026-2027: the table in utils/time_utils.py, from
# https://www.nyse.com/markets/hours-calendars (checked 2026-10-12). Extend both when 2028 is published.
NYSE_HOLIDAYS = frozenset({
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
})
NYSE_EARLY_CLOSES = frozenset({date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)})

# Thresholds
HEALTH_MAX_SECONDS = 5.0
DIGEST_MAX_SECONDS = 3.0
DIGEST_MAX_AGE = timedelta(minutes=30)      # during market hours
PULSE_MAX_AGE = timedelta(minutes=15)       # during market hours
PRICES_MAX_AGE = timedelta(minutes=30)      # latest ticker_prices write, during market hours (written every 5 min)
PRICES_OPEN_GRACE = timedelta(minutes=20)   # after the open, before prices must be moving
BRIEF_DUE = dt_time(10, 0)                  # ET, trading days
INSIDER_DUE = dt_time(12, 0)                # ET, trading days
MIN_MCP_TOOLS = 24  # at least this many: new tools shouldn't page anyone, losing tools should
# Any one of these on the home page counts as "the real site is up" (the headline is being reworded)
HOME_TEXTS = ("Receipts for market calls", "Receipts for every trade", "Practice trading stocks")
REQUEST_TIMEOUT = 20


# --- NYSE calendar ---------------------------------------------------------------------------

def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in NYSE_HOLIDAYS


def market_session(day: date) -> Optional[Tuple[datetime, datetime]]:
    """Open and close (ET, aware) of the day's regular session, or None when the market is closed all day."""
    if not is_trading_day(day):
        return None
    close = EARLY_CLOSE if day in NYSE_EARLY_CLOSES else REGULAR_CLOSE
    return (datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN), datetime.combine(day, close, tzinfo=EASTERN))


def market_is_open(now: datetime) -> bool:
    et = now.astimezone(EASTERN)
    session = market_session(et.date())
    return session is not None and session[0] <= et < session[1]


# --- Results -------------------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    skipped: bool = False   # not applicable right now (e.g. a freshness check outside market hours)


def ok(name: str, detail: str) -> Result:
    return Result(name, True, detail)


def fail(name: str, detail: str) -> Result:
    return Result(name, False, detail)


def skip(name: str, detail: str) -> Result:
    return Result(name, True, detail, skipped=True)


def parse_ts(value: Any) -> Optional[datetime]:
    """ISO-8601 timestamp from the API ('...Z', '+00:00' or naive UTC) as an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age(ts: datetime, now: datetime) -> str:
    minutes = max(0, int((now - ts).total_seconds() // 60))
    return f"{minutes} min old"


# --- Pure evaluators (one per check) ---------------------------------------------------------------

def eval_health(status: int, seconds: float) -> Result:
    if status != 200:
        return fail("api health", f"HTTP {status or 'no response'}")
    if seconds > HEALTH_MAX_SECONDS:
        return fail("api health", f"{seconds:.1f}s (limit {HEALTH_MAX_SECONDS:.0f}s)")
    return ok("api health", f"{seconds:.2f}s")


def eval_home(status: int, text: str) -> Result:
    if status != 200:
        return fail("website home", f"HTTP {status or 'no response'}")
    if not any(t in (text or "") for t in HOME_TEXTS):
        return fail("website home", "200 but none of the expected headlines is on the page")
    return ok("website home", "200")


def eval_digest_speed(status: int, seconds: float) -> Result:
    if status != 200:
        return fail("digest", f"HTTP {status or 'no response'}")
    if seconds >= DIGEST_MAX_SECONDS:
        return fail("digest", f"{seconds:.1f}s (limit {DIGEST_MAX_SECONDS:.0f}s; the cache warmer should keep it fast)")
    return ok("digest", f"{seconds:.2f}s")


def eval_status(name: str, status: int) -> Result:
    return ok(name, "200") if status == 200 else fail(name, f"HTTP {status or 'no response'}")


def eval_mcp(server_name: Optional[str], tool_count: Optional[int], error: Optional[str] = None) -> Result:
    if error:
        return fail("mcp", error)
    if not server_name:
        return fail("mcp", "initialize returned no serverInfo")
    if tool_count is None or tool_count < MIN_MCP_TOOLS:
        return fail("mcp", f"{tool_count} tools (expected at least {MIN_MCP_TOOLS})")
    return ok("mcp", f"{tool_count} tools")


def eval_leaderboard(status: int, payload: Any) -> Result:
    if status != 200:
        return fail("leaderboard", f"HTTP {status or 'no response'}")
    rows = payload.get("leaderboard") if isinstance(payload, dict) else None
    if not rows:
        return fail("leaderboard", "200 but no rows")
    return ok("leaderboard", f"{len(rows)} rows")


def eval_arena_consistency(status: int, payload: Any) -> Result:
    """The Arena standings agree with themselves (owner request #35, after bug #34): every row's closed record plus
    its open trades adds up to the trades it's scored on, closed-only seasons score closed trades only, ranked players
    meet the minimum, and ranks follow the average return."""
    name = "arena data"
    if status != 200 or not isinstance(payload, dict):
        return fail(name, f"HTTP {status or 'no response'}")
    season = payload.get("season") or {}
    basis = season.get("scoring_basis") or payload.get("scoring_basis") or "mark_to_market"
    minimum = season.get("min_signals") or 10
    rows = payload.get("standings") or []
    problems: List[str] = []
    for row in rows:
        who = row.get("username", "?")
        cw, cl, op = row.get("closed_wins"), row.get("closed_losses"), row.get("open_signals")
        wins, losses = row.get("wins") or 0, row.get("losses") or 0
        if None not in (cw, cl, op):
            if basis == "closed_only":
                if (wins, losses) != (cw, cl):
                    problems.append(f"{who}: {wins}-{losses} scored but {cw}-{cl} closed")
            elif cw + cl > wins + losses or cw + cl + op < wins + losses:
                # Open trades include ones waiting for the next open's price, which aren't scored yet, so closed + open
                # can be more than the scored count, never less, and closed alone can't exceed it
                problems.append(f"{who}: {cw}-{cl} closed + {op} open vs {wins + losses} scored")
        if row.get("ranked") and (row.get("signals") or 0) < minimum:
            problems.append(f"{who}: ranked with {row.get('signals')} trades (minimum {minimum})")
    ranked = sorted((r for r in rows if r.get("ranked") and r.get("rank")), key=lambda r: r["rank"])
    if [r["rank"] for r in ranked] != list(range(1, len(ranked) + 1)):
        problems.append("ranks aren't 1..N")
    returns = [r.get("avg_return") or 0 for r in ranked]
    if any(a < b for a, b in zip(returns, returns[1:])):
        problems.append("ranks don't follow average return")
    if problems:
        return fail(name, "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
    return ok(name, f"{len(rows)} rows consistent ({basis})")


def eval_digest_fresh(payload: Any, now: datetime) -> Result:
    if not market_is_open(now):
        return skip("digest freshness", "market closed")
    ts = parse_ts(payload.get("generated_at") if isinstance(payload, dict) else None)
    if ts is None:
        return fail("digest freshness", "no generated_at")
    if now - ts > DIGEST_MAX_AGE:
        return fail("digest freshness", f"generated_at {_age(ts, now)} (limit {int(DIGEST_MAX_AGE.total_seconds() // 60)} min)")
    return ok("digest freshness", _age(ts, now))


def eval_pulse_fresh(payload: Any, now: datetime) -> Result:
    if not market_is_open(now):
        return skip("market pulse freshness", "market closed")
    ts = parse_ts(payload.get("as_of") if isinstance(payload, dict) else None)
    if ts is None:
        return fail("market pulse freshness", "no as_of")
    if now - ts > PULSE_MAX_AGE:
        return fail("market pulse freshness", f"as_of {_age(ts, now)} (limit {int(PULSE_MAX_AGE.total_seconds() // 60)} min)")
    return ok("market pulse freshness", _age(ts, now))


def eval_prices_fresh(payload: Any, now: datetime) -> Result:
    """From /health/data: the latest price write. Pulse's as_of is its build time, so it can't see stale prices."""
    name = "price freshness"
    if not market_is_open(now):
        return skip(name, "market closed")
    et = now.astimezone(EASTERN)
    session = market_session(et.date())
    if session and et < session[0] + PRICES_OPEN_GRACE:
        return skip(name, f"first {int(PRICES_OPEN_GRACE.total_seconds() // 60)} min after the open")
    prices = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(prices, dict):
        return fail(name, "no prices in /health/data")
    if prices.get("error"):
        return fail(name, str(prices["error"]))
    age = prices.get("latest_update_age_seconds")
    if not isinstance(age, (int, float)):
        return fail(name, "no price writes recorded")
    detail = f"latest write {int(age // 60)} min old, {prices.get('updated_last_30_min')} tickers in 30 min"
    if age > PRICES_MAX_AGE.total_seconds():
        return fail(name, f"{detail} (limit {int(PRICES_MAX_AGE.total_seconds() // 60)} min)")
    return ok(name, detail)


def eval_brief(payload: Any, now: datetime) -> Result:
    et = now.astimezone(EASTERN)
    if not is_trading_day(et.date()):
        return skip("daily brief", "not a trading day")
    if et.time() < BRIEF_DUE:
        return skip("daily brief", "due at 10:00 ET")
    brief = payload.get("brief") if isinstance(payload, dict) else None
    if not isinstance(brief, dict) or not (brief.get("text") or "").strip():
        return fail("daily brief", "no brief after 10:00 ET")
    if brief.get("date") != et.date().isoformat():
        return fail("daily brief", f"brief is for {brief.get('date')}, not {et.date().isoformat()}")
    return ok("daily brief", f"{brief.get('kind') or 'brief'} for {brief.get('date')}")


def eval_insider(payload: Any, now: datetime) -> Result:
    et = now.astimezone(EASTERN)
    if not is_trading_day(et.date()):
        return skip("insider moves", "not a trading day")
    if et.time() < INSIDER_DUE:
        return skip("insider moves", "due by noon ET")
    moves = payload.get("moves") if isinstance(payload, dict) else None
    if not moves:
        return fail("insider moves", "empty after noon ET")
    return ok("insider moves", f"{len(moves)} moves")


def eval_forced(url: str, status: int) -> Result:
    """Test-only probe (MONITOR_FAIL_URL): passes only on 200, so a definitely-failing URL proves the alert path."""
    name = "forced test probe"
    return ok(name, f"{url} 200") if status == 200 else fail(name, f"{url} HTTP {status or 'no response'}")


# --- Issue decisions -----------------------------------------------------------------------------

@dataclass
class IssueAction:
    kind: str                       # "noop" | "create" | "update" | "close"
    title: str = ""
    body: str = ""
    comment: str = ""               # posted with update (only when new checks start failing) and close
    state: Dict[str, Any] = field(default_factory=dict)


def parse_state(body: Optional[str]) -> Dict[str, Any]:
    match = re.search(r"<!--\s*" + STATE_MARKER + r"\s+(\{.*?\})\s*-->", body or "", re.S)
    if not match:
        return {}
    try:
        state = json.loads(match.group(1))
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _fmt(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render_body(failures: List[Result], first_seen: datetime, last_seen: datetime, run_url: str,
                state: Dict[str, Any]) -> str:
    rows = "\n".join(f"| {r.name} | {r.detail.replace('|', '/')} |" for r in failures)
    run_line = f"\nLatest run: {run_url}\n" if run_url else "\n"
    return (
        "The production monitor found checks failing.\n\n"
        "| Check | Value |\n|---|---|\n"
        f"{rows}\n\n"
        f"- First seen: {_fmt(first_seen)}\n"
        f"- Last seen: {_fmt(last_seen)}\n"
        f"{run_line}\n"
        "This issue updates on every run while anything fails and closes itself when all checks pass. "
        "Checks and thresholds: `scripts/monitor/monitor.py`.\n\n"
        f"<!-- {STATE_MARKER} {json.dumps(state, sort_keys=True)} -->\n"
    )


def decide(open_issue: Optional[Dict[str, Any]], results: List[Result], now: datetime, run_url: str = "") -> IssueAction:
    """What to do with the monitor issue given this run's results (pure; tested)."""
    failures = [r for r in results if not r.ok]
    failing = sorted(r.name for r in failures)
    if not failures:
        if not open_issue:
            return IssueAction("noop")
        state = parse_state(open_issue.get("body"))
        since = state.get("first_seen", "unknown")
        was = ", ".join(state.get("failing", [])) or "checks"
        return IssueAction("close", comment=(
            f"All checks pass again at {_fmt(now)} ({was} failing since {since}). Closing."
            + (f"\n\nRun: {run_url}" if run_url else "")))
    title = f"Production monitor: {len(failing)} check{'s' if len(failing) != 1 else ''} failing ({', '.join(failing)})"
    if not open_issue:
        state = {"first_seen": _fmt(now), "failing": failing}
        return IssueAction("create", title=title, body=render_body(failures, now, now, run_url, state), state=state)
    previous = parse_state(open_issue.get("body"))
    first_seen_text = previous.get("first_seen") or _fmt(now)
    try:
        first_seen = datetime.strptime(first_seen_text, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        first_seen = now
    state = {"first_seen": _fmt(first_seen), "failing": failing}
    new_names = sorted(set(failing) - set(previous.get("failing", [])))
    comment = ""
    if new_names:
        comment = "Newly failing: " + "; ".join(f"{r.name}: {r.detail}" for r in failures if r.name in new_names)
    return IssueAction("update", title=title, body=render_body(failures, first_seen, now, run_url, state),
                       comment=comment, state=state)


# --- HTTP -----------------------------------------------------------------------------------------

def fetch(url: str, *, method: str = "GET", body: Optional[dict] = None, headers: Optional[dict] = None,
          timeout: float = REQUEST_TIMEOUT) -> Tuple[int, float, str, Dict[str, str]]:
    """(status, seconds, text, headers); status 0 when the request itself failed."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": USER_AGENT, **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, time.monotonic() - start, text, dict(resp.headers)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, time.monotonic() - start, text, dict(e.headers or {})
    except Exception as e:  # timeouts, DNS, TLS
        return 0, time.monotonic() - start, f"{type(e).__name__}: {e}", {}


def fetch_json(url: str) -> Tuple[int, float, Any]:
    status, seconds, text, _ = fetch(url)
    try:
        payload = json.loads(text) if status == 200 else None
    except ValueError:
        payload = None
    return status, seconds, payload


def _mcp_post(payload: dict, session: Optional[str]) -> Tuple[int, Any, Optional[str]]:
    headers = {"Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    status, _, text, resp_headers = fetch(f"{API_BASE}/mcp", method="POST", body=payload, headers=headers)
    session = session or next((v for k, v in resp_headers.items() if k.lower() == "mcp-session-id"), None)
    message = None
    if status == 200:
        if text.lstrip().startswith("{"):
            message = json.loads(text)
        else:  # server-sent events: take the last data line
            data_lines = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
            message = json.loads(data_lines[-1]) if data_lines else None
    return status, message, session


# --- Checks (network) -------------------------------------------------------------------------------

def check_health(now: datetime) -> List[Result]:
    status, seconds, _, _ = fetch(f"{API_BASE}/health")
    return [eval_health(status, seconds)]


def check_home(now: datetime) -> List[Result]:
    status, _, text, _ = fetch(f"{WWW_BASE}/")
    return [eval_home(status, text)]


def check_digest(now: datetime) -> List[Result]:
    status, seconds, payload = fetch_json(f"{API_BASE}/api/feed/daily-digest")
    results = [eval_digest_speed(status, seconds)]
    if status == 200:
        results.append(eval_digest_fresh(payload, now))
    return results


def check_mcp(now: datetime) -> List[Result]:
    try:
        status, init, session = _mcp_post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "ab-monitor", "version": "1.0"}}}, None)
        if status != 200 or not isinstance(init, dict) or "result" not in init:
            return [eval_mcp(None, None, f"initialize HTTP {status or 'no response'}")]
        server_name = ((init.get("result") or {}).get("serverInfo") or {}).get("name")
        _mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        status, listed, _ = _mcp_post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session)
        if status != 200 or not isinstance(listed, dict) or "result" not in listed:
            return [eval_mcp(server_name, None, f"tools/list HTTP {status or 'no response'}")]
        return [eval_mcp(server_name, len((listed.get("result") or {}).get("tools") or []))]
    except Exception as e:
        return [eval_mcp(None, None, f"{type(e).__name__}: {e}")]


def check_arena(now: datetime) -> List[Result]:
    status, _, _, _ = fetch(f"{API_BASE}/api/arena/seasons")
    return [eval_status("arena seasons", status)]


def check_arena_data(now: datetime) -> List[Result]:
    status, _, payload = fetch_json(f"{API_BASE}/api/arena/standings?limit=50")
    return [eval_arena_consistency(status, payload)]


def check_skill(now: datetime) -> List[Result]:
    status, _, _, _ = fetch(f"{WWW_BASE}/skill.md")
    return [eval_status("skill.md", status)]


def check_leaderboard(now: datetime) -> List[Result]:
    status, _, payload = fetch_json(f"{API_BASE}/api/leaderboard/community?limit=5")
    return [eval_leaderboard(status, payload)]


def check_pulse(now: datetime) -> List[Result]:
    if not market_is_open(now):
        return [skip("market pulse freshness", "market closed")]
    status, _, payload = fetch_json(f"{API_BASE}/api/market/pulse")
    if status != 200:
        return [fail("market pulse freshness", f"HTTP {status or 'no response'}")]
    return [eval_pulse_fresh(payload, now)]


def check_prices(now: datetime) -> List[Result]:
    first = eval_prices_fresh({}, now)
    if first.skipped:
        return [first]
    status, _, payload = fetch_json(f"{API_BASE}/health/data")
    if status != 200:
        return [fail("price freshness", f"/health/data HTTP {status or 'no response'}")]
    return [eval_prices_fresh(payload, now)]


def check_brief(now: datetime) -> List[Result]:
    first = eval_brief({}, now)
    if first.skipped:
        return [first]
    status, _, payload = fetch_json(f"{API_BASE}/api/feed/daily-brief")
    if status != 200:
        return [fail("daily brief", f"HTTP {status or 'no response'}")]
    return [eval_brief(payload, now)]


def check_insider(now: datetime) -> List[Result]:
    first = eval_insider({}, now)
    if first.skipped:
        return [first]
    status, _, payload = fetch_json(f"{API_BASE}/api/feed/insider-moves?limit=5")
    if status != 200:
        return [fail("insider moves", f"HTTP {status or 'no response'}")]
    return [eval_insider(payload, now)]


CHECKS: List[Callable[[datetime], List[Result]]] = [
    check_health, check_home, check_digest, check_mcp, check_arena, check_arena_data, check_skill, check_leaderboard,
    check_pulse, check_prices, check_brief, check_insider,
]


def run_checks(now: datetime, fail_url: str = "", retry_delay: float = 20.0) -> List[Result]:
    """Run every check; re-run each check that failed once after `retry_delay` so a single blip doesn't alert."""
    checks = list(CHECKS)
    if fail_url:
        checks.append(lambda _now, url=fail_url: [eval_forced(url, fetch(url)[0])])
    by_check = [(check, check(now)) for check in checks]
    failing = [check for check, results in by_check if any(not r.ok for r in results)]
    if failing and retry_delay:
        time.sleep(retry_delay)
        retried = {id(check): check(datetime.now(timezone.utc)) for check in failing}
        by_check = [(check, retried.get(id(check), results)) for check, results in by_check]
    return [r for _, results in by_check for r in results]


# --- GitHub issue I/O -----------------------------------------------------------------------------

class GitHub:
    def __init__(self, token: str, repo: str):
        self.token, self.repo = token, repo

    def call(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        status, _, text, _ = fetch(f"https://api.github.com/repos/{self.repo}{path}", method=method, body=body,
                                   headers={"Authorization": f"Bearer {self.token}",
                                            "Accept": "application/vnd.github+json",
                                            "X-GitHub-Api-Version": "2022-11-28"})
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, None

    def open_issue(self) -> Optional[Dict[str, Any]]:
        status, issues = self.call("GET", f"/issues?labels={ISSUE_LABEL}&state=open&per_page=10")
        if status != 200:
            raise RuntimeError(f"listing monitor issues failed: HTTP {status}")
        issues = [i for i in issues or [] if "pull_request" not in i]
        return min(issues, key=lambda i: i["number"]) if issues else None

    def ensure_label(self) -> None:
        status, _ = self.call("POST", "/labels", {"name": ISSUE_LABEL, "color": "d73a4a",
                                                  "description": "Opened and closed by the production monitor"})
        if status not in (201, 422):  # 422: already exists
            raise RuntimeError(f"creating the {ISSUE_LABEL} label failed: HTTP {status}")

    def apply(self, action: IssueAction, issue: Optional[Dict[str, Any]]) -> str:
        if action.kind == "noop":
            return "no issue needed"
        if action.kind == "create":
            self.ensure_label()
            status, created = self.call("POST", "/issues", {"title": action.title, "body": action.body,
                                                            "labels": [ISSUE_LABEL]})
            if status != 201:
                raise RuntimeError(f"creating the monitor issue failed: HTTP {status}")
            return f"opened issue #{created['number']}"
        number = issue["number"]
        if action.kind == "update":
            status, _ = self.call("PATCH", f"/issues/{number}", {"title": action.title, "body": action.body})
            if status != 200:
                raise RuntimeError(f"updating issue #{number} failed: HTTP {status}")
            if action.comment:
                self.call("POST", f"/issues/{number}/comments", {"body": action.comment})
            return f"updated issue #{number}" + (" (new failures commented)" if action.comment else "")
        if action.kind == "close":
            self.call("POST", f"/issues/{number}/comments", {"body": action.comment})
            status, _ = self.call("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})
            if status != 200:
                raise RuntimeError(f"closing issue #{number} failed: HTTP {status}")
            return f"closed issue #{number}"
        raise ValueError(action.kind)


# --- Main -------------------------------------------------------------------------------------------

def summary_table(results: List[Result]) -> str:
    lines = ["| Check | Result | Value |", "|---|---|---|"]
    for r in results:
        mark = "skipped" if r.skipped else ("ok" if r.ok else "FAIL")
        lines.append(f"| {r.name} | {mark} | {r.detail.replace('|', '/')} |")
    return "\n".join(lines)


def main() -> int:
    now = datetime.now(timezone.utc)
    fail_url = os.getenv("MONITOR_FAIL_URL", "").strip()
    run_url = os.getenv("MONITOR_RUN_URL", "")
    results = run_checks(now, fail_url=fail_url)
    table = summary_table(results)
    et = now.astimezone(EASTERN)
    header = (f"Production monitor at {et:%Y-%m-%d %H:%M} ET "
              f"({'market open' if market_is_open(now) else 'market closed'}; "
              f"{'trading day' if is_trading_day(et.date()) else 'not a trading day'})")
    print(header)
    print(table)

    token, repo = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
    dry_run = os.getenv("MONITOR_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    if dry_run or not token or not repo:
        open_issue = None
        outcome = "dry run: no issue changes" if dry_run else "no GITHUB_TOKEN: report only"
        action = decide(open_issue, results, now, run_url)
        outcome += f" (with no open issue this run would: {action.kind})"
    else:
        gh = GitHub(token, repo)
        open_issue = gh.open_issue()
        action = decide(open_issue, results, now, run_url)
        outcome = gh.apply(action, open_issue)
    print(f"\nIssue: {outcome}")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"### {header}\n\n{table}\n\nIssue: {outcome}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
