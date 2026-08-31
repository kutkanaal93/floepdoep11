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

LINEUP_ENABLED = os.getenv("LINEUP_ENABLED", "1").lower() not in {"0", "false", "no"}
MARKET_ENABLED = os.getenv("MARKET_ENABLED", "1").lower() not in {"0", "false", "no"}

MARKET_RARITIES = [
    r.strip().lower()
    for r in os.getenv("MARKET_RARITIES", "limited,rare").split(",")
    if r.strip().lower() in {"limited", "rare", "super_rare", "unique"}
]
if not MARKET_RARITIES:
    MARKET_RARITIES = ["limited", "rare"]

BUY_SPIKE_COUNT = int(os.getenv("BUY_SPIKE_COUNT", "3"))
BUY_SPIKE_MINUTES = int(os.getenv("BUY_SPIKE_MINUTES", "5"))
BUY_FLOOR_COUNT = int(os.getenv("BUY_FLOOR_COUNT", "2"))
BUY_FLOOR_MINUTES = int(os.getenv("BUY_FLOOR_MINUTES", "10"))
BUY_FLOOR_PCT = float(os.getenv("BUY_FLOOR_PCT", "30"))
FLOOR_EXPLOSION_PCT = float(os.getenv("FLOOR_EXPLOSION_PCT", "75"))
EXTREME_BUY_COUNT = int(os.getenv("EXTREME_BUY_COUNT", "5"))
EXTREME_BUY_MINUTES = int(os.getenv("EXTREME_BUY_MINUTES", "15"))
MARKET_ALERT_COOLDOWN_MINUTES = int(os.getenv("MARKET_ALERT_COOLDOWN_MINUTES", "30"))
FLOOR_HISTORY_MINUTES = int(os.getenv("FLOOR_HISTORY_MINUTES", "45"))
FLOOR_BASELINE_MINUTES = int(os.getenv("FLOOR_BASELINE_MINUTES", "10"))
MARKET_QUERY_LOOKBACK_MINUTES = max(
    int(os.getenv("MARKET_QUERY_LOOKBACK_MINUTES", "20")),
    BUY_SPIKE_MINUTES,
    BUY_FLOOR_MINUTES,
    EXTREME_BUY_MINUTES,
)

RUN_ONCE = os.getenv("RUN_ONCE", "0").lower() in {"1", "true", "yes"}
TEST_ALERT = os.getenv("TEST_ALERT", "0").lower() in {"1", "true", "yes"}
TEST_MARKET_ALERT = os.getenv("TEST_MARKET_ALERT", "0").lower() in {"1", "true", "yes"}

BASE_DIR = Path(__file__).resolve().parent
KEEPERS_FILE = BASE_DIR / "keepers.csv"
SLUG_CACHE_FILE = BASE_DIR / "slug_cache.json"
SENT_ALERTS_FILE = BASE_DIR / "sent_alerts.json"
MARKET_STATE_FILE = BASE_DIR / "market_state.json"

session = requests.Session()
sent_alerts = set()
market_state = {"floor_history": {}, "last_market_alert": {}}


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("ø", "o").replace("đ", "d").replace("ð", "d").replace("þ", "th")
    return re.sub(r"[^a-z0-9]+", "", value)


def split_keeper_names(value):
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
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        print(f"Kan {path.name} niet opslaan: {exc}", flush=True)


