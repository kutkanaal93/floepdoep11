
import os
import csv
import time
import requests
from datetime import datetime, timezone

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
CHECK_INTERVAL = 300

sent_alerts = set()


def norm(value):
    return "".join(c.lower() for c in str(value) if c.isalnum())


def load_keepers():
    keepers = set()

    with open("keepers.csv", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        print("CSV headers:", reader.fieldnames)

        for row in reader:
            club = (
                row.get("club")
                or row.get("Club")
                or row.get("team")
                or row.get("Team")
            )

            keeper = (
                row.get("keeper")
                or row.get("Keeper")
                or row.get("wisselkeeper")
                or row.get("Wisselkeeper")
            )

            if not club or not keeper:
                continue

            keepers.add((norm(club), norm(keeper)))

    print(f"CSV geladen: {len(keepers)} keepers")
    return keepers


def send_discord(message):
    if not WEBHOOK:
        print(message)
        return

    requests.post(
        WEBHOOK,
        json={"content": message},
        timeout=10
    )


def get_matches():
    # FotMob connector komt hierna
    return []


def main():
    keepers = load_keepers()
    print("Keeper alert bot gestart")

    while True:
        try:
            for match in get_matches():
                pass

        except Exception as e:
            print("Fout:", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
