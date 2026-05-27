from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from benchmarks import (
    age_grade,
    age_grade_label,
    fuelling_schedule,
    marathon_from_vo2max,
    target_splits,
    vo2max_category,
)
from body_metrics import (
    bmi,
    bmi_category,
    calories_per_km,
    get_profile,
    hydration_target_ml,
    lthr,
    marathon_time_gain_sec,
    max_hr_estimate,
    pace_gain_sec_per_km,
    racing_weight_kg,
    save_profile,
    sweat_rate_ml_per_hour,
)
from database import get_conn, init_db
from projections import (
    current_projection,
    finish_time_str,
    get_all_runs,
    get_recent_runs,
    pace_str_to_sec_per_km,
    projection_history,
    riegel,
    sec_per_km_to_pace_str,
    weekly_summary,
)
from training_load import compute_load_series, project_load_to_race
from training_plan import (
    MARATHON_KM,
    PLAN_KM,
    PLAN_START,
    RACE_DATE,
    current_week,
    get_workout,
    get_weekly_planned_km,
    plan_week_dates,
)

CONFIG_PATH = Path.home() / ".fitnessapp" / "config.json"
TOKEN_DIR = Path.home() / ".garminconnect"


def _get_garmin_client():
    """
    Return an authenticated Garmin client, trying in order:
    1. Already stored in session state (fastest, works on Streamlit Cloud)
    2. Streamlit secrets GARMIN_EMAIL / GARMIN_PASSWORD (Streamlit Cloud config)
    3. Local token files in TOKEN_DIR (local development)
    """
    if st.session_state.get("garmin_client"):
        return st.session_state["garmin_client"]

    from garminconnect import Garmin

    # Streamlit secrets
    try:
        email = st.secrets.get("GARMIN_EMAIL", "")
        password = st.secrets.get("GARMIN_PASSWORD", "")
        if email and password:
            client = Garmin(email=email, password=password)
            client.login()
            st.session_state["garmin_client"] = client
            return client
    except Exception:
        pass

    # Local token files
    try:
        if TOKEN_DIR.exists() and any(TOKEN_DIR.iterdir()):
            client = Garmin()
            client.login(tokenstore=str(TOKEN_DIR))
            st.session_state["garmin_client"] = client
            return client
    except Exception:
        pass

    return None


# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"goal_pace": "6:00"}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ── Cached data loaders ───────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _weekly_summary():
    return weekly_summary()


@st.cache_data(ttl=300)
def _all_runs():
    return get_all_runs()


@st.cache_data(ttl=300)
def _load_series(age: int, max_hr: Optional[int]):
    return compute_load_series(age, max_hr)


@st.cache_data(ttl=300)
def _projection_history():
    return projection_history()


def _no_data(msg: str = "No data yet — click **Sync Garmin Data** in the sidebar."):
    st.info(msg)


# ── Init ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Marathon Dashboard", page_icon="🏃", layout="wide")
init_db()
cfg = load_config()
profile = get_profile()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Marathon Tracker")
    st.caption("Hal Higdon Novice 1  ·  18 weeks")
    st.divider()

    # Goal pace
    goal_pace_input = st.text_input(
        "Goal marathon pace (mm:ss/km)",
        value=cfg.get("goal_pace", "6:00"),
        help="e.g. 6:00  →  ~4h13m finish",
    )
    goal_pace_sec: Optional[float] = None
    goal_finish_str = "–"
    try:
        goal_pace_sec = pace_str_to_sec_per_km(goal_pace_input)
        goal_finish_str = finish_time_str(goal_pace_sec * MARATHON_KM)
        st.metric("Goal finish time", goal_finish_str)
        if goal_pace_input != cfg.get("goal_pace"):
            cfg["goal_pace"] = goal_pace_input
            save_config(cfg)
    except Exception:
        st.warning("Enter pace as M:SS (e.g. 6:00)")

    st.divider()

    # Editable weight (affects calorie/projection calcs)
    sidebar_weight = st.number_input(
        "Current weight (kg)",
        min_value=40.0, max_value=200.0,
        value=profile["weight_kg"], step=0.5,
    )
    if abs(sidebar_weight - profile["weight_kg"]) > 0.1:
        save_profile(sidebar_weight, profile["height_cm"], profile["age"], profile["sex"])
        profile["weight_kg"] = sidebar_weight
        st.cache_data.clear()

    st.divider()

    # Race countdown
    days_to_race = (RACE_DATE - date.today()).days
    cw = current_week()
    st.metric("Race date", RACE_DATE.strftime("%b %d, %Y"))
    st.metric("Days to race", days_to_race)
    st.metric("Plan week", f"{cw} / 18")

    today_workout = get_workout(date.today())
    if today_workout:
        if today_workout.is_rest:
            st.info("Today: Rest day")
        elif today_workout.is_cross:
            st.info("Today: Cross-training")
        else:
            st.success(f"Today: {today_workout.label}")

    st.divider()

    # ── Garmin Connect auth + sync ────────────────────────────────────────────
    garmin_client = _get_garmin_client()

    if garmin_client:
        sync_days = st.slider("Days to sync", 30, 180, 120, 30)
        if st.button("Sync Garmin Data", use_container_width=True, type="primary"):
            with st.spinner("Syncing Garmin Connect…"):
                try:
                    from garmin_sync import sync
                    count = sync(days=sync_days, client=garmin_client)
                    st.success(f"Synced {count} run activities")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Sync failed: {exc}")

        from garmin_sync import last_sync_time
        last = last_sync_time()
        if last:
            st.caption(f"Last sync: {datetime.fromisoformat(last).strftime('%b %d, %H:%M')}")

        if st.button("Log out", use_container_width=True):
            st.session_state.pop("garmin_client", None)
            import shutil
            shutil.rmtree(TOKEN_DIR, ignore_errors=True)
            st.rerun()

    else:
        st.markdown("**Connect Garmin Account**")

        if st.session_state.get("garmin_mfa_pending"):
            st.info("Enter the code from your Garmin / authenticator email.")
            mfa_code = st.text_input("Verification code", max_chars=10)
            c1, c2 = st.columns(2)
            if c1.button("Verify", use_container_width=True, type="primary"):
                try:
                    pending = st.session_state["garmin_client_pending"]
                    pending.resume_login(mfa_code=mfa_code)
                    st.session_state["garmin_client"] = pending
                    st.session_state.pop("garmin_mfa_pending", None)
                    st.session_state.pop("garmin_client_pending", None)
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Code incorrect: {exc}")
            if c2.button("Cancel", use_container_width=True):
                st.session_state.pop("garmin_mfa_pending", None)
                st.session_state.pop("garmin_client_pending", None)
                st.rerun()

        else:
            email = st.text_input("Garmin email")
            password = st.text_input("Garmin password", type="password")
            st.caption(
                "Using Google Sign-In? Set a Garmin password first:  \n"
                "connect.garmin.com → Account → Security → Password"
            )
            if st.button("Connect", use_container_width=True, type="primary"):
                if not email or not password:
                    st.warning("Enter your email and password.")
                else:
                    with st.spinner("Connecting…"):
                        try:
                            from garminconnect import Garmin
                            client = Garmin(email=email, password=password, return_on_mfa=True)
                            status, _ = client.login()
                            if status == "needs_mfa":
                                st.session_state["garmin_mfa_pending"] = True
                                st.session_state["garmin_client_pending"] = client
                                st.rerun()
                            else:
                                st.session_state["garmin_client"] = client
                                st.cache_data.clear()
                                st.rerun()
                        except Exception as exc:
                            st.error(f"Login failed: {exc}")

