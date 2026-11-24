import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

MIN_PLAYERS = 200
MAX_AGE_DAYS = 30
MIN_PRICE_CENTS = 100  # $1.00 USD
FEED_URL = "https://eric8239.github.io/steam-roguelike-rss/feed.xml"

# Matches your two SteamDB URLs
QUERIES = [
    {"tags": "1716", "untags": "42804,615955"},
    {"tags": "3959", "untags": "1716,1723,42804,615955"},
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SteamRoguelikeRSS/1.0"})
SEEN_FILE = Path("seen.json")
FEED_FILE = Path("feed.xml")


def parse_steam_date(date_str: str):
    """Parse Steam's English release date strings."""
    cleaned = date_str.strip()
    for fmt in ("%b %d, %Y", "%d %b, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fetch_recent_candidates(cutoff: datetime) -> dict:
    """Paginate Steam search sorted newest-first until past the 30-day cutoff."""
    candidates = {}
    now = datetime.now(timezone.utc)

    for q in QUERIES:
        start = 0
        old_streak = 0

        while start < 500:
            params = {
                "query": "",
                "start": start,
                "count": 50,
                "sort_by": "Released_DESC",
                "category1": "998",   # Games only
                "hidef2p": "1",       # Hide Free to Play
                "tags": q["tags"],
                "untags": q["untags"],
                "cc": "us",
                "l": "english",
                "infinite": "1",
            }
            resp = SESSION.get(
                "https://store.steampowered.com/search/results/",
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            html = resp.json().get("results_html", "")
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select("a.search_result_row")
            if not rows:
                break

            for row in rows:
                appid = row.get("data-ds-appid")
                if not appid or "," in appid:
                    continue  # Skip bundles/packages

                date_el = row.select_one(".search_released")
                rel_date = parse_steam_date(date_el.get_text()) if date_el else None
                if not rel_date:
                    continue

                if rel_date < cutoff:
                    old_streak += 1
                    continue
                if rel_date > now + timedelta(days=1):
                    continue  # Skip unreleased games

                old_streak = 0
                title_el = row.select_one(".title")
                candidates[appid] = {
                    "appid": appid,
                    "title": title_el.get_text(strip=True) if title_el else f"App {appid}",
                    "release_date": rel_date.isoformat(),
                }

            # Stop once we have clearly passed the 30-day window
            if old_streak >= 10:
                break

            start += 50
            time.sleep(0.5)

    return candidates


def get_current_players(appid: str) -> int:
    resp = SESSION.get(
        "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
        params={"appid": appid},
        timeout=15,
    )
    if resp.status_code != 200:
        return 0
    return resp.json().get("response", {}).get("player_count", 0)


def get_app_details(appid: str):
    resp = SESSION.get(
        "https://store.steampowered.com/api/appdetails",
        params={"appids": appid, "cc": "us", "l": "english"},
        timeout=20,
    )
    if resp.status_code != 200:
        return None
    entry = resp.json().get(str(appid), {})
    if not entry.get("success"):
        return None
    return entry.get("data")


def is_valid_paid_full_release(data: dict, cutoff: datetime) -> bool:
    if data.get("type") != "game":
        return False
    if data.get("is_free", False):
        return False

    price = data.get("price_overview")
    if not price or price.get("final", 0) < MIN_PRICE_CENTS:
        return False

    # Genre ID "70" is Steam's official Early Access status
    genres = data.get("genres", [])
    if any(str(g.get("id")) == "70" for g in genres):
        return False

    rel_info = data.get("release_date", {})
    if rel_info.get("coming_soon"):
        return False

    rel_date = parse_steam_date(rel_info.get("date", ""))
    if rel_date and rel_date < cutoff:
        return False

    return True


def build_rss(items: dict):
    rss = ET.Element("rss", version="2.0", attrib={"xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "New Steam Roguelikes & Roguelites (200+ Players)"
    ET.SubElement(channel, "link").text = "https://steamdb.info/tag/1716/"
    ET.SubElement(channel, "description").text = (
        "Paid, non-Early-Access roguelike/roguelite Steam games released in the last "
        "30 days with at least 200 concurrent players."
    )
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(
        channel,
        "atom:link",
        href=FEED_URL,
        rel="self",
        type="application/rss+xml",
    )

    sorted_games = sorted(
        items.values(),
        key=lambda g: g["first_matched"],
        reverse=True,
    )

    for g in sorted_games:
        item = ET.SubElement(channel, "item")
        appid = g["appid"]
        store_url = f"https://store.steampowered.com/app/{appid}/"
        steamdb_url = f"https://steamdb.info/app/{appid}/charts/"

        ET.SubElement(item, "title").text = (
            f"{g['title']} ({g['peak_seen_players']:,} players | {g['price']})"
        )
        ET.SubElement(item, "link").text = store_url
        guid = ET.SubElement(item, "guid", isPermaLink="false")
        guid.text = f"steam-roguelike-{appid}"

        pub_dt = datetime.fromisoformat(g["first_matched"])
        ET.SubElement(item, "pubDate").text = format_datetime(pub_dt)

        desc_html = (
            f'<p><img src="{g["header_image"]}" alt="{g["title"]}" /></p>'
            f'<p><strong>Release Date:</strong> {g["release_date_display"]} | '
            f'<strong>Price:</strong> {g["price"]} | '
            f'<strong>Peak Seen Players:</strong> {g["peak_seen_players"]:,}</p>'
            f'<p>{g["short_description"]}</p>'
            f'<p><a href="{store_url}">Steam Store</a> | '
            f'<a href="{steamdb_url}">SteamDB Charts</a></p>'
        )
        ET.SubElement(item, "description").text = desc_html

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(FEED_FILE, encoding="utf-8", xml_declaration=True)


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)

    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}

    # Prune games released more than 30 days ago
    seen = {
        appid: info
        for appid, info in seen.items()
        if datetime.fromisoformat(info["release_date"]) >= cutoff
    }

    candidates = fetch_recent_candidates(cutoff)
    print(f"Found {len(candidates)} recent candidates across both tag queries.")

    for appid, cand in candidates.items():
        players = get_current_players(appid)
        time.sleep(0.25)

        if players < MIN_PLAYERS:
            continue

        if appid in seen:
            seen[appid]["peak_seen_players"] = max(seen[appid]["peak_seen_players"], players)
            continue

        details = get_app_details(appid)
        time.sleep(0.5)
        if not details or not is_valid_paid_full_release(details, cutoff):
            continue

        seen[appid] = {
            "appid": appid,
            "title": details.get("name", cand["title"]),
            "release_date": cand["release_date"],
            "release_date_display": details.get("release_date", {}).get("date", ""),
            "price": details.get("price_overview", {}).get("final_formatted", "Paid"),
            "short_description": details.get("short_description", ""),
            "header_image": details.get("header_image", ""),
            "peak_seen_players": players,
            "first_matched": now.isoformat(),
        }
        print(f"Added: {seen[appid]['title']} ({players} players)")

    SEEN_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")
    build_rss(seen)


if __name__ == "__main__":
    main()
