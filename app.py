import datetime as dt
import requests
import pandas as pd
import streamlit as st

MLB_API = "https://statsapi.mlb.com/api/v1"
GUARDIANS_TEAM_ID = 114  # Cleveland Guardians

st.set_page_config(page_title="Guardians Hit Probability", page_icon="⚾", layout="wide")
st.title("⚾ Cleveland Guardians — Hit Probability (v2)")
st.caption("Guardians hitters only • Uses probable pitcher, BvP, home/away split, and last 10 games.")

# ---------------- API helpers ----------------

@st.cache_data(ttl=600)
def get_schedule(date_str: str):
    # schedule endpoint (public)
    url = f"{MLB_API}/schedule"
    params = {"sportId": 1, "date": date_str, "teamId": GUARDIANS_TEAM_ID}
    return requests.get(url, params=params, timeout=20).json()

@st.cache_data(ttl=600)
def get_game_feed(game_pk: int):
    # live feed endpoint (public)
    url = f"{MLB_API}/game/{game_pk}/feed/live"
    return requests.get(url, timeout=20).json()

@st.cache_data(ttl=3600)
def get_team_roster(team_id: int, season: int, roster_type: str = "active"):
    # rosterType values exist (active/fullSeason/etc.)
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
def get_bvp_stats(batter_id: int, pitcher_id: int, season: int):
    """
    Batter vs Pitcher using people endpoint + stats hydration:
      hydrate=stats(group=[hitting],type=[vsPlayer],opposingPlayerId={pitcherId},sportId=1,season=YYYY)

    This approach is commonly used for BvP in Stats API usage. [4](https://www.reddit.com/r/mlbdata/comments/133806h/retrieving_a_hitters_current_batting_average/)
    """
    url = f"{MLB_API}/people"
    hydrate = f"stats(group=[hitting],type=[vsPlayer],opposingPlayerId={pitcher_id},sportId=1,season={season})"
    params = {"personIds": batter_id, "hydrate": hydrate}
    return requests.get(url, params=params, timeout=20).json()

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

def extract_first_stat(stats_json):
    """Extract first split stat dict if available."""
    try:
        splits = stats_json["stats"][0]["splits"]
        if not splits:
            return None
        return splits[0]["stat"]
    except Exception:
        return None

def extract_split_stat(stats_json):
    """Extract list of splits if available (for homeAndAway, etc.)."""
    try:
        return stats_json["stats"][0]["splits"]
    except Exception:
        return []

def ba_from_stat(stat: dict):
    """Return BA + AB + H if present."""
    if not stat:
        return None, None, None
    ba = safe_float(stat.get("avg"))
    ab = safe_float(stat.get("atBats"))
    h = safe_float(stat.get("hits"))
    return ba, ab, h

def clamp(x, lo=0.02, hi=0.80):
    return max(lo, min(hi, x))

def weighted_ba(metrics: list[dict]):
    """
    Combine BA-like metrics into one probability.
    Each metric dict:
      {name, ba, ab, weight_base}
    We down-weight small samples using min(1, ab/20).
    """
    usable = [m for m in metrics if m.get("ba") is not None]
    if not usable:
        return None, pd.DataFrame()

    weights = []
    values = []
    for m in usable:
        ab = m.get("ab") or 0
        sample_factor = min(1.0, ab / 20.0) if ab else 0.25  # if AB missing, small but nonzero
        w = m["weight_base"] * sample_factor
        weights.append(w)
        values.append(m["ba"])

    total_w = sum(weights)
    if total_w <= 0:
        return None, pd.DataFrame()

    p = sum(v * w for v, w in zip(values, weights)) / total_w
    breakdown = pd.DataFrame([{
        "Metric": m["name"],
        "BA": m.get("ba"),
        "AB": m.get("ab"),
        "Hits": m.get("h"),
        "Base Weight": m.get("weight_base"),
    } for m in usable])

    return clamp(p), breakdown

# ---------------- UI ----------------

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

# ---------------- Pitcher selection ----------------

probables = feed.get("gameData", {}).get("probablePitchers", {})  # appears in gameData [5](https://statsapi.mlb.com/api/v1.1/game/1/feed/live)
opp_probable = probables.get("home" if not is_home_game else "away")  # opponent side
opp_pitcher_id = opp_probable.get("id") if isinstance(opp_probable, dict) else None
opp_pitcher_name = opp_probable.get("fullName") if isinstance(opp_probable, dict) else None

st.subheader("Opponent Pitcher")
use_probable = False
if opp_pitcher_id and opp_pitcher_name:
    use_probable = st.checkbox(f"Use probable pitcher: {opp_pitcher_name}", value=True)
else:
    st.info("Probable pitcher not available yet. Pick a pitcher manually.")

if not use_probable:
    # pull opponent roster and let user pick a pitcher
    opp_roster_json = get_team_roster(opponent_id, season, roster_type="active")
    opp_roster = opp_roster_json.get("roster", [])
    pitchers = []
    for r in opp_roster:
        person = r.get("person", {}) or {}
        pid = person.get("id")
        name = person.get("fullName")
        pos = (r.get("position", {}) or {}).get("abbreviation", "")
        if pid and name and pos == "P":
            pitchers.append((name, pid))

    if not pitchers:
        # try fullSeason if active empty
        opp_roster_json = get_team_roster(opponent_id, season, roster_type="fullSeason")
        opp_roster = opp_roster_json.get("roster", [])
        for r in opp_roster:
            person = r.get("person", {}) or {}
            pid = person.get("id")
            name = person.get("fullName")
            pos = (r.get("position", {}) or {}).get("abbreviation", "")
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
else:
    opp_pitcher_id = opp_pitcher_id
    opp_pitcher_name = opp_pitcher_name