# ── Header ────────────────────────────────────────────────────────────────────

st.title("Marathon Training Dashboard")
st.caption(
    f"Hal Higdon Novice 1  ·  Race {RACE_DATE.strftime('%B %d, %Y')}"
    f"  ·  Week {cw}/18  ·  {days_to_race} days to go"
)

tabs = st.tabs([
    "Volume",
    "Fitness",
    "HR & Recovery",
    "Training Load",
    "Race Planner",
    "Body & Benchmarks",
])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — WEEKLY VOLUME
# ═══════════════════════════════════════════════════════════════════════════════

with tabs[0]:
    st.subheader("Weekly Volume: Actual vs Planned")

    weekly = _weekly_summary()
    df_w = pd.DataFrame(weekly)

    def _bar_color(row):
        if row["actual_km"] is None:
            return "rgba(0,0,0,0)"
        if row["actual_km"] >= row["planned_km"] * 0.9:
            return "rgba(0,200,120,0.75)"
        if row["actual_km"] >= row["planned_km"] * 0.6:
            return "rgba(255,165,0,0.75)"
        return "rgba(239,85,59,0.75)"

    df_w["color"] = df_w.apply(_bar_color, axis=1)
    actual_df = df_w[df_w["actual_km"].notna()].copy()

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df_w["week"], y=df_w["planned_km"], name="Planned",
        marker_color="rgba(99,110,250,0.2)",
        marker_line_color="rgba(99,110,250,0.7)", marker_line_width=1.5,
    ))
    fig.add_trace(go.Bar(
        x=actual_df["week"], y=actual_df["actual_km"], name="Actual",
        marker_color=actual_df["color"].tolist(),
        customdata=actual_df[["planned_km"]].values,
        hovertemplate="Week %{x}<br>Actual: %{y:.1f} km<br>Planned: %{customdata[0]:.1f} km<extra></extra>",
    ))
    fig.add_vline(x=cw, line_dash="dash", line_color="orange",
                  annotation_text=f"Week {cw}", annotation_position="top right",
                  annotation_font_color="orange")
    fig.update_layout(barmode="overlay", xaxis=dict(title="Week", dtick=1),
                      yaxis_title="km", legend=dict(orientation="h", y=1.08), height=380)
    st.plotly_chart(fig, use_container_width=True)

    # Summary metrics
    completed = actual_df[~actual_df["is_current"]]
    if not completed.empty:
        on_track = int((completed["actual_km"] >= completed["planned_km"] * 0.9).sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("Weeks on track (≥90%)", f"{on_track} / {len(completed)}")
        c2.metric("Actual km so far", f"{completed['actual_km'].sum():.0f} km")
        c3.metric("Planned km so far", f"{completed['planned_km'].sum():.0f} km",
                  delta=f"{completed['actual_km'].sum() - completed['planned_km'].sum():+.0f} km")

    # 10% rule analysis
    st.subheader("10% Rule Analysis")
    st.caption("Weeks where planned volume increases >10% vs prior week — a known injury-risk signal.")

    rule_rows = []
    for i in range(1, len(PLAN_KM)):
        prev = get_weekly_planned_km(i)
        curr = get_weekly_planned_km(i + 1)
        change_pct = (curr - prev) / prev * 100 if prev > 0 else 0
        rule_rows.append({
            "Week": f"{i} → {i+1}",
            "Prev (km)": round(prev, 1),
            "Next (km)": round(curr, 1),
            "Change": f"{change_pct:+.0f}%",
            "Flag": "⚠️ >10% spike" if change_pct > 10 else ("📉 Step-back" if change_pct < -5 else "✅"),
        })
    df_rule = pd.DataFrame(rule_rows)
    st.dataframe(df_rule, use_container_width=True, hide_index=True,
                 column_config={"Flag": st.column_config.TextColumn(width="medium")})

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — FITNESS
# ═══════════════════════════════════════════════════════════════════════════════

with tabs[1]:
    runs = _all_runs()

    # ── Pace Trend ────────────────────────────────────────────────────────────
    st.subheader("Training Pace Trend")

    if not runs:
        _no_data()
    else:
        df_r = pd.DataFrame(runs)
        df_r = df_r[df_r["avg_pace_sec_per_km"].notna()].copy()
        df_r["start_date"] = pd.to_datetime(df_r["start_time"]).dt.date
        df_r = df_r.sort_values("start_date")
        df_r["pace_min_km"] = df_r["avg_pace_sec_per_km"] / 60

        fig2 = go.Figure()
        sizes = (df_r["distance_km"].clip(upper=35) * 0.55 + 4).tolist()
        fig2.add_trace(go.Scatter(
            x=df_r["start_date"], y=df_r["pace_min_km"],
            mode="markers+lines", name="Run pace",
            marker=dict(size=sizes, color="rgba(0,180,130,0.8)"),
            line=dict(color="rgba(0,180,130,0.35)", width=1),
            customdata=df_r[["distance_km", "avg_hr"]].fillna(0).values,
            hovertemplate="<b>%{x}</b><br>Pace: %{y:.2f} min/km<br>Distance: %{customdata[0]:.1f} km<br>HR: %{customdata[1]:.0f} bpm<extra></extra>",
        ))
        # 7-day rolling
        rolling = df_r.set_index("start_date")["pace_min_km"].rolling(7, min_periods=1).mean()
        fig2.add_trace(go.Scatter(
            x=rolling.index, y=rolling.values, mode="lines", name="7-day trend",
            line=dict(color="rgba(0,180,130,0.9)", width=2.5, dash="dot"),
        ))
        if goal_pace_sec:
            easy = (goal_pace_sec + 75) / 60
            fig2.add_hline(y=goal_pace_sec / 60, line_dash="dash",
                           line_color="rgba(220,50,50,0.8)",
                           annotation_text=f"Goal: {goal_pace_input}/km",
                           annotation_position="bottom right")
            fig2.add_hline(y=easy, line_dash="dot",
                           line_color="rgba(255,165,0,0.6)",
                           annotation_text=f"Easy: {sec_per_km_to_pace_str(goal_pace_sec+75)}",
                           annotation_position="top right")
        fig2.update_layout(xaxis_title="Date",
                           yaxis=dict(title="min/km", autorange="reversed"),
                           height=380, legend=dict(orientation="h", y=1.08))
        st.plotly_chart(fig2, use_container_width=True)
        st.caption("Bubble size = distance. Red dashed = goal marathon pace. Orange = easy training pace (goal + 75 sec/km).")

    st.divider()

    # ── HR Efficiency Index ───────────────────────────────────────────────────
    st.subheader("Cardiac Efficiency Index")
    st.caption("Speed ÷ heart rate × 100. Rising trend = getting fitter at the same effort.")

    hr_runs = [r for r in (runs or []) if r.get("avg_hr") and r.get("avg_pace_sec_per_km")]
    if not hr_runs:
        _no_data("No runs with HR data yet.")
    else:
        df_eff = pd.DataFrame(hr_runs)
        df_eff["start_date"] = pd.to_datetime(df_eff["start_time"]).dt.date
        df_eff = df_eff.sort_values("start_date")
        df_eff["speed_km_h"] = 3600 / df_eff["avg_pace_sec_per_km"]
        df_eff["efficiency"] = df_eff["speed_km_h"] / df_eff["avg_hr"] * 100

        fig_eff = go.Figure()
        fig_eff.add_trace(go.Scatter(
            x=df_eff["start_date"], y=df_eff["efficiency"],
            mode="markers+lines", name="Efficiency",
            marker=dict(size=8, color="rgba(99,110,250,0.8)"),
            line=dict(color="rgba(99,110,250,0.3)", width=1),
            hovertemplate="<b>%{x}</b><br>Efficiency: %{y:.2f}<br><extra></extra>",
        ))
        rolling_eff = df_eff.set_index("start_date")["efficiency"].rolling(7, min_periods=1).mean()
        fig_eff.add_trace(go.Scatter(
            x=rolling_eff.index, y=rolling_eff.values, mode="lines", name="7-day trend",
            line=dict(color="rgba(99,110,250,0.9)", width=2.5, dash="dot"),
        ))
        fig_eff.update_layout(xaxis_title="Date", yaxis_title="km/h per bpm × 100",
                              height=300, legend=dict(orientation="h", y=1.1))
        st.plotly_chart(fig_eff, use_container_width=True)

        first_3 = df_eff["efficiency"].head(3).mean()
        last_3 = df_eff["efficiency"].tail(3).mean()
        if not math.isnan(first_3) and not math.isnan(last_3):
            pct_change = (last_3 - first_3) / first_3 * 100
            sign = "+" if pct_change > 0 else ""
            c1, c2 = st.columns(2)
            c1.metric("Early training efficiency", f"{first_3:.2f}")
            c2.metric("Recent efficiency", f"{last_3:.2f}", delta=f"{sign}{pct_change:.1f}%")

    st.divider()

    # ── VO2max trend ──────────────────────────────────────────────────────────
    st.subheader("VO2max Trend")
    with get_conn() as conn:
        vo2_rows = conn.execute(
            "SELECT date, vo2max, fitness_age FROM vo2max WHERE vo2max IS NOT NULL ORDER BY date"
        ).fetchall()
    if vo2_rows:
        df_vo2 = pd.DataFrame([dict(r) for r in vo2_rows])
        df_vo2["date"] = pd.to_datetime(df_vo2["date"])
        latest_vo2 = float(df_vo2["vo2max"].iloc[-1])

        fig_vo2 = go.Figure()
        fig_vo2.add_trace(go.Scatter(
            x=df_vo2["date"], y=df_vo2["vo2max"],
            mode="lines+markers", name="VO2max",
            line=dict(color="rgba(239,85,59,0.8)", width=2),
            marker=dict(size=8),
        ))
        cat, pct = vo2max_category(latest_vo2)
        fig_vo2.add_hline(y=latest_vo2, line_dash="dash", line_color="rgba(239,85,59,0.4)",
                          annotation_text=f"Current: {latest_vo2:.1f} ({cat})")
        fig_vo2.update_layout(xaxis_title="Date", yaxis_title="ml/kg/min",
                              height=280, legend=dict(orientation="h"))
        st.plotly_chart(fig_vo2, use_container_width=True)

        predicted_marathon = marathon_from_vo2max(latest_vo2)
        c1, c2, c3 = st.columns(3)
        c1.metric("Current VO2max", f"{latest_vo2:.1f} ml/kg/min")
        c2.metric("Fitness category", f"{cat} ({pct}th %ile)")
        if not math.isnan(predicted_marathon):
            c3.metric("Predicted marathon (VO2max)", finish_time_str(predicted_marathon))
        if df_vo2.get("fitness_age") is not None:
            fa = df_vo2["fitness_age"].iloc[-1]
            if pd.notna(fa):
                st.caption(f"Garmin fitness age: {int(fa)}  (your actual age: {profile['age']})")
    else:
        st.info("VO2max data not yet synced — requires a Garmin device that estimates VO2max (e.g. Forerunner, Fenix).")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — HEART RATE & RECOVERY
# ═══════════════════════════════════════════════════════════════════════════════

with tabs[2]:

    col_rhr, col_run_hr = st.columns(2)

    # ── Resting HR ────────────────────────────────────────────────────────────
    with col_rhr:
        st.subheader("Resting Heart Rate")
        cutoff = (date.today() - timedelta(days=120)).isoformat()
        with get_conn() as conn:
            hr_rows = conn.execute(
                "SELECT date, resting_hr FROM daily_hr WHERE date >= ? ORDER BY date", (cutoff,)
            ).fetchall()

        if hr_rows:
            hr_df = pd.DataFrame([dict(r) for r in hr_rows])
            hr_df["date"] = pd.to_datetime(hr_df["date"])
            hr_df["rolling"] = hr_df["resting_hr"].rolling(7, min_periods=1).mean()

            fig3 = go.Figure()
            fig3.add_trace(go.Scatter(x=hr_df["date"], y=hr_df["resting_hr"],
                                     mode="markers", name="Daily",
                                     marker=dict(color="rgba(239,85,59,0.45)", size=5)))
            fig3.add_trace(go.Scatter(x=hr_df["date"], y=hr_df["rolling"],
                                     mode="lines", name="7-day avg",
                                     line=dict(color="rgba(200,30,30,0.85)", width=2.5),
                                     fill="tozeroy", fillcolor="rgba(239,85,59,0.06)"))
            fig3.update_layout(yaxis_title="bpm", height=280, legend=dict(orientation="h"),
                               margin=dict(t=20, b=30))
            st.plotly_chart(fig3, use_container_width=True)

            latest_rhr = int(hr_df["resting_hr"].iloc[-1])
            avg_rhr = hr_df["resting_hr"].mean()
            st.metric("Latest RHR", f"{latest_rhr} bpm",
                      delta=f"{latest_rhr - avg_rhr:+.0f} vs 120-day avg", delta_color="inverse")
        else:
            _no_data()

    # ── Per-run HR ────────────────────────────────────────────────────────────
    with col_run_hr:
        st.subheader("Avg HR per Run")
        hr_runs = [r for r in (_all_runs() or []) if r.get("avg_hr")]
        if hr_runs:
            df_rh = pd.DataFrame(hr_runs)
            df_rh["start_date"] = pd.to_datetime(df_rh["start_time"]).dt.date
            df_rh = df_rh.sort_values("start_date")

            fig4 = go.Figure()
            fig4.add_trace(go.Scatter(
                x=df_rh["start_date"], y=df_rh["avg_hr"],
                mode="markers+lines", name="Avg HR",
                marker=dict(color="rgba(239,85,59,0.7)", size=7),
                line=dict(color="rgba(239,85,59,0.3)", width=1),
                customdata=df_rh[["distance_km", "avg_pace_sec_per_km"]].fillna(0).values,
                hovertemplate="<b>%{x}</b><br>HR: %{y} bpm<br>Distance: %{customdata[0]:.1f} km<extra></extra>",
            ))
            lthr_val = lthr(profile["age"])
            fig4.add_hline(y=lthr_val, line_dash="dot", line_color="rgba(255,165,0,0.6)",
                           annotation_text=f"LTHR est: {lthr_val:.0f}", annotation_position="right")
            fig4.update_layout(yaxis_title="bpm", height=280, legend=dict(orientation="h"),
                               margin=dict(t=20, b=30))
            st.plotly_chart(fig4, use_container_width=True)
            st.metric("Latest run avg HR", f"{int(df_rh['avg_hr'].iloc[-1])} bpm")
        else:
            _no_data()

    st.divider()

    # ── Sleep–Performance Correlation ─────────────────────────────────────────
    st.subheader("Sleep Score vs Next-Day Run Pace")
    st.caption("Does a bad night's sleep slow you down? Runs are matched to the previous night's sleep score.")

    with get_conn() as conn:
        sleep_run_rows = conn.execute(
            """
            SELECT a.start_time, a.avg_pace_sec_per_km, a.distance_km, s.sleep_score
            FROM activities a
            JOIN sleep s ON date(a.start_time, '-1 day') = s.date
            WHERE a.activity_type = 'running'
              AND a.avg_pace_sec_per_km IS NOT NULL
              AND s.sleep_score IS NOT NULL
            ORDER BY a.start_time
            """
        ).fetchall()

    if len(sleep_run_rows) >= 5:
        df_sl = pd.DataFrame([dict(r) for r in sleep_run_rows])
        df_sl["pace_min_km"] = df_sl["avg_pace_sec_per_km"] / 60
        df_sl["start_date"] = pd.to_datetime(df_sl["start_time"]).dt.date

        corr = df_sl["sleep_score"].corr(df_sl["pace_min_km"])
        direction = "slower after bad sleep" if corr > 0 else "not strongly correlated"

        fig_sl = go.Figure()
        sizes = (df_sl["distance_km"].clip(upper=30) * 0.5 + 5).tolist()
        fig_sl.add_trace(go.Scatter(
            x=df_sl["sleep_score"], y=df_sl["pace_min_km"],
            mode="markers",
            marker=dict(size=sizes, color="rgba(99,110,250,0.7)"),
            text=df_sl["start_date"].astype(str),
            hovertemplate="Sleep: %{x}<br>Pace: %{y:.2f} min/km<br>%{text}<extra></extra>",
        ))
        # Trend line
        z = np.polyfit(df_sl["sleep_score"], df_sl["pace_min_km"], 1)
        x_line = np.linspace(df_sl["sleep_score"].min(), df_sl["sleep_score"].max(), 50)
        fig_sl.add_trace(go.Scatter(x=x_line, y=np.polyval(z, x_line), mode="lines",
                                    name="Trend", line=dict(color="orange", dash="dash", width=2)))
        fig_sl.update_layout(
            xaxis_title="Sleep score (previous night)",
            yaxis=dict(title="Run pace (min/km)", autorange="reversed"),
            height=300, showlegend=False,
        )
        st.plotly_chart(fig_sl, use_container_width=True)
        st.metric("Correlation coefficient", f"{corr:+.2f}",
                  help="-1 = faster after good sleep, +1 = slower after good sleep, 0 = no relationship")
        st.caption(f"Based on {len(df_sl)} matched sleep/run pairs. Interpretation: {direction}.")
    else:
        _no_data(f"Need at least 5 matched sleep/run pairs (have {len(sleep_run_rows)}). Sync more data.")

    st.divider()

    col_bb, col_stress, col_hrv = st.columns(3)

    # ── Body Battery ──────────────────────────────────────────────────────────
    with col_bb:
        st.subheader("Body Battery")
        with get_conn() as conn:
            bb_rows = conn.execute(
                "SELECT date, charged, drained FROM body_battery WHERE date >= ? ORDER BY date",
                (cutoff,)
            ).fetchall()
        if bb_rows:
            df_bb = pd.DataFrame([dict(r) for r in bb_rows])
            df_bb["date"] = pd.to_datetime(df_bb["date"])
            df_bb = df_bb.dropna(subset=["charged"])
            fig_bb = go.Figure()
            fig_bb.add_trace(go.Bar(x=df_bb["date"], y=df_bb["charged"],
                                    name="Charged", marker_color="rgba(0,200,120,0.7)"))
            if df_bb["drained"].notna().any():
                fig_bb.add_trace(go.Bar(x=df_bb["date"], y=-df_bb["drained"].abs(),
                                        name="Drained", marker_color="rgba(239,85,59,0.7)"))
            fig_bb.update_layout(barmode="relative", yaxis_title="Battery points",
                                 height=250, legend=dict(orientation="h"), margin=dict(t=10, b=20))
            st.plotly_chart(fig_bb, use_container_width=True)
        else:
            _no_data("Body Battery not available (requires supported Garmin device).")

    # ── Stress ────────────────────────────────────────────────────────────────
    with col_stress:
        st.subheader("Daily Stress")
        with get_conn() as conn:
            stress_rows = conn.execute(
                "SELECT date, avg_stress, max_stress FROM stress WHERE date >= ? AND avg_stress IS NOT NULL ORDER BY date",
                (cutoff,)
            ).fetchall()
        if stress_rows:
            df_st = pd.DataFrame([dict(r) for r in stress_rows])
            df_st["date"] = pd.to_datetime(df_st["date"])
            fig_st = go.Figure()
            fig_st.add_trace(go.Scatter(x=df_st["date"], y=df_st["avg_stress"],
                                        mode="lines", name="Avg stress",
                                        fill="tozeroy", fillcolor="rgba(255,165,0,0.15)",
                                        line=dict(color="rgba(255,165,0,0.8)", width=2)))
            fig_st.add_hline(y=25, line_dash="dot", line_color="rgba(0,200,120,0.5)",
                             annotation_text="Low", annotation_position="right")
            fig_st.add_hline(y=50, line_dash="dot", line_color="rgba(255,165,0,0.5)",
                             annotation_text="Medium", annotation_position="right")
            fig_st.update_layout(yaxis=dict(title="Stress (0-100)", range=[0, 100]),
                                 height=250, showlegend=False, margin=dict(t=10, b=20))
            st.plotly_chart(fig_st, use_container_width=True)
        else:
            _no_data("Stress data not yet available.")

    # ── HRV ───────────────────────────────────────────────────────────────────
    with col_hrv:
        st.subheader("HRV (Heart Rate Variability)")
        with get_conn() as conn:
            hrv_rows = conn.execute(
                "SELECT date, weekly_avg, last_night FROM hrv WHERE date >= ? AND last_night IS NOT NULL ORDER BY date",
                (cutoff,)
            ).fetchall()
        if hrv_rows:
            df_hrv = pd.DataFrame([dict(r) for r in hrv_rows])
            df_hrv["date"] = pd.to_datetime(df_hrv["date"])
            fig_hrv = go.Figure()
            fig_hrv.add_trace(go.Scatter(x=df_hrv["date"], y=df_hrv["last_night"],
                                         mode="markers", name="Last night",
                                         marker=dict(color="rgba(99,110,250,0.6)", size=5)))
            if df_hrv["weekly_avg"].notna().any():
                fig_hrv.add_trace(go.Scatter(x=df_hrv["date"], y=df_hrv["weekly_avg"],
                                             mode="lines", name="Weekly avg",
                                             line=dict(color="rgba(99,110,250,0.9)", width=2)))
            fig_hrv.update_layout(yaxis_title="ms", height=250,
                                  legend=dict(orientation="h"), margin=dict(t=10, b=20))
            st.plotly_chart(fig_hrv, use_container_width=True)
            st.caption("Higher & stable HRV = well recovered. Sudden drops = fatigue or illness.")
        else:
            _no_data("HRV data not yet available (requires compatible Garmin device).")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — TRAINING LOAD (ATL / CTL / TSB)
# ═══════════════════════════════════════════════════════════════════════════════

with tabs[3]:
    st.subheader("Training Load: Fitness · Fatigue · Form")
    st.caption(
        "**CTL** (42-day avg) = Fitness.  "
        "**ATL** (7-day avg) = Fatigue.  "
        "**TSB** = Fitness − Fatigue = Form.  "
        "Positive TSB on race day = fresh and ready."
    )

    with get_conn() as conn:
        has_hr = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE avg_hr > 0"
        ).fetchone()[0]
        max_hr_row = conn.execute(
            "SELECT MAX(max_hr) FROM activities"
        ).fetchone()[0]

    if has_hr == 0:
        _no_data("Need run activities with HR data to compute training load.")
    else:
        actual_series = _load_series(profile["age"], max_hr_row)
        projected_series = project_load_to_race(actual_series, profile["age"], max_hr_row)
        combined = actual_series + projected_series

        if combined:
            df_load = pd.DataFrame(combined)
            df_load["date"] = pd.to_datetime(df_load["date"])
            df_load["projected"] = df_load.get("projected", False).fillna(False)

            actual_mask = ~df_load["projected"].astype(bool)
            proj_mask = df_load["projected"].astype(bool)

            fig_load = go.Figure()

            for col, color, label in [
                ("ctl", "rgba(0,180,130,0.9)", "Fitness (CTL)"),
                ("atl", "rgba(239,85,59,0.9)", "Fatigue (ATL)"),
            ]:
                # Actual
                fig_load.add_trace(go.Scatter(
                    x=df_load.loc[actual_mask, "date"],
                    y=df_load.loc[actual_mask, col],
                    mode="lines", name=label,
                    line=dict(color=color, width=2.5),
                ))
                # Projected
                if proj_mask.any():
                    fig_load.add_trace(go.Scatter(
                        x=df_load.loc[proj_mask, "date"],
                        y=df_load.loc[proj_mask, col],
                        mode="lines", name=f"{label} (projected)",
                        line=dict(color=color, width=1.5, dash="dot"),
                        showlegend=False,
                    ))

            # TSB as bar
            fig_load.add_trace(go.Bar(
                x=df_load.loc[actual_mask, "date"],
                y=df_load.loc[actual_mask, "tsb"],
                name="Form (TSB)",
                marker_color=[
                    "rgba(0,200,120,0.5)" if v >= 0 else "rgba(239,85,59,0.5)"
                    for v in df_load.loc[actual_mask, "tsb"]
                ],
                yaxis="y2",
            ))

            for x_val, color, label in [
                (date.today().isoformat(), "orange", "Today"),
                (RACE_DATE.isoformat(), "rgba(220,50,50,0.7)", "Race"),
            ]:
                fig_load.add_shape(
                    type="line", x0=x_val, x1=x_val, y0=0, y1=1, yref="paper",
                    line=dict(dash="dash", color=color, width=1),
                )
                fig_load.add_annotation(
                    x=x_val, y=1.02, yref="paper", text=label,
                    showarrow=False, font=dict(color=color),
                )
            fig_load.add_hline(y=0, line_color="rgba(100,100,100,0.3)", yref="y2")

            fig_load.update_layout(
                yaxis=dict(title="Load (TSS units)"),
                yaxis2=dict(title="Form (TSB)", overlaying="y", side="right",
                            zeroline=True, zerolinecolor="rgba(100,100,100,0.3)"),
                legend=dict(orientation="h", y=1.08),
                height=420,
                barmode="relative",
            )
            st.plotly_chart(fig_load, use_container_width=True)

            # Current state metrics
            if actual_series:
                latest = actual_series[-1]
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Fitness (CTL)", f"{latest['ctl']:.0f}")
                c2.metric("Fatigue (ATL)", f"{latest['atl']:.0f}")
                tsb = latest["tsb"]
                c3.metric("Form (TSB)", f"{tsb:+.0f}",
                          delta="Fresh" if tsb > 5 else ("Neutral" if tsb > -10 else "Fatigued"),
                          delta_color="normal" if tsb > 5 else ("off" if tsb > -10 else "inverse"))

                # Race-day projected form
                if projected_series:
                    race_day = projected_series[-1]
                    c4.metric("Projected race-day TSB", f"{race_day['tsb']:+.0f}",
                              help="Target: +5 to +15 (taper effect)")

        lthr_val = lthr(profile["age"], max_hr_row)
        st.caption(
            f"LTHR used: {lthr_val:.0f} bpm  "
            f"(90% of {'measured' if max_hr_row else 'estimated'} max HR "
            f"{max_hr_row or max_hr_estimate(profile['age'])}).  "
            "Bars show TSB on right axis — green = positive form, red = accumulated fatigue."
        )

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5 — RACE PLANNER
# ═══════════════════════════════════════════════════════════════════════════════

