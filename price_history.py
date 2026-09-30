"""Append-only SQLite persistence for scheduled Ctrip price observations."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 2
TIME_ZONE = ZoneInfo("Asia/Shanghai")
SUCCESS_QUERY_STATUSES = ("success", "no_results")


def gmt8_now() -> str:
    """Return an ISO-8601 timestamp with the explicit China Standard Time offset."""
    return datetime.now(TIME_ZONE).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _coverage_city_key(city: str, airport_code: str | None) -> str:
    """Keep the legacy unique key while distinguishing city airport pairs."""
    return f"{city} [{airport_code}]" if airport_code else city


class PriceHistoryStore:
    """Small SQLite repository; every run/query/flight card is retained forever."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PriceHistoryStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _migrate(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {version} is newer than supported {SCHEMA_VERSION}."
            )
        if version == SCHEMA_VERSION:
            return
        if version == 1:
            with self.connection:
                self.connection.execute("ALTER TABLE query_observations ADD COLUMN departure_city_name TEXT")
                self.connection.execute("ALTER TABLE query_observations ADD COLUMN departure_airport_code TEXT")
                self.connection.execute("ALTER TABLE query_observations ADD COLUMN departure_airport_name TEXT")
                self.connection.execute("ALTER TABLE query_observations ADD COLUMN arrival_city_name TEXT")
                self.connection.execute("ALTER TABLE query_observations ADD COLUMN arrival_airport_code TEXT")
                self.connection.execute("ALTER TABLE query_observations ADD COLUMN arrival_airport_name TEXT")
                self.connection.execute(
                    """CREATE INDEX idx_queries_airport_route_date
                       ON query_observations(departure_city_name, departure_airport_code,
                                             arrival_city_name, arrival_airport_code,
                                             departure_date, captured_at)"""
                )
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if version != 0:
            raise RuntimeError(f"No migration path from database schema version {version}.")

        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('running','completed','partial','failed')),
                    scope_json TEXT NOT NULL,
                    expected_queries INTEGER NOT NULL CHECK (expected_queries >= 0),
                    attempted_queries INTEGER NOT NULL DEFAULT 0,
                    successful_queries INTEGER NOT NULL DEFAULT 0,
                    failed_queries INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE query_observations (
                    query_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    departure_city TEXT NOT NULL,
                    departure_city_name TEXT,
                    departure_airport_code TEXT,
                    departure_airport_name TEXT,
                    arrival_city TEXT NOT NULL,
                    arrival_city_name TEXT,
                    arrival_airport_code TEXT,
                    arrival_airport_name TEXT,
                    departure_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending','running','success','no_results','blocked','error','not_run')),
                    captured_at TEXT,
                    source TEXT,
                    error TEXT,
                    itinerary_count INTEGER,
                    direct_itinerary_count INTEGER,
                    fare_card_count INTEGER,
                    UNIQUE (run_id, departure_city, arrival_city, departure_date)
                );

                CREATE TABLE flight_observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query_id INTEGER NOT NULL REFERENCES query_observations(query_id),
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    captured_at TEXT NOT NULL,
                    departure_city TEXT NOT NULL,
                    departure_airport TEXT,
                    departure_airport_code TEXT,
                    arrival_city TEXT NOT NULL,
                    arrival_airport TEXT,
                    arrival_airport_code TEXT,
                    departure_date TEXT NOT NULL,
                    flight_no TEXT NOT NULL,
                    airline TEXT,
                    departure_time TEXT,
                    arrival_time TEXT,
                    economy_fare_cny TEXT NOT NULL,
                    currency TEXT NOT NULL CHECK (currency = 'CNY'),
                    stops INTEGER NOT NULL CHECK (stops >= 0),
                    direct INTEGER NOT NULL CHECK (direct IN (0,1)),
                    economy INTEGER NOT NULL CHECK (economy IN (0,1)),
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    flight_card_json TEXT NOT NULL,
                    fare_card_json TEXT NOT NULL
                );

                -- Date-level minimum-price and connecting-flight summaries are deliberately
                -- isolated from flight/fare-card history.
                CREATE TABLE daily_summaries (
                    summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query_id INTEGER NOT NULL UNIQUE REFERENCES query_observations(query_id),
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    captured_at TEXT NOT NULL,
                    departure_city TEXT NOT NULL,
                    arrival_city TEXT NOT NULL,
                    departure_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    itinerary_count INTEGER NOT NULL,
                    direct_itinerary_count INTEGER NOT NULL,
                    connecting_itinerary_count INTEGER NOT NULL,
                    all_economy_min_fare_cny TEXT,
                    direct_economy_min_fare_cny TEXT,
                    transfer_summary_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );

                CREATE INDEX idx_queries_run_status
                    ON query_observations(run_id, status);
                CREATE INDEX idx_queries_route_date
                    ON query_observations(departure_city, arrival_city, departure_date, captured_at);
                CREATE INDEX idx_queries_airport_route_date
                    ON query_observations(departure_city_name, departure_airport_code,
                                          arrival_city_name, arrival_airport_code,
                                          departure_date, captured_at);
                CREATE INDEX idx_flights_run_route_date
                    ON flight_observations(run_id, departure_city, arrival_city, departure_date);
                CREATE INDEX idx_flights_flight_date_captured
                    ON flight_observations(flight_no, departure_date, captured_at);
                CREATE INDEX idx_flights_route_date_captured
                    ON flight_observations(departure_city, arrival_city, departure_date, captured_at);
                CREATE INDEX idx_summaries_route_date_captured
                    ON daily_summaries(departure_city, arrival_city, departure_date, captured_at);
                """
            )
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create_run(self, run_id: str, scope: Iterable[dict[str, Any]], started_at: str | None = None) -> None:
        queries = list(scope)
        with self.connection:
            self.connection.execute(
                """INSERT INTO runs(run_id, started_at, status, scope_json, expected_queries)
                   VALUES (?, ?, 'running', ?, ?)""",
                (run_id, started_at or gmt8_now(), _json(queries), len(queries)),
            )
            self.connection.executemany(
                """INSERT INTO query_observations
                   (run_id, departure_city, departure_city_name, departure_airport_code,
                    departure_airport_name, arrival_city, arrival_city_name,
                    arrival_airport_code, arrival_airport_name, departure_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                [
                    (
                        run_id,
                        _coverage_city_key(item["departure_city"], item.get("departure_airport_code")),
                        item["departure_city"],
                        item.get("departure_airport_code"),
                        item.get("departure_airport_name"),
                        _coverage_city_key(item["arrival_city"], item.get("arrival_airport_code")),
                        item["arrival_city"],
                        item.get("arrival_airport_code"),
                        item.get("arrival_airport_name"),
                        item["departure_date"],
                    )
                    for item in queries
                ],
            )

    def mark_query_running(
        self,
        run_id: str,
        departure_city: str,
        arrival_city: str,
        departure_date: str,
        departure_airport_code: str | None = None,
        arrival_airport_code: str | None = None,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE query_observations SET status='running'
                   WHERE run_id=? AND departure_city=? AND arrival_city=? AND departure_date=?
                   AND status='pending'""",
                (
                    run_id,
                    _coverage_city_key(departure_city, departure_airport_code),
                    _coverage_city_key(arrival_city, arrival_airport_code),
                    departure_date,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Query scope row was not pending exactly once.")

    def record_query_result(
        self,
        *,
        run_id: str,
        departure_city: str,
        arrival_city: str,
        departure_date: str,
        status: str,
        captured_at: str,
        source: str,
        departure_airport_code: str | None = None,
        arrival_airport_code: str | None = None,
        error: str | None = None,
        observations: Iterable[dict[str, Any]] = (),
        summary: dict[str, Any] | None = None,
    ) -> None:
        if status not in ("success", "no_results", "blocked", "error"):
            raise ValueError(f"Unsupported final query status: {status}")
        observation_rows = list(observations)
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE query_observations
                   SET status=?, captured_at=?, source=?, error=?, itinerary_count=?,
                       direct_itinerary_count=?, fare_card_count=?
                   WHERE run_id=? AND departure_city=? AND arrival_city=? AND departure_date=?
                   AND status='running'""",
                (
                    status,
                    captured_at,
                    source,
                    error,
                    (summary or {}).get("itinerary_count"),
                    (summary or {}).get("direct_itinerary_count"),
                    len(observation_rows),
                    run_id,
                    _coverage_city_key(departure_city, departure_airport_code),
                    _coverage_city_key(arrival_city, arrival_airport_code),
                    departure_date,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Query scope row was not running exactly once.")
            query = self.connection.execute(
                """SELECT query_id FROM query_observations
                   WHERE run_id=? AND departure_city=? AND arrival_city=? AND departure_date=?""",
                (
                    run_id,
                    _coverage_city_key(departure_city, departure_airport_code),
                    _coverage_city_key(arrival_city, arrival_airport_code),
                    departure_date,
                ),
            ).fetchone()
            query_id = int(query["query_id"])

            for item in observation_rows:
                self.connection.execute(
                    """INSERT INTO flight_observations(
                         query_id, run_id, captured_at, departure_city, departure_airport,
                         departure_airport_code, arrival_city, arrival_airport,
                         arrival_airport_code, departure_date, flight_no, airline,
                         departure_time, arrival_time, economy_fare_cny, currency, stops,
                         direct, economy, source, status, error, flight_card_json, fare_card_json
                       ) VALUES (
                         :query_id, :run_id, :captured_at, :departure_city, :departure_airport,
                         :departure_airport_code, :arrival_city, :arrival_airport,
                         :arrival_airport_code, :departure_date, :flight_no, :airline,
                         :departure_time, :arrival_time, :economy_fare_cny, :currency, :stops,
                         :direct, :economy, :source, :status, :error, :flight_card_json, :fare_card_json
                       )""",
                    {
                        **item,
                        "query_id": query_id,
                        "run_id": run_id,
                        "captured_at": captured_at,
                        "departure_city": departure_city,
                        "arrival_city": arrival_city,
                        "departure_date": departure_date,
                        "source": source,
                        "status": item.get("status", "ok"),
                        "error": item.get("error"),
                        "currency": item.get("currency", "CNY"),
                        "direct": int(item.get("direct", True)),
                        "economy": int(item.get("economy", True)),
                    },
                )
            if summary is not None:
                self.connection.execute(
                    """INSERT INTO daily_summaries(
                         query_id, run_id, captured_at, departure_city, arrival_city,
                         departure_date, status, source, itinerary_count,
                         direct_itinerary_count, connecting_itinerary_count,
                         all_economy_min_fare_cny, direct_economy_min_fare_cny,
                         transfer_summary_json, summary_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        query_id,
                        run_id,
                        captured_at,
                        departure_city,
                        arrival_city,
                        departure_date,
                        status,
                        source,
                        summary["itinerary_count"],
                        summary["direct_itinerary_count"],
                        summary["connecting_itinerary_count"],
                        summary.get("all_economy_min_fare_cny"),
                        summary.get("direct_economy_min_fare_cny"),
                        _json(summary["transfer_summary"]),
                        _json(summary["summary"]),
                    ),
                )

    def finish_run(self, run_id: str, stop_reason: str | None = None, finished_at: str | None = None) -> dict[str, int | str]:
        with self.connection:
            self.connection.execute(
                """UPDATE query_observations SET status='not_run', error=COALESCE(error, ?)
                   WHERE run_id=? AND status IN ('pending','running')""",
                (stop_reason or "Not attempted before this run ended.", run_id),
            )
            row = self.connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status IN ('success','no_results') THEN 1 ELSE 0 END) AS successful,
                          SUM(CASE WHEN status IN ('blocked','error') THEN 1 ELSE 0 END) AS failed,
                          SUM(CASE WHEN status='not_run' THEN 1 ELSE 0 END) AS not_run
                   FROM query_observations WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            total = int(row["total"] or 0)
            successful = int(row["successful"] or 0)
            failed = int(row["failed"] or 0)
            not_run = int(row["not_run"] or 0)
            attempted = total - not_run
            run = self.connection.execute(
                "SELECT expected_queries FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run_id: {run_id}")
            expected = int(run["expected_queries"])
            if successful == expected and failed == 0 and not_run == 0:
                run_status = "completed"
            elif successful == 0 and attempted == 0:
                run_status = "failed"
            elif successful == 0 and failed > 0:
                run_status = "failed"
            else:
                run_status = "partial"
            self.connection.execute(
                """UPDATE runs SET finished_at=?, status=?, attempted_queries=?,
                   successful_queries=?, failed_queries=?, error=? WHERE run_id=?""",
                (finished_at or gmt8_now(), run_status, attempted, successful, failed, stop_reason, run_id),
            )
        return {
            "run_id": run_id,
            "status": run_status,
            "expected_queries": expected,
            "attempted_queries": attempted,
            "successful_queries": successful,
            "failed_queries": failed,
            "not_run_queries": not_run,
        }

    def run_record(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def export_csv(self, output_path: str | Path, kind: str = "flights") -> int:
        queries = {
            "flights": "SELECT * FROM flight_observations ORDER BY observation_id",
            "summaries": "SELECT * FROM daily_summaries ORDER BY summary_id",
            "queries": "SELECT * FROM query_observations ORDER BY query_id",
        }
        if kind not in queries:
            raise ValueError("kind must be one of: flights, summaries, queries")
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        cursor = self.connection.execute(queries[kind])
        fieldnames = [column[0] for column in cursor.description]
        count = 0
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            while rows := cursor.fetchmany(1000):
                writer.writerows(dict(row) for row in rows)
                count += len(rows)
        return count
