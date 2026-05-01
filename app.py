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
    # Schedule endpoint example documented publicly by MLB Stats API community docs. [1](https://openpublicapis.com/api/mlb-records-and-stats)
    url = f"{MLB_API}/schedule"
    params = {"sportId": 1, "date": date_str, "teamId": GUARDIANS_TEAM_ID}
    return requests.get(url, params=params, timeout=20).json()

@st.cache_data(ttl=600)
def get_game_feed(game_pk: int):
    # Live feed endpoint is a standard Stats API endpoint for play-by-play and game data. [2](https://github.com/toddrob99/MLB-StatsAPI/wiki/Endpoints)[1](https://openpublicapis.com/api/mlb-records-and-stats)
    url = f"{MLB_API}/game/{game_pk}/feed/live"
    return requests.get(url, timeout=20).json()

@st.cache_data(ttl=3600)
def get_player_season_stats(person_id: int, season: int):
    url = f"{MLB_API}/people/{person_id}/stats"
    params = {"stats": "season", "group": "hitting", "season": season}
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

# --- UI ---
today = st.date_input("Pick a date", value=dt.date.today())
date_str = today.strftime("%Y-%m-%d")

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

# Try to list batters from boxscore (often available close to game time)
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

if df.empty:
