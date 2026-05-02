import datetime as dt
import math
import requests
import pandas as pd
import streamlit as st

MLB_API = "https://statsapi.mlb.com/api/v1"
GUARDIANS_TEAM_ID = 114  # Cleveland Guardians

st.set_page_config(page_title="Guardians Hit Probability", page_icon="⚾", layout="wide")
st.title("⚾ Cleveland Guardians — Hit Probability")
st.caption("Guardians hitters only • Weighted: Last 10 games (most) → vs pitcher → home/away (least).")

# -----------------------------
# Refresh button (clears cache + reruns)
# -----------------------------
top_left, top_right = st.columns([1, 3])
with top_left:
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()
with top_right:
    st.caption(f"Last refreshed: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# -----------------------------
# Sidebar controls
# -----------------------------
st.sidebar.header("Game settings")
expected_ab = st.sidebar.slider("Expected at-bats (AB) per hitter", min_value=2, max_value=6, value=4, step=1)
st.sidebar.caption("Used to convert per-AB probability into game-level probabilities (1+ hits, 2+ hits).")

# -----------------------------
# Team logo helpers (SVG via HTML <img>)
# MLB team logo URL pattern:
# https://www.mlbstatic.com/team-logos/team-cap-on-dark/{teamId}.svg [5](https://openpublicapis.com/api/mlb-records-and-stats)
# -----------------------------
def team_logo_url(team_id: int) -> str:
    return f"https://www.mlbstatic.com/team-logos/team-cap-on-dark/{team_id}.svg"

def logo_img_html(team_id: int, size: int = 70) -> str:
    url = team_logo_url(team_id)
    return f"""
    <div style="display:flex;align-items:center;justify-content:center;">
        <img src="{url}" width="{size}" />
    </div>
    """

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
    Combine BA-like metrics into one per-AB probability (p).
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
            sample_factor = 0.25

        w = (m.get("weight_base", 0) or 0) * sample_factor
        weights.append(w)
        values.append(m["ba"])

    total_w = sum(weights)
    if total_w <= 0:
        return None

    p = sum(v * w for v, w in zip(values, weights)) / total_w
    return clamp(p)

def build_breakdown(metrics: list[dict]) -> pd.DataFrame:
    rows = []
    for m in metrics:
        if m.get("ba") is None:
            continue
        rows.append({
            "Metric": m.get("name"),
            "BA": m.get("ba"),
            "AB": m.get("ab"),
            "Hits": m.get("h"),
            "Base Weight": m.get("weight_base"),
            "FullWeightAB": m.get("full_weight_ab")
        })
    return pd.DataFrame(rows)

def prob_1plus_hits(p_per_ab: float, n_ab: int) -> float:
    # P(X >= 1) = 1 - (1-p)^n
    return 1.0 - (1.0 - p_per_ab) ** n_ab

def prob_2plus_hits(p_per_ab: float, n_ab: int) -> float:
    # P(X >= 2) = 1 - [P(0) + P(1)]
    p0 = (1.0 - p_per_ab) ** n_ab
    p1 = n_ab * p_per_ab * (1.0 - p_per_ab) ** (n_ab - 1)
    return max(0.0, 1.0 - (p0 + p1))

# -----------------------------
# API Calls (cached)
# -----------------------------
@st.cache_data(ttl=600)
def get_schedule(date_str: str):
    """
    Use schedule hydration to get probable pitchers earlier:
    hydrate=team,probablePitcher(note)
    This can return teams.home/away.probablePitcher objects. [3](https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-04-26&hydrate=probablePitcher%28note%29,team,linescore)[4](https://github.com/pseudo-r/Public-MLB-API/blob/main/docs/schedule.md)
    """
    url = f"{MLB_API}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "teamId": GUARDIANS_TEAM_ID,
        "hydrate": "team,probablePitcher(note)"
    }
    return requests.get(url, params=params, timeout=20).json()

@st.cache_data(ttl=600)
def get_game_feed(game_pk: int):
    # Live feed endpoint (public) [1](https://github.com/toddrob99/MLB-StatsAPI/wiki/Endpoints)
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
    params = {"stats": stats_type, "group": group, "season": season, "sportId": 1}
    if extra_params:
        params.update(extra_params)
    return requests.get(url, params=params, timeout=20).json()

