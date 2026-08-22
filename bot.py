
import os
import csv
import time
import requests
from datetime import datetime, timezone

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")

CHECK_INTERVAL = 300
START_WINDOW_MINUTES = 60

sent_alerts = set()


def load_keepers():
    with open("keepers.csv", encoding="utf-8") as f:
        return {
            (
                row["club"].strip().lower(),
                row["keeper"].strip().lower()
            )
            for row in csv.DictReader(f)
        }


def discord(message):
    if not WEBHOOK:
        print(message)
        return

    requests.post(
        WEBHOOK,
        json={"content": message},
        timeout=10
    )


def fotmob_matches():
    """
    Placeholder for FotMob fixtures endpoint.
    Returns:
    [
      {
        "id": "...",
        "club": "...",
        "opponent": "...",
        "kickoff": "...",
        "status": "upcoming"
      }
    ]
    """
    return []


def fotmob_lineup(match_id):
    """
    Placeholder for FotMob lineup endpoint.

    Expected:
    {
      "keeper": "Nick Olij",
      "club": "PSV"
    }
    """
    return None


def minutes_until(kickoff):
    # Placeholder. Will use FotMob timestamp when connector is added.
    return 999


def check():
    keepers = load_keepers()

    for match in fotmob_matches():

        if minutes_until(match["kickoff"]) > START_WINDOW_MINUTES:
            continue

        lineup = fotmob_lineup(match["id"])

        if not lineup:
            continue

        key = (
            lineup["club"].lower(),
            lineup["keeper"].lower()
        )

        if key in keepers and match["id"] not in sent_alerts:
            discord(
                "🚨 KEEPER ALERT\n\n"
                f"🧤 {lineup['keeper']} start voor {lineup['club']}\n"
                f"⚽ {match['opponent']}\n"
                f"⏰ Aftrap: {match['kickoff']}"
            )

            sent_alerts.add(match["id"])


def main():
    print("Keeper alert bot gestart")

    while True:
        try:
            check()
        except Exception as e:
            print("Fout:", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
