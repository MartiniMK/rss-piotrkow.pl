#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import sys
import time
import html
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from email.utils import format_datetime
from xml.etree.ElementTree import Element, SubElement, tostring

# ================== KONFIG ==================
BASE = "https://www.piotrkow.pl"
LISTING_URLS = [
    "https://www.piotrkow.pl/nasze-miasto-t70/aktualnosci-a75",
    "https://www.piotrkow.pl/gospodarka-t71/aktualnosci-a107",
    "https://www.piotrkow.pl/kultura-i-edukacja-t72/aktualnosci-a108",
    "https://www.piotrkow.pl/sport-i-turystyka-t73/aktualnosci-a109",
]

# Ile stron listingu maksymalnie próbować na każdej kategorii.
# Jeśli nie ma paginacji, scraper zostanie na stronie 1.
MAX_PAGES_PER_LIST = 5

# Docelowa liczba itemów w RSS (twardy limit, żeby nie rosło w nieskończoność)
MAX_ITEMS = 300

# Długość leadu (opieka nad czytelnością w czytnikach)
MAX_LEAD = 400

# WAŻNE: Ustaw prawidłowy self-link RSS:
SELF_LINK = "https://martinimk.github.io/rss-piotrkow.pl/feed.xml"
CHANNEL_TITLE = "Piotrkow.pl – Zbiorczy RSS"
CHANNEL_DESC = "Automatyczny agregat: Aktualności (Miasto, Gospodarka, Kultura, Sport)"
CHANNEL_LINK = "https://www.piotrkow.pl/"

# ===========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# Polskie miesiące -> numer
PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10, "pazdziernika": 10,
    "listopada": 11, "grudnia": 12
}

def requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        # Mocny, „realny” UA – wiele stron blokuje GitHub Actions na domyślnych UA
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/127.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
    })
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

SESSION = requests_session()

def get(url: str) -> requests.Response | None:
    for attempt in range(1, 6):
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 200:
                return r
            logging.warning("GET %s -> %s", url, r.status_code)
            # delikatne wytchnienie przy 403/5xx
            time.sleep(1.5 * attempt)
        except requests.RequestException as e:
            logging.warning("GET %s failed (%s), attempt %s", url, e, attempt)
            time.sleep(1.5 * attempt)
    return None

def discover_pagination_urls(first_url: str, html_doc: str) -> list[str]:
    """Znajdź linki do kolejnych stron paginacji; fallback do domyślnej heurystyki '?strona=' / '?page='."""
    urls = [first_url]
    soup = BeautifulSoup(html_doc, "lxml")

    # Szukamy linków paginacji (a[href] z parametrami strony)
    a_candidates = soup.select('a[href]')
    seen = set(urls)
    for a in a_candidates:
        href = a.get("href", "")
        # typowe ścieżki na CMS-ach
        if "?" in href and ("page=" in href or "strona=" in href):
            full = urljoin(BASE, href)
            if full not in seen:
                urls.append(full)
                seen.add(full)

    # Jeśli nic nie znaleziono – spróbujmy heurystyki page/strona (pójdziemy max do MAX_PAGES_PER_LIST)
    if len(urls) == 1:
        for p in range(2, MAX_PAGES_PER_LIST + 1):
            for param in ("page", "strona"):
                sep = "&" if "?" in first_url else "?"
                candidate = f"{first_url}{sep}{param}={p}"
                urls.append(candidate)

    # Utnij do limitu
    return urls[:MAX_PAGES_PER_LIST]

def is_article_href(href: str) -> bool:
    """Heurystyka rozpoznania linku do artykułu (a nie do listy czy strony kategorii)."""
    if not href:
        return False
    if href.startswith("#"):
        return False
    if "aktualnosci-a" in href:
        # to listing
        return False
    # linki ze slugiem /…/…-aNN/… lub /artykul/…
    patterns = [
        r"/nasze-miasto-t70/.*",
        r"/gospodarka-t71/.*",
        r"/kultura-i-edukacja-t72/.*",
        r"/sport-i-turystyka-t73/.*",
        r"/artykul/.*",
    ]
    return any(re.search(p, href) for p in patterns)