@st.cache_data(ttl=3600)
def get_bvp_stats_bulk(batter_ids: list[int], pitcher_id: int, season: int):
    if not batter_ids or not pitcher_id:
        return {}

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
# Robust home/away BA extraction (fallback to season BA)
# -----------------------------
def get_home_away_ba(player_id: int, season: int, is_home_game: bool, season_fallback_ba: float | None):
    ha_stats = get_player_stats(player_id, season, stats_type="homeAndAway", group="hitting")
    splits = extract_splits(ha_stats)

    home_stat = None
    away_stat = None

    for s in splits:
        split_obj = s.get("split")
        if isinstance(split_obj, dict):
            desc = (split_obj.get("description") or "").strip().lower()
            code = (split_obj.get("code") or "").strip().lower()
            tag = desc or code
        elif isinstance(split_obj, str):
            tag = split_obj.strip().lower()
        else:
            tag = ""

        if tag == "home":
            home_stat = s.get("stat")
        elif tag == "away":
            away_stat = s.get("stat")

    pick = home_stat if is_home_game else away_stat
    ha_ba, ha_ab, ha_h = ba_from_stat(pick)

    if ha_ba is not None:
        return ha_ba, ha_ab, ha_h

    return season_fallback_ba, None, None

# -----------------------------
# NEW: Expected starter logic (Schedule first, then Live Feed, then manual)
# -----------------------------
def get_opponent_starter_from_schedule(schedule_game: dict, is_home_game: bool):
    """
    From schedule endpoint hydration, probablePitcher may appear at:
      game['teams']['home']['probablePitcher'] and ['away']['probablePitcher'] [3](https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-04-26&hydrate=probablePitcher%28note%29,team,linescore)[4](https://github.com/pseudo-r/Public-MLB-API/blob/main/docs/schedule.md)
    Opponent is away when Guardians are home; opponent is home when Guardians are away.
    """
    teams = schedule_game.get("teams", {})
    home_obj = teams.get("home", {}) or {}
    away_obj = teams.get("away", {}) or {}

    opponent_obj = away_obj if is_home_game else home_obj
    pp = opponent_obj.get("probablePitcher") or {}

    pid = pp.get("id")
    name = pp.get("fullName")
    note = opponent_obj.get("pitcherNote")  # sometimes present with probablePitcher(note)
    return pid, name, note

def get_opponent_starter_from_live_feed(feed: dict, is_home_game: bool):
    """
    Live feed may include gameData.probablePitchers, but it can be blank pregame. [2](https://www.pinterest.com/pin/625226360789608600/)[1](https://github.com/toddrob99/MLB-StatsAPI/wiki/Endpoints)
    Opponent is away when Guardians are home; opponent is home when Guardians are away.
    """
    probables = (feed.get("gameData", {}) or {}).get("probablePitchers", {}) or {}
    opponent_side = "away" if is_home_game else "home"
    pp = probables.get(opponent_side)

    if isinstance(pp, dict):
        return pp.get("id"), pp.get("fullName")
    return None, None

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

sched_home = game.get("teams", {}).get("home", {}).get("team", {})
sched_away = game.get("teams", {}).get("away", {}).get("team", {})
home_name = sched_home.get("name", "Home")
away_name = sched_away.get("name", "Away")
home_id = sched_home.get("id")
away_id = sched_away.get("id")

guardians_side = "home" if home_id == GUARDIANS_TEAM_ID else "away"
is_home_game = guardians_side == "home"
opponent_id = away_id if is_home_game else home_id
opponent_name = away_name if is_home_game else home_name

matchup_text = f"Cleveland Guardians vs {opponent_name}" if is_home_game else f"Cleveland Guardians @ {opponent_name}"

# Matchup banner with logos
lcol, mcol, rcol = st.columns([1, 6, 1], vertical_alignment="center")
with lcol:
    st.markdown(logo_img_html(GUARDIANS_TEAM_ID, 70), unsafe_allow_html=True)
with mcol:
    st.markdown(f"### 🆚 {matchup_text}")
with rcol:
    if opponent_id:
        st.markdown(logo_img_html(int(opponent_id), 70), unsafe_allow_html=True)
    else:
        st.write("")

# Live feed for extra info (still useful)
feed = get_game_feed(game_pk)

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    st.metric("gamePk", game_pk)
with c2:
    st.metric("Status", game["status"]["detailedState"])
with c3:
    st.metric("Guardians", "Home" if is_home_game else "Away")
with c4:
    st.metric("Opponent", opponent_name)
with c5:
    st.metric("Venue", game.get("venue", {}).get("name", "Unknown"))

st.subheader("Matchup")
st.write(f"**{away_name} @ {home_name}**")

# -----------------------------
# Pitcher selection (AUTO expected starter first)
# -----------------------------
st.subheader("Opponent Pitcher")