def graphql(query, variables=None, attempts=3):
    if not SORARE_API_KEY:
        raise RuntimeError("SORARE_API_KEY ontbreekt in Render Environment.")

    headers = {
        "Content-Type": "application/json",
        "APIKEY": SORARE_API_KEY,
        "User-Agent": "SorareKeeperMarketWatch/2.0",
    }

    for _ in range(attempts):
        response = session.post(
            SORARE_URL,
            headers=headers,
            json={"query": query, "variables": variables or {}},
            timeout=35,
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
  searchPlayers(query: $query, page: 1, pageSize: 10) {
    hits {
      player {
        slug
        displayName
        activeClub { slug name }
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
    activeClub { slug name }
    nextGame {
      id
      date
      homeTeam { slug name }
      awayTeam { slug name }
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
      homeTeam { slug name }
      awayTeam { slug name }
      homeFormation {
        startingLineupAvailable
        startingLineup { slug displayName }
      }
      awayFormation {
        startingLineupAvailable
        startingLineup { slug displayName }
      }
    }
  }
}
"""

FLOOR_FIELDS = {
    "limited": "limitedFloor",
    "rare": "rareFloor",
    "super_rare": "superRareFloor",
    "unique": "uniqueFloor",
}


def floor_query():
    parts = []
    for rarity in MARKET_RARITIES:
        alias = FLOOR_FIELDS[rarity]
        parts.append(
            f"{alias}: lowestPriceAnyCard(inSeason: true, rarity: {rarity}) "
            "{ slug publicMinPrices { eurCents } }"
        )
    return "query KeeperFloors($slugs: [String!]) { players(slugs: $slugs) { slug displayName " + " ".join(parts) + " } }"


def resolve_slug(keeper_name, configured_club):
    data = graphql(SEARCH_PLAYER_QUERY, {"query": keeper_name})
    hits = (data.get("searchPlayers") or {}).get("hits") or []
    candidates = []

    for hit in hits:
        player = (hit or {}).get("player") or {}
        slug = player.get("slug")
        name = player.get("displayName") or ""
        club_name = ((player.get("activeClub") or {}).get("name") or "")
        if not slug:
            continue
        name_score = difflib.SequenceMatcher(None, norm(keeper_name), norm(name)).ratio()
        club_score = difflib.SequenceMatcher(None, norm(configured_club), norm(club_name)).ratio()
        exact_name = norm(keeper_name) == norm(name)
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

    print(f"Sorare-match: {keeper_name} -> {best[2]} [{best[1]}] ({best[3] or 'geen club'})", flush=True)
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
            for keeper_name in names:
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
                    tracked.append({"club": club, "keeper": keeper_name, "slug": slug})
                else:
                    print(f"NIET GEVONDEN: {club} | {keeper_name}", flush=True)

    save_json(SLUG_CACHE_FILE, cache)
    deduped = {row["slug"]: row for row in tracked}
    result = list(deduped.values())
    print(f"Tracking actief voor {len(result)} keeper(s).", flush=True)
    return result


def batched(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def euro(cents):
    if cents is None:
        return "geen floor"
    return f"€{cents / 100:.2f}".replace(".", ",")


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("GEEN DISCORD_WEBHOOK_URL. Alert alleen in log:", flush=True)
        print(message, flush=True)
        return
    response = session.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    response.raise_for_status()


# ---------- OFFICIAL LINEUP ----------

def get_upcoming_games(tracked):
    by_slug = {row["slug"]: row for row in tracked}
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=GRACE_MINUTES)
    window_end = now + timedelta(minutes=LOOKAHEAD_MINUTES)
    games = {}

    for chunk in batched(list(by_slug), 40):
        data = graphql(TRACKED_PLAYERS_QUERY, {"slugs": chunk})
        for player in data.get("players") or []:
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
            games.setdefault(game_id, {"game": game, "tracked": []})
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
            continue

        message = (
            f"🚨 **LINEUP — {row['keeper']} keept voor {team_name} om {kickoff_text}!**\n"
            f"**VERKOPEN / CHECK MARKET**\n"
            f"https://sorare.com/football/players/{slug}"
        )
        send_discord(message)
        sent_alerts.add(alert_key)
        save_json(SENT_ALERTS_FILE, sorted(sent_alerts))
        print("LINEUP ALERT:", row["keeper"], team_name, flush=True)
        alerts += 1
    return alerts


def check_lineups(tracked):
    if not LINEUP_ENABLED:
        return 0
    games = get_upcoming_games(tracked)
    if games:
        print(f"Lineup: {len(games)} relevante wedstrijd(en) binnen {LOOKAHEAD_MINUTES} min.", flush=True)
    total = 0
    for game_id, info in games.items():
        try:
            total += alert_for_game(game_id, info["tracked"])
        except Exception as exc:
            print(f"Lineup-check mislukt voor game {game_id}: {exc}", flush=True)
    return total


# ---------- MARKET WATCH ----------

def market_sales_query(chunk):
    fields = []
    alias_map = {}
    idx = 0
    for row in chunk:
        slug = row["slug"]
        for rarity in MARKET_RARITIES:
            alias = f"p{idx}_{rarity.replace('_', '')}"
            idx += 1
            alias_map[alias] = (slug, rarity)
            fields.append(
                f'''{alias}: tokenPrices(
                    first: 10
                    from: $from
                    to: $to
                    includePrivateSales: false
                    playerSlug: "{slug}"
                    rarity: {rarity}
                    seasonEligibility: IN_SEASON
                  ) {{
                    id
                    date
                    amounts {{ eurCents }}
                    card {{ slug seasonYear inSeasonEligible rarityTyped }}
                    deal {{
                      __typename
                      ... on TokenOffer {{ id type status transactionDate }}
                    }}
                  }}'''
            )
    query = "query KeeperMarketSales($from: ISO8601DateTime!, $to: ISO8601DateTime!) { tokens { " + " ".join(fields) + " } }"
    return query, alias_map


def get_recent_secondary_sales(tracked, now):
    start = now - timedelta(minutes=MARKET_QUERY_LOOKBACK_MINUTES)
    result = {}

    # 10 players * two rarities = 20 tokenPrices aliases per request.
    # This keeps GraphQL complexity deliberately low while staying far below the API rate limit.
    for chunk in batched(tracked, 10):
        query, alias_map = market_sales_query(chunk)
        data = graphql(query, {"from": iso_utc(start), "to": iso_utc(now)})
        token_data = data.get("tokens") or {}

        for alias, (slug, rarity) in alias_map.items():
            key = f"{slug}|{rarity}"
            unique = {}
            for item in token_data.get(alias) or []:
                deal = item.get("deal") or {}
                # Count actual public secondary transactions; ignore direct/private, auction and primary.
                if deal.get("__typename") != "TokenOffer":
                    continue
                if deal.get("type") not in {"SINGLE_SALE_OFFER", "SINGLE_BUY_OFFER"}:
                    continue
                date = parse_iso(item.get("date"))
                if not date or date < start or date > now + timedelta(seconds=30):
                    continue
                cents = (item.get("amounts") or {}).get("eurCents")
                sale = {
                    "id": item.get("id"),
                    "date": date,
                    "eur_cents": cents if isinstance(cents, int) else None,
                    "offer_type": deal.get("type"),
                    "card_slug": (item.get("card") or {}).get("slug"),
                }
                sale_id = sale["id"] or f"{date.isoformat()}|{sale.get('card_slug')}"
                unique[sale_id] = sale
            result[key] = sorted(unique.values(), key=lambda x: x["date"])
    return result


def get_current_floors(tracked):
    result = {}
    query = floor_query()
    for chunk in batched(tracked, 40):
        slugs = [row["slug"] for row in chunk]
        data = graphql(query, {"slugs": slugs})
        for player in data.get("players") or []:
            if not player:
                continue
            slug = player.get("slug")
            for rarity in MARKET_RARITIES:
                card = player.get(FLOOR_FIELDS[rarity])
                cents = None
                if card:
                    value = (card.get("publicMinPrices") or {}).get("eurCents")
                    if isinstance(value, int) and value > 0:
                        cents = value
                result[f"{slug}|{rarity}"] = cents
    return result


def sales_since(sales, now, minutes):
    cutoff = now - timedelta(minutes=minutes)
    return [sale for sale in sales if sale["date"] >= cutoff]


def floor_history_for(key):
    return market_state.setdefault("floor_history", {}).setdefault(key, [])


def baseline_floor(key, now):
    history = floor_history_for(key)
    if not history:
        return None
    cutoff = now - timedelta(minutes=FLOOR_HISTORY_MINUTES)
    target = now - timedelta(minutes=FLOOR_BASELINE_MINUTES)
    candidates = []
    previous = []

    for item in history:
        try:
            ts = parse_iso(item["time"])
            cents = int(item["cents"])
        except Exception:
            continue
        if not ts or cents <= 0 or ts < cutoff:
            continue
        if ts <= target:
            candidates.append((ts, cents))
        if ts < now - timedelta(seconds=30):
            previous.append((ts, cents))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    if previous:
        previous.sort(key=lambda x: x[0], reverse=True)
        return previous[0][1]
    return None


def record_floor(key, now, floor_cents):
    history = floor_history_for(key)
    cutoff = now - timedelta(minutes=FLOOR_HISTORY_MINUTES)
    cleaned = []
    for item in history:
        try:
            ts = parse_iso(item["time"])
            cents = int(item["cents"])
            if ts and ts >= cutoff and cents > 0:
                cleaned.append({"time": iso_utc(ts), "cents": cents})
        except Exception:
            pass
    if floor_cents:
        cleaned.append({"time": iso_utc(now), "cents": int(floor_cents)})
    market_state["floor_history"][key] = cleaned[-30:]


def pct_change(old, new):
    if not old or not new or old <= 0:
        return None
    return ((new / old) - 1.0) * 100.0


def market_alert_allowed(key, now):
    raw = market_state.setdefault("last_market_alert", {}).get(key)
    if not raw:
        return True
    last = parse_iso(raw)
    return not last or now - last >= timedelta(minutes=MARKET_ALERT_COOLDOWN_MINUTES)


def build_market_signal(sales, current_floor, base_floor, now):
    s5 = sales_since(sales, now, BUY_SPIKE_MINUTES)
    s10 = sales_since(sales, now, BUY_FLOOR_MINUTES)
    s15 = sales_since(sales, now, EXTREME_BUY_MINUTES)
    fpct = pct_change(base_floor, current_floor)

    if len(s15) >= EXTREME_BUY_COUNT:
        return "🔥 EXTREME MARKET", s15, fpct
    if len(s5) >= BUY_SPIKE_COUNT:
        return "🚨 BUY SPIKE", s5, fpct
    if len(s10) >= BUY_FLOOR_COUNT and fpct is not None and fpct >= BUY_FLOOR_PCT:
        return "🚨 BUY + FLOOR SPIKE", s10, fpct
    if len(s10) >= 1 and fpct is not None and fpct >= FLOOR_EXPLOSION_PCT:
        return "🚨 FLOOR EXPLOSION", s10, fpct
    return None, [], fpct


def send_market_alert(row, rarity, label, triggering_sales, base_floor, current_floor, floor_pct):
    sorted_sales = sorted(triggering_sales, key=lambda x: x["date"], reverse=True)
    last_prices = [euro(s["eur_cents"]) for s in sorted_sales[:5] if s.get("eur_cents") is not None]
    floor_line = f"{euro(base_floor)} → {euro(current_floor)}"
    if floor_pct is not None:
        floor_line += f" ({floor_pct:+.0f}%)"
    rarity_name = rarity.replace("_", " ").title()

    lines = [
        f"{label} — **{row['keeper']}**",
        f"{rarity_name} In-Season | {row['club']}",
        f"**{len(triggering_sales)} echte public secondary buy(s)** in korte tijd",
        f"Floor: **{floor_line}**",
    ]
    if last_prices:
        lines.append("Laatste saleprijzen: " + " / ".join(last_prices))
    lines.extend([
        "**CHECK NIEUWS / VERKOPEN**",
        f"https://sorare.com/football/players/{row['slug']}",
    ])
    send_discord("\n".join(lines))


def check_market(tracked):
    if not MARKET_ENABLED:
        return 0

    now = datetime.now(timezone.utc)
    sales_by_key = get_recent_secondary_sales(tracked, now)
    floors = get_current_floors(tracked)
    alerts = 0
    active_markets = 0

    for row in tracked:
        slug = row["slug"]
        for rarity in MARKET_RARITIES:
            key = f"{slug}|{rarity}"
            sales = sales_by_key.get(key, [])
            current_floor = floors.get(key)
            base_floor = baseline_floor(key, now)

            if sales_since(sales, now, MARKET_QUERY_LOOKBACK_MINUTES):
                active_markets += 1

            label, trigger_sales, floor_pct = build_market_signal(sales, current_floor, base_floor, now)
            if label and market_alert_allowed(key, now):
                send_market_alert(row, rarity, label, trigger_sales, base_floor, current_floor, floor_pct)
                market_state["last_market_alert"][key] = iso_utc(now)
                alerts += 1
                print(
                    f"MARKET ALERT: {row['keeper']} | {rarity} | {label} | "
                    f"{len(trigger_sales)} sale(s) | {euro(base_floor)} -> {euro(current_floor)}",
                    flush=True,
                )

            record_floor(key, now, current_floor)

    save_json(MARKET_STATE_FILE, market_state)
    print(
        f"Market scan klaar: {len(tracked)} keepers x {len(MARKET_RARITIES)} rarity(s) | "
        f"{active_markets} markten met recente secondary sale(s) | {alerts} alert(s).",
        flush=True,
    )
    return alerts


# ---------- TEST MODES ----------

def run_test_alert(tracked):
    row = tracked[0]
    send_discord(
        "🧪 **TEST SORARE KEEPER BOT**\n"
        f"Testkeeper: {row['keeper']} ({row['club']})\n"
        "Dit is GEEN echte lineup-alert."
    )
    print("TEST ALERT VERZONDEN:", row["keeper"], flush=True)


def run_test_market_alert(tracked):
    row = tracked[0]
    send_discord(
        "🧪 **TEST MARKET SPIKE — SORARE KEEPER BOT**\n"
        f"{row['keeper']} ({row['club']})\n"
        "3 nep-buys in 5 min | Floor €2,50 → €7,50 (+200%)\n"
        "**GEEN echte market move.**"
    )
    print("TEST MARKET ALERT VERZONDEN:", row["keeper"], flush=True)


def main():
    global sent_alerts, market_state

    print("=== SORARE KEEPER BOT v2.0 — LINEUP + MARKET WATCH ===", flush=True)
    print(
        f"Check elke {CHECK_INTERVAL}s | lineup={LINEUP_ENABLED} | market={MARKET_ENABLED} | "
        f"rarities={','.join(MARKET_RARITIES)} | tijdzone={TIMEZONE_NAME}",
        flush=True,
    )
    print(
        f"Market triggers: {BUY_SPIKE_COUNT}+ buys/{BUY_SPIKE_MINUTES}m | "
        f"{BUY_FLOOR_COUNT}+ buys/{BUY_FLOOR_MINUTES}m + floor {BUY_FLOOR_PCT:.0f}% | "
        f"1+ buy + floor {FLOOR_EXPLOSION_PCT:.0f}% | "
        f"extreme {EXTREME_BUY_COUNT}+/{EXTREME_BUY_MINUTES}m",
        flush=True,
    )

    if not SORARE_API_KEY:
        raise RuntimeError("SORARE_API_KEY ontbreekt. Voeg hem in Render toe bij Environment.")

    sent_alerts = set(load_json(SENT_ALERTS_FILE, []))
    loaded = load_json(MARKET_STATE_FILE, {})
    if isinstance(loaded, dict):
        market_state["floor_history"] = loaded.get("floor_history", {}) or {}
        market_state["last_market_alert"] = loaded.get("last_market_alert", {}) or {}

    tracked = load_keepers()
    if not tracked:
        raise RuntimeError("Geen keepers konden aan Sorare worden gekoppeld.")

    if TEST_ALERT:
        run_test_alert(tracked)
        print("TEST_ALERT klaar; bot stopt bewust.", flush=True)
        return
    if TEST_MARKET_ALERT:
        run_test_market_alert(tracked)
        print("TEST_MARKET_ALERT klaar; bot stopt bewust.", flush=True)
        return

    while True:
        started = time.time()
        total_alerts = 0

        try:
            total_alerts += check_market(tracked)
        except Exception as exc:
            print(f"MARKETFOUT: {type(exc).__name__}: {exc}", flush=True)

        try:
            total_alerts += check_lineups(tracked)
        except Exception as exc:
            print(f"LINEUPFOUT: {type(exc).__name__}: {exc}", flush=True)

        if total_alerts:
            print(f"Totaal {total_alerts} nieuwe alert(s) deze ronde.", flush=True)
        if RUN_ONCE:
            print("RUN_ONCE klaar.", flush=True)
            break

        elapsed = time.time() - started
        print(f"Ronde klaar in {elapsed:.1f}s; slapen tot volgende check.", flush=True)
        time.sleep(max(1, CHECK_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
