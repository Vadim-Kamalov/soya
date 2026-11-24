import logging
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from threading import Event, Thread

from fastapi import FastAPI, HTTPException

from .export import NoMetricsError, export_day, pending_days
from .metrics import calculate, unprocessed_days
from .settings import RETENTION_DAYS, utc_today
from .storage import ClickHouse, pg_connect

log = logging.getLogger(__name__)


def schedule(stop, interval, lookback):
    while not stop.is_set():
        today = utc_today()
        # The last retention value is final only on the day after D7.
        oldest_offset = max(lookback, RETENTION_DAYS + 1)
        days = [today - timedelta(days=offset) for offset in range(oldest_offset + 1)]
        try:
            days.extend(unprocessed_days(days[-1]))
        except Exception:
            log.exception("Could not load missing aggregation dates; will retry on the next cycle")
        for day in days:
            if stop.is_set():
                return
            try:
                calculate(day, force=False)
            except Exception:
                pass  # calculate logs the complete failure before propagating it.
        try:
            for day in pending_days(today):
                if stop.is_set():
                    return
                try:
                    export_day(day)
                except Exception:
                    log.exception("Scheduled export failed day=%s; will retry on the next cycle", day)
        except Exception:
            log.exception("Could not load pending exports; will retry on the next cycle")
        stop.wait(interval)


@asynccontextmanager
async def lifespan(_app):
    interval = float(os.getenv("AGGREGATION_INTERVAL_SECONDS", "60"))
    lookback = int(os.getenv("AGGREGATION_LOOKBACK_DAYS", "14"))
    if interval <= 0 or lookback < RETENTION_DAYS:
        raise ValueError("Aggregation requires a positive interval and at least seven days of lookback")
    stop = Event()
    worker = Thread(target=schedule, args=(stop, interval, lookback), daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="Movie Analytics", lifespan=lifespan)


def validate_day(day):
    if not date(1900, 1, 1) <= day <= utc_today():
        raise HTTPException(422, "Date must be between 1900-01-01 and today (UTC)")


@app.get("/health")
def health():
    try:
        with ClickHouse() as ch, pg_connect() as connection:
            ch.execute("SELECT 1 FROM cinema.daily_aggregates LIMIT 0")
            connection.execute("SELECT 1 FROM daily_metrics LIMIT 0")
    except Exception as exc:
        log.warning("Aggregation health probe failed", exc_info=True)
        raise HTTPException(503, "Analytics storage is unavailable") from exc
    return {"status": "ok"}


@app.post("/aggregate/{day}")
def recalculate(day: date):
    validate_day(day)
    try:
        return calculate(day)
    except Exception as exc:
        raise HTTPException(503, "Aggregation failed") from exc


@app.post("/export/{day}")
def export_metrics(day: date):
    validate_day(day)
    try:
        return export_day(day, requested=True)
    except NoMetricsError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        log.exception("Manual export failed day=%s", day)
        raise HTTPException(503, "Export failed") from exc
