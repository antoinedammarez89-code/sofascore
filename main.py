import requests
import time
import random
from datetime import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials

# ========================
# CONFIG
# ========================

BASE_URL = "https://www.sofascore.com/api/v1"
TIMEZONE = "Europe/Paris"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8"
}

session = requests.Session()

standings_cache = {}
history_cache = {}

# ========================
# GOOGLE SHEETS
# ========================

def connect_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    client = gspread.authorize(creds)

    return client.open_by_key("1a1iQ8Rq6wa3ECm84hVfVpIcoorgsCkiT5up06bk7gyw")

# ========================
# UTILS
# ========================

def sleep():
    time.sleep(0.8 + random.random() * 0.4)

def fetch(url, headers=None, retry=2):
    sleep()
    try:
        res = session.get(url, headers=headers or HEADERS)

        if res.status_code != 200:
            if retry > 0:
                time.sleep(0.5)
                return fetch(url, headers, retry - 1)
            print(f"❌ {res.status_code} {url}")
            return None

        return res.json()
    except Exception as e:
        print("Erreur:", e)
        return None

# ========================
# COMPETITIONS
# ========================

def get_checked_competitions(sheet):
    ws = sheet.worksheet("Export Competition")
    data = ws.get_all_values()[1:]

    checked = {}

    for row in data:
        try:
            comp_id = int(row[2])
            is_checked = row[4].lower() == "true"

            if is_checked:
                checked[comp_id] = True
        except:
            continue

    return checked

# ========================
# STANDINGS
# ========================

def get_standings(comp_id, season_id):
    key = f"{comp_id}_{season_id}"
    if key in standings_cache:
        return standings_cache[key]

    urls = [
        f"{BASE_URL}/unique-tournament/{comp_id}/season/{season_id}/standings/total",
        f"{BASE_URL}/unique-tournament/{comp_id}/season/{season_id}/standings"
    ]

    for url in urls:
        data = fetch(url)
        if not data or "standings" not in data:
            continue

        standings = {}
        groups = data["standings"]
        selected = [groups[0]] if len(groups) > 1 else groups

        for group in selected:
            for row in group.get("rows", []):
                team = row.get("team")
                if team:
                    standings[team["id"]] = {
                        "position": row["position"],
                        "points": row["points"]
                    }

        standings_cache[key] = standings
        return standings

    return {}

# ========================
# HISTORY
# ========================

def get_team_history(team_id, tournament_id, season_id):
    key = f"{team_id}_{tournament_id}"

    if key in history_cache:
        return history_cache[key]

    url = f"{BASE_URL}/team/{team_id}/unique-tournament/{tournament_id}/events/last/0"
    data = fetch(url)

    if not data:
        history_cache[key] = []
        return []

    events = [
        e for e in data.get("events", [])
        if e.get("season", {}).get("id") == season_id
    ]

    history_cache[key] = events
    return events

# ========================
# ANALYSE MATCH
# ========================

def analyze_match(event, checked):
    status = (event.get("status", {}).get("description", "")).lower()

    if "postponed" in status or "canceled" in status:
        return None

    comp_id = event["tournament"]["uniqueTournament"]["id"]

    if not checked.get(comp_id):
        return None

    season_id = event["season"]["id"]

    standings = get_standings(comp_id, season_id)

    home = event["homeTeam"]
    away = event["awayTeam"]

    home_id = home["id"]
    away_id = away["id"]

    home_rank = standings.get(home_id, {}).get("position")
    away_rank = standings.get(away_id, {}).get("position")

    if not home_rank or not away_rank:
        return None

    best_rank = min(home_rank, away_rank)

    if best_rank > 8:
        return None

    if abs(home_rank - away_rank) < 4:
        return None

    home_pts = standings.get(home_id, {}).get("points", 0)
    away_pts = standings.get(away_id, {}).get("points", 0)

    if abs(home_pts - away_pts) < 7:
        return None

    best_team = home_id if home_rank < away_rank else away_id

    history = get_team_history(best_team, comp_id, season_id)

    if len(history) < 4:
        return None

    sorted_matches = sorted(history, key=lambda x: x["startTimestamp"], reverse=True)

    # LAST 2
    losses_last2 = 0
    for m in sorted_matches[:2]:
        is_home = m["homeTeam"]["id"] == best_team
        if (is_home and m["winnerCode"] == 2) or (not is_home and m["winnerCode"] == 1):
            losses_last2 += 1

    if losses_last2 == 2:
        return None

    # LAST 5
    losses5 = 0
    for m in sorted_matches[:5]:
        is_home = m["homeTeam"]["id"] == best_team
        if (is_home and m["winnerCode"] == 2) or (not is_home and m["winnerCode"] == 1):
            losses5 += 1

    if losses5 > 2:
        return None

    # LAST 20
    wins = 0
    losses = 0

    for m in sorted_matches[:20]:
        is_home = m["homeTeam"]["id"] == best_team

        if (is_home and m["winnerCode"] == 1) or (not is_home and m["winnerCode"] == 2):
            wins += 1
        elif (is_home and m["winnerCode"] == 2) or (not is_home and m["winnerCode"] == 1):
            losses += 1

    if losses > wins:
        return None

    return {
        "competition": event["tournament"]["name"],
        "home": home["name"],
        "away": away["name"],
        "layDraw": "Oui"
    }

# ========================
# WRITE SHEET
# ========================

def write_sheet(sheet, matches):
    ws = sheet.worksheet("MATCHS")

    ws.clear()

    headers = ["Compétition", "Domicile", "Extérieur", "Lay Draw"]
    ws.append_row(headers)

    for m in matches:
        ws.append_row([
            m["competition"],
            m["home"],
            m["away"],
            m["layDraw"]
        ])

# ========================
# MAIN
# ========================

def main():
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).strftime("%Y-%m-%d")

    sheet = connect_sheet()
    checked = get_checked_competitions(sheet)

    data = fetch(f"{BASE_URL}/sport/football/scheduled-events/{today}")

    matches = []

    for event in data.get("events", []):
        res = analyze_match(event, checked)
        if res:
            matches.append(res)

    write_sheet(sheet, matches)

if __name__ == "__main__":
    main()