# 1) Best: schedule hydration probablePitcher(note) (pregame reliable) [3](https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-04-26&hydrate=probablePitcher%28note%29,team,linescore)[4](https://github.com/pseudo-r/Public-MLB-API/blob/main/docs/schedule.md)
sched_pid, sched_name, sched_note = get_opponent_starter_from_schedule(game, is_home_game)

opp_pitcher_id = None
opp_pitcher_name = None
starter_source = None

if sched_pid and sched_name:
    opp_pitcher_id = sched_pid
    opp_pitcher_name = sched_name
    starter_source = "Schedule (expected starter)"
else:
    # 2) fallback: live feed probablePitchers (sometimes blank pregame) [2](https://www.pinterest.com/pin/625226360789608600/)[1](https://github.com/toddrob99/MLB-StatsAPI/wiki/Endpoints)
    live_pid, live_name = get_opponent_starter_from_live_feed(feed, is_home_game)
    if live_pid and live_name:
        opp_pitcher_id = live_pid
        opp_pitcher_name = live_name
        starter_source = "Live Feed (probablePitchers)"

if opp_pitcher_id and opp_pitcher_name:
    st.success(f"Expected starter: **{opp_pitcher_name}**  \nSource: {starter_source}")
    if sched_note:
        st.caption(f"Pitcher note: {sched_note}")
    use_auto = st.checkbox("Use this expected starter for matchup stats (recommended)", value=True)
else:
    use_auto = False
    st.info("Expected starter not available from MLB yet. You can select a pitcher manually below.")

# If user unchecks, fall back to manual selection
if (not opp_pitcher_id) or (not use_auto):
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
            pitchers.append((name, int(pid)))

    if not pitchers:
        st.warning("Could not load opponent pitchers right now. (BvP will be skipped.)")
        opp_pitcher_id = None
        opp_pitcher_name = None
    else:
        chosen = st.selectbox("Select opponent pitcher", [p[0] for p in pitchers])
        opp_pitcher_name = chosen
        opp_pitcher_id = [p[1] for p in pitchers if p[0] == chosen][0]

# -----------------------------
# Guardians hitters (roster)
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

hitters = sorted(hitters, key=lambda x: x[0])
hitter_ids = [h[1] for h in hitters]

# Bulk BvP
bvp_map = {}
if opp_pitcher_id:
    bvp_map = get_bvp_stats_bulk(hitter_ids, opp_pitcher_id, season)

# -----------------------------
# Rank all hitters
# -----------------------------
st.subheader("🏆 Top Projected Hitters (Top 5)")
st.caption("Numbers shown are game-level probabilities based on the selected Expected AB in the sidebar.")

with st.spinner("Calculating hitter probabilities..."):
    rows = []
    for name, pid in hitters:
        # Season BA
        season_stats = get_player_stats(pid, season, stats_type="season", group="hitting")
        season_stat = extract_first_stat(season_stats)
        season_ba, season_ab, season_h = ba_from_stat(season_stat)

        # Last 10 games BA
        last10_stats = get_player_stats(pid, season, stats_type="lastXGames", group="hitting", extra_params={"limit": 10})
        last10_stat = extract_first_stat(last10_stats)
        last10_ba, last10_ab, last10_h = ba_from_stat(last10_stat)

        # Home/Away BA (fallback to season BA if split missing)
        ha_ba, ha_ab, ha_h = get_home_away_ba(pid, season, is_home_game, season_ba)

        # BvP
        bvp_stat = bvp_map.get(pid) if opp_pitcher_id else None
        bvp_ba, bvp_ab, bvp_h = ba_from_stat(bvp_stat)

        metrics = [
            {"name": "Season BA", "ba": season_ba, "ab": season_ab, "h": season_h,
             "weight_base": 0.10, "full_weight_ab": 120},
            {"name": "Last 10 Games BA", "ba": last10_ba, "ab": last10_ab, "h": last10_h,
             "weight_base": 0.60, "full_weight_ab": 20},
            {"name": f"Home/Away BA ({'Home' if is_home_game else 'Away'})", "ba": ha_ba, "ab": ha_ab, "h": ha_h,
             "weight_base": 0.15, "full_weight_ab": 60},
        ]
        if opp_pitcher_id:
            metrics.append({
                "name": f"Vs Pitcher BA ({opp_pitcher_name})",
                "ba": bvp_ba, "ab": bvp_ab, "h": bvp_h,
                "weight_base": 0.40, "full_weight_ab": 25
            })

        p_per_ab = weighted_ba(metrics)
        if p_per_ab is None:
            continue

        p1 = prob_1plus_hits(p_per_ab, expected_ab)
        p2 = prob_2plus_hits(p_per_ab, expected_ab)

        rows.append({
            "Hitter": name,
            "P(≥1 hit)": p1,
            "P(≥2 hits)": p2,
            "Last10 BA": last10_ba,
            "VsPitcher BA": bvp_ba,
            "Home/Away BA": ha_ba,
            "Season BA": season_ba
        })

