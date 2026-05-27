from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

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
from training_plan import (
    MARATHON_KM,
    PLAN_START,
    RACE_DATE,
    current_week,
    get_workout,
    plan_week_dates,
)

CONFIG_PATH = Path.home() / ".fitnessapp" / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"goal_pace": "6:00"}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ── Init ────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Marathon Dashboard",
    page_icon="🏃",
    layout="wide",
)

init_db()
cfg = load_config()

# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Marathon Tracker")
    st.caption("Hal Higdon Novice 1")
    st.divider()

    goal_pace_input = st.text_input(
        "Goal marathon pace (mm:ss/km)",
        value=cfg.get("goal_pace", "6:00"),
        help="e.g. 6:00  →  finish ~4h13m",
    )

    goal_pace_sec: Optional[float] = None
    goal_finish_str = "–"
    try:
        goal_pace_sec = pace_str_to_sec_per_km(goal_pace_input)
        goal_finish_sec = goal_pace_sec * MARATHON_KM
        goal_finish_str = finish_time_str(goal_finish_sec)
        st.metric("Goal finish time", goal_finish_str)
        if goal_pace_input != cfg.get("goal_pace"):
            cfg["goal_pace"] = goal_pace_input
            save_config(cfg)
    except Exception:
        st.warning("Enter pace as M:SS (e.g. 6:00)")

    st.divider()

    days_to_race = (RACE_DATE - date.today()).days
    st.metric("Race date", RACE_DATE.strftime("%b %d, %Y"))
    st.metric("Days to race", days_to_race)
    cw = current_week()
    st.metric("Plan week", f"{cw} / 18")

    today_workout = get_workout(date.today())
    if today_workout:
        if today_workout.is_rest:
            st.info("Today: Rest day")
        elif today_workout.is_cross:
            st.info("Today: Cross-training")
        elif today_workout.is_race:
            st.success(f"Today: {today_workout.label}")
        else:
            st.success(f"Today: {today_workout.label}")

    st.divider()

    sync_days = st.slider("Days to sync", min_value=30, max_value=180, value=120, step=30)
    if st.button("Sync Garmin Data", use_container_width=True):
        with st.spinner("Connecting to Garmin..."):
            try:
                from garmin_sync import sync
                count = sync(days=sync_days)
                st.success(f"Synced {count} run activities")
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Sync failed: {exc}")

    from garmin_sync import last_sync_time
    last = last_sync_time()
    if last:
        last_dt = datetime.fromisoformat(last)
        st.caption(f"Last sync: {last_dt.strftime('%b %d, %H:%M')}")
    else:
        st.caption("Not yet synced — run `python setup_auth.py` first")

# ── Header ───────────────────────────────────────────────────────────────────
st.title("Marathon Training Dashboard")
st.caption(
    f"Hal Higdon Novice 1  ·  Race: {RACE_DATE.strftime('%B %d, %Y')}"
    f"  ·  Week {cw} of 18  ·  {days_to_race} days to go"
)

tab1, tab2, tab3, tab4 = st.tabs(
    ["Weekly Mileage", "Pace Trend", "Heart Rate", "Finish Time Projection"]
)

