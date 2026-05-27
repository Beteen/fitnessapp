"""
Marathon benchmarking utilities: VO2max categories, age grading, Jack Daniels
pace prediction, fuelling schedules and target splits.
"""

import math

# ── Constants ─────────────────────────────────────────────────────────────────
MARATHON_KM = 42.195
MARATHON_WR_SEC = 7235  # Kiptum 2023: 2:00:35

# WMA age-grading factors for male marathon, ages 20-30
_WMA_MALE_FACTORS: dict[int, float] = {
    20: 0.9573,
    21: 0.9638,
    22: 0.9695,
    23: 0.9745,
    24: 0.9787,
    25: 0.9821,
    26: 0.9847,
    27: 0.9866,
    28: 0.9878,
    29: 0.9882,
    30: 0.9878,
}

# ACSM VO2max categories for males aged 20-29
# Each entry: (category, lower_bound, percentile)
_ACSM_MALE_20_29: list[tuple[str, float, int]] = [
    ("Superior",   62.0, 97),
    ("Excellent",  56.0, 90),
    ("Good",       51.0, 75),
    ("Average",    44.0, 50),
    ("Fair",       38.0, 25),
    ("Poor",        0.0, 10),
]


# ── VO2max categorisation ─────────────────────────────────────────────────────

def vo2max_category(vo2max: float, age: int = 26, sex: str = "male") -> tuple[str, int]:
    """Return (category, percentile) for a given VO2max value.

    Currently implemented for males aged 20-29 only; all other combinations
    fall back to the same table.
    """
    for category, lower, percentile in _ACSM_MALE_20_29:
        if vo2max >= lower:
            return category, percentile
    # Below the lowest threshold
    return "Poor", 10


# ── Jack Daniels marathon time prediction ─────────────────────────────────────

def marathon_from_vo2max(vo2max: float) -> float:
    """Return predicted marathon finish time (seconds) from VO2max.

    Uses the Jack Daniels / Daniels Running Formula method:
      1. Target VO2 = vo2max × 0.839
      2. Solve the quadratic  0.000104v² + 0.182258v + (−4.60 − target_vo2) = 0
         for v (m/min).
      3. Finish time = (MARATHON_KM × 1000 / v) × 60  seconds.

    Returns math.nan if the discriminant is negative (no real solution).
    """
    target_vo2 = vo2max * 0.839
    a = 0.000104
    b = 0.182258
    c = -4.60 - target_vo2

    discriminant = b ** 2 - 4 * a * c
    if discriminant < 0:
        return math.nan

    # Take the positive root only
    v = (-b + math.sqrt(discriminant)) / (2 * a)  # m/min

    finish_sec = (MARATHON_KM * 1000.0 / v) * 60.0
    return finish_sec


# ── Age grading ───────────────────────────────────────────────────────────────

def age_grade(
    finish_sec: float, age: int = 26, sex: str = "male"
) -> tuple[float, float]:
    """Return (age_grade_pct, open_equivalent_sec) for a marathon finish.

    age_grade_pct = (WR_sec / finish_sec) × age_factor × 100
    open_equivalent_sec = finish_sec / age_factor

    WMA factors are looked up from _WMA_MALE_FACTORS; ages outside the
    20-30 range are clamped to the nearest available key.
    """
    clamped_age = max(20, min(30, age))
    age_factor = _WMA_MALE_FACTORS[clamped_age]

    pct = (MARATHON_WR_SEC / finish_sec) * age_factor * 100.0
    open_equiv = finish_sec / age_factor
    return pct, open_equiv


def age_grade_label(pct: float) -> str:
    """Return a descriptive label for an age-grade percentage."""
    if pct >= 90:
        return "World class"
    elif pct >= 80:
        return "National class"
    elif pct >= 70:
        return "Regional class"
    elif pct >= 60:
        return "Local class"
    elif pct >= 50:
        return "Average recreational"
    else:
        return "Beginner"


# ── Fuelling schedule ─────────────────────────────────────────────────────────

def fuelling_schedule(
    finish_sec: float,
    interval_sec: float = 2700,
    first_gel_sec: float = 2700,
) -> list[dict]:
    """Return a list of gel-taking events for a marathon.

    Gels are taken at first_gel_sec and then every interval_sec thereafter.
    The final gel is omitted if it falls within 900 seconds of the finish.

    Each dict contains:
        gel (int)  – gel number (1-indexed)
        time_sec (float) – elapsed time in seconds
        time_str (str)   – formatted as "H:MM" or "H:MM:SS" (always H:MM)
    """
    schedule: list[dict] = []
    gel_num = 1
    t = first_gel_sec

    while t <= finish_sec - 900:
        hours = int(t // 3600)
        minutes = int((t % 3600) // 60)
        time_str = f"{hours}:{minutes:02d}"
        schedule.append(
            {
                "gel": gel_num,
                "time_sec": t,
                "time_str": time_str,
            }
        )
        gel_num += 1
        t += interval_sec

    return schedule


# ── Target splits ─────────────────────────────────────────────────────────────

def target_splits(
    finish_sec: float,
    km_markers: list[float] = None,
) -> list[dict]:
    """Return target split times for a marathon using a negative-split strategy.

    The first half (0–21.0975 km) is allocated finish_sec/2 + 15 seconds;
    the second half receives the remainder.  Pace within each half is assumed
    to be constant.

    Each dict contains:
        km (float)             – distance marker
        pace_sec_per_km (float) – average pace for the segment ending here
        elapsed_sec (float)    – cumulative elapsed time at the marker
        elapsed_str (str)      – formatted as "H:MM:SS"
    """
    if km_markers is None:
        km_markers = [5, 10, 15, 20, 21.1, 25, 30, 35, 40, 42.195]

    half_km = MARATHON_KM / 2.0  # 21.0975 km

    first_half_sec = finish_sec / 2.0 + 15.0
    second_half_sec = finish_sec - first_half_sec

    first_pace = first_half_sec / half_km    # sec/km
    second_pace = second_half_sec / half_km  # sec/km

    def _elapsed_at(km: float) -> float:
        if km <= half_km:
            return km * first_pace
        else:
            return first_half_sec + (km - half_km) * second_pace

    def _format_elapsed(sec: float) -> str:
        total = int(round(sec))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h}:{m:02d}:{s:02d}"

    results: list[dict] = []
    prev_km = 0.0
    prev_elapsed = 0.0

    for km in km_markers:
        elapsed = _elapsed_at(km)
        segment_km = km - prev_km
        segment_sec = elapsed - prev_elapsed
        pace = segment_sec / segment_km if segment_km > 0 else 0.0

        results.append(
            {
                "km": km,
                "pace_sec_per_km": pace,
                "elapsed_sec": elapsed,
                "elapsed_str": _format_elapsed(elapsed),
            }
        )
        prev_km = km
        prev_elapsed = elapsed

    return results
