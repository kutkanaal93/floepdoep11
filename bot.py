import csv
import difflib
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SORARE_URL = "https://api.sorare.com/graphql"
SORARE_API_KEY = os.getenv("SORARE_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", "65"))
GRACE_MINUTES = int(os.getenv("GRACE_MINUTES", "10"))
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Amsterdam")
RUN_ONCE = os.getenv("RUN_ONCE", "0").lower() in {"1", "true", "yes"}

BASE_DIR = Path(__file__).resolve().parent
KEEPERS_FILE = BASE_DIR / "keepers.csv"
SLUG_CACHE_FILE = BASE_DIR / "slug_cache.json"
SENT_ALERTS_FILE = BASE_DIR / "sent_alerts.json"

session = requests.Session()
sent_alerts = set()


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = (
        value.replace("ø", "o")
        .replace("đ", "d")
        .replace("ð", "d")
        .replace("þ", "th")
    )
    return re.sub(r"[^a-z0-9]+", "", value)


def split_keeper_names(value):
    """Support old uncertain cells such as 'A / B' without breaking normal names."""
    value = str(value or "").strip()
    if not value:
        return []
    parts = [p.strip() for p in re.split(r"\s+/\s+|\s+\|\s+", value) if p.strip()]
    return parts or [value]


def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Kan {path.name} niet lezen: {exc}", flush=True)
    return default


def save_json(path, data):
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"Kan {path.name} niet opslaan: {exc}", flush=True)


def graphql(query, variables=None, attempts=3):
    if not SORARE_API_KEY:
        raise RuntimeError("SORARE_API_KEY ontbreekt in Render Environment.")

    headers = {
        "Content-Type": "application/json",
        "APIKEY": SORARE_API_KEY,
        "User-Agent": "SorareBackupKeeperAlert/1.0",
    }

    for attempt in range(1, attempts + 1):
        response = session.post(
            SORARE_URL,
            headers=headers,
            json={"query": query, "variables": variables or {}},
            timeout=25,
        )

        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "5"))
            print(f"Sorare rate limit; {wait}s wachten.", flush=True)
            time.sleep(wait)
            continue

        response.raise_for_status()
        payload = response.json()

        if payload.get("errors"):
            raise RuntimeError(
                "Sorare GraphQL fout: "
                + " | ".join(str(e.get("message", e)) for e in payload["errors"])
            )

        return payload.get("data") or {}

    raise RuntimeError("Sorare API bleef rate-limiten.")


SEARCH_PLAYER_QUERY = """
query SearchTrackedKeeper($query: String!) {
  searchPlayers(query: $query, page: 0, pageSize: 10) {
    hits {
      player {
        slug
        displayName
        activeClub {
          slug
          name
        }
      }
    }
  }
}
"""

TRACKED_PLAYERS_QUERY = """
query TrackedKeepers($slugs: [String!]) {
  players(slugs: $slugs) {
    slug
    displayName
    activeClub {
      slug
      name
    }
    nextGame {
      id
      date
      homeTeam {
        slug
        name
      }
      awayTeam {
        slug
        name
      }
    }
  }
}
"""

OFFICIAL_LINEUP_QUERY = """
query OfficialLineup($gameId: ID!) {
  football {
    game(id: $gameId) {
      id
      date
      homeTeam {
        slug
        name
      }
      awayTeam {
        slug
        name
      }
      homeFormation {
        startingLineupAvailable
        startingLineup {
          slug
          displayName
        }
      }
      awayFormation {
        startingLineupAvailable
        startingLineup {
          slug
          displayName
        }
      }
    }
  }
}
"""


def resolve_slug(keeper_name, configured_club):
    data = graphql(SEARCH_PLAYER_QUERY, {"query": keeper_name})
    hits = (data.get("searchPlayers") or {}).get("hits") or []

    candidates = []
    for hit in hits:
        player = (hit or {}).get("player") or {}
        slug = player.get("slug")
        name = player.get("displayName") or ""
        club = player.get("activeClub") or {}
        club_name = club.get("name") or ""
        if not slug:
            continue

        name_score = difflib.SequenceMatcher(None, norm(keeper_name), norm(name)).ratio()
        club_score = difflib.SequenceMatcher(None, norm(configured_club), norm(club_name)).ratio()
        exact_name = norm(keeper_name) == norm(name)

        # Exact player name matters most; current club breaks ties.
        score = (2.0 if exact_name else name_score) + (0.6 * club_score)
        candidates.append((score, slug, name, club_name))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    best = candidates[0]

    if best[0] < 1.2:
        print(
            f"Geen betrouwbare Sorare-match: {keeper_name} ({configured_club}). "
            f"Beste: {best[2]} / {best[3]}",
            flush=True,
        )
        return None

    print(
        f"Sorare-match: {keeper_name} -> {best[2]} "
        f"[{best[1]}] ({best[3] or 'geen club'})",
        flush=True,
    )
    return best[1]


