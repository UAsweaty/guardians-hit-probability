import datetime as dt
import requests
import pandas as pd
import streamlit as st

MLB_API = "https://statsapi.mlb.com/api/v1"
GUARDIANS_TEAM_ID = 114  # Cleveland Guardians

st.set_page_config(page_title="Guardians Hit Probability", page_icon="⚾", layout="wide")
st.title("⚾ Cleveland Guardians — Hit Probability (v3)")
st.caption("Guardians hitters only • Weighted: Last 10 games (most) → vs pitcher → home/away (least).")

# -----------------------------
# Helpers
# -----------------------------
def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

def clamp(x, lo=0.02, hi=0.80):
    return max(lo, min(hi, x))

def extract_first_stat(stats_json):
    """Extract stats[0].splits[0].stat if available."""
    try:
        splits = stats_json["stats"][0]["splits"]
        if not splits:
            return None
        return splits[0]["stat"]
    except Exception:
        return None

def extract_splits(stats_json):
    """Extract stats[0].splits list if available."""
    try:
        return stats_json["stats"][0]["splits"]
    except Exception:
        return []

def ba_from_stat(stat: dict):
    """Return BA, AB, H from a stat dict."""
    if not stat:
        return None, None, None
    ba = safe_float(stat.get("avg"))
    ab = safe_float(stat.get("atBats"))
    h = safe_float(stat.get("hits"))
    return ba, ab, h

def weighted_ba(metrics: list[dict]):
    """
    Combine BA-like metrics into one probability.

    Each metric:
      {
        name, ba, ab, h,
        weight_base,     # importance
        full_weight_ab   # AB needed for full weight
      }

    sample_factor = min(1, ab / full_weight_ab)
    """
    usable = [m for m in metrics if m.get("ba") is not None]
    if not usable:
        return None

    weights = []
    values = []
    for m in usable:
        ab = m.get("ab") or 0
        full_ab = m.get("full_weight_ab", 20) or 20

        if ab and ab > 0:
            sample_factor = min(1.0, ab / full_ab)
        else:
            sample_factor = 0.25  # if AB missing, small contribution

        w = (m.get("weight_base", 0) or 0) * sample_factor
        weights.append(w)
        values.append(m["ba"])

    total_w = sum(weights)
    if total_w <= 0:
        return None

    p = sum(v * w for v, w in zip(values, weights)) / total_w
    return clamp(p)

# -----------------------------
# API Calls (cached)
# -----------------------------
@st.cache_data(ttl=600)
def get_schedule(date_str: str):
    url = f"{MLB_API}/schedule"
    params = {"sportId": 1, "date": date_str, "teamId": GUARDIANS_TEAM_ID}
    return requests.get(url, params=params, timeout=20).json()

@st.cache_data(ttl=600)
def get_game_feed(game_pk: int):
    url = f"{MLB_API}/game/{game_pk}/feed/live"
    return requests.get(url, timeout=20).json()

@st.cache_data(ttl=3600)
def get_team_roster(team_id: int, season: int, roster_type: str = "active"):
    url = f"{MLB_API}/teams/{team_id}/roster"
    params = {"season": season, "rosterType": roster_type}
    return requests.get(url, params=params, timeout=20).json()

@st.cache_data(ttl=3600)
def get_player_stats(person_id: int, season: int, stats_type: str, group: str = "hitting", extra_params: dict | None = None):
    url = f"{MLB_API}/people/{person_id}/stats"
    params = {"stats": stats_type, "group": group, "season": season}
    if extra_params:
        params.update(extra_params)
    return requests.get(url, params=params, timeout=20).json()

