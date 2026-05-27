from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from database import get_conn, init_db

TOKEN_DIR = Path.home() / ".garminconnect"


def _get_client():
    """Load Garmin client from saved tokens."""
    try:
        from garminconnect import Garmin
        client = Garmin()
        client.login(tokenstore=str(TOKEN_DIR))
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
    _sync_user_profile(client)
    _sync_daily_hr(client, start, end)
    _sync_sleep(client, start, end)
    _sync_vo2max(client, start, end)
    _sync_body_battery(client, start, end)
    _sync_stress(client, start, end)
    _sync_hrv(client, start, end)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_log (synced_at, days_synced) VALUES (?, ?)",
            (datetime.now().isoformat(), days),
        )

    return count


# ── Activities ───────────────────────────────────────────────────────────────

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


# ── User Profile ─────────────────────────────────────────────────────────────

def _sync_user_profile(client):
    try:
        profile = client.get_user_profile()
        if not profile:
            return
        weight = None
        height = None
        if isinstance(profile, dict):
            weight = profile.get("weight")
            height = profile.get("height")
            if weight:
                weight = float(weight) / 1000  # Garmin stores in grams
        with get_conn() as conn:
            if weight:
                conn.execute("INSERT OR REPLACE INTO user_profile (key, value) VALUES ('weight_kg', ?)", (str(round(weight, 1)),))
            if height:
                conn.execute("INSERT OR REPLACE INTO user_profile (key, value) VALUES ('height_cm', ?)", (str(round(float(height), 1)),))
    except Exception:
        pass


# ── Resting Heart Rate ───────────────────────────────────────────────────────

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


# ── Sleep ────────────────────────────────────────────────────────────────────

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


# ── VO2max ───────────────────────────────────────────────────────────────────

def _sync_vo2max(client, start: date, end: date):
    with get_conn() as conn:
        current = start
        while current <= end:
            try:
                data = client.get_max_metrics(current.isoformat())
                if not data:
                    current += timedelta(days=7)  # VO2max updates weekly
                    continue
                vo2 = None
                fit_age = None
                if isinstance(data, list) and data:
                    item = data[0]
                    generic = item.get("generic") or {}
                    vo2 = generic.get("vo2MaxPreciseValue") or generic.get("vo2max")
                    fit_age = generic.get("fitnessAge")
                elif isinstance(data, dict):
                    vo2 = data.get("vo2MaxPreciseValue") or data.get("vo2max")
                    fit_age = data.get("fitnessAge")
                if vo2:
                    conn.execute(
                        "INSERT OR REPLACE INTO vo2max (date, vo2max, fitness_age) VALUES (?, ?, ?)",
                        (current.isoformat(), float(vo2), int(fit_age) if fit_age else None),
                    )
            except Exception:
                pass
            current += timedelta(days=7)


# ── Body Battery ─────────────────────────────────────────────────────────────

def _sync_body_battery(client, start: date, end: date):
    try:
        data = client.get_body_battery(start.isoformat(), end.isoformat())
        if not data or not isinstance(data, list):
            return
        with get_conn() as conn:
            for item in data:
                if not isinstance(item, dict):
                    continue
                d = item.get("date") or item.get("calendarDate")
                if not d:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO body_battery
                        (date, morning_value, evening_value, charged, drained)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        d,
                        item.get("startTimestampGMT") and None or item.get("bodyBatteryValueDescriptorFormatDto", {}).get("bodyBatteryValueDescriptorIndex"),
                        item.get("endTimestampGMT") and None or None,
                        item.get("charged"),
                        item.get("drained"),
                    ),
                )
    except Exception:
        pass


def _sync_body_battery_daily(client, start: date, end: date):
    """Fallback: fetch body battery day by day."""
    with get_conn() as conn:
        current = start
        while current <= end:
            try:
                data = client.get_body_battery(current.isoformat(), current.isoformat())
                if data and isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            d = item.get("date") or item.get("calendarDate") or current.isoformat()
                            charged = item.get("charged")
                            drained = item.get("drained")
                            if charged is not None or drained is not None:
                                conn.execute(
                                    "INSERT OR REPLACE INTO body_battery (date, charged, drained) VALUES (?, ?, ?)",
                                    (d, charged, drained),
                                )
            except Exception:
                pass
            current += timedelta(days=1)


# ── Stress ───────────────────────────────────────────────────────────────────

def _sync_stress(client, start: date, end: date):
    with get_conn() as conn:
        current = start
        while current <= end:
            try:
                data = client.get_stress_data(current.isoformat())
                if not data or not isinstance(data, dict):
                    current += timedelta(days=1)
                    continue
                avg_s = data.get("avgStressLevel")
                max_s = data.get("maxStressLevel")
                duration_map = data.get("stressDurationData") or {}
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stress
                        (date, avg_stress, max_stress, rest_stress_mins,
                         low_stress_mins, med_stress_mins, high_stress_mins)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current.isoformat(),
                        int(avg_s) if avg_s is not None else None,
                        int(max_s) if max_s is not None else None,
                        duration_map.get("restStressDuration"),
                        duration_map.get("lowStressDuration"),
                        duration_map.get("mediumStressDuration"),
                        duration_map.get("highStressDuration"),
                    ),
                )
            except Exception:
                pass
            current += timedelta(days=1)


# ── HRV ──────────────────────────────────────────────────────────────────────

def _sync_hrv(client, start: date, end: date):
    with get_conn() as conn:
        current = start
        while current <= end:
            try:
                data = client.get_hrv_data(current.isoformat())
                if not data or not isinstance(data, dict):
                    current += timedelta(days=1)
                    continue
                summary = data.get("hrvSummary") or {}
                weekly_avg = summary.get("weeklyAvg")
                last_night = summary.get("lastNight")
                five_min = summary.get("lastNight5MinHigh")
                if weekly_avg or last_night:
                    conn.execute(
                        "INSERT OR REPLACE INTO hrv (date, weekly_avg, last_night, five_min_high) VALUES (?, ?, ?, ?)",
                        (current.isoformat(), weekly_avg, last_night, five_min),
                    )
            except Exception:
                pass
            current += timedelta(days=1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def last_sync_time() -> Optional[str]:
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT synced_at FROM sync_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["synced_at"] if row else None
    except Exception:
        return None
