import datetime as dt
import requests
import pandas as pd
import streamlit as st

MLB_API = "https://statsapi.mlb.com/api/v1"
GUARDIANS_TEAM_ID = 114  # Cleveland Guardians

st.set_page_config(page_title="Guardians Hit Probability", page_icon="⚾", layout="wide")
st.title("⚾ Cleveland Guardians — Hit Probability (v1)")
st.caption("Starter model: uses season batting average (BA) as a rough per-AB hit probability. We'll improve it later.")

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
def get_player_season_stats(person_id: int, season: int):
    url = f"{MLB_API}/people/{person_id}/stats"
    params = {"stats": "season", "group": "hitting", "season": season}
    return requests.get(url, params=params, timeout=20).json()

@st.cache_data(ttl=3600)
def get_team_roster(team_id: int, season: int, roster_type: str = "active"):
    url = f"{MLB_API}/teams/{team_id}/roster"
    params = {"season": season, "rosterType": roster_type}
    return requests.get(url, params=params, timeout=20).json()

def extract_ba(stats_json):
    """Return batting average as float if available."""
    try:
        splits = stats_json["stats"][0]["splits"]
        if not splits:
            return None, None
        stat = splits[0]["stat"]
        ba = stat.get("avg")
        if ba is None:
            return None, stat
        return float(ba), stat
    except Exception:
        return None, None

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

c1, c2, c3 = st.columns(3)
with c1:
    st.metric("gamePk", game_pk)
with c2:
    st.metric("Status", game["status"]["detailedState"])
with c3:
    st.metric("Venue", game.get("venue", {}).get("name", "Unknown"))

feed = get_game_feed(game_pk)

st.subheader("Matchup")
teams = feed.get("gameData", {}).get("teams", {})
home = teams.get("home", {}).get("name", "Home")
away = teams.get("away", {}).get("name", "Away")
st.write(f"**{away} @ {home}**")

# Determine which side is Guardians (home/away) when the feed provides IDs
home_id = teams.get("home", {}).get("id")
away_id = teams.get("away", {}).get("id")
guardians_side = None
if home_id == GUARDIANS_TEAM_ID:
    guardians_side = "home"
elif away_id == GUARDIANS_TEAM_ID:
    guardians_side = "away"

# ---------------- Players from boxscore (game-time) ----------------
boxscore = feed.get("liveData", {}).get("boxscore", {})
batters = []

for side in ["home", "away"]:
    team = boxscore.get("teams", {}).get(side, {})
    players = team.get("players", {})
    for _, p in players.items():
        person = p.get("person", {})
        pid = person.get("id")
        name = person.get("fullName")
        if pid and name:
            batters.append({"side": side, "name": name, "id": pid})

df = pd.DataFrame(batters).drop_duplicates(subset=["id"])

# ---------------- PREGAME FALLBACK: use Guardians roster ----------------
if df.empty:
    st.info("Lineup/boxscore isn't available yet. Using the Guardians roster instead (pregame fallback).")

    # Try ACTIVE roster first (best pregame option)
    roster_json = get_team_roster(GUARDIANS_TEAM_ID, season, roster_type="active")
    roster = roster_json.get("roster", [])

    # If ACTIVE roster is empty, try fullSeason
    if len(roster) == 0:
        roster_json = get_team_roster(GUARDIANS_TEAM_ID, season, roster_type="fullSeason")
        roster = roster_json.get("roster", [])

    st.caption(f"Roster rows returned: {len(roster)}")

    roster_rows = []
    for r in roster:
        person = (r.get("person", {}) or {})
        pid = person.get("id")
        name = person.get("fullName")

        position = (r.get("position", {}) or {})
        pos_abbr = position.get("abbreviation", "")
        pos_name = position.get("name", "")

        if pid and name:
            roster_rows.append({
                "side": "guardians",
                "name": name,
                "id": pid,
                "pos": pos_abbr or pos_name
            })

    df = pd.DataFrame(roster_rows).drop_duplicates(subset=["id"])

    # Filter out pitchers safely (keeps hitters)
    if not df.empty and "pos" in df.columns:
        df = df[~df["pos"].astype(str).str.contains(r"(^P$|Pitcher)", case=False, na=False)]

    if df.empty:
        st.error("Roster fallback failed (no players returned after filtering).")
        with st.expander("Debug roster JSON (helps us fix instantly)"):
            st.json(roster_json)
        st.stop()

# ---------------- Player selection UI ----------------
st.subheader("Pick a player")

show_team = st.selectbox("Which players to show?", ["Guardians only", "Both teams"], index=0)

if show_team == "Guardians only":
    # If we used roster fallback, side is literally "guardians"
    if "guardians" in df["side"].unique():
        df_show = df[df["side"] == "guardians"].copy()
    else:
        # If we have game boxscore data, use home/away side
        if guardians_side is None:
            # Safety fallback if team IDs aren't available for some reason
            df_show = df.copy()
        else:
            df_show = df[df["side"] == guardians_side].copy()
else:
    df_show = df.copy()

df_show = df_show.dropna(subset=["name", "id"])

if df_show.empty:
    st.warning("No players available to show for this selection.")
    st.stop()

player_name = st.selectbox("Select a batter", sorted(df_show["name"].unique()))
player_id = int(df_show[df_show["name"] == player_name]["id"].iloc[0])

# ---------------- Probability ----------------
stats_json = get_player_season_stats(player_id, season)
ba, raw = extract_ba(stats_json)

st.subheader("Estimated Hit Probability (starter)")
