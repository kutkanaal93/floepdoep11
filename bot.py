
import os
import csv
import time
import requests
from datetime import datetime, timezone

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
INTERVAL = 300

sent = set()

def norm(x):
    return "".join(c.lower() for c in str(x) if c.isalnum())

def load_keepers():
    with open("keepers.csv", encoding="utf-8") as f:
        return {
            (norm(r["club"]), norm(r["keeper"]))
            for r in csv.DictReader(f)
        }

def discord(msg):
    if WEBHOOK:
        requests.post(WEBHOOK, json={"content": msg}, timeout=10)
    else:
        print(msg)

def get_matches():
    today = datetime.now().strftime("%Y%m%d")
    url = "https://www.fotmob.com/api/matches"
    r = requests.get(url, params={"date": today}, timeout=15)
    r.raise_for_status()

    data = r.json()
    matches = []

    for league in data.get("leagues", []):
        for match in league.get("matches", []):
            matches.append(match)

    return matches

def get_lineup(match_id):
    url = "https://www.fotmob.com/api/matchDetails"
    r = requests.get(url, params={"matchId": match_id}, timeout=15)
    r.raise_for_status()

    data = r.json()

    lineup = data.get("content", {}).get("lineup", {})

    for team in lineup.get("lineup", []):
        for player in team.get("players", []):
            pos = player.get("position")
            if pos in ("G", "GK", "Goalkeeper"):
                return {
                    "club": team.get("teamName", ""),
                    "keeper": player.get("name", "")
                }

    return None

def main():
    keepers = load_keepers()
    print("Keeper alert bot gestart")

    while True:
        try:
            for match in get_matches():
                status = match.get("status", {})
                if status.get("started"):
                    continue

                utc = status.get("utcTime")
                if not utc:
                    continue

                kickoff = datetime.fromisoformat(
                    utc.replace("Z", "+00:00")
                )

                minutes = (kickoff - datetime.now(timezone.utc)).total_seconds() / 60

                if minutes > 60 or minutes < -5:
                    continue

                detail = get_lineup(match["id"])
                if not detail:
                    continue

                key = (
                    norm(detail["club"]),
                    norm(detail["keeper"])
                )

                if key in keepers and match["id"] not in sent:
                    home = match.get("home", {}).get("name")
                    away = match.get("away", {}).get("name")

                    discord(
                        "🚨 KEEPER ALERT\n\n"
                        f"🧤 {detail['keeper']} staat BASIS voor {detail['club']}\n"
                        f"⚽ {home} - {away}\n"
                        f"⏰ Aftrap: {utc}"
                    )

                    sent.add(match["id"])

        except Exception as e:
            print("Fout:", e)

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
