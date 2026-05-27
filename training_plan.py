from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional

RACE_DATE = date(2026, 8, 30)

# Week 18 Sunday = race date → Week 1 Monday = race_date - 17 weeks - 6 days
_WEEK_18_MON = RACE_DATE - timedelta(days=6)
PLAN_START = _WEEK_18_MON - timedelta(weeks=17)  # 2026-04-27

MARATHON_KM = 42.195
HALF_MARATHON_KM = 21.0975

# Hal Higdon Novice 1 distances in km per day: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
# 0 = Rest, -1 = Cross-training
PLAN_KM = [
    [0, 4.8,  4.8,  4.8, 0,  9.7,          -1],   # Week 1
    [0, 4.8,  4.8,  4.8, 0, 11.3,          -1],   # Week 2
    [0, 4.8,  6.4,  4.8, 0,  8.1,          -1],   # Week 3
    [0, 4.8,  6.4,  4.8, 0, 14.5,          -1],   # Week 4
    [0, 4.8,  8.1,  4.8, 0, 16.1,          -1],   # Week 5
    [0, 4.8,  8.1,  4.8, 0, 11.3,          -1],   # Week 6
    [0, 4.8,  9.7,  4.8, 0, 19.3,          -1],   # Week 7
    [0, 4.8,  9.7,  4.8, 0,  0,   HALF_MARATHON_KM],  # Week 8 – Half Marathon
    [0, 4.8, 11.3,  6.4, 0, 16.1,          -1],   # Week 9
    [0, 4.8, 11.3,  6.4, 0, 24.1,          -1],   # Week 10
    [0, 6.4, 12.9,  6.4, 0, 25.7,          -1],   # Week 11
    [0, 6.4, 12.9,  8.1, 0, 19.3,          -1],   # Week 12
    [0, 6.4, 14.5,  8.1, 0, 29.0,          -1],   # Week 13
    [0, 8.1, 14.5,  8.1, 0, 22.5,          -1],   # Week 14
    [0, 8.1, 16.1,  8.1, 0, 32.2,          -1],   # Week 15
    [0, 8.1, 12.9,  6.4, 0, 19.3,          -1],   # Week 16
    [0, 6.4,  9.7,  4.8, 0, 12.9,          -1],   # Week 17
    [0, 4.8,  6.4,  3.2, 0,  0,   MARATHON_KM],   # Week 18 – Marathon
]

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class PlannedWorkout:
    date: date
    week: int
    day_name: str
    distance_km: float
    is_rest: bool
    is_cross: bool
    is_race: bool
    label: str


def get_workout(d: date) -> Optional[PlannedWorkout]:
    delta = (d - PLAN_START).days
    if delta < 0 or delta >= 18 * 7:
        return None
    week = delta // 7
    day = delta % 7
    dist = PLAN_KM[week][day]
    is_rest = dist == 0
    is_cross = dist == -1
    is_race = (week == 7 and day == 6) or (week == 17 and day == 6)

    if is_rest:
        label = "Rest"
    elif is_cross:
        label = "Cross-training"
    elif is_race and week == 7:
        label = "Half Marathon"
    elif is_race and week == 17:
        label = "Marathon"
    else:
        label = f"Run {max(dist, 0):.1f} km"

    return PlannedWorkout(
        date=d,
        week=week + 1,
        day_name=DAY_LABELS[day],
        distance_km=max(dist, 0.0),
        is_rest=is_rest,
        is_cross=is_cross,
        is_race=is_race,
        label=label,
    )


def get_weekly_planned_km(week: int) -> float:
    """Total planned running km for a given week (1-indexed). Cross-training excluded."""
    return sum(d for d in PLAN_KM[week - 1] if d > 0)


def current_week() -> int:
    delta = (date.today() - PLAN_START).days
    return min(max(delta // 7 + 1, 1), 18)


def plan_week_dates(week: int) -> tuple[date, date]:
    """Return (monday, sunday) for a 1-indexed plan week."""
    monday = PLAN_START + timedelta(weeks=week - 1)
    return monday, monday + timedelta(days=6)
