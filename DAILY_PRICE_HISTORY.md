# Daily Flight Price History Collector

This guide covers the dedicated daily entry point. It does **not** use the legacy login/cookie/proxy/automatic-retry flow.

## Scope and behavior

A normal `run` uses the GMT+8 calendar date when the run starts and builds the complete scope:

- **30 natural departure dates**, including today through today + 29 days.
- **10 directed city routes**: Shanghai to/from Beijing, Guangzhou, Shenzhen, Chengdu, and Urumqi.
- **300 route/date searches** in total.
- Results retained are **nonstop economy (cabin Y)** only.
- One browser process, one query at a time, with a minimum **5-second post-query delay**. The actual full run will take at least 25 minutes plus page, selection, and response time.

The normal entry point never limits or silently narrows this scope:

```bash
python3 daily_price_collector.py run
```

Preview the scope without opening a browser, creating a run record, or querying Ctrip:

```bash
python3 daily_price_collector.py run --dry-run
```

`--max-queries N` exists **only** for an explicitly controlled validation. It still records the approved 300-query scope; all unattempted entries are written as `not_run`, and the run cannot be reported as complete. Do not pass this option for the daily run.

## Install and initialize

Use Python 3.10+ and a locally available Chromium/Chrome browser. Selenium Manager may obtain a matching driver if needed. TLS certificate validation remains at Chromium's default secure setting.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python daily_price_collector.py init-db
```

Default database:

```text
/workspace/Ctrip-Crawler/data/ctrip_price_history.sqlite3
```

The SQLite schema uses `PRAGMA user_version = 1`. It retains runs, one query-coverage row per planned city/date, one row per collected direct economy fare card, and a separate date-level summary table. The full itinerary card and complete raw fare-card JSON are stored with each flight observation. The summary table stores overall/direct economy minima and transfer-count/minimum summaries only; connecting fares never enter `flight_observations`. History is append-only; there is no automatic retention or cleanup.

Useful exports (UTF-8 with BOM for spreadsheet compatibility):

```bash
.venv/bin/python daily_price_collector.py export-csv --kind flights --output exports/flights.csv
.venv/bin/python daily_price_collector.py export-csv --kind summaries --output exports/date-summaries.csv
.venv/bin/python daily_price_collector.py export-csv --kind queries --output exports/query-coverage.csv
```

## 08:00 GMT+8 operation

The executable command is `python3 daily_price_collector.py run`. **No recurring schedule has been created or started** in this implementation. If a local cron entry is later wanted, it must run on a host that is awake and available at that time. Example only (not installed):

```cron
CRON_TZ=Asia/Shanghai
0 8 * * * cd /workspace/Ctrip-Crawler && /workspace/Ctrip-Crawler/.venv/bin/python daily_price_collector.py run >> /workspace/Ctrip-Crawler/logs/daily.log 2>&1
```

A sleeping/offline Sandbox cannot execute a cron job while unavailable; this example is not a guarantee of 08:00 execution. The one-off command can also be run manually from the project directory.

## Safety and failure semantics

- Each run creates all 300 expected query rows before collection. The run and every attempted/unattempted query receive a status.
- A CAPTCHA, login/password prompt, authentication challenge, access denial, HTTP 401/403/429, or explicit login-required response stops that run. The affected query is recorded as `blocked`; the remainder is `not_run`.
- Selector changes, unverified city suggestions/date cells, a response mismatch, a schema change, or a network/parse error stop the run with an `error` and `not_run` remainder. There is **no automatic retry, account/Cookie access, proxy, IP rotation, TLS override, stealth flag, or bypass**.
- A successful empty nonstop-economy result is recorded as `no_results`; it still counts as a completed query. A full `completed` run requires all 300 queries to end in `success` or `no_results`.
- The live public DOM inspection verified the departure and arrival textboxes by accessible name `可输入城市或机场`, and the read-only date textbox by `请选择日期`, within the observed form containers. The selected date is read from `u_remark`. The search itself must still verify the selected city/date on the page and match the captured response. If the UI does not expose a unique city suggestion or enabled date cell, the query is not submitted.
- **Current live blocker:** typing `上海` exposes three airport-level POI choices—浦东国际机场 (PVG), 虹桥国际机场 (SHA), and 金山水上通用机场 (JS2)—not a verified city-level selection. The collector refuses to choose one because that would narrow the requested city-level scope. The evidence is retained in `tests/fixtures/ctrip_city_suggestions_excerpt.html`. Consequently the single controlled runner validation stopped before Search, and no live fare query was submitted.
- The live DOM exposed date titles and day cells, but next/previous-month navigation has not been verified. Even after the city-level mapping is resolved, the full 30-date live workflow still needs a controlled validation of its calendar navigation; do not claim the 300-query run is live-verified.

## Offline tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The offline suite covers the 30-day inclusive window, all 10 directed routes/300 expected queries, direct-economy filtering, separate transfer summaries, append-only SQLite history, schema version/indexes, blocked/partial coverage, and separate CSV exports. A passing offline suite is not proof that the live site will permit a full 300-query run.
