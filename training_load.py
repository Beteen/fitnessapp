"""
ATL / CTL / TSB (acute training load, chronic training load, training stress
balance) calculations for the fitness app.

CTL models fitness using an exponential moving average with a 42-day time
constant; ATL models fatigue with a 7-day time constant.
TSB = CTL − ATL  (positive = fresh, negative = fatigued).
"""

from datetime import date, timedelta
from typing import Optional

from training_plan import PLAN_START, RACE_DATE, PLAN_KM
from database import get_conn
from body_metrics import lthr, max_hr_estimate

# ── Time constants ─────────────────────────────────────────────────────────────
CTL_TAU = 42  # days – fitness
ATL_TAU = 7   # days – fatigue

# EMA decay factors
_CTL_K = 1.0 - (1.0 / CTL_TAU)   # equivalent to exp(-1/42) ≈ 0.9764
_ATL_K = 1.0 - (1.0 / ATL_TAU)   # equivalent to exp(-1/7)  ≈ 0.8571


# ── TSS helpers ───────────────────────────────────────────────────────────────

def run_tss(duration_sec: float, avg_hr: float, lthr_val: float) -> float:
    """Return Training Stress Score for a single run.

    TSS = (duration_sec × IF² × 100) / 3600
    where IF (Intensity Factor) = avg_hr / lthr_val.
    """
    intensity_factor = avg_hr / lthr_val
    return (duration_sec * intensity_factor ** 2 * 100.0) / 3600.0


# ── Actual load series ────────────────────────────────────────────────────────

def compute_load_series(age: int = 26, known_max_hr: Optional[int] = None) -> list[dict]:
    """Return ATL/CTL/TSB series from PLAN_START to today.

    The EMA is seeded by walking day-by-day from (PLAN_START − 60 days) so
    that early plan values are not distorted by a cold-start assumption of
    zero fitness.

    Each dict in the returned list contains:
        date (date), tss (float), ctl (float), atl (float), tsb (float)

    Only rows from PLAN_START onward are returned; the 60-day warm-up period
    is used to prime the EMA but is not included in the output.
    """
    lthr_val = lthr(age, known_max_hr)
    today = date.today()

    # Build a lookup of daily TSS from the activities table.
    warm_start = PLAN_START - timedelta(days=60)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DATE(start_time) AS day,
                   SUM(
                       (duration_seconds * (avg_hr * 1.0 / ?) * (avg_hr * 1.0 / ?) * 100.0)
                       / 3600.0
                   ) AS day_tss
            FROM   activities
            WHERE  activity_type = 'running'
              AND  avg_hr IS NOT NULL
              AND  duration_seconds IS NOT NULL
              AND  DATE(start_time) >= ?
              AND  DATE(start_time) <= ?
            GROUP  BY day
            """,
            (lthr_val, lthr_val, warm_start.isoformat(), today.isoformat()),
        ).fetchall()

    tss_by_date: dict[date, float] = {}
    for row in rows:
        tss_by_date[date.fromisoformat(row["day"])] = row["day_tss"]

    # Walk day-by-day, applying EMA.
    ctl = 0.0
    atl = 0.0
    current = warm_start
    results: list[dict] = []

    while current <= today:
        tss = tss_by_date.get(current, 0.0)
        ctl = ctl * _CTL_K + tss * (1.0 - _CTL_K)
        atl = atl * _ATL_K + tss * (1.0 - _ATL_K)
        tsb = ctl - atl

        if current >= PLAN_START:
            results.append(
                {
                    "date": current,
                    "tss": tss,
                    "ctl": ctl,
                    "atl": atl,
                    "tsb": tsb,
                }
            )
        current += timedelta(days=1)

    return results


# ── Projected load series ─────────────────────────────────────────────────────

def _estimate_tss_per_km(age: int = 26, known_max_hr: Optional[int] = None) -> float:
    """Estimate TSS per km from the last 30 days of activities.

    Falls back to a sensible default (8 TSS/km at moderate effort) if there
    is insufficient data.
    """
    lthr_val = lthr(age, known_max_hr)
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT SUM(distance_km)       AS total_km,
                   SUM(
                       (duration_seconds * (avg_hr * 1.0 / ?) * (avg_hr * 1.0 / ?) * 100.0)
                       / 3600.0
                   ) AS total_tss
            FROM   activities
            WHERE  activity_type = 'running'
              AND  avg_hr IS NOT NULL
              AND  duration_seconds IS NOT NULL
              AND  distance_km > 0
              AND  DATE(start_time) >= ?
            """,
            (lthr_val, lthr_val, cutoff),
        ).fetchone()

    total_km = row["total_km"]
    total_tss = row["total_tss"]

    if total_km and total_km > 0 and total_tss and total_tss > 0:
        return total_tss / total_km

    # Default: moderate aerobic effort, roughly 8 TSS/km
    return 8.0


def project_load_to_race(
    existing_series: list[dict],
    age: int = 26,
    known_max_hr: Optional[int] = None,
) -> list[dict]:
    """Continue ATL/CTL/TSB from the last actual day forward to RACE_DATE.

    Planned km for each future day is taken from PLAN_KM.  TSS per km is
    estimated from the last 30 days of recorded activities.

    Each dict in the returned list contains:
        date (date), tss (float), ctl (float), atl (float), tsb (float),
        projected (bool, always True)
    """
    if existing_series:
        last = existing_series[-1]
        ctl = last["ctl"]
        atl = last["atl"]
        start_date = last["date"] + timedelta(days=1)
    else:
        ctl = 0.0
        atl = 0.0
        start_date = date.today()

    tss_per_km = _estimate_tss_per_km(age, known_max_hr)

    # Build a lookup of planned km by calendar date.
    planned_km_by_date: dict[date, float] = {}
    for week_idx, week_days in enumerate(PLAN_KM):
        for day_idx, km in enumerate(week_days):
            d = PLAN_START + timedelta(weeks=week_idx, days=day_idx)
            # Negative values mean cross-training (no running TSS).
            planned_km_by_date[d] = max(km, 0.0)

    current = start_date
    results: list[dict] = []

    while current <= RACE_DATE:
        planned_km = planned_km_by_date.get(current, 0.0)
        tss = planned_km * tss_per_km

        ctl = ctl * _CTL_K + tss * (1.0 - _CTL_K)
        atl = atl * _ATL_K + tss * (1.0 - _ATL_K)
        tsb = ctl - atl

        results.append(
            {
                "date": current,
                "tss": tss,
                "ctl": ctl,
                "atl": atl,
                "tsb": tsb,
                "projected": True,
            }
        )
        current += timedelta(days=1)

    return results