def extract_links_from_listing(url: str, html_doc: str) -> list[str]:
    soup = BeautifulSoup(html_doc, "lxml")
    links = set()

    # 1) mocno zawężone selektory (typowe listy z kafelkami)
    selectors = [
        "a.image-tile-overlay",               # kafle duże
        ".image-tile a",                      # kafle mniejsze
        ".news-listing-item a[href]",         # listy poziome
        ".latest-news__wrapper a[href]",      # „przeczytaj jeszcze”
        "a[href^='https://www.piotrkow.pl/']",# dowolne bezwzględne
        "a[href^='/']"                        # fallback – względne
    ]
    for sel in selectors:
        for a in soup.select(sel):
            href = a.get("href", "")
            if is_article_href(href):
                links.add(urljoin(BASE, href))

    # 2) dodatkowy fallback: każde <a> z domeną i wzorcem ścieżek
    if not links:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if is_article_href(href):
                links.add(urljoin(BASE, href))

    logging.info("Listing links @ %s -> %s", url, len(links))
    return list(links)

def parse_pl_date(text: str) -> datetime | None:
    """
    Próbujemy sparsować polskie daty typu:
    'poniedziałek, 13 października 2025, 14:32'
    lub '13 października 2025 14:32' itp.
    """
    text = html.unescape(text).strip()
    # wytnij prefiksy 'Opublikowano:' itp.
    text = re.sub(r"(?i)Opublikowano:\s*", "", text)
    text = re.sub(r"(?i)Aktualizacja:\s*.*$", "", text)

    # Formaty z nazwą miesiąca
    m = re.search(r"(\d{1,2})\s+([A-Za-ząćęłńóśźż]+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if m:
        day = int(m.group(1))
        mon_name = m.group(2).lower()
        year = int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        mon = PL_MONTHS.get(mon_name)
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, tzinfo=timezone.utc)
            except ValueError:
                pass

    # ISO w meta (na wszelki)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def first_sentence(s: str, max_len: int = 400) -> str:
    s = " ".join(s.split())
    if len(s) <= max_len:
        # spróbuj zakończyć pełnym zdaniem
        end = max(s.rfind(". "), s.rfind("… "), s.rfind("! "), s.rfind("? "))
        if end != -1 and end + 1 >= max_len * 0.6:
            return s[:end+1]
        return s
    # utnij ~po kropce, jeśli jest
    cut = s[:max_len]
    end = cut.rfind(". ")
    if end >= max_len * 0.5:
        return cut[:end+1]
    return cut.rstrip() + "…"

def extract_article(url: str) -> dict | None:
    r = get(url)
    if not r:
        logging.warning("No response for article %s", url)
        return None
    soup = BeautifulSoup(r.text, "lxml")

    # Tytuł
    title = None
    ogt = soup.find("meta", property="og:title")
    if ogt and ogt.get("content"):
        title = ogt["content"].strip()
    if not title:
        h1 = soup.find(["h1", "h2"])
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        title = url

    # Obraz
    image = None
    ogimg = soup.find("meta", property="og:image")
    if ogimg and ogimg.get("content"):
        image = urljoin(url, ogimg["content"].strip())

    # Data publikacji
    pub = None
    for meta_name in ("article:published_time", "og:published_time", "article:modified_time"):
        m = soup.find("meta", property=meta_name)
        if m and m.get("content"):
            pub = parse_pl_date(m["content"])
            if pub:
                break
    if not pub:
        # time datetime
        t = soup.find("time")
        if t and (t.get("datetime") or t.get_text(strip=True)):
            pub = parse_pl_date(t.get("datetime") or t.get_text(strip=True))
    if not pub:
        # tekst "Opublikowano: ..."
        cand = soup.find(string=re.compile(r"(?i)opublikowano"))
        if cand:
            pub = parse_pl_date(str(cand))
    if not pub:
        pub = datetime.now(timezone.utc)

    # Lead (wstęp)
    lead = None
    # 1) og:description
    ogd = soup.find("meta", property="og:description")
    if ogd and ogd.get("content"):
        lead = ogd["content"].strip()

    # 2) pierwszy sensowny <p> w treści artykułu
    if not lead:
        # typowe kontenery
        containers = soup.select(
            ".article, article, .content, #content, .page-content, .text, .entry-content, .post-content"
        )
        ps = []
        for c in containers or []:
            ps.extend(c.find_all("p"))
        if not ps:
            ps = soup.find_all("p")
        for p in ps:
            txt = p.get_text(" ", strip=True)
            if txt and len(txt) > 30:
                lead = txt
                break

    if lead:
        lead = first_sentence(lead, MAX_LEAD)

    return {
        "url": url,
        "title": title,
        "image": image,
        "pubdate": pub,
        "lead": lead,
    }

