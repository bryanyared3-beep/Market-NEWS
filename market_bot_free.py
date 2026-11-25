#!/usr/bin/env python3
"""
Free market news bot: RSS feeds -> Gemini (free tier) -> Telegram.

Usage:
  python market_bot_free.py digest   # full update of the last few hours
  python market_bot_free.py alert    # only sends if something major is found

Env vars:
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  GEMINI_MODEL (optional, default below; change it if Google retires the model)
Install:
  pip install feedparser requests
"""
import calendar
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys
import time

import feedparser
import requests

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = pathlib.Path(__file__).with_name("seen.json")
UA = "Mozilla/5.0 (compatible; market-news-bot/1.0)"

# Free RSS feeds. Some may change or break over time; failures are skipped.
FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
}

# Cheap pre-filter so Gemini is only called when something might be big.
BIG_WORDS = re.compile(
    r"\b(?:crash\w*|plunge\w*|plummet\w*|surge\w*|soar\w*|tumble\w*|record high|record low|"
    r"hack\w*|exploit\w*|bankrupt\w*|default\w*|emergency|rate cut|rate hike|rate decision|"
    r"halt\w*|collaps\w*|sanction\w*|tariff\w*|war|attack\w*|invasion|ban|banned|lawsuit|"
    r"sec|etf|liquidat\w*|outage|downgrade\w*|recession|shock\w*|bank run|circuit breaker|"
    r"flash crash|depeg\w*|insolven\w*|fed|powell|inflation|cpi|payrolls)\b",
    re.IGNORECASE,
)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def fetch_items(hours: float) -> list:
    cutoff = time.time() - hours * 3600
    items, seen_titles = [], set()
    for src, url in FEEDS.items():
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            feed = feedparser.parse(r.content)
        except Exception as exc:  # noqa: BLE001
            print(f"Feed failed ({src}): {exc}")
            continue
        for e in feed.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            t = calendar.timegm(ts) if ts else time.time()
            title = clean(e.get("title", ""))
            key = title.lower()
            if t < cutoff or not title or key in seen_titles:
                continue
            seen_titles.add(key)
            link = e.get("link", "")
            items.append(
                {
                    "id": hashlib.md5((link or title).encode()).hexdigest()[:12],
                    "src": src,
                    "title": title,
                    "summary": clean(e.get("summary", ""))[:220],
                    "link": link,
                    "t": t,
                }
            )
    items.sort(key=lambda x: -x["t"])
    return items


def gemini(prompt: str, json_mode: bool = False) -> str:
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    last = "unknown"
    # Try the main model first, then fall back to others if it is overloaded or retired.
    for model in dict.fromkeys([MODEL, "gemini-3.6-flash", "gemini-3.5-flash-lite"]):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=90)
            except requests.RequestException as exc:
                last = str(exc)
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504):  # busy or rate limited: wait, retry
                last = f"{model}: HTTP {r.status_code}"
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 404:  # model not available: try the next one
                last = f"{model}: HTTP 404"
                break
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip()
    raise RuntimeError(f"Gemini unavailable after retries ({last})")


def send_telegram(text: str) -> None:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 4000:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": chunk, "disable_web_page_preview": True},
            timeout=30,
        )
        r.raise_for_status()


def load_seen() -> list:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return []


def save_seen(ids: list) -> None:
    STATE_FILE.write_text(json.dumps(ids[-2000:]))


def age(t: float) -> str:
    mins = int((time.time() - t) / 60)
    return f"{mins}m ago" if mins < 90 else f"{mins // 60}h ago"


def now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%A %d %B %Y, %H:%M UTC")


DIGEST_PROMPT = """You are a market news analyst. Now: {now}.
Below are recent headlines from financial news feeds. Use ONLY these headlines; never invent news, prices or numbers.
The outlooks are inferred from headlines alone, not from live prices.

Write a Telegram message in PLAIN TEXT (no markdown, no asterisks, no tables), under 3000 characters.
Start with: "Market update {now}"
Pick the 5-8 most market-moving stories (merge duplicates), highest impact first. For each:
<emoji> Headline in your own words
Affected: tickers/coins/sectors
Outlook: 🟢 Bullish / 🔴 Bearish / ⚪ Neutral / 🟡 Mixed | Impact: High/Medium/Low | Horizon: short/medium/long
Why: one or two sentences on cause and effect
Source: name, age
If something is truly major, put it first under "🚨 BREAKING".
End with: Overall mood (Bullish/Bearish/Neutral), top 3 things to watch.
Last line: "Not financial advice."

Headlines:
{headlines}
"""

ALERT_PROMPT = """You are a market news monitor. Now: {now}.
Below are numbered headlines that may be important. Pick ONLY the ones that are truly major market events:
a surprise central bank decision, a major index moving more than 3%, a top-10 crypto moving more than 8% in a day,
an exchange hack or collapse, a major bankruptcy, a large regulatory ruling, war escalation or sanctions that move
markets, or an unexpected data shock. Routine news does NOT count. Merge headlines about the same event into one alert.
Use ONLY the information given; never invent numbers. If nothing qualifies, return {{"alerts": []}}.

Respond with ONLY JSON:
{{"alerts": [{{"n": 1, "affected": "...", "outlook": "Bullish|Bearish|Neutral|Mixed",
"why": "1-2 sentences", "confidence": "High|Medium|Low"}}]}}

Headlines:
{headlines}
"""


def run_digest(hours: float = 5) -> None:
    items = fetch_items(hours)[:45]
    if not items:
        print("No fresh headlines; skipping digest.")
        return
    lines = "\n".join(f"- [{i['src']}, {age(i['t'])}] {i['title']}. {i['summary']}" for i in items)
    text = gemini(DIGEST_PROMPT.format(now=now_str(), headlines=lines))
    send_telegram(text)


def run_alert(hours: float = 3) -> None:
    seen = load_seen()
    seen_set = set(seen)
    fresh = [i for i in fetch_items(hours) if i["id"] not in seen_set]
    candidates = [i for i in fresh if BIG_WORDS.search(i["title"])][:25]
    if not candidates:
        print("No candidates.")
        return
    lines = "\n".join(
        f"{n}. [{i['src']}, {age(i['t'])}] {i['title']}. {i['summary']}" for n, i in enumerate(candidates, 1)
    )
    raw = gemini(ALERT_PROMPT.format(now=now_str(), headlines=lines), json_mode=True)
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except ValueError:
        print("Could not parse JSON:", raw[:200])
        return
    icons = {"Bullish": "🟢", "Bearish": "🔴", "Neutral": "⚪", "Mixed": "🟡"}
    for a in data.get("alerts", []):
        try:
            item = candidates[int(a["n"]) - 1]
        except (KeyError, ValueError, IndexError):
            continue
        send_telegram(
            f"🚨 BREAKING: {item['title']}\n"
            f"Affected: {a.get('affected', '-')}\n"
            f"Outlook: {icons.get(a.get('outlook'), '')} {a.get('outlook', '-')} "
            f"(confidence: {a.get('confidence', '-')})\n"
            f"Why: {a.get('why', '-')}\n"
            f"Source: {item['src']}, {age(item['t'])}\n{item['link']}\n"
            f"Not financial advice."
        )
    save_seen(seen + [i["id"] for i in candidates])


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "digest"
    if mode == "digest":
        run_digest()
    elif mode == "alert":
        run_alert()
    else:
        sys.exit("Usage: python market_bot_free.py [digest|alert]")