@st.cache_data(ttl=3600)
def get_bvp_stats_bulk(batter_ids: list[int], pitcher_id: int, season: int):
    """
    Bulk batter-vs-pitcher using /people with personIds + hydrate=stats(... type=[vsPlayer] opposingPlayerId=...)
    Returns dict: batter_id -> stat dict (or None)
    """
    if not batter_ids or not pitcher_id:
        return {}

    # MLB API accepts comma-separated personIds
    url = f"{MLB_API}/people"
    ids_str = ",".join(str(i) for i in batter_ids)

    hydrate = f"stats(group=[hitting],type=[vsPlayer],opposingPlayerId={pitcher_id},sportId=1,season={season})"
    params = {"personIds": ids_str, "hydrate": hydrate}

    data = requests.get(url, params=params, timeout=30).json()
    out = {}

    for person in data.get("people", []):
        pid = person.get("id")
        stat_dict = None
        try:
            stats = person.get("stats", [])
            if stats and stats[0].get("splits"):
                stat_dict = stats[0]["splits"][0]["stat"]
        except Exception:
            stat_dict = None
        if pid:
            out[int(pid)] = stat_dict
    return out


# -----------------------------
# UI - Date/Game
# -----------------------------
picked_date = st.date_input("Pick a date", value=dt.date.today())
date_str = picked_date.strftime("%Y-%m-%d")
season = picked_date.year

schedule = get_schedule(date_str)
dates = schedule.get("dates", [])

if not dates or not dates[0].get("games"):
    st.warning("No Guardians game found for this date.")
    st.stop()

game = dates[0]["games"][0]
game_pk = game["gamePk"]

feed = get_game_feed(game_pk)

teams = feed.get("gameData", {}).get("teams", {})
home_team = teams.get("home", {})
away_team = teams.get("away", {})

home_name = home_team.get("name", "Home")
away_name = away_team.get("name", "Away")
home_id = home_team.get("id")
away_id = away_team.get("id")

guardians_side = "home" if home_id == GUARDIANS_TEAM_ID else "away"
is_home_game = guardians_side == "home"
opponent_id = away_id if is_home_game else home_id
opponent_name = away_name if is_home_game else home_name
opponent_side = "home" if not is_home_game else "away"

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("gamePk", game_pk)
with c2:
    st.metric("Status", game["status"]["detailedState"])
with c3:
    st.metric("Guardians", "Home" if is_home_game else "Away")
with c4:
    st.metric("Opponent", opponent_name)

st.subheader("Matchup")
st.write(f"**{away_name} @ {home_name}**")

# -----------------------------
# Pitcher selection (probable or manual)
# -----------------------------
st.subheader("Opponent Pitcher")

probables = (feed.get("gameData", {}) or {}).get("probablePitchers", {}) or {}
opp_probable = probables.get(opponent_side)

opp_pitcher_id = None
opp_pitcher_name = None
if isinstance(opp_probable, dict):
    opp_pitcher_id = opp_probable.get("id")
    opp_pitcher_name = opp_probable.get("fullName")

use_probable = False
if opp_pitcher_id and opp_pitcher_name:
    use_probable = st.checkbox(f"Use probable pitcher: {opp_pitcher_name}", value=True)
else:
    st.info("Probable pitcher not available yet. Pick a pitcher manually.")

if not use_probable:
    pitchers = []

    opp_roster_json = get_team_roster(opponent_id, season, roster_type="active")
    opp_roster = opp_roster_json.get("roster", [])
    if len(opp_roster) == 0:
        opp_roster_json = get_team_roster(opponent_id, season, roster_type="fullSeason")
        opp_roster = opp_roster_json.get("roster", [])

    for r in opp_roster:
        person = (r.get("person", {}) or {})
        pid = person.get("id")
        name = person.get("fullName")
        pos = ((r.get("position", {}) or {}).get("abbreviation", "") or "")
        if pid and name and pos == "P":
            pitchers.append((name, pid))

    if not pitchers:
        st.warning("Could not load opponent pitchers right now.")
        opp_pitcher_id = None
        opp_pitcher_name = None
    else:
        chosen = st.selectbox("Select opponent pitcher", [p[0] for p in pitchers])
        opp_pitcher_name = chosen
        opp_pitcher_id = [p[1] for p in pitchers if p[0] == chosen][0]


