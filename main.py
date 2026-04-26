import asyncio
import pandas as pd
from datetime import datetime
from playwright.async_api import async_playwright
from datetime import timezone
import gspread
from google.oauth2.service_account import Credentials
import json
import os

#DATE = "2026-04-25"  # ← modifie ici
DATE = datetime.now().strftime("%Y-%m-%d")  # auto (désactivé)
LOG_FILE = f"logs_{DATE}.txt"

def log(match_id, text):
    print(f"{match_id} - {text}")

def exclude(home, away, reason, extra=None):
    match_name = f"{home} vs {away}"

    if extra:
        print(f"{match_name} - ❌ EXCLU : {reason} | {extra}")
    else:
        print(f"{match_name} - ❌ EXCLU : {reason}")


standings_cache = {}
history_cache = {}

# =========================
# COMPETITIONS AUTORISÉES
# =========================

ALLOWED = {
    17,18,24,25,20,22,34,182,39,47,40,46,188,675,187,325,
    170,45,172,41,55,36,211,212,215,242,35,44,23,53,8,54,
    38,9,136,37,131,238,239,52,202,155,192,196,402,185,
    152,247,218,178,649,782,254,210,197,410,808,955,358,1032
}

# =========================
# FETCH UTIL (via browser)
# =========================

async def fetch_json(context, url):
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded")
        data = await page.evaluate("() => fetch(location.href).then(r => r.json())")
        return data
    except:
        return None
    finally:
        await page.close()

# =========================
# API CALLS
# =========================

async def get_matches(context):
    url = f"https://www.sofascore.com/api/v1/sport/football/scheduled-events/{DATE}"
    log("API", f"🔎 FETCH MATCHES URL = {url}")
    data = await fetch_json(context, url)
    events = data.get("events", [])
    log("API", f"📦 EVENTS RECEIVED = {len(events)}")
    if len(events) == 0:
        log("API", "⚠️ LISTE VIDE → problème endpoint ou filtre DATE")
    return data.get("events", []) if data else []
    
def translate_status(status_str):
    status = (status_str or "").lower()

    mapping = {
        "not started": "Non démarré",
        "scheduled": "Programmé",
        "1st half": "1ère mi-temps",
        "2nd half": "2ème mi-temps",
        "halftime": "Mi-temps",
        "finished": "Fini",
        "ended": "Fini",
        "postponed": "Reporté",
        "cancelled": "Annulé",
        "abandoned": "Annulé"
    }

    for key in mapping:
        if key in status:
            return mapping[key]

    return status

async def get_standings(context, comp_id, season_id):

    key = f"{comp_id}_{season_id}"
    if key in standings_cache:
        return standings_cache[key]

    url = f"https://www.sofascore.com/api/v1/unique-tournament/{comp_id}/season/{season_id}/standings/total"
    data = await fetch_json(context, url)

    if not data:
        return {}

    groups = data.get("standings", [])

    # 🔥 IDENTIQUE APP SCRIPT
    selected_groups = groups
    
    standings = {}

    for group in selected_groups:
        rows = group.get("rows", [])

        for r in rows:
            team = r.get("team")
            team_id = team.get("id")
            
            if not team or not team_id:
                exclude("STANDINGS", "team invalide ou id manquant")
                continue
            
            if team and team.get("id"):
                standings[team_id] = {
                    "position": r.get("position"),
                    "points": r.get("points"),
                    "group": group.get("name")
                }    
            
            # ✅ LOG ICI (quand tout est OK)
            log(
                "STANDINGS",
                f"✔ {team.get('name')} | "
                f"pos={r.get('position')} | "
                f"pts={r.get('points')} | "
                f"group={group.get('name')} | "
            )

    standings_cache[key] = standings
    return standings


async def get_history(context, team_id, comp_id, season_id):

    key = f"{team_id}_{comp_id}_{season_id}"
    if key in history_cache:
        return history_cache[key]

    url = f"https://www.sofascore.com/api/v1/team/{team_id}/unique-tournament/{comp_id}/events/last/0"
    data = await fetch_json(context, url)

    events = data.get("events", []) if data else []

    # 🔥 FILTRE IMPORTANT : même saison uniquement
    events = [
        e for e in events
        if e.get("season", {}).get("id") == season_id
    ]

    history_cache[key] = events
    return events


async def get_possession(context, match_id, is_home):

    url = f"https://www.sofascore.com/api/v1/event/{match_id}/statistics"
    data = await fetch_json(context, url)

    if not data:
        return None

    for stat in data.get("statistics", []):
        for g in stat.get("groups", []):
            for item in g.get("statisticsItems", []):
                if item.get("name", "").lower() == "ball possession":
                    return item["homeValue"] if is_home else item["awayValue"]

    return None