def build_feed(items: list[dict]) -> bytes:
    rss = Element("rss", {
        "version": "2.0",
        "xmlns:media": "http://search.yahoo.com/mrss/",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
    })
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = CHANNEL_TITLE
    SubElement(channel, "link").text = CHANNEL_LINK
    SubElement(channel, "description").text = CHANNEL_DESC
    atom = SubElement(channel, "{http://www.w3.org/2005/Atom}link", {
        "rel": "self",
        "type": "application/rss+xml",
        "href": SELF_LINK,
    })
    SubElement(channel, "language").text = "pl-PL"
    SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    SubElement(channel, "ttl").text = "60"

    for it in items[:MAX_ITEMS]:
        i = SubElement(channel, "item")
        t = SubElement(i, "title")
        t.text = f"<![CDATA[ {it['title']} ]]>"
        SubElement(i, "link").text = it["url"]
        SubElement(i, "guid", {"isPermaLink": "false"}).text = \
            re.sub(r"[^a-f0-9]", "", re.sub(r"^https?://", "", it["url"].lower()))[:40]
        SubElement(i, "pubDate").text = format_datetime(it["pubdate"])
        # description z obrazkiem + leadem
        desc_html = ""
        if it.get("image"):
            desc_html += f'<p><img src="{html.escape(it["image"])}" alt="miniatura"/></p>'
        if it.get("lead"):
            desc_html += f"<p>{html.escape(it['lead'])}</p>"
        else:
            desc_html += f"<p>{html.escape(it['title'])}</p>"
        d = SubElement(i, "description")
        d.text = f"<![CDATA[ {desc_html} ]]>"
        if it.get("image"):
            SubElement(i, "enclosure", {"url": it["image"], "type": "image/*"})
            SubElement(i, "{http://search.yahoo.com/mrss/}content",
                       {"url": it["image"], "medium": "image"})
            SubElement(i, "{http://search.yahoo.com/mrss/}thumbnail",
                       {"url": it["image"]})

    return tostring(rss, encoding="utf-8", xml_declaration=True)

def collect_all_articles() -> list[dict]:
    all_urls = set()
    # Zbierz linki z list, z paginacją
    for base_url in LISTING_URLS:
        logging.info("Listing 1 -> %s", base_url)
        r = get(base_url)
        if not r:
            logging.warning("No response for %s", base_url)
            continue
        page_urls = discover_pagination_urls(base_url, r.text)
        for pu in page_urls:
            logging.info("Parse listing page -> %s", pu)
            rr = get(pu)
            if not rr:
                logging.warning("No response for page %s", pu)
                continue
            links = extract_links_from_listing(pu, rr.text)
            for L in links:
                all_urls.add(L)

    logging.info("Collected %s unique article URLs", len(all_urls))

    # Ściągnij szczegóły
    items: list[dict] = []
    for idx, u in enumerate(sorted(all_urls)):
        art = extract_article(u)
        if not art:
            continue
        items.append(art)
        # lekki oddech by nie triggerować zabezpieczeń
        if (idx + 1) % 5 == 0:
            time.sleep(0.8)

    # Sortuj malejąco po dacie
    items.sort(key=lambda x: x["pubdate"], reverse=True)
    return items

def main():
    items = collect_all_articles()
    xml_bytes = build_feed(items)
    with open("feed.xml", "wb") as f:
        f.write(xml_bytes)
    logging.info("Wrote feed.xml (%s items)", len(items))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception("Fatal error: %s", e)
        sys.exit(1)
