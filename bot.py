import os
import csv
import time

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
CHECK_INTERVAL = 300


def norm(value):
    return "".join(c.lower() for c in str(value) if c.isalnum())


def load_keepers():
    keepers = set()

    with open("keepers.csv", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        print("CSV headers:", reader.fieldnames, flush=True)

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

    print(f"CSV geladen: {len(keepers)} keepers", flush=True)
    return keepers


def main():
    print("BOT START TEST", flush=True)

    keepers = load_keepers()

    print("Keeper alert bot gestart", flush=True)

    while True:
        # FotMob koppeling komt hierna
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