# ── Tab 1: Weekly Mileage ────────────────────────────────────────────────────
with tab1:
    st.subheader("Weekly Volume: Actual vs Planned")

    weekly = weekly_summary()
    df_w = pd.DataFrame(weekly)

    fig = go.Figure()

    # Planned (background bars)
    fig.add_trace(
        go.Bar(
            x=df_w["week"],
            y=df_w["planned_km"],
            name="Planned",
            marker_color="rgba(99,110,250,0.25)",
            marker_line_color="rgba(99,110,250,0.7)",
            marker_line_width=1.5,
        )
    )

    # Actual (filled bars for past + current weeks)
    actual_df = df_w[df_w["actual_km"].notna()].copy()
    # Colour: green if within 10% of plan, amber if under, grey if over (taper)
    def bar_color(row):
        if row["actual_km"] >= row["planned_km"] * 0.9:
            return "rgba(0,200,120,0.75)"
        if row["actual_km"] >= row["planned_km"] * 0.6:
            return "rgba(255,165,0,0.75)"
        return "rgba(239,85,59,0.75)"

    actual_df["color"] = actual_df.apply(bar_color, axis=1)

    fig.add_trace(
        go.Bar(
            x=actual_df["week"],
            y=actual_df["actual_km"],
            name="Actual",
            marker_color=actual_df["color"].tolist(),
            customdata=actual_df[["planned_km"]],
            hovertemplate="Week %{x}<br>Actual: %{y:.1f} km<br>Planned: %{customdata[0]:.1f} km<extra></extra>",
        )
    )

    fig.add_vline(
        x=cw,
        line_dash="dash",
        line_color="rgba(255,200,0,0.8)",
        annotation_text=f"← week {cw}",
        annotation_position="top right",
        annotation_font_color="orange",
    )

    fig.update_layout(
        barmode="overlay",
        xaxis=dict(title="Week", dtick=1),
        yaxis_title="km",
        legend=dict(orientation="h", y=1.08),
        height=400,
        margin=dict(t=40, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)

    completed = actual_df[~actual_df["is_current"]]
    if not completed.empty:
        on_track = int((completed["actual_km"] >= completed["planned_km"] * 0.9).sum())
        total_actual = completed["actual_km"].sum()
        total_planned = completed["planned_km"].sum()
        c1, c2, c3 = st.columns(3)
        c1.metric("Weeks on track (≥90%)", f"{on_track} / {len(completed)}")
        c2.metric("Total km run", f"{total_actual:.0f} km")
        c3.metric("vs planned", f"{total_planned:.0f} km", delta=f"{total_actual - total_planned:+.0f} km")

# ── Tab 2: Pace Trend ────────────────────────────────────────────────────────
with tab2:
    st.subheader("Training Pace Trend")

    runs = get_all_runs()

    if not runs:
        st.info(
            "No running activities found.  \n"
            "Click **Sync Garmin Data** in the sidebar after running `python setup_auth.py`."
        )
    else:
        df_r = pd.DataFrame(runs)
        df_r["start_date"] = pd.to_datetime(df_r["start_time"]).dt.date
        df_r = df_r[df_r["avg_pace_sec_per_km"].notna()].sort_values("start_date")
        df_r["pace_min_km"] = df_r["avg_pace_sec_per_km"] / 60

        fig2 = go.Figure()

        # Scatter: size encodes distance
        sizes = (df_r["distance_km"].clip(upper=35) * 0.55 + 4).tolist()
        fig2.add_trace(
            go.Scatter(
                x=df_r["start_date"],
                y=df_r["pace_min_km"],
                mode="markers+lines",
                name="Run pace",
                marker=dict(size=sizes, color="rgba(0,180,130,0.8)"),
                line=dict(color="rgba(0,180,130,0.35)", width=1),
                customdata=df_r[["distance_km", "avg_hr"]].values,
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    "Pace: %{y:.2f} min/km<br>"
                    "Distance: %{customdata[0]:.1f} km<br>"
                    "Avg HR: %{customdata[1]:.0f} bpm<extra></extra>"
                ),
            )
        )

        # 7-day rolling average pace
        df_r_indexed = df_r.set_index("start_date")
        rolling = df_r_indexed["pace_min_km"].rolling(7, min_periods=1).mean()
        fig2.add_trace(
            go.Scatter(
                x=rolling.index,
                y=rolling.values,
                mode="lines",
                name="7-day trend",
                line=dict(color="rgba(0,180,130,0.9)", width=2.5, dash="dot"),
            )
        )

        if goal_pace_sec:
            easy_pace_min = (goal_pace_sec + 75) / 60
            fig2.add_hline(
                y=goal_pace_sec / 60,
                line_dash="dash",
                line_color="rgba(220,50,50,0.8)",
                annotation_text=f"Goal: {goal_pace_input}/km",
                annotation_position="bottom right",
                annotation_font_color="rgba(220,50,50,0.9)",
            )
            fig2.add_hline(
                y=easy_pace_min,
                line_dash="dot",
                line_color="rgba(255,165,0,0.6)",
                annotation_text=f"Easy: {sec_per_km_to_pace_str(goal_pace_sec + 75)}",
                annotation_position="top right",
                annotation_font_color="rgba(255,165,0,0.9)",
            )

        fig2.update_layout(
            xaxis_title="Date",
            yaxis=dict(title="min/km", autorange="reversed"),
            height=420,
            legend=dict(orientation="h", y=1.08),
            margin=dict(t=40, b=40),
        )
        st.plotly_chart(fig2, use_container_width=True)
        st.caption(
            "Bubble size = run distance.  "
            "Red dashed = goal marathon pace.  "
            "Orange dotted = target easy/long-run pace (goal + 75 sec)."
        )

        # Recent runs table
        with st.expander("Recent runs"):
            display = df_r.tail(15)[["start_date", "distance_km", "pace_min_km", "avg_hr"]].copy()
            display.columns = ["Date", "Distance (km)", "Pace (min/km)", "Avg HR"]
            display["Pace (min/km)"] = display["Pace (min/km)"].apply(lambda x: f"{x:.2f}")
            st.dataframe(display.iloc[::-1].reset_index(drop=True), use_container_width=True)

