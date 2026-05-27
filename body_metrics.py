"""
Body metrics and physiology calculations for the fitness app.

Default profile: 87 kg, 178 cm, 26 yo male.
Profile values are persisted in the SQLite `user_profile` key/value table and
override these defaults when present.
"""

from database import get_conn

# ── Hardcoded defaults ────────────────────────────────────────────────────────
_DEFAULT_WEIGHT_KG = 87.0
_DEFAULT_HEIGHT_CM = 178.0
_DEFAULT_AGE = 26
_DEFAULT_SEX = "male"


# ── Profile persistence ───────────────────────────────────────────────────────

def _ensure_profile_table() -> None:
    """Create user_profile table if it does not exist."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def get_profile() -> dict:
    """Return the user profile as a dict.

    Keys: weight_kg (float), height_cm (float), age (int), sex (str).
    Reads from the SQLite ``user_profile`` key/value table; falls back to
    hardcoded defaults for any missing keys.
    """
    _ensure_profile_table()
    defaults = {
        "weight_kg": _DEFAULT_WEIGHT_KG,
        "height_cm": _DEFAULT_HEIGHT_CM,
        "age": _DEFAULT_AGE,
        "sex": _DEFAULT_SEX,
    }
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM user_profile").fetchall()

    db_values = {row["key"]: row["value"] for row in rows}

    return {
        "weight_kg": float(db_values.get("weight_kg", defaults["weight_kg"])),
        "height_cm": float(db_values.get("height_cm", defaults["height_cm"])),
        "age": int(db_values.get("age", defaults["age"])),
        "sex": str(db_values.get("sex", defaults["sex"])),
    }


def save_profile(weight_kg: float, height_cm: float, age: int, sex: str) -> None:
    """Upsert user profile values into the ``user_profile`` table."""
    _ensure_profile_table()
    entries = [
        ("weight_kg", str(weight_kg)),
        ("height_cm", str(height_cm)),
        ("age", str(age)),
        ("sex", str(sex)),
    ]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO user_profile (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            entries,
        )


# ── BMI ───────────────────────────────────────────────────────────────────────

def bmi(weight_kg: float, height_cm: float) -> float:
    """Return Body Mass Index (kg/m²)."""
    height_m = height_cm / 100.0
    return weight_kg / (height_m ** 2)


def bmi_category(bmi_val: float) -> str:
    """Return WHO BMI category string."""
    if bmi_val < 18.5:
        return "Underweight"
    elif bmi_val < 25.0:
        return "Normal"
    elif bmi_val < 30.0:
        return "Overweight"
    else:
        return "Obese"


# ── Racing weight ─────────────────────────────────────────────────────────────

def racing_weight_kg(height_cm: float) -> float:
    """Return the estimated racing weight using BMI 21.5 as target.

    BMI 21.5 is empirically associated with elite male marathon performance.
    """
    height_m = height_cm / 100.0
    return 21.5 * (height_m ** 2)


# ── Performance projections ───────────────────────────────────────────────────

def pace_gain_sec_per_km(current_kg: float, target_kg: float) -> float:
    """Return projected pace improvement (sec/km) from losing weight.

    Uses the rule of thumb: 1.25 sec/km faster per kg of body-weight lost.
    A positive return value means the runner gets faster.
    """
    return (current_kg - target_kg) * 1.25


def marathon_time_gain_sec(current_kg: float, target_kg: float) -> float:
    """Return total marathon time improvement (seconds) from losing weight."""
    return pace_gain_sec_per_km(current_kg, target_kg) * 42.195


# ── Energy & hydration ────────────────────────────────────────────────────────

def calories_per_km(weight_kg: float) -> float:
    """Return estimated calorie expenditure per km of running (kcal/km).

    Uses the approximate formula: weight_kg × 1.036.
    """
    return weight_kg * 1.036


def sweat_rate_ml_per_hour(weight_kg: float, pace_sec_per_km: float = None) -> float:
    """Return estimated sweat rate in ml/hour.

    Base rate: weight_kg × 11.5 ml/hour.
    Intensity multipliers:
      - pace < 300 sec/km (> ~20 km/h): × 1.3
      - pace < 360 sec/km (> ~16.7 km/h): × 1.15
      - otherwise: × 1.0
    """
    base = weight_kg * 11.5
    if pace_sec_per_km is not None:
        if pace_sec_per_km < 300:
            multiplier = 1.3
        elif pace_sec_per_km < 360:
            multiplier = 1.15
        else:
            multiplier = 1.0
    else:
        multiplier = 1.0
    return base * multiplier


def hydration_target_ml(
    weight_kg: float,
    duration_sec: float,
    pace_sec_per_km: float = None,
) -> float:
    """Return total hydration target in ml for a run of given duration.

    Derived from sweat_rate_ml_per_hour × duration in hours.
    """
    duration_hours = duration_sec / 3600.0
    rate = sweat_rate_ml_per_hour(weight_kg, pace_sec_per_km)
    return rate * duration_hours


# ── Heart-rate zones ──────────────────────────────────────────────────────────

def max_hr_estimate(age: int) -> int:
    """Return estimated maximum heart rate using the Tanaka formula.

    Tanaka et al. (2001): HRmax = 208 − 0.7 × age.
    """
    return round(208 - 0.7 * age)


def lthr(age: int, known_max_hr: int = None) -> float:
    """Return estimated lactate-threshold heart rate (90 % of HRmax).

    If ``known_max_hr`` is supplied it is used directly; otherwise HRmax is
    estimated via the Tanaka formula.
    """
    hr_max = known_max_hr if known_max_hr is not None else max_hr_estimate(age)
    return hr_max * 0.90
