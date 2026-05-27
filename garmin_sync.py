from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from database import get_conn, init_db

GARTH_TOKEN_DIR = Path.home() / ".garth"


def _get_client():
    """Load Garmin client from saved garth tokens."""
    try:
        from garminconnect import Garmin
        client = Garmin(tokenstore=str(GARTH_TOKEN_DIR))
        client.login()
        return client
    except Exception as exc:
        raise RuntimeError(
            f"Garmin auth failed: {exc}\n"
            "Run `python setup_auth.py` to authenticate first."
        ) from exc


def sync(days: int = 120) -> int:
    """Sync the last N days from Garmin Connect. Returns number of run activities stored."""
    init_db()
    client = _get_client()

    end = date.today()
    start = end - timedelta(days=days)

    count = _sync_activities(client, start, end)
    _sync_daily_hr(client, start, end)
    _sync_sleep(client, start, end)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_log (synced_at, days_synced) VALUES (?, ?)",
            (datetime.now().isoformat(), days),
        )

    return count


def _sync_activities(client, start: date, end: date) -> int:
    activities = client.get_activities_by_date(
        start.isoformat(), end.isoformat(), activitytype="running"
    )
    count = 0
    with get_conn() as conn:
        for act in activities:
            aid = str(act.get("activityId", ""))
            if not aid:
                continue

            distance_m = float(act.get("distance") or 0)
            duration_s = float(act.get("duration") or 0)
            distance_km = distance_m / 1000.0
            pace_sec = (duration_s / distance_km) if distance_km > 0.1 else None
            avg_hr = act.get("averageHR") or act.get("avgHr")
            max_hr = act.get("maxHR") or act.get("maxHr")

            conn.execute(
                """
                INSERT OR REPLACE INTO activities
                    (activity_id, start_time, activity_type, distance_km,
                     duration_seconds, avg_pace_sec_per_km, avg_hr, max_hr, calories)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    act.get("startTimeLocal", ""),
                    round(distance_km, 3),
                    int(duration_s),
                    round(pace_sec, 2) if pace_sec else None,
                    int(avg_hr) if avg_hr else None,
                    int(max_hr) if max_hr else None,
                    act.get("calories"),
                ),
            )
            count += 1
    return count


def _sync_daily_hr(client, start: date, end: date):
    with get_conn() as conn:
        current = start
        while current <= end:
            try:
                data = client.get_rhr_day(current.isoformat())
                rhr = _extract_rhr(data)
                if rhr:
                    conn.execute(
                        "INSERT OR REPLACE INTO daily_hr (date, resting_hr) VALUES (?, ?)",
                        (current.isoformat(), rhr),
                    )
            except Exception:
                pass
            current += timedelta(days=1)


def _extract_rhr(data) -> Optional[int]:
    if not data:
        return None
    if isinstance(data, (int, float)):
        return int(data)
    if isinstance(data, dict):
        val = data.get("value") or data.get("restingHeartRate") or data.get("rhr")
        return int(val) if val else None
    if isinstance(data, list) and data:
        return _extract_rhr(data[0])
    return None


def _sync_sleep(client, start: date, end: date):
    with get_conn() as conn:
        current = start
        while current <= end:
            try:
                data = client.get_sleep_data(current.isoformat())
                if data and isinstance(data, dict):
                    daily = data.get("dailySleepDTO") or {}
                    score_block = (daily.get("sleepScores") or {}).get("overall") or {}
                    score = score_block.get("value")
                    duration = daily.get("sleepTimeSeconds")
                    if score or duration:
                        conn.execute(
                            "INSERT OR REPLACE INTO sleep (date, sleep_score, duration_seconds) VALUES (?, ?, ?)",
                            (current.isoformat(), score, duration),
                        )
            except Exception:
                pass
            current += timedelta(days=1)


def last_sync_time() -> Optional[str]:
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT synced_at FROM sync_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["synced_at"] if row else None
    except Exception:
        return None