def load_keepers():
    cache = load_json(SLUG_CACHE_FILE, {})
    tracked = []

    with KEEPERS_FILE.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        print("CSV headers:", reader.fieldnames, flush=True)

        for row in reader:
            club = (row.get("club") or row.get("Club") or "").strip()
            keeper_cell = (
                row.get("keeper")
                or row.get("Keeper")
                or row.get("wisselkeeper")
                or row.get("Wisselkeeper")
                or ""
            ).strip()
            configured_slug = (row.get("slug") or row.get("Slug") or "").strip()

            if not club or not keeper_cell:
                continue

            names = split_keeper_names(keeper_cell)

            for index, keeper_name in enumerate(names):
                # A manually supplied slug applies only to a single-name row.
                slug = configured_slug if len(names) == 1 else ""
                cache_key = f"{club}|{keeper_name}"

                if not slug:
                    slug = cache.get(cache_key, "")

                if not slug:
                    try:
                        slug = resolve_slug(keeper_name, club) or ""
                    except Exception as exc:
                        print(f"Slug zoeken mislukt voor {keeper_name}: {exc}", flush=True)

                if slug:
                    cache[cache_key] = slug
                    tracked.append({
                        "club": club,
                        "keeper": keeper_name,
                        "slug": slug,
                    })
                else:
                    print(f"NIET GEVONDEN: {club} | {keeper_name}", flush=True)

    save_json(SLUG_CACHE_FILE, cache)
    print(f"Tracking actief voor {len(tracked)} keeper(s).", flush=True)
    return tracked


def batched(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def get_upcoming_games(tracked):
    by_slug = {row["slug"]: row for row in tracked}
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=GRACE_MINUTES)
    window_end = now + timedelta(minutes=LOOKAHEAD_MINUTES)

    games = {}

    for chunk in batched(list(by_slug), 40):
        data = graphql(TRACKED_PLAYERS_QUERY, {"slugs": chunk})
        players = data.get("players") or []

        for player in players:
            if not player:
                continue

            slug = player.get("slug")
            tracked_row = by_slug.get(slug)
            game = player.get("nextGame")

            if not tracked_row or not game:
                continue

            kickoff = parse_iso(game.get("date"))
            if not kickoff or not (window_start <= kickoff <= window_end):
                continue

            game_id = game.get("id")
            if not game_id:
                continue

            games.setdefault(game_id, {
                "game": game,
                "tracked": [],
            })
            games[game_id]["tracked"].append(tracked_row)

    return games


def flatten_lineup(lines):
    result = set()
    for line in lines or []:
        for player in line or []:
            slug = (player or {}).get("slug")
            if slug:
                result.add(slug)
    return result


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("GEEN DISCORD_WEBHOOK_URL. Alert alleen in log:", flush=True)
        print(message, flush=True)
        return

    response = session.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message},
        timeout=15,
    )
    response.raise_for_status()


def alert_for_game(game_id, tracked_rows):
    data = graphql(OFFICIAL_LINEUP_QUERY, {"gameId": game_id})
    game = ((data.get("football") or {}).get("game") or {})

    home = game.get("homeTeam") or {}
    away = game.get("awayTeam") or {}
    home_form = game.get("homeFormation") or {}
    away_form = game.get("awayFormation") or {}

    home_ready = bool(home_form.get("startingLineupAvailable"))
    away_ready = bool(away_form.get("startingLineupAvailable"))

    if not home_ready and not away_ready:
        return 0

    home_starters = flatten_lineup(home_form.get("startingLineup")) if home_ready else set()
    away_starters = flatten_lineup(away_form.get("startingLineup")) if away_ready else set()

    kickoff = parse_iso(game.get("date"))
    tz = ZoneInfo(TIMEZONE_NAME)
    kickoff_text = kickoff.astimezone(tz).strftime("%H:%M") if kickoff else "onbekend"

    alerts = 0

    for row in tracked_rows:
        slug = row["slug"]
        alert_key = f"{game_id}|{slug}"

        if alert_key in sent_alerts:
            continue

        team_name = None
        if slug in home_starters:
            team_name = home.get("name") or row["club"]
        elif slug in away_starters:
            team_name = away.get("name") or row["club"]

        if not team_name:
            # Official lineup known, but the tracked reserve is not starting.
            # Intentionally NO bench alert.
            continue

        message = (
            f"🚨 **{row['keeper']} keept voor {team_name} om {kickoff_text}! Verkopen.**"
        )

        send_discord(message)
        sent_alerts.add(alert_key)
        save_json(SENT_ALERTS_FILE, sorted(sent_alerts))
        print("ALERT:", message, flush=True)
        alerts += 1

    return alerts


def check_once(tracked):
    games = get_upcoming_games(tracked)

    if games:
        print(f"{len(games)} relevante wedstrijd(en) binnen {LOOKAHEAD_MINUTES} min.", flush=True)

    total_alerts = 0
    for game_id, info in games.items():
        try:
            total_alerts += alert_for_game(game_id, info["tracked"])
        except Exception as exc:
            print(f"Lineup-check mislukt voor game {game_id}: {exc}", flush=True)

    return total_alerts


def main():
    global sent_alerts

    print("=== SORARE OFFICIAL LINEUP KEEPER BOT ===", flush=True)
    print(
        f"Check elke {CHECK_INTERVAL}s | venster {LOOKAHEAD_MINUTES} min | "
        f"tijdzone {TIMEZONE_NAME}",
        flush=True,
    )

    if not SORARE_API_KEY:
        raise RuntimeError(
            "SORARE_API_KEY ontbreekt. Voeg hem in Render toe bij Environment."
        )

    sent_alerts = set(load_json(SENT_ALERTS_FILE, []))
    tracked = load_keepers()

    if not tracked:
        raise RuntimeError("Geen keepers konden aan Sorare worden gekoppeld.")

    while True:
        started = time.time()

        try:
            alerts = check_once(tracked)
            if alerts:
                print(f"{alerts} nieuwe Discord-alert(s) verzonden.", flush=True)
        except Exception as exc:
            print(f"BOTFOUT: {type(exc).__name__}: {exc}", flush=True)

        if RUN_ONCE:
            print("RUN_ONCE klaar.", flush=True)
            break

        elapsed = time.time() - started
        time.sleep(max(1, CHECK_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
