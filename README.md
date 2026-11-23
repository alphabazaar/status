# Alpha Bazaar status

Public uptime and data-freshness checks for [alphabazaar.ai](https://alphabazaar.ai), run by GitHub Actions.

Every hour on weekdays (every 3 hours on weekends) [`scripts/monitor/monitor.py`](scripts/monitor/monitor.py)
checks the site's public pages and API: that they answer, that they're fast enough, and that market data, the daily
brief and other feeds are fresh. It uses only public endpoints and no secrets.

- **Something failing?** The monitor opens one issue labeled `monitor`, updates it on every run while anything still
  fails, and closes it when everything passes again. Open issues here are the current outages.
- **Run history:** the [Actions tab](../../actions/workflows/monitor.yml).

The decision logic (trading days, freshness windows, thresholds) is pure functions, tested in
`tests/unit/test_monitor_logic.py` (`python3 -m pytest tests`).
