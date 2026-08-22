import os
import time
import csv
import requests

WEBHOOK = os.getenv('DISCORD_WEBHOOK_URL')

def load_keepers():
    with open('keepers.csv', encoding='utf-8') as f:
        return {(r['club'].lower(), r['keeper'].lower()) for r in csv.DictReader(f)}

def send_discord(msg):
    if WEBHOOK:
        requests.post(WEBHOOK, json={'content': msg}, timeout=10)
    else:
        print(msg)

def get_lineups():
    # FotMob koppeling komt hier.
    return []

def main():
    keepers = load_keepers()
    sent = set()

    while True:
        for x in get_lineups():
            key = (x['club'].lower(), x['keeper'].lower())
            if key in keepers and x['match_id'] not in sent:
                send_discord(
                    f"🚨 KEEPER ALERT\n\n"
                    f"🧤 {x['keeper']} start voor {x['club']}\n"
                    f"⚽ {x['match']}"
                )
                sent.add(x['match_id'])
        time.sleep(300)

if __name__ == '__main__':
    main()
