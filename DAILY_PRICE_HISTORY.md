# Daily Flight Price History Collector

This guide documents the anonymous, append-only airport-pair collector and the distinction between Ctrip's public airport directory and its live search autocomplete. The configured scope counts only airports classified as serving **scheduled civil passenger flights**; general-aviation airports and non-airport transport nodes are excluded. Directory counts below are candidates only, not proof that the runtime selector is exhaustive.

## Requested airport scope and Ctrip directory evidence

The requested Shanghai endpoints are **PVG (浦东国际机场)** and **SHA (虹桥国际机场)** only. A previous Shanghai dropdown excerpt also showed **JS2 (金山水上通用机场)**; JS2 is explicitly classified as general aviation and is outside the requested PVG/SHA scope. An earlier visible Beijing autocomplete also showed **MY2 (密云穆家峪通用机场)**, which is excluded by the same service-class rule.

The Ctrip [airport guide / domestic-airport directory](https://flights.ctrip.com/booking/airport-guides.html) lists the following scheduled-passenger airport candidates for the destination cities. Urumqi's **URC** identity was separately supported by its [Ctrip airport page](https://flights.ctrip.com/booking/airport-urc); that page is not a complete live-selector listing.

| Destination | Configured scheduled-passenger candidates from the directory | Candidate count | Full autocomplete list verified? |
| --- | --- | ---: | --- |
| 北京 | 首都国际机场 **PEK**, 大兴国际机场 **PKX** | 2 | No |
| 广州 | 白云国际机场 **CAN** | 1 | No |
| 深圳 | 宝安国际机场 **SZX** | 1 | No |
| 成都 | 天府国际机场 **TFU**, 双流国际机场 **CTU** | 2 | No |
| 乌鲁木齐 | 天山国际机场 **URC** | 1 | No |
| **Total directory-listed destination airports** |  | **7** | **No** |

The same directory also contains Guangzhou passenger-terminal entries **ZTI**, **NSZ**, and **PFT**, and Shenzhen cruise-port entry **ZYK**. Their names identify passenger ports/terminals, not airline airports, so they are recorded in the evidence fixture but excluded from flight routes. General-aviation autocomplete observations **MY2** (Beijing) and **JS2** (Shanghai) are also recorded and excluded; neither can enter the scheduled-passenger route matrix. The reviewed page is an airport-guide directory; it does **not** state that its rows are identical to, or exhaustive for, the domestic flight-search autocomplete.

The source snapshot is retained at `tests/fixtures/ctrip_airport_directory_snapshot.json`. It records the seven configured scheduled-passenger candidates, known general-aviation exclusions, excluded port rows, their Ctrip links, and the evidence boundary. The airport list in `airport_scope.py` is therefore the **directory-derived candidate list**, not a claim that all runtime selector choices are known or verified.

The parallel runtime-page cross-check on 2026-09-30 captured **no airport candidates**: the page became stale and DOM output was limited. No destination city, nor the Shanghai origin autocomplete, is verified complete. Existing directory codes and earlier partial observations remain evidence only; none may be promoted to a verified live list based on this attempt.

## Candidate matrix and runtime fail-closed rule

For the seven directory-derived scheduled-passenger candidates, there are `2 × 7 = 14` candidate Shanghai-origin/destination airport pairs (PVG or SHA paired with each destination). Each pair creates two directed routes, one in each direction, so the directory-based candidate matrix is:

```text
S_directory = 2 + 1 + 1 + 2 + 1 = 7
Distinct airport pairs = 2 × S_directory = 14
Directed routes per date = 2 × 14 = 4 × S_directory = 28
30-date directory-based candidate rows = 30 × 28 = 30 × 4 × 7 = 840
```

**840 is only the count for the seven configured scheduled-passenger candidates recorded in the Ctrip directory fixture. It is not a verified full-autocomplete maximum or an executable scope.** If the flight-search autocomplete contains another eligible airport option absent from the directory, its complete matrix would differ. The executable selector counts `n_i`, their sum `S`, and `runs.expected_queries` remain unknown (`null`) until every destination city's runtime airport-option list is independently enumerated, classified under the scheduled-passenger rule, and shown to be complete.

Before creating any runtime scope, the collector still requires complete evidence for every destination city, with options classified as scheduled civil passenger airports versus exclusions such as general-aviation airports. At collection time it also requires one unique visible option whose airport name and `data-u_remark` code match the requested airport. A city-level autocomplete result without a specifically verified airport is never treated as a single-airport city. The directory entries alone do not unlock the collector: both `run` and `run --dry-run` remain `scope_unverified` and stop before database or browser access until all full selector inventories are verified. No fare search or daily batch run was performed for this update.

The shared matrix formula is `30 × 4 × S`, not `2 × S`; the old city-level `300` count is obsolete. Once (and only if) every selector inventory is complete, the same airport-pair scope supplies the SQLite expected count, coverage rows, run counters, query-coverage CSV export, and tests. Synthetic complete inventories in unit tests exercise arithmetic only; they are not Ctrip airport evidence.

## Date window, cabin, and operating target

A future eligible run covers 30 natural dates, including the GMT+8 run date through that date + 29 days. The requested collection filters to domestic nonstop economy itineraries. The intended daily start remains **08:00 GMT+8**; no recurring schedule was created or updated.

## Local prerequisites

Use Python 3.10+ and a locally available Chromium/Chrome browser. Selenium Manager may obtain a matching driver if needed; TLS verification remains at Chromium's default secure setting.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python daily_price_collector.py init-db
```

The default database path is `data/ctrip_price_history.sqlite3`. Initializing or opening an existing version-1 database applies only the additive version-2 schema migration; existing run, query, flight, and summary rows are retained. The directory-scope update itself does not migrate, rewrite, or delete the existing SQLite database.

## SQLite history and exports

The database remains append-only: each run, route/date/airport coverage row, fare-card observation, and date summary is retained; repeated observations are added rather than replacing earlier records. Schema version 2 adds canonical city names and departure/arrival airport names and codes to `query_observations`. On first use, a version-1 database is migrated by adding nullable columns and an index; existing rows and observations are preserved. Airport-specific route keys distinguish same-city, same-date airport pairs while exported city and airport fields remain explicit.

Useful exports (UTF-8 with BOM):

```bash
.venv/bin/python daily_price_collector.py export-csv --kind flights --output exports/flights.csv
.venv/bin/python daily_price_collector.py export-csv --kind summaries --output exports/date-summaries.csv
.venv/bin/python daily_price_collector.py export-csv --kind queries --output exports/query-coverage.csv
```

The queries export includes planned/attempted status, canonical departure/arrival cities, airport names and codes, and dates. Full fare history remains in the existing flight-observation table.

## Safety and offline verification

The collector uses ordinary Chromium with default TLS verification, no account or user Cookie, no login, no CAPTCHA bypass, no access-control or anti-bot bypass, no proxy/IP rotation, and no stealth parameters. The current source update changed only offline airport configuration, its evidence fixture, tests, and documentation; it did not run a fare search, a full collection, or a schedule.

Run the offline suite with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```