async def get_season_id(context, comp_id):
    url = f"https://www.sofascore.com/api/v1/unique-tournament/{comp_id}/seasons"
    data = await fetch_json(context, url)

    if not data:
        return None

    seasons = data.get("seasons", [])
    if not seasons:
        return None

    active = next((s for s in seasons if s.get("active")), None)
    return (active or seasons[0]).get("id")
    
    
# =========================
# LOGIQUE IDENTIQUE APP SCRIPT
# =========================

async def process(context):

    matches = await get_matches(context)
    log("SYSTEM", f"🔄 START - {len(matches)} matchs récupérés")
    results = []

    for m in matches:
        
        start_ts = m.get("startTimestamp")

        if not start_ts:
            exclude(m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"), "startTimestamp manquant")
            continue

        match_date = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        log("DEBUG", f"match_date={match_date} | expected={DATE}")

        if match_date != DATE:
            continue
        comp = m.get("tournament", {})
        comp_id = m.get("tournament", {}).get("uniqueTournament", {}).get("id")
        
        competition_name = (comp.get("name") or "").lower()

        if "relegation" in competition_name or "playout" in competition_name or "Qualifying" in competition_name:
            exclude(m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"), f"compétition exclue (relegation/playout: {competition_name})")
            continue

        if "round" in competition_name:
            exclude(m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"), f"compétition round non autorisée ({competition_name})")
            continue

        if comp_id not in ALLOWED:
            exclude(m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"), f"compétition non autorisée (id={comp_id})")
            continue

        status = (m.get("status", {}).get("description") or "").lower()
        if "postponed" in status or "canceled" in status:
            exclude(m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"), f"match annulé ou reporté ({status})")
            continue

        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})

        log("DEBUG", f"{home.get('name')} vs {away.get('name')}")
        home_id = home.get("id")
        away_id = away.get("id")

        season_id = m.get("season", {}).get("id")
        if not season_id:
            season_id = await get_season_id(context, comp_id)

        if not season_id:
            exclude(m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"), "season_id manquant (impossible de récupérer la saison)")
            continue

        standings = await get_standings(context, comp_id, season_id)

        home_rank = standings.get(home_id, {}).get("position")
        away_rank = standings.get(away_id, {}).get("position")

        home_points = standings.get(home_id, {}).get("points")
        away_points = standings.get(away_id, {}).get("points")

        if not home_rank or not away_rank:
            exclude(m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"), "rank manquant (home ou away)")
            continue

        best_rank = min(home_rank, away_rank)

        if best_rank > 8:
            exclude(m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"), f"rang trop faible (best_rank={best_rank} > 8)")
            continue

        if abs(home_rank - away_rank) < 4:
            exclude(
                m.get("homeTeam", {}).get("name", "HOME?"),
                m.get("awayTeam", {}).get("name", "AWAY?"),
                "écart de ranking insuffisant (<4)",
                extra=f"home={home_rank} away={away_rank}"
            )
            continue

        if abs(home_points - away_points) < 7:
            exclude(
                m.get("homeTeam", {}).get("name", "HOME?"),
                m.get("awayTeam", {}).get("name", "AWAY?"),
                "écart de points insuffisant (<7)",
                extra=f"home={home_points} away={away_points}"
            )
            continue

        best_team = home_id if home_rank < away_rank else away_id
        is_best_home = best_team == home_id

        history = await get_history(context, best_team, comp_id, season_id)

        clean = [
            h for h in history
            if h.get("homeTeam")
            and h.get("awayTeam")
            and h.get("winnerCode") in [1, 2, 3]
            and (h.get("status", {}).get("type", "").lower() == "finished")
        ]

        clean.sort(key=lambda x: x["startTimestamp"], reverse=True)

        if len(clean) < 4:
            exclude(
            m.get("homeTeam", {}).get("name", "HOME?"),
            m.get("awayTeam", {}).get("name", "AWAY?"),
            "pas assez de matchs historiques",
            extra=f"clean={len(clean)}"
        )
            continue

        # ❌ last 2 défaites consécutives
        last2 = clean[:2]
        if sum(
            1 for h in last2
            if (h["homeTeam"]["id"] == best_team and h["winnerCode"] == 2) or
               (h["awayTeam"]["id"] == best_team and h["winnerCode"] == 1)
        ) == 2:
            continue

        last20 = clean[:20]

        
# =========================
# LOGIQUE DOMICILE / EXTÉRIEUR (comme App Script)
# =========================

        home_wins = 0
        home_losses = 0
        away_wins = 0
        away_losses = 0

        for h in last20:

            is_home_match = h["homeTeam"]["id"] == best_team

            wc = h.get("winnerCode")
            if wc is None or wc == 3:
                continue

            win = (is_home_match and wc == 1) or (not is_home_match and wc == 2)
            loss = (is_home_match and wc == 2) or (not is_home_match and wc == 1)

            if is_home_match:
                if win:
                    home_wins += 1
                if loss:
                    home_losses += 1
            else:
                if win:
                    away_wins += 1
                if loss:
                    away_losses += 1

# =========================
# FILTRE IDENTIQUE APP SCRIPT
# =========================

        if is_best_home:
            if home_wins <= home_losses:
                exclude(
                    m.get("homeTeam", {}).get("name", "HOME?"),
                    m.get("awayTeam", {}).get("name", "AWAY?"),
                    "forme domicile insuffisante (home wins <= losses)",
                    extra=f"{home_wins}W - {home_losses}L"
                )
                continue
            else:
                if away_wins <= away_losses:
                    exclude(
                        m.get("homeTeam", {}).get("name", "HOME?"),
                        m.get("awayTeam", {}).get("name", "AWAY?"),
                        "forme extérieur insuffisante (away wins <= losses)",
                        extra=f"{away_wins}W - {away_losses}L"
                    )
                    continue

        # last 5 défaites
        last5 = clean[:5]
        if sum(
            1 for h in last5
            if (h["homeTeam"]["id"] == best_team and h["winnerCode"] == 2) or
               (h["awayTeam"]["id"] == best_team and h["winnerCode"] == 1)
        ) > 2:
            exclude(
                m.get("homeTeam", {}).get("name", "HOME?"),
                m.get("awayTeam", {}).get("name", "AWAY?"),
                "trop de défaites sur les 5 derniers matchs"
            )
            continue

        # draws + possession
        draw_matches = []

        for h in last20:
            if h.get("winnerCode") != 3:
                continue

            is_home_match = h["homeTeam"]["id"] == best_team

            # ✅ même logique que App Script (domicile / extérieur du favori)
            if is_best_home and is_home_match:
                draw_matches.append(h)
            elif not is_best_home and not is_home_match:
                draw_matches.append(h)

        high_pos = 0

        for d in draw_matches:
            is_fav_home = d["homeTeam"]["id"] == best_team
            poss = await get_possession(context, d["id"], is_fav_home)

            if poss is None:
                exclude(
                    m.get("homeTeam", {}).get("name", "HOME?"),
                    m.get("awayTeam", {}).get("name", "AWAY?"),
                    "possession manquante (API)",
                    extra=f"match={d.get('id')}"
                )
                continue

            if poss >= 55:
                high_pos += 1

        # ✅ logique identique App Script
        if len(draw_matches) == 0 or high_pos == 0:
            lay_draw = True
        elif high_pos >= 2:
            lay_draw = False
        else:
            lay_draw = True
        
        # 🕒 Date du match
        start_ts = m.get("startTimestamp")

        if start_ts:
            dt = datetime.fromtimestamp(start_ts)
            start_date = dt.strftime("%Y-%m-%d %H:%M")
        else:
            start_date = ""

        status_raw = m.get("status", {}).get("description", "")
        status = translate_status(status_raw)

        results.append({
            "id": m["id"],
            "competition": comp.get("name"),
            "start": start_date,
            "status": status,
            "home": home.get("name"),
            "away": away.get("name"),
            "home_rank": home_rank,
            "away_rank": away_rank,
            "home_points": home_points,
            "away_points": away_points,
            "lay_draw": "Oui" if lay_draw else "Non"
        })
    log("SYSTEM", f"✔ {len(results)} matchs validés")
    return results

# =========================
# MAIN (PLAYWRIGHT + COOKIES AUTO)
# =========================

def send_to_sheets(df):
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])

    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=SCOPES
    )

    client = gspread.authorize(creds)

    # ton fichier
    sheet = client.open_by_url(
        "https://docs.google.com/spreadsheets/d/1a1iQ8Rq6wa3ECm84hVfVpIcoorgsCkiT5up06bk7gyw/edit"
    )

    worksheet = sheet.get_worksheet(0)  # ou .worksheet("nom")

    worksheet.clear()

    worksheet.update([df.columns.values.tolist()] + df.values.tolist())
    
async def main():
    standings_cache.clear()
    history_cache.clear()
    async with async_playwright() as p:

        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context()

        # 🔥 IMPORTANT : initialise cookies automatiquement
        page = await context.new_page()
        await page.goto("https://www.sofascore.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await page.close()

        data = await process(context)
        data.sort(key=lambda x: x["start"] or "")
        df = pd.DataFrame(data)
        df.to_csv("matches.csv", index=False)

        print(f"✔ {len(df)} matchs exportés")
        df = pd.DataFrame(data)
        send_to_sheets(df)

        await browser.close()


asyncio.run(main())
