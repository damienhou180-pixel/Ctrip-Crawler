# Daily Flight Price History Collector

This guide describes the anonymous, append-only airport-pair collector. It does not use the legacy login, account, Cookie, proxy, retry, or challenge-handling flows.

## Requested airport scope and current evidence

The requested Shanghai endpoints are **PVG (浦东国际机场)** and **SHA (虹桥国际机场)** only. The Ctrip Shanghai dropdown excerpt also contained **JS2 (金山水上通用机场)**; JS2 is deliberately excluded because the requested origin scope is PVG + SHA.

Ctrip's public airport directory supports these destination entries, but the evidence does not establish a complete airport-selector inventory for four cities. Only Chengdu is treated as complete, based on the two entries in Ctrip's [domestic airport directory](https://flights.ctrip.com/booking/airport-guides.html). Airport pages and a directory are public information sources, not proof that a flight-search dropdown exposes no additional options.

| Destination | Ctrip-listed airport entries | Complete `n_i`? | Evidence status |
| --- | --- | ---: | --- |
| 北京 | 首都国际机场 **PEK**, 大兴国际机场 **PKX** ([Ctrip airport directory](https://flights.ctrip.com/booking/airport-guides.html)) | Unknown | Two entries are supported, but not proven to be every selector choice. |
| 广州 | 白云国际机场 **CAN** ([Ctrip airport directory](https://flights.ctrip.com/booking/airport-guides.html), [airport page](https://flights.ctrip.com/booking/airport-baiyun/)) | Unknown | CAN is supported; completeness of the city selector is not established. |
| 深圳 | 宝安国际机场 **SZX** ([Ctrip airport directory](https://flights.ctrip.com/booking/airport-guides.html), [airport page](https://flights.ctrip.com/booking/airport-szx/jichangjianjie.html)) | Unknown | SZX is supported; completeness of the city selector is not established. |
| 成都 | 天府国际机场 **TFU**, 双流国际机场 **CTU** ([Ctrip domestic airport directory](https://flights.ctrip.com/booking/airport-guides.html), [TFU page](https://flights.ctrip.com/booking/airport-tfu), [CTU page](https://flights.ctrip.com/booking/airport-ctu)) | **2** | Both Chengdu entries are listed in the Ctrip domestic-airport directory. The live search textbox itself was not accessible for a dropdown check in this run. |
| 乌鲁木齐 | 天山国际机场 **URC** ([Ctrip airport page](https://flights.ctrip.com/booking/airport-urc)) | Unknown | URC is supported; the page does not enumerate every selector choice. |

The full matrix is **not numerically computable yet**: four `n_i` values remain unknown. Partial directory counts must not be substituted for complete city counts.

## Matrix and fail-closed behavior

For each destination airport, pair both Shanghai origin airports in both directions: PVG → destination, SHA → destination, destination → PVG, and destination → SHA. Across 30 natural dates beginning on the GMT+8 run date, the expected query count is:

```text
S = sum(n_i for 北京、广州、深圳、成都、乌鲁木齐)
per-date directed routes = 4 × S
total expected queries = 30 × 4 × S = 120S
```

This is not `2 × S`, and the former city-level `300` count is obsolete. `airport_scope.py` is the shared source for the formula; scope construction, SQLite `runs.expected_queries`, coverage rows, run counters, query-coverage CSV export, and tests use the same airport-pair scope. The tests use a clearly synthetic inventory only to verify arithmetic; its result is not a real Ctrip query count.

Because Beijing, Guangzhou, Shenzhen, and Urumqi do not yet have evidence proving complete airport-option lists, both `run` and `run --dry-run` return `scope_unverified` before opening a browser, creating/updating a database, or submitting a flight search. The incomplete four-city lists are never silently narrowed or treated as complete. **No full batch collection is enabled in this state.**

When all five inventories have complete evidence, a scope will cover 30 dates from today through today + 29 days, with only nonstop economy fares retained. Each submitted request and each direct itinerary must match the exact departure and arrival airport codes. The UI must expose one unique visible option whose displayed name and `data-u_remark` airport code match; otherwise the run stops before Search.

## Local prerequisites

Use Python 3.10+ and a locally available Chromium/Chrome browser. Selenium Manager may obtain a matching driver if needed; TLS verification remains at Chromium's default secure setting.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python daily_price_collector.py init-db
```

The default database path is `data/ctrip_price_history.sqlite3`. Initializing or opening an existing version-1 database applies only the additive version-2 schema migration; existing run, query, flight, and summary rows are retained.

## SQLite history and exports

The database remains append-only: each run, route/date/airport coverage row, fare-card observation, and date summary is retained; repeated observations are added rather than replacing earlier records. Schema version 2 adds canonical city names and departure/arrival airport names and codes to `query_observations`. On first use, a version-1 database is migrated by adding nullable columns and an index; existing rows and observations are preserved. Airport-specific route keys distinguish same-city, same-date airport pairs while exported city and airport fields remain explicit.

Useful exports (UTF-8 with BOM):

```bash
.venv/bin/python daily_price_collector.py export-csv --kind flights --output exports/flights.csv
.venv/bin/python daily_price_collector.py export-csv --kind summaries --output exports/date-summaries.csv
.venv/bin/python daily_price_collector.py export-csv --kind queries --output exports/query-coverage.csv
```

The queries export includes planned/attempted status, canonical departure/arrival cities, airport names and codes, and dates. Full fare history remains in the existing flight-observation table.

## 08:00 GMT+8 target and safety

The intended daily start remains **08:00 GMT+8**. No recurring schedule was created or updated in this work. The requested future schedule remains a separate user-managed step.

The live check used a fresh temporary Chromium profile, direct connection, and default TLS certificate verification. The public page showed form wrappers, but the departure textbox was not uniquely accessible by its observed name, so no city autocomplete was entered. Ctrip's public airport-information pages were used only to verify airport entries; **no fare search or batch price query was submitted**. No account or user Cookie was used; no login, CAPTCHA bypass, access-control/anti-bot bypass, proxy, IP rotation, TLS override, or stealth parameter was used. The full 30-date calendar navigation also remains unverified.

Offline tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```
