from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from database import get_conn
from training_plan import PLAN_START, MARATHON_KM, get_weekly_planned_km


def sec_per_km_to_pace_str(sec: float) -> str:
    m = int(sec // 60)
    s = int(sec % 60)
    return f"{m}:{s:02d}/km"


def pace_str_to_sec_per_km(pace: str) -> float:
    """Parse 'M:SS' or 'MM:SS/km' into seconds per km."""
    cleaned = pace.strip().replace("/km", "").replace(" ", "")
    parts = cleaned.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def finish_time_str(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m:02d}m"


def riegel(distance_km: float, time_sec: float, target_km: float) -> float:
    """T2 = T1 × (D2/D1)^1.06"""
    return time_sec * (target_km / distance_km) ** 1.06


def get_all_runs() -> list[dict]:
    start_str = PLAN_START.isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT start_time, distance_km, duration_seconds,
                   avg_pace_sec_per_km, avg_hr
            FROM activities
            WHERE activity_type = 'running'
              AND date(start_time) >= ?
              AND distance_km > 1.0
              AND duration_seconds > 0
            ORDER BY start_time
            """,
            (start_str,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_runs(days: int = 60) -> list[dict]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT start_time, distance_km, duration_seconds,
                   avg_pace_sec_per_km, avg_hr
            FROM activities
            WHERE activity_type = 'running'
              AND date(start_time) >= ?
              AND distance_km > 1.0
              AND duration_seconds > 0
            ORDER BY start_time DESC
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def current_projection(goal_pace_sec: Optional[float] = None) -> Optional[dict]:
    """
    Returns projection dict based on the longest recent run (Riegel)
    plus an average-pace estimate from the last 5 runs.
    """
    runs = get_recent_runs(days=60)
    if not runs:
        return None

    best = max(runs, key=lambda r: r["distance_km"])
    if best["distance_km"] < 5.0:
        return None

    riegel_sec = riegel(best["distance_km"], best["duration_seconds"], MARATHON_KM)

    paces = [r["avg_pace_sec_per_km"] for r in runs[:5] if r.get("avg_pace_sec_per_km")]
    avg_pace_sec = sum(paces) / len(paces) if paces else None

    return {
        "riegel_seconds": riegel_sec,
        "pace_based_seconds": avg_pace_sec * MARATHON_KM if avg_pace_sec else None,
        "goal_seconds": goal_pace_sec * MARATHON_KM if goal_pace_sec else None,
        "based_on_run_km": best["distance_km"],
        "based_on_run_pace": best["avg_pace_sec_per_km"],
    }


def projection_history() -> list[dict]:
    """Riegel projection after each completed week, using longest run up to that point."""
    results = []
    today = date.today()

    for week in range(1, 19):
        week_end = PLAN_START + timedelta(weeks=week) - timedelta(days=1)
        if week_end >= today:
            break

        with get_conn() as conn:
            best = conn.execute(
                """
                SELECT distance_km, duration_seconds
                FROM activities
                WHERE activity_type = 'running'
                  AND date(start_time) <= ?
                  AND distance_km >= 5.0
                  AND duration_seconds > 0
                ORDER BY distance_km DESC
                LIMIT 1
                """,
                (week_end.isoformat(),),
            ).fetchone()

        if best:
            projected = riegel(best["distance_km"], best["duration_seconds"], MARATHON_KM)
            results.append(
                {
                    "week": week,
                    "date": week_end.isoformat(),
                    "projected_seconds": projected,
                }
            )

    return results


def weekly_summary() -> list[dict]:
    """Planned vs actual km for all 18 weeks."""
    today = date.today()
    rows = []

    for week in range(1, 19):
        week_start = PLAN_START + timedelta(weeks=week - 1)
        week_end = week_start + timedelta(days=6)
        planned = get_weekly_planned_km(week)
        is_future = week_start > today
        is_current = week_start <= today <= week_end

        if is_future:
            rows.append(
                {"week": week, "planned_km": planned, "actual_km": None, "is_current": False}
            )
            continue

        with get_conn() as conn:
            result = conn.execute(
                """
                SELECT COALESCE(SUM(distance_km), 0) AS total
                FROM activities
                WHERE activity_type = 'running'
                  AND date(start_time) BETWEEN ? AND ?
                """,
                (week_start.isoformat(), min(week_end, today).isoformat()),
            ).fetchone()

        actual = result["total"] if result else 0.0
        rows.append(
            {
                "week": week,
                "planned_km": planned,
                "actual_km": round(actual, 1),
                "is_current": is_current,
            }
        )

    return rows