with tabs[4]:
    st.subheader("Projected Finish Time")

    history = _projection_history()
    proj = current_projection(goal_pace_sec)

    # Multi-model comparison
    with get_conn() as conn:
        vo2_latest = conn.execute(
            "SELECT vo2max FROM vo2max WHERE vo2max IS NOT NULL ORDER BY date DESC LIMIT 1"
        ).fetchone()
    latest_vo2 = float(vo2_latest["vo2max"]) if vo2_latest else None

    models: dict[str, float] = {}
    if proj:
        models["Riegel (longest run)"] = proj["riegel_seconds"]
        if proj.get("pace_based_seconds"):
            models["Avg pace (last 5 runs)"] = proj["pace_based_seconds"]
    if latest_vo2 and not math.isnan(marathon_from_vo2max(latest_vo2)):
        models[f"VO2max ({latest_vo2:.0f})"] = marathon_from_vo2max(latest_vo2)
    if goal_pace_sec:
        models["Goal target"] = goal_pace_sec * MARATHON_KM

    if models:
        col_models, col_history = st.columns([1, 2])

        with col_models:
            st.markdown("**Model comparison**")
            for label, secs in sorted(models.items(), key=lambda x: x[1]):
                st.metric(label, finish_time_str(secs))
            if proj and proj.get("based_on_run_km"):
                st.caption(
                    f"Riegel based on {proj['based_on_run_km']:.1f} km run "
                    f"at {sec_per_km_to_pace_str(proj['based_on_run_pace'])}"
                )

        with col_history:
            if history:
                df_ph = pd.DataFrame(history)
                df_ph["hours"] = df_ph["projected_seconds"] / 3600

                fig5 = go.Figure()
                fig5.add_trace(go.Scatter(
                    x=df_ph["week"], y=df_ph["hours"],
                    mode="lines+markers", name="Riegel projection",
                    line=dict(color="rgba(0,180,200,0.85)", width=2.5),
                    marker=dict(size=8),
                    hovertemplate="Week %{x}<br>Projected: %{y:.2f}h<extra></extra>",
                ))
                if goal_pace_sec:
                    goal_h = goal_pace_sec * MARATHON_KM / 3600
                    fig5.add_hline(y=goal_h, line_dash="dash",
                                   line_color="rgba(220,50,50,0.7)",
                                   annotation_text=f"Goal: {goal_finish_str}")
                fig5.update_layout(
                    xaxis=dict(title="Plan week", dtick=1, range=[1, 18]),
                    yaxis_title="Projected finish (hours)",
                    height=320, legend=dict(orientation="h", y=1.08),
                )
                st.plotly_chart(fig5, use_container_width=True)
                st.caption("Projection updates weekly as your longest run grows. Dotted = remaining plan.")
            else:
                _no_data("Sync at least one run of 5+ km to see projections.")
    else:
        _no_data()

    st.divider()

    # ── Target Splits ─────────────────────────────────────────────────────────
    best_estimate_sec = min(models.values()) if models else (goal_pace_sec * MARATHON_KM if goal_pace_sec else None)

    if best_estimate_sec:
        col_splits, col_fuel = st.columns(2)

        with col_splits:
            st.subheader("Target Splits")
            st.caption("Negative split strategy: first half +15 sec, second half faster.")
            splits = target_splits(best_estimate_sec)
            df_splits = pd.DataFrame(splits)
            df_splits["Pace"] = df_splits["pace_sec_per_km"].apply(sec_per_km_to_pace_str)
            df_splits["Time"] = df_splits["elapsed_sec"].apply(finish_time_str)
            df_splits = df_splits[["km", "Pace", "Time"]].rename(columns={"km": "km marker"})
            st.dataframe(df_splits, use_container_width=True, hide_index=True)

        with col_fuel:
            st.subheader("Fuelling Schedule")
            st.caption("Gel every 45 min from 45 min in. Stop ~15 min before finish.")
            gels = fuelling_schedule(best_estimate_sec)
            df_gel = pd.DataFrame(gels)
            df_gel = df_gel[["gel", "time_str"]].rename(columns={"gel": "Gel #", "time_str": "Take at"})
            st.dataframe(df_gel, use_container_width=True, hide_index=True)

            cal_marathon = calories_per_km(profile["weight_kg"]) * MARATHON_KM
            st.metric("Estimated calories burned (marathon)", f"{cal_marathon:.0f} kcal")
            st.caption(f"Each gel ≈ 100 kcal → {len(gels)} gels covers ~{len(gels)*100} kcal "
                       f"({len(gels)*100/cal_marathon*100:.0f}% of burn). Fat covers the rest.")

        # Heat adjustment note
        st.divider()
        st.subheader("Heat Adjustment")
        temp_c = st.slider("Expected race-day temperature (°C)", 5, 35, 18)
        if temp_c > 15:
            penalty_pct = ((temp_c - 15) / 5) * 2
            adj_sec = best_estimate_sec * (1 + penalty_pct / 100)
            st.warning(
                f"At {temp_c}°C: expected finish time adds ~{penalty_pct:.0f}% "
                f"→ adjusted estimate **{finish_time_str(adj_sec)}** "
                f"(+{finish_time_str(adj_sec - best_estimate_sec)} vs ideal conditions)"
            )
        else:
            st.success(f"At {temp_c}°C: ideal marathon conditions — no heat penalty.")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 6 — BODY & BENCHMARKS
