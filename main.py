import argparse
import html
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests

from curl_cffi import requests as cffi_requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DB_PATH = "olx_bot.db"
API_URL = "https://www.olx.ua/api/v1/offers/"
PAGE_SIZE = 50
PAGES = 10
MAX_AGE_HOURS = 24
MAX_ALERTS_PER_RUN = 10
CHECK_INTERVAL = (300, 600)
MIN_PRICE = 100

PRICE_LIMITS = {8: 1300, 16: 2600}

SEARCHES = [
    "ddr4", "ddr4 8gb", "ddr4 16gb",
    "8gb ddr4", "16gb ddr4", "8gb ddr", "16gb ddr",
]

BAD_WORDS = [
    "запчаст", "не працює", "не робоч", "нерабоч", "битий", "бита", "розбит",
    "несправн", "куплю", "обмін", "обмен",
]

DESC_BAD_WORDS = [
    "на запчаст", "не працює", "не робоч", "нерабоч", "битий", "розбит", "несправн",
    "не запуска", "не включа", "не вмика", "під ремонт", "на ремонт", "на відновлення",
]

LAPTOP_RAM_WORDS = [
    "sodimm", "so-dimm", "so dimm", "ноутбуч", "для ноутбука",
    "для ноут", "laptop", "notebook", "260-pin", "260 pin",
]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, sent_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    return conn

class FetchError(Exception):
    pass

_session = None

def get_session():
    global _session
    if _session is None:
        _session = cffi_requests.Session(impersonate="chrome")
        _session.headers.update({"Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8"})
        try:
            _session.get("https://www.olx.ua/uk/", timeout=20)
        except Exception:
            pass
    return _session

def fetch_offers(query, offset):
    params = {"query": query, "offset": offset, "limit": PAGE_SIZE, "sort_by": "created_at:desc"}
    try:
        resp = get_session().get(API_URL, params=params, timeout=20)
    except Exception as e:
        raise FetchError(str(e)) from e
    if resp.status_code >= 400:
        snippet = " ".join(resp.text[:150].split())
        raise FetchError(f"HTTP {resp.status_code} (server: {resp.headers.get('server', '?')}) {snippet}")
    try:
        return resp.json().get("data", [])
    except Exception as e:
        raise FetchError("відповідь не схожа на JSON") from e

def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def parse_offer_price(offer):
    for param in offer.get("params") or []:
        if param.get("key") != "price":
            continue
        value = param.get("value") or {}
        if value.get("currency") == "UAH" and value.get("value") is not None:
            return int(round(value["value"]))
        if value.get("converted_currency") == "UAH" and value.get("converted_value") is not None:
            return int(round(value["converted_value"]))
    return None

def clean_description(offer):
    return re.sub(r"<[^>]+>", " ", offer.get("description") or "").lower()

def ddr_types(text):
    return {f"ddr{d}" for d in re.findall(r"ddr\s*([2345])", text.lower())}

def is_ddr4(title, description):
    return (ddr_types(title) or ddr_types(description)) == {"ddr4"}

def find_ram_size(text):
    t = text.lower()
    kit = re.search(r"(\d{1,2})\s*[x×х]\s*(\d{1,3})\s*(?:gb|гб)\b", t)
    if kit:
        return int(kit.group(1)) * int(kit.group(2))

    single = re.search(r"(?<!\d)(\d{1,3})\s*(?:gb|гб)\b", t)
    return int(single.group(1)) if single else None

def is_laptop_ram(title, description):
    text = f"{title} {description}".lower()
    return any(word in text for word in LAPTOP_RAM_WORDS)

def match_offer(offer, posted, stats):
    def reject(reason):
        stats[reason] = stats.get(reason, 0) + 1
        return None

    title = offer.get("title") or ""
    url = offer.get("url") or ""
    price = parse_offer_price(offer)
    if not url or price is None or price < MIN_PRICE:
        return reject("немає ціни в грн / ціна < MIN_PRICE")
    if any(w in title.lower() for w in BAD_WORDS):
        return reject("погані слова в назві")
    description = clean_description(offer)
    if any(w in description for w in DESC_BAD_WORDS):
        return reject("погані слова в описі")
    if is_laptop_ram(title, description):
        return reject("ноутбучна пам'ять")
    if not is_ddr4(title, description):
        return reject("не DDR4")
    size = find_ram_size(title) or find_ram_size(description)
    limit = PRICE_LIMITS.get(size)
    if limit is None:
        return reject("обсяг не знайдено або не 8/16")
    if price > limit:
        return reject(f"дорожче планки ({size} GB)")
    return {"title": title, "url": url, "price": price, "size": size, "limit": limit, "posted": posted}

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Telegram не налаштований, виводжу в консоль:\n" + text + "\n")
        return True
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    if not resp.ok:
        print("[!] Помилка Telegram:", resp.text)
    return resp.ok

def format_alert(ad):
    def money(v):
        return f"{int(v):,}".replace(",", " ")

    return "\n".join([
        f"🔥 <b>{html.escape(ad['title'], quote=False)}</b>",
        f"{ad['size']} GB DDR4: <b>{money(ad['price'])} грн</b> (планка {money(ad['limit'])} грн)",
        f"Опубліковано: {ad['posted']:%d.%m %H:%M}",
        ad["url"],
    ])

def run_once(conn):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    conn.execute("DELETE FROM sent WHERE sent_at < datetime('now', '-2 days')")

    found = {}
    stats = {}
    for query in SEARCHES:
        offset = 0
        pages = scanned = in_window = 0
        for page in range(PAGES):
            try:
                offers = fetch_offers(query, offset)
            except FetchError as e:
                print(f"[!] «{query}», запит {page + 1}: {e}")
                break
            if not offers:
                break
            offset += len(offers)
            pages += 1

            for offer in offers:
                posted = parse_time(offer.get("created_time"))
                if posted is None:
                    continue
                scanned += 1
                if posted < cutoff:
                    continue
                in_window += 1
                ad = match_offer(offer, posted, stats)
                if ad:
                    found[ad["url"]] = ad

            time.sleep(random.uniform(2, 5))

        print(f"[i] «{query}»: сторінок {pages}, переглянуто {scanned}, за добу {in_window}")

    fresh = [
        ad for url, ad in found.items()
        if not conn.execute("SELECT 1 FROM sent WHERE id = ?", (url,)).fetchone()
    ]
    fresh.sort(key=lambda ad: ad["posted"], reverse=True)

    sent = 0
    for ad in fresh[:MAX_ALERTS_PER_RUN]:
        if send_telegram(format_alert(ad)):
            conn.execute("INSERT OR IGNORE INTO sent (id) VALUES (?)", (ad["url"],))
            sent += 1
    conn.commit()

    print(f"Відсіяно: {stats}")
    print(f"Підходящих: {len(found)}, нових: {len(fresh)}, надіслано: {sent}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="одна перевірка і вихід")
    args = parser.parse_args()

    conn = init_db()
    while True:
        try:
            run_once(conn)
        except Exception as e:
            print(f"[!] Непередбачена помилка: {e}")
        if args.once:
            break
        delay = random.uniform(*CHECK_INTERVAL)
        print(f"Наступна перевірка через {delay / 60:.1f} хв")
        time.sleep(delay)

if __name__ == "__main__":
    main()