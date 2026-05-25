#!/usr/bin/env python3
"""Price tracker: monitors specific URLs and hunts web deal feeds by keyword."""

import argparse
import json
import logging
import os
import re
import smtplib
import sqlite3
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import time

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path("price_history.db")
ITEMS_PATH = Path("items.json")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEAL_FEEDS = [
    (
        "Slickdeals",
        "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&q={q}&rss=1",
    ),
    (
        "Reddit /r/deals",
        "https://www.reddit.com/r/deals/search.rss?q={q}&sort=new&restrict_sr=1&t=week",
    ),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Item:
    name: str
    target_price: float
    keywords: str = ""  # search terms; defaults to name if blank
    url: str = ""  # optional direct URL to also monitor
    selector: str = ""
    id: Optional[int] = None

    @property
    def search_terms(self) -> str:
        return self.keywords.strip() or self.name


@dataclass
class WebDeal:
    source: str
    title: str
    link: str
    price: Optional[float]


@dataclass
class PriceAlert:
    item: Item
    price: float
    score: float
    reasons: list[str]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id           INTEGER PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            url          TEXT DEFAULT '',
            target_price REAL NOT NULL,
            selector     TEXT DEFAULT '',
            keywords     TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS prices (
            id      INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id),
            price   REAL NOT NULL,
            ts      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen_deals (
            link       TEXT PRIMARY KEY,
            item_id    INTEGER NOT NULL,
            first_seen TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prices_item_ts ON prices(item_id, ts);
    """)
    conn.commit()
    return conn


def seed_from_json(conn: sqlite3.Connection, path: Path = ITEMS_PATH) -> None:
    """Keep the DB in sync with items.json — adds new items, removes deleted ones."""
    if not path.exists():
        return
    json_items = json.loads(path.read_text())
    json_names = {it["name"] for it in json_items}

    # Remove items no longer in the file
    for row in conn.execute("SELECT id, name FROM items").fetchall():
        if row[1] not in json_names:
            conn.execute("DELETE FROM prices WHERE item_id=?", (row[0],))
            conn.execute("DELETE FROM seen_deals WHERE item_id=?", (row[0],))
            conn.execute("DELETE FROM items WHERE id=?", (row[0],))
            log.info("Removed '%s' (no longer in items.json)", row[1])

    # Add or update items from the file
    for it in json_items:
        conn.execute(
            """INSERT INTO items (name, url, target_price, selector, keywords)
               VALUES (?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 url=excluded.url,
                 target_price=excluded.target_price,
                 selector=excluded.selector,
                 keywords=excluded.keywords""",
            (
                it["name"],
                it.get("url", ""),
                float(it["target_price"]),
                it.get("selector", ""),
                it.get("keywords", ""),
            ),
        )
    conn.commit()


def get_items(conn: sqlite3.Connection) -> list[Item]:
    rows = conn.execute(
        "SELECT id, name, url, target_price, selector, keywords FROM items ORDER BY id"
    ).fetchall()
    return [
        Item(
            id=r[0],
            name=r[1],
            url=r[2],
            target_price=r[3],
            selector=r[4],
            keywords=r[5],
        )
        for r in rows
    ]


def save_price(conn: sqlite3.Connection, item_id: int, price: float) -> None:
    conn.execute(
        "INSERT INTO prices (item_id, price, ts) VALUES (?,?,?)",
        (item_id, price, datetime.now().isoformat()),
    )
    conn.commit()


def price_stats(conn: sqlite3.Connection, item_id: int, days: int = 30) -> dict:
    since = (datetime.now() - timedelta(days=days)).isoformat()
    recent = [
        r[0]
        for r in conn.execute(
            "SELECT price FROM prices WHERE item_id=? AND ts>=? ORDER BY ts",
            (item_id, since),
        )
    ]
    all_time = conn.execute(
        "SELECT MIN(price), MAX(price), COUNT(*) FROM prices WHERE item_id=?",
        (item_id,),
    ).fetchone()
    return {
        "recent": recent,
        "avg": sum(recent) / len(recent) if recent else None,
        "min_all_time": all_time[0],
        "max_all_time": all_time[1],
        "total_checks": all_time[2],
    }


def is_new_deal(conn: sqlite3.Connection, link: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM seen_deals WHERE link=?", (link,)).fetchone()
        is None
    )


def mark_seen(conn: sqlite3.Connection, link: str, item_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_deals (link, item_id, first_seen) VALUES (?,?,?)",
        (link, item_id, datetime.now().isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Web deal hunting — RSS feed search
# ---------------------------------------------------------------------------
def search_web_deals(item: Item) -> list[WebDeal]:
    q = urllib.parse.quote_plus(item.search_terms)
    deals: list[WebDeal] = []
    for source, url_template in DEAL_FEEDS:
        url = url_template.format(q=q)
        try:
            resp = requests.get(url, headers={"User-Agent": _UA}, timeout=12)
            resp.raise_for_status()
            deals.extend(_parse_rss(source, resp.text))
        except requests.RequestException as e:
            log.warning("Feed fetch failed (%s): %s", source, e)
        except ET.ParseError as e:
            log.warning("Feed parse failed (%s): %s", source, e)
    return deals


def _parse_rss(source: str, xml_text: str) -> list[WebDeal]:
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    deals: list[WebDeal] = []

    # Standard RSS <item> elements
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if title and link:
            deals.append(
                WebDeal(
                    source=source,
                    title=title,
                    link=link,
                    price=_extract_price(title + " " + desc),
                )
            )

    # Atom <entry> elements (Reddit uses Atom)
    for entry in root.findall(".//atom:entry", ns):
        title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        link = (link_el.get("href") if link_el is not None else "").strip()
        if title and link:
            deals.append(
                WebDeal(
                    source=source,
                    title=title,
                    link=link,
                    price=_extract_price(title),
                )
            )

    return deals


def _extract_price(text: str) -> Optional[float]:
    """Find the lowest plausible price mentioned in deal text (sale price < original)."""
    prices = []
    for m in re.finditer(r"\$[\d,]+\.?\d{0,2}", text):
        p = _parse_price(m.group())
        if p and 0.50 < p < 10_000:
            prices.append(p)
    return min(prices) if prices else None


# ---------------------------------------------------------------------------
# Direct URL monitoring (existing behaviour, now optional per item)
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_url_price(item: Item) -> Optional[float]:
    for attempt in range(3):
        try:
            resp = requests.get(item.url, headers=_HEADERS, timeout=15)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10 * (attempt + 1)))
                log.warning("Rate limited on '%s' — waiting %ds", item.name, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            log.warning("Fetch failed for '%s': %s", item.name, e)
            return None
    else:
        log.warning("Giving up on '%s' after 3 attempts", item.name)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            price = _schema_price(json.loads(tag.string or ""))
            if price:
                return price
        except (json.JSONDecodeError, AttributeError):
            continue

    for prop in ("product:price:amount", "og:price:amount"):
        tag = soup.find("meta", property=prop) or soup.find("meta", {"name": prop})
        if tag and tag.get("content"):
            price = _parse_price(tag["content"])
            if price:
                return price

    if item.selector:
        el = soup.select_one(item.selector)
        if el:
            price = _parse_price(el.get_text())
            if price:
                return price

    return _heuristic_price(soup)


def _schema_price(data) -> Optional[float]:
    if isinstance(data, list):
        for d in data:
            p = _schema_price(d)
            if p:
                return p
    if isinstance(data, dict):
        if data.get("@type") in ("Offer", "AggregateOffer"):
            return _parse_price(str(data.get("price", "")))
        for key in ("offers", "Offers"):
            if key in data:
                return _schema_price(data[key])
    return None


def _heuristic_price(soup: BeautifulSoup) -> Optional[float]:
    price_re = re.compile(r"\$[\d,]+\.?\d{0,2}")
    candidates: list[float] = []
    for sel in (
        '[class*="price"]',
        '[id*="price"]',
        '[itemprop="price"]',
        '[class*="sale"]',
        '[class*="cost"]',
        "[data-price]",
    ):
        for el in soup.select(sel)[:8]:
            for m in price_re.finditer(el.get_text()):
                p = _parse_price(m.group())
                if p and 0.01 < p < 100_000:
                    candidates.append(p)
    return Counter(candidates).most_common(1)[0][0] if candidates else None


def _parse_price(text: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Deal scoring (URL-monitored items)
# ---------------------------------------------------------------------------
def score_deal(item: Item, price: float, stats: dict) -> Optional[PriceAlert]:
    reasons: list[str] = []
    scores: list[float] = []

    if price <= item.target_price:
        pct = (item.target_price - price) / item.target_price
        reasons.append(
            f"${price:.2f} hits target ${item.target_price:.2f} ({pct:.0%} below)"
        )
        scores.append(min(1.0, 0.4 + pct * 1.5))

    if stats["avg"] and stats["total_checks"] >= 3:
        pct = (stats["avg"] - price) / stats["avg"]
        if pct >= 0.05:
            reasons.append(f"{pct:.0%} below 30-day avg ${stats['avg']:.2f}")
            scores.append(min(1.0, pct * 3))

    if (
        stats["min_all_time"]
        and stats["total_checks"] >= 5
        and price < stats["min_all_time"]
    ):
        reasons.append(f"All-time low! Previous best: ${stats['min_all_time']:.2f}")
        scores.append(1.0)

    if not reasons:
        return None
    return PriceAlert(item=item, price=price, score=max(scores), reasons=reasons)


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------
def check_all(
    conn: sqlite3.Connection,
) -> tuple[list[PriceAlert], list[tuple[Item, WebDeal]]]:
    items = get_items(conn)
    if not items:
        log.warning("No items configured — add some via 'add' or items.json")
        return [], []

    price_alerts: list[PriceAlert] = []
    new_web_deals: list[tuple[Item, WebDeal]] = []

    for item in items:
        log.info("Checking: %s", item.name)

        # 1. URL monitoring
        if item.url:
            price = fetch_url_price(item)
            if price is not None:
                log.info("  URL price: $%.2f (target $%.2f)", price, item.target_price)
                save_price(conn, item.id, price)
                alert = score_deal(item, price, price_stats(conn, item.id))
                if alert:
                    log.info("  PRICE ALERT  score=%.0f%%", alert.score * 100)
                    price_alerts.append(alert)
            else:
                log.warning("  Could not extract URL price")

        # 2. Web deal hunting
        deals = search_web_deals(item)
        log.info("  Found %d deal post(s) across feeds", len(deals))
        for deal in deals:
            if not is_new_deal(conn, deal.link):
                continue
            # Filter: if price is known, skip if it's above target
            if deal.price is not None and deal.price > item.target_price:
                mark_seen(conn, deal.link, item.id)  # seen but not worth alerting
                continue
            mark_seen(conn, deal.link, item.id)
            log.info(
                "  NEW DEAL [%s] %s%s",
                deal.source,
                deal.title[:60],
                f" (${deal.price:.2f})" if deal.price else "",
            )
            new_web_deals.append((item, deal))

    notify(price_alerts, new_web_deals)
    return price_alerts, new_web_deals


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def notify(
    price_alerts: list[PriceAlert], web_deals: list[tuple[Item, WebDeal]]
) -> None:
    host = os.getenv("SMTP_HOST")
    if not host or (not price_alerts and not web_deals):
        return

    lines: list[str] = []

    if web_deals:
        lines.append("=== New Deals Found ===\n")
        for item, deal in web_deals:
            price_str = f" — ${deal.price:.2f}" if deal.price else ""
            lines.append(f"[{deal.source}] {item.name}{price_str}")
            lines.append(f"  {deal.title}")
            lines.append(f"  {deal.link}\n")

    if price_alerts:
        lines.append("=== Price Drop Alerts ===\n")
        for a in price_alerts:
            lines.append(f"{a.item.name}: ${a.price:.2f}  (score {a.score:.0%})")
            for r in a.reasons:
                lines.append(f"  • {r}")
            lines.append(f"  {a.item.url}\n")

    msg = MIMEText("\n".join(lines))
    total = len(price_alerts) + len(web_deals)
    msg["Subject"] = f"[Deal Alert] {total} deal(s) found"
    msg["From"] = os.getenv("SMTP_USER", "")
    msg["To"] = os.getenv("NOTIFY_EMAIL") or os.getenv("SMTP_USER", "")

    port = int(os.getenv("SMTP_PORT", "465"))
    try:
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
        log.info("Email sent: %d alert(s)", total)
    except Exception as e:
        log.error("Email failed: %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_run(args, conn: sqlite3.Connection) -> None:
    price_alerts, web_deals = check_all(conn)
    total = len(price_alerts) + len(web_deals)
    if not total:
        print("No new deals found.")
        return

    if web_deals:
        print(f"\n{len(web_deals)} new web deal(s):")
        for item, deal in web_deals:
            price_str = f"  ${deal.price:.2f}" if deal.price else ""
            print(f"\n  [{deal.source}] {item.name}{price_str}")
            print(f"  {deal.title}")
            print(f"  {deal.link}")

    if price_alerts:
        print(f"\n{len(price_alerts)} price alert(s):")
        for a in price_alerts:
            print(f"\n  {a.item.name}: ${a.price:.2f}  (score {a.score:.0%})")
            for r in a.reasons:
                print(f"    • {r}")


def cmd_list(args, conn: sqlite3.Connection) -> None:
    items = get_items(conn)
    if not items:
        print("No items tracked.")
        return
    print(f"\n{'ID':>4}  {'Name':<28}  {'Target':>8}  {'Keywords':<24}  URL")
    print("─" * 84)
    for it in items:
        url_str = it.url[:30] + "…" if len(it.url) > 30 else it.url
        kw_str = (
            it.search_terms[:22] + "…" if len(it.search_terms) > 22 else it.search_terms
        )
        print(
            f"{it.id:>4}  {it.name:<28}  ${it.target_price:>7.2f}  {kw_str:<24}  {url_str}"
        )


def cmd_history(args, conn: sqlite3.Connection) -> None:
    items = {it.id: it for it in get_items(conn)}
    if args.id not in items:
        sys.exit(f"No item with id {args.id}")
    it = items[args.id]
    s = price_stats(conn, args.id, days=90)
    seen_count = conn.execute(
        "SELECT COUNT(*) FROM seen_deals WHERE item_id=?", (args.id,)
    ).fetchone()[0]
    print(f"\n{it.name}")
    print(f"  Target:         ${it.target_price:.2f}")
    if s["min_all_time"] is not None:
        print(f"  All-time low:   ${s['min_all_time']:.2f}")
        print(f"  All-time high:  ${s['max_all_time']:.2f}")
    if s["avg"]:
        print(f"  30-day avg:     ${s['avg']:.2f}")
    if s["recent"]:
        print(f"  Last URL price: ${s['recent'][-1]:.2f}")
    print(f"  URL checks:     {s['total_checks']}")
    print(f"  Web deals seen: {seen_count}")


def cmd_add(args, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO items (name, url, target_price, selector, keywords) VALUES (?,?,?,?,?)",
        (
            args.name,
            args.url or "",
            args.target_price,
            args.selector or "",
            args.keywords or "",
        ),
    )
    conn.commit()
    print(f"Added: {args.name}  target=${args.target_price:.2f}")


def cmd_remove(args, conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM prices WHERE item_id=?", (args.id,))
    conn.execute("DELETE FROM seen_deals WHERE item_id=?", (args.id,))
    conn.execute("DELETE FROM items WHERE id=?", (args.id,))
    conn.commit()
    print(f"Removed item {args.id}")


def main() -> None:
    p = argparse.ArgumentParser(description="Price tracker with web deal hunting")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("run", help="Hunt for deals and check URLs (run via cron)")
    sub.add_parser("list", help="List tracked items")

    ph = sub.add_parser("history", help="Show stats for an item")
    ph.add_argument("id", type=int)

    pa = sub.add_parser("add", help="Add an item to track")
    pa.add_argument("name", help="Display name")
    pa.add_argument("target_price", type=float, help="Alert threshold ($)")
    pa.add_argument("--keywords", default="", help="Search terms (default: name)")
    pa.add_argument("--url", default="", help="Direct product URL to also monitor")
    pa.add_argument(
        "--selector", default="", help="CSS selector for URL price (optional)"
    )

    pr = sub.add_parser("remove", help="Remove an item")
    pr.add_argument("id", type=int)

    args = p.parse_args()
    conn = init_db()
    seed_from_json(conn)

    dispatch = {
        "run": cmd_run,
        "list": cmd_list,
        "history": cmd_history,
        "add": cmd_add,
        "remove": cmd_remove,
    }
    if args.cmd in dispatch:
        dispatch[args.cmd](args, conn)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