# ── Tab 3: Heart Rate ────────────────────────────────────────────────────────
with tab3:
    st.subheader("Heart Rate Trends")

    col_rhr, col_run_hr = st.columns(2)

    # Resting HR
    with col_rhr:
        st.markdown("**Resting Heart Rate**")
        with get_conn() as conn:
            hr_rows = conn.execute(
                """
                SELECT date, resting_hr FROM daily_hr
                WHERE date >= ?
                ORDER BY date
                """,
                ((date.today() - timedelta(days=120)).isoformat(),),
            ).fetchall()

        if hr_rows:
            hr_df = pd.DataFrame([dict(r) for r in hr_rows])
            hr_df["date"] = pd.to_datetime(hr_df["date"])
            hr_df["rolling"] = hr_df["resting_hr"].rolling(7, min_periods=1).mean()

            fig3 = go.Figure()
            fig3.add_trace(
                go.Scatter(
                    x=hr_df["date"],
                    y=hr_df["resting_hr"],
                    mode="markers",
                    name="Daily RHR",
                    marker=dict(color="rgba(239,85,59,0.55)", size=5),
                )
            )
            fig3.add_trace(
                go.Scatter(
                    x=hr_df["date"],
                    y=hr_df["rolling"],
                    mode="lines",
                    name="7-day avg",
                    line=dict(color="rgba(200,30,30,0.85)", width=2.5),
                    fill="tozeroy",
                    fillcolor="rgba(239,85,59,0.07)",
                )
            )
            fig3.update_layout(
                yaxis_title="bpm",
                height=300,
                legend=dict(orientation="h"),
                margin=dict(t=10, b=30),
            )
            st.plotly_chart(fig3, use_container_width=True)

            latest = int(hr_df["resting_hr"].iloc[-1])
            avg = hr_df["resting_hr"].mean()
            st.metric("Latest RHR", f"{latest} bpm", delta=f"{latest - avg:+.0f} vs 120-day avg", delta_color="inverse")
        else:
            st.info("No resting HR data synced yet.")

    # Per-run avg HR
    with col_run_hr:
        st.markdown("**Average HR per Run**")
        runs_hr = [r for r in (get_all_runs() or []) if r.get("avg_hr")]
        if runs_hr:
            df_rh = pd.DataFrame(runs_hr)
            df_rh["start_date"] = pd.to_datetime(df_rh["start_time"]).dt.date
            df_rh = df_rh.sort_values("start_date")

            fig4 = go.Figure()
            fig4.add_trace(
                go.Scatter(
                    x=df_rh["start_date"],
                    y=df_rh["avg_hr"],
                    mode="markers+lines",
                    name="Avg HR",
                    marker=dict(color="rgba(239,85,59,0.7)", size=7),
                    line=dict(color="rgba(239,85,59,0.3)", width=1),
                    customdata=df_rh[["distance_km"]].values,
                    hovertemplate="<b>%{x}</b><br>Avg HR: %{y} bpm<br>Distance: %{customdata[0]:.1f} km<extra></extra>",
                )
            )
            fig4.update_layout(
                yaxis_title="bpm",
                height=300,
                legend=dict(orientation="h"),
                margin=dict(t=10, b=30),
            )
            st.plotly_chart(fig4, use_container_width=True)

            latest_run_hr = int(df_rh["avg_hr"].iloc[-1])
            st.metric("Latest run avg HR", f"{latest_run_hr} bpm")
        else:
            st.info("No per-run HR data available yet.")