rank_df = pd.DataFrame(rows)
rank_df = rank_df.dropna(subset=["P(≥1 hit)"]).sort_values("P(≥1 hit)", ascending=False).reset_index(drop=True)

if rank_df.empty:
    st.warning("Not enough data to rank hitters yet.")
else:
    top5 = rank_df.head(5).copy()
    top5["P(≥1 hit)"] = top5["P(≥1 hit)"].map(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "")
    top5["P(≥2 hits)"] = top5["P(≥2 hits)"].map(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "")

    st.dataframe(
        top5[["Hitter", "P(≥1 hit)", "P(≥2 hits)", "Last10 BA", "VsPitcher BA", "Home/Away BA", "Season BA"]],
        use_container_width=True
    )

    show_more = st.checkbox("Show Top 15 / more hitters", value=False)
    if show_more:
        show_n = st.slider("How many hitters to show?", 5, min(30, len(rank_df)), min(15, len(rank_df)))
        show_df = rank_df.head(show_n).copy()
        show_df["P(≥1 hit)"] = show_df["P(≥1 hit)"].map(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "")
        show_df["P(≥2 hits)"] = show_df["P(≥2 hits)"].map(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "")
        st.dataframe(
            show_df[["Hitter", "P(≥1 hit)", "P(≥2 hits)", "Last10 BA", "VsPitcher BA", "Home/Away BA", "Season BA"]],
            use_container_width=True
        )

st.caption("Tip: Click 🔄 Refresh data after probables/updates to recalculate with the newest MLB API data.")

# -----------------------------
# Optional: Single hitter details + breakdown
# -----------------------------
st.subheader("🔎 Hitter Details (optional)")
selected_name = st.selectbox("Select a hitter to view the breakdown", [h[0] for h in hitters])
selected_id = [h[1] for h in hitters if h[0] == selected_name][0]

season_stats = get_player_stats(selected_id, season, stats_type="season", group="hitting")
season_stat = extract_first_stat(season_stats)
season_ba, season_ab, season_h = ba_from_stat(season_stat)

last10_stats = get_player_stats(selected_id, season, stats_type="lastXGames", group="hitting", extra_params={"limit": 10})
last10_stat = extract_first_stat(last10_stats)
last10_ba, last10_ab, last10_h = ba_from_stat(last10_stat)

ha_ba, ha_ab, ha_h = get_home_away_ba(selected_id, season, is_home_game, season_ba)

bvp_stat = bvp_map.get(selected_id) if opp_pitcher_id else None
bvp_ba, bvp_ab, bvp_h = ba_from_stat(bvp_stat)

metrics_detail = [
    {"name": "Season BA", "ba": season_ba, "ab": season_ab, "h": season_h,
     "weight_base": 0.10, "full_weight_ab": 120},
    {"name": "Last 10 Games BA", "ba": last10_ba, "ab": last10_ab, "h": last10_h,
     "weight_base": 0.60, "full_weight_ab": 20},
    {"name": f"Home/Away BA ({'Home' if is_home_game else 'Away'})", "ba": ha_ba, "ab": ha_ab, "h": ha_h,
     "weight_base": 0.15, "full_weight_ab": 60},
]
if opp_pitcher_id:
    metrics_detail.append({
        "name": f"Vs Pitcher BA ({opp_pitcher_name})",
        "ba": bvp_ba, "ab": bvp_ab, "h": bvp_h,
        "weight_base": 0.40, "full_weight_ab": 25
    })

p_per_ab = weighted_ba(metrics_detail)
if p_per_ab is not None:
    p1 = prob_1plus_hits(p_per_ab, expected_ab)
    p2 = prob_2plus_hits(p_per_ab, expected_ab)

    d1, d2 = st.columns(2)
    with d1:
        st.metric("P(≥1 hit) in game", f"{p1*100:.1f}%")
    with d2:
        st.metric("P(≥2 hits) in game", f"{p2*100:.1f}%")

    st.caption(f"Based on Expected AB = {expected_ab} and the per-AB estimate from the model.")

st.dataframe(build_breakdown(metrics_detail), use_container_width=True)

with st.expander("Debug details"):
    st.write(f"Opponent pitcher: {opp_pitcher_name} (ID: {opp_pitcher_id})")
    st.write(f"Guardians are {'HOME' if is_home_game else 'AWAY'}")
