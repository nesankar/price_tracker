# Price Tracker

Watches product pages for price drops and alerts you when deals hit. Works on most retail sites without needing to configure CSS selectors, using a four-strategy price extraction cascade.

## How it works

**Web deal hunting** — each run searches Slickdeals and Reddit r/deals RSS feeds for your keywords. New posts that mention a price at or below your target get surfaced immediately. Already-seen posts are tracked in SQLite so you're never double-alerted.

**Direct URL monitoring** (optional) — if you also supply a product URL, the tracker fetches it and extracts the price using a four-strategy cascade:
1. JSON-LD `schema.org/Offer` — stable structured data most retailers publish
2. `<meta>` OpenGraph/product price tags
3. CSS selector you specify
4. Heuristic — scans price-labelled elements for the most common `$X.XX` value

URL price alerts also factor in 30-day average drops (≥5%) and all-time lows.

## Quick start

```bash
git clone <repo>
cd price_tracker
uv sync

cp items.example.json items.json
# edit items.json — add your products and target prices

uv run deal_tracker.py run  # test it
```

Then add to cron (`crontab -e`) to run every 6 hours:

```
0 */6 * * * cd /path/to/price_tracker && source .env && uv run deal_tracker.py run
```

## Adding items

Edit `items.json` (only `name`, `target_price`, and `keywords` are required):

```json
[
  {
    "name": "Sony WH-1000XM5",
    "target_price": 250.00,
    "keywords": "sony wh-1000xm5 headphones"
  },
  {
    "name": "iPad Air",
    "target_price": 499.00,
    "keywords": "ipad air m2",
    "url": "https://www.bestbuy.com/...",
    "selector": ""
  }
]
```

`keywords` drives the RSS feed search — be specific enough to avoid noise. `url` and `selector` are optional; add them if you also want to monitor a specific product page directly.

Items are imported from `items.json` into SQLite on first run. After that, use the CLI:

```bash
uv run deal_tracker.py add "AirPods Pro" 179.99 --keywords "airpods pro 2nd gen"
uv run deal_tracker.py add "LG C3 65" 999.00 --keywords "lg c3 65 inch oled" --url "https://..."
uv run deal_tracker.py remove 3
```

## CLI reference

```
uv run deal_tracker.py run                                         # hunt deals + check URLs
uv run deal_tracker.py list                                        # show tracked items
uv run deal_tracker.py history <id>                                # price stats for one item
uv run deal_tracker.py add <name> <price> --keywords <terms>      # add an item
uv run deal_tracker.py add <name> <price> --keywords <terms> --url <url>  # with URL monitoring
uv run deal_tracker.py remove <id>                                 # remove an item
```

## Email alerts

Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
# edit .env

# then source it before running:
source .env && python deal_tracker.py schedule
```

Gmail: use an [App Password](https://myaccount.google.com/apppasswords) (not your account password). Enable 2FA first.

## Notes

- **Amazon** actively blocks scrapers. For Amazon, use the [Keepa API](https://keepa.com/#!api) instead — free tier covers most personal use.
- **Rate limiting** — the default 6h interval is safe for most sites. Don't go below 1h or you risk IP blocks.
- **Selector rot** — selectors break when sites redesign. If a price stops showing up, re-inspect the element or leave `selector` empty to rely on auto-detection.

## Dependencies

Managed via [uv](https://docs.astral.sh/uv/). Install uv, then `uv sync` handles everything.