# ── Tab 4: Finish Time Projection ────────────────────────────────────────────
with tab4:
    st.subheader("Projected Marathon Finish Time")

    history = projection_history()
    proj = current_projection(goal_pace_sec)

    if not history and not proj:
        st.info("Sync at least one run of 5+ km to see projections.")
    else:
        chart_col, stats_col = st.columns([3, 1])

        with chart_col:
            if history:
                df_proj = pd.DataFrame(history)
                df_proj["hours"] = df_proj["projected_seconds"] / 3600

                fig5 = go.Figure()
                fig5.add_trace(
                    go.Scatter(
                        x=df_proj["week"],
                        y=df_proj["hours"],
                        mode="lines+markers",
                        name="Riegel projection",
                        line=dict(color="rgba(0,180,200,0.85)", width=2.5),
                        marker=dict(size=8, color="rgba(0,180,200,0.85)"),
                        hovertemplate="Week %{x}<br>Projected: %{y:.2f} h<extra></extra>",
                    )
                )

                if goal_pace_sec:
                    goal_h = goal_pace_sec * MARATHON_KM / 3600
                    fig5.add_hline(
                        y=goal_h,
                        line_dash="dash",
                        line_color="rgba(220,50,50,0.7)",
                        annotation_text=f"Goal: {goal_finish_str}",
                        annotation_position="bottom right",
                        annotation_font_color="rgba(220,50,50,0.9)",
                    )

                # Future projection — extend line to race week (18) if we have data
                if proj and proj.get("riegel_seconds"):
                    future_h = proj["riegel_seconds"] / 3600
                    last_week = df_proj["week"].max()
                    fig5.add_trace(
                        go.Scatter(
                            x=[last_week, 18],
                            y=[df_proj["hours"].iloc[-1], future_h],
                            mode="lines",
                            name="Forecast",
                            line=dict(color="rgba(0,180,200,0.4)", width=2, dash="dot"),
                            showlegend=True,
                        )
                    )

                fig5.update_layout(
                    xaxis=dict(title="Plan week", dtick=1, range=[1, 18]),
                    yaxis_title="Projected finish (hours)",
                    height=380,
                    legend=dict(orientation="h", y=1.08),
                    margin=dict(t=40, b=40),
                )
                st.plotly_chart(fig5, use_container_width=True)
                st.caption(
                    "Riegel formula: T₂ = T₁ × (D₂/D₁)^1.06 — "
                    "projection updates each week as your longest run grows."
                )

        with stats_col:
            if proj:
                st.markdown("**Current estimates**")
                riegel_str = finish_time_str(proj["riegel_seconds"])
                st.metric("Riegel (longest run)", riegel_str)

                if proj.get("pace_based_seconds"):
                    st.metric("Avg pace (last 5 runs)", finish_time_str(proj["pace_based_seconds"]))

                if goal_pace_sec:
                    st.metric("Goal", goal_finish_str)

                    diff = proj["riegel_seconds"] - goal_pace_sec * MARATHON_KM
                    sign = "+" if diff > 0 else ""
                    st.caption(f"Riegel vs goal: {sign}{finish_time_str(abs(diff))}")

                if proj.get("based_on_run_km") and proj.get("based_on_run_pace"):
                    km = proj["based_on_run_km"]
                    pace = proj["based_on_run_pace"]
                    st.caption(
                        f"Based on {km:.1f} km run at "
                        f"{sec_per_km_to_pace_str(pace)}"
                    )