# -----------------------------
# Guardians hitters only (from roster)
# -----------------------------
st.subheader("Guardians Hitters")

g_roster_json = get_team_roster(GUARDIANS_TEAM_ID, season, roster_type="active")
g_roster = g_roster_json.get("roster", [])
if len(g_roster) == 0:
    g_roster_json = get_team_roster(GUARDIANS_TEAM_ID, season, roster_type="fullSeason")
    g_roster = g_roster_json.get("roster", [])

hitters = []
for r in g_roster:
    person = (r.get("person", {}) or {})
    pid = person.get("id")
    name = person.get("fullName")
    pos = ((r.get("position", {}) or {}).get("abbreviation", "") or "")
    if pid and name and pos != "P":
        hitters.append((name, int(pid)))

if not hitters:
    st.error("Could not load Guardians hitters right now.")
    with st.expander("Debug roster JSON"):
        st.json(g_roster_json)
    st.stop()

# Sort hitters alphabetically for the dropdown
hitters = sorted(hitters, key=lambda x: x[0])
hitter_ids = [h[1] for h in hitters]

# Pre-fetch BvP stats in one call (fast)
bvp_map = {}
if opp_pitcher_id:
    bvp_map = get_bvp_stats_bulk(hitter_ids, opp_pitcher_id, season)

# -----------------------------
# Compute ranking for all hitters
# -----------------------------
rows = []
for name, pid in hitters:
    # Season BA
    season_stats = get_player_stats(pid, season, stats_type="season", group="hitting")
    season_stat = extract_first_stat(season_stats)
    season_ba, season_ab, season_h = ba_from_stat(season_stat)

    # Last 10 games BA (BA-based)
    last10_stats = get_player_stats(pid, season, stats_type="lastXGames", group="hitting", extra_params={"limit": 10})
    last10_stat = extract_first_stat(last10_stats)
    last10_ba, last10_ab, last10_h = ba_from_stat(last10_stat)

    # Home/Away split BA
    ha_stats = get_player_stats(pid, season, stats_type="homeAndAway", group="hitting")
    ha_splits = extract_splits(ha_stats)

    home_stat = None
    away_stat = None
    for s in ha_splits:
        desc = ((s.get("split", {}) or {}).get("description", "") or "")
        if desc == "Home":
            home_stat = s.get("stat")
        elif desc == "Away":
            away_stat = s.get("stat")

    ha_pick = home_stat if is_home_game else away_stat
    ha_ba, ha_ab, ha_h = ba_from_stat(ha_pick)

    # BvP
    bvp_stat = bvp_map.get(pid) if opp_pitcher_id else None
    bvp_ba, bvp_ab, bvp_h = ba_from_stat(bvp_stat)

    # Weights: Last10 (most) -> Pitcher -> Home/Away (least). Season is small stabilizer.
    metrics = [
        {"name": "Season BA", "ba": season_ba, "ab": season_ab, "h": season_h,
         "weight_base": 0.10, "full_weight_ab": 120},

        {"name": "Last 10 Games BA", "ba": last10_ba, "ab": last10_ab, "h": last10_h,
         "weight_base": 0.60, "full_weight_ab": 20},

        {"name": "Home/Away Split BA", "ba": ha_ba, "ab": ha_ab, "h": ha_h,
         "weight_base": 0.15, "full_weight_ab": 60},
    ]

    if opp_pitcher_id:
        metrics.append({"name": "Vs Pitcher BA", "ba": bvp_ba, "ab": bvp_ab, "h": bvp_h,
                        "weight_base": 0.40, "full_weight_ab": 25})

    p_hit = weighted_ba(metrics)

    rows.append({
        "Hitter": name,
        "P(hit)": p_hit,
        "Last10 BA": last10_ba,
        "Last10 AB": last10_ab,
        "VsPitcher BA": bvp_ba,
        "VsPitcher AB": bvp_ab,
        "Home/Away BA": ha_ba,
        "Season BA": season_ba
    })