# ═══════════════════════════════════════════════════════════════════════════════

with tabs[5]:

    col_body, col_bench = st.columns(2)

    # ── Body Metrics ──────────────────────────────────────────────────────────
    with col_body:
        st.subheader("Body Metrics")
        w = profile["weight_kg"]
        h = profile["height_cm"]
        age = profile["age"]

        bmi_val = bmi(w, h)
        racing_w = racing_weight_kg(h)
        deficit_kg = max(w - racing_w, 0)
        time_gain = marathon_time_gain_sec(w, racing_w)
        cal_km = calories_per_km(w)
        sweat_hr = sweat_rate_ml_per_hour(w)
        mhr = max_hr_estimate(age)
        lthr_val = lthr(age)

        c1, c2 = st.columns(2)
        c1.metric("Current weight", f"{w:.1f} kg")
        c2.metric("BMI", f"{bmi_val:.1f}", delta=bmi_category(bmi_val), delta_color="off")
        c1.metric("Racing weight target", f"{racing_w:.1f} kg")
        c2.metric("Weight to lose", f"{deficit_kg:.1f} kg")

        if deficit_kg > 0:
            st.metric(
                "Potential time gain at racing weight",
                finish_time_str(time_gain),
                help=f"~1.25 sec/km per kg → {time_gain/60:.0f} min over 42.2 km",
            )

        st.divider()
        c1, c2 = st.columns(2)
        c1.metric("Calories per km", f"{cal_km:.0f} kcal")
        c2.metric("Sweat rate (est.)", f"{sweat_hr:.0f} ml/hr")
        c1.metric("Max HR (Tanaka)", f"{mhr} bpm")
        c2.metric("LTHR (est.)", f"{lthr_val:.0f} bpm")

        st.divider()
        st.subheader("Per-Run Calorie & Hydration")
        runs_disp = _all_runs()
        if runs_disp:
            df_cal = pd.DataFrame(runs_disp[-10:])
            df_cal["start_date"] = pd.to_datetime(df_cal["start_time"]).dt.date
            df_cal = df_cal.sort_values("start_date", ascending=False)
            df_cal["Calories (kcal)"] = (df_cal["distance_km"] * cal_km).round(0).astype(int)
            df_cal["Hydration (ml)"] = df_cal.apply(
                lambda r: round(hydration_target_ml(w, r["duration_seconds"], r["avg_pace_sec_per_km"])),
                axis=1,
            )
            df_cal = df_cal[["start_date", "distance_km", "Calories (kcal)", "Hydration (ml)"]].rename(
                columns={"start_date": "Date", "distance_km": "km"}
            )
            st.dataframe(df_cal, use_container_width=True, hide_index=True)
        else:
            _no_data()

    # ── Benchmarks ────────────────────────────────────────────────────────────
    with col_bench:
        st.subheader("Benchmarks & Age Grading")

        # VO2max percentile
        if latest_vo2:
            cat, percentile = vo2max_category(latest_vo2, age)
            st.markdown(f"**VO2max: {latest_vo2:.1f} ml/kg/min**")

            fig_gauge = go.Figure(go.Indicator(
                mode="gauge+number",
                value=latest_vo2,
                domain={"x": [0, 1], "y": [0, 1]},
                title={"text": "VO2max vs age group (26yo male)"},
                gauge={
                    "axis": {"range": [25, 70]},
                    "bar": {"color": "rgba(0,180,130,0.85)"},
                    "steps": [
                        {"range": [25, 38], "color": "rgba(239,85,59,0.3)"},
                        {"range": [38, 44], "color": "rgba(255,165,0,0.3)"},
                        {"range": [44, 51], "color": "rgba(255,220,0,0.3)"},
                        {"range": [51, 56], "color": "rgba(0,200,120,0.3)"},
                        {"range": [56, 70], "color": "rgba(0,100,200,0.3)"},
                    ],
                    "threshold": {"line": {"color": "red", "width": 3}, "value": latest_vo2},
                },
            ))
            fig_gauge.update_layout(height=280, margin=dict(t=40, b=10))
            st.plotly_chart(fig_gauge, use_container_width=True)
            st.markdown(
                f"**{cat}** — ~{percentile}th percentile for 26yo males  \n"
                f"Poor <38 · Fair 38–44 · Average 44–51 · Good 51–56 · Excellent 56–62 · Superior 62+"
            )
        else:
            st.info("VO2max gauge will appear after syncing data from a Garmin device that estimates VO2max.")

        st.divider()

        # Age grading & population
        if models:
            best_sec = min(models.values())
            ag_pct, open_equiv = age_grade(best_sec, age)
            label = age_grade_label(ag_pct)

            st.markdown("**Age-Graded Performance (WMA)**")
            c1, c2 = st.columns(2)
            c1.metric("Age-graded score", f"{ag_pct:.1f}%")
            c2.metric("Performance level", label)
            c1.metric("Open-equivalent time", finish_time_str(open_equiv))
            st.caption("WMA age factor for 26yo male: 0.9847 (near peak — males peak ~28-29yo).")

            st.divider()

            st.markdown("**Population Comparison (Male Marathon)**")
            pop_data = {
                "Group": ["Your best estimate", "Avg first-timer (male)", "Avg 26yo recreational", "Avg all male finishers"],
                "Finish time": [
                    finish_time_str(best_sec),
                    "4h 45m",
                    "4h 25m",
                    "4h 22m",
                ],
            }
            st.dataframe(pd.DataFrame(pop_data), use_container_width=True, hide_index=True)

            # Visual bar
            times_h = [best_sec / 3600, 4.75, 4.417, 4.367]
            labels = ["You (projected)", "First-timer avg", "26yo avg", "All-male avg"]
            colors = ["rgba(0,180,130,0.8)" if i == 0 else "rgba(150,150,150,0.5)" for i in range(4)]
            fig_pop = go.Figure(go.Bar(
                x=times_h, y=labels, orientation="h",
                marker_color=colors,
                text=[finish_time_str(t * 3600) for t in times_h],
                textposition="outside",
            ))
            fig_pop.update_layout(xaxis_title="Hours", height=200,
                                  xaxis=dict(range=[3, 6]), margin=dict(t=10, b=30))
            st.plotly_chart(fig_pop, use_container_width=True)
        else:
            st.info("Sync run data to see age-grading and population benchmarks.")