# ---------------- Guardians hitters only ----------------

st.subheader("Guardians Hitter")
roster_json = get_team_roster(GUARDIANS_TEAM_ID, season, roster_type="active")
roster = roster_json.get("roster", [])

# If active roster empty, try fullSeason
if len(roster) == 0:
    roster_json = get_team_roster(GUARDIANS_TEAM_ID, season, roster_type="fullSeason")
    roster = roster_json.get("roster", [])

hitters = []
for r in roster:
    person = r.get("person", {}) or {}
    pid = person.get("id")
    name = person.get("fullName")
    pos = (r.get("position", {}) or {}).get("abbreviation", "")
    if pid and name and pos != "P":
        hitters.append((name, pid))

if not hitters:
    st.error("Could not load Guardians hitters right now.")
    with st.expander("Debug roster JSON"):
        st.json(roster_json)
    st.stop()

batter_name = st.selectbox("Select Guardians hitter", sorted([h[0] for h in hitters]))
batter_id = [h[1] for h in hitters if h[0] == batter_name][0]

# ---------------- Metrics ----------------

# Season BA (baseline)
season_stats = get_player_stats(batter_id, season, stats_type="season", group="hitting")
season_stat = extract_first_stat(season_stats)
season_ba, season_ab, season_h = ba_from_stat(season_stat)

# Last 10 games BA (best-effort)
# statTypes supports lastXGames [3](https://statsapi.mlb.com/api/v1/statTypes)[6](https://statsapi.mlb.com/api/v1/stats?stats=lastXGames&group=hitting&teamId=117)
last10_stats = get_player_stats(
    batter_id,
    season,
    stats_type="lastXGames",
    group="hitting",
    extra_params={"limit": 10}  # if ignored by API, splits may be empty; we handle gracefully
)
last10_stat = extract_first_stat(last10_stats)
last10_ba, last10_ab, last10_h = ba_from_stat(last10_stat)

# Home/Away split BA (choose based on Guardians home/away)
# statTypes supports homeAndAway [3](https://statsapi.mlb.com/api/v1/statTypes)
ha_stats = get_player_stats(batter_id, season, stats_type="homeAndAway", group="hitting")
ha_splits = extract_split_stat(ha_stats)

home_stat = None
away_stat = None
for s in ha_splits:
    desc = (s.get("split", {}) or {}).get("description", "")
    if desc == "Home":
        home_stat = s.get("stat")
    if desc == "Away":
        away_stat = s.get("stat")

ha_pick = home_stat if is_home_game else away_stat
ha_ba, ha_ab, ha_h = ba_from_stat(ha_pick)

# Batter vs Pitcher (if pitcher chosen)
bvp_ba = bvp_ab = bvp_h = None
bvp_raw = None
if opp_pitcher_id:
    bvp_json = get_bvp_stats(batter_id, opp_pitcher_id, season)
    # Parse people[0].stats[0].splits[0].stat
    try:
        person = bvp_json.get("people", [])[0]
        stats = person.get("stats", [])
        if stats and stats[0].get("splits"):
            bvp_raw = stats[0]["splits"][0]["stat"]
            bvp_ba, bvp_ab, bvp_h = ba_from_stat(bvp_raw)
    except Exception:
        pass

# ---------------- Probability ----------------

metrics = [
    {"name": "Season BA", "ba": season_ba, "ab": season_ab, "h": season_h, "weight_base": 0.45},
    {"name": "Last 10 Games BA", "ba": last10_ba, "ab": last10_ab, "h": last10_h, "weight_base": 0.25},
    {"name": "Home/Away Split BA", "ba": ha_ba, "ab": ha_ab, "h": ha_h, "weight_base": 0.20},
]

if opp_pitcher_id:
    metrics.append({"name": f"Vs Pitcher BA ({opp_pitcher_name})", "ba": bvp_ba, "ab": bvp_ab, "h": bvp_h, "weight_base": 0.25})

p_hit, breakdown_df = weighted_ba(metrics)

st.subheader("Hit Probability (v2)")
if p_hit is None:
    st.error("Could not compute probability (missing stats).")
else:
    st.metric("Estimated P(hit)", f"{p_hit:.3f}")

st.caption("Weights downshift automatically for small AB samples. BvP has lower influence when AB is small.")

with st.expander("Breakdown (what went into the estimate)"):
    if not breakdown_df.empty:
        st.dataframe(breakdown_df, use_container_width=True)
    else:
        st.write("No breakdown available.")

with st.expander("Debug details"):
    st.write(f"Opponent pitcher: {opp_pitcher_name} (ID: {opp_pitcher_id})")
    st.write(f"Guardians are {'HOME' if is_home_game else 'AWAY'}")
    if bvp_raw:
        st.write("BvP raw stat:")
        st.json(bvp_raw)