rank_df = pd.DataFrame(rows)

# Clean + sort
rank_df = rank_df.dropna(subset=["P(hit)"]).sort_values("P(hit)", ascending=False).reset_index(drop=True)

# -----------------------------
# Top 5 output
# -----------------------------
st.subheader("🏆 Top Projected Hitters (Top 5)")
if rank_df.empty:
    st.warning("Not enough data to rank hitters yet.")
else:
    top5 = rank_df.head(5).copy()
    top5["P(hit)"] = top5["P(hit)"].map(lambda x: f"{x:.3f}" if pd.notna(x) else "")
    st.dataframe(top5[["Hitter", "P(hit)", "Last10 BA", "VsPitcher BA", "Home/Away BA", "Season BA"]], use_container_width=True)

    show_more = st.checkbox("Show Top 15 / Full list", value=False)
    if show_more:
        show_n = st.slider("How many hitters to show?", min_value=5, max_value=min(30, len(rank_df)), value=min(15, len(rank_df)))
        show_df = rank_df.head(show_n).copy()
        show_df["P(hit)"] = show_df["P(hit)"].map(lambda x: f"{x:.3f}" if pd.notna(x) else "")
        st.dataframe(show_df[["Hitter", "P(hit)", "Last10 BA", "VsPitcher BA", "Home/Away BA", "Season BA"]], use_container_width=True)

# -----------------------------
# Optional: Select a hitter for details
# -----------------------------
st.subheader("🔎 Hitter Details (optional)")
selected_name = st.selectbox("Select a hitter to view details", [h[0] for h in hitters])
selected_id = [h[1] for h in hitters if h[0] == selected_name][0]

# Pull stats again for the selected hitter (fast due to cache)
season_stats = get_player_stats(selected_id, season, stats_type="season", group="hitting")
season_stat = extract_first_stat(season_stats)
season_ba, season_ab, season_h = ba_from_stat(season_stat)

last10_stats = get_player_stats(selected_id, season, stats_type="lastXGames", group="hitting", extra_params={"limit": 10})
last10_stat = extract_first_stat(last10_stats)
last10_ba, last10_ab, last10_h = ba_from_stat(last10_stat)

ha_stats = get_player_stats(selected_id, season, stats_type="homeAndAway", group="hitting")
ha_splits = extract_splits(ha_stats)

home_stat = None
away_stat = None
for s in ha_splits:
    desc = ((s.get("split", {}) or {}).get("description", "") or "")
    if desc == "Home":
        home_stat = s.get("stat")
    elif desc == "Away":
        away_stat = s.get("stat")

ha_pick = home_stat if is_home_game else away_stat
ha_ba, ha_ab, ha_h = ba_from_stat(ha_pick)

bvp_stat = bvp_map.get(selected_id) if opp_pitcher_id else None
bvp_ba, bvp_ab, bvp_h = ba_from_stat(bvp_stat)

metrics_detail = [
    {"Metric": "Season BA", "BA": season_ba, "AB": season_ab, "Hits": season_h},
    {"Metric": "Last 10 Games BA", "BA": last10_ba, "AB": last10_ab, "Hits": last10_h},
    {"Metric": "Home/Away Split BA", "BA": ha_ba, "AB": ha_ab, "Hits": ha_h},
]
if opp_pitcher_id:
    metrics_detail.append({"Metric": f"Vs Pitcher BA ({opp_pitcher_name})", "BA": bvp_ba, "AB": bvp_ab, "Hits": bvp_h})

st.dataframe(pd.DataFrame(metrics_detail), use_container_width=True)

st.caption("Note: BvP influence is automatically reduced when AB vs pitcher is small.")
