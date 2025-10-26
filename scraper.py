#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import logging
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# ------------------ USTAWIENIA ------------------

SECTIONS = [
    "https://www.piotrkow.pl/nasze-miasto-t70/aktualnosci-a75",
    "https://www.piotrkow.pl/gospodarka-t71/aktualnosci-a107",
    "https://www.piotrkow.pl/kultura-i-edukacja-t72/aktualnosci-a108",
    "https://www.piotrkow.pl/sport-i-turystyka-t73/aktualnosci-a109",
]

PAGES_PER_SECTION = 5          # ile stron przeglądać w każdym dziale
MAX_ITEMS = 500                # maksymalna liczba wpisów w RSS
TIMEOUT = 20
MAX_LEAD_LEN = 500             # maksymalna długość leadu (znaki)

# Nagłówki jak zwykła przeglądarka
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}

# Proxy „bez-JS” – pomaga omijać 403/antyboty
def proxied(url: str) -> str:
    # r.jina.ai wymaga http:// (nie https://) wewnątrz ścieżki
    u = url.replace("https://", "http://")
    return f"https://r.jina.ai/{u}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ------------------ POMOCNICZE ------------------

def get(url: str) -> requests.Response | None:
    """Pobierz URL przez proxy, z kilkoma próbami."""
    target = proxied(url)
    for i in range(3):
        try:
            r = SESSION.get(target, timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                return r
            logging.warning("GET %s -> %s", url, r.status_code)
        except requests.RequestException as e:
            logging.warning("GET %s error: %s", url, e)
    return None


def absolute(base: str, href: str) -> str:
    if not href:
        return ""
    return urljoin(base, href)


def unique_id(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s


def truncate(s: str, n: int) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


def parse_date_candidates(soup: BeautifulSoup) -> datetime | None:
    """
    Spróbuj wyciągnąć datę publikacji z meta/struktury.
    Obsługujemy m.in.:
    - meta property="article:published_time"
    - meta name="pubdate"/"date"/"DC.date.issued"
    - elementy z klasami zawierającymi 'date'
    """
    # meta og:article time
    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if meta and meta.get("content"):
        try:
            return dateparser.parse(meta["content"])
        except Exception:
            pass

    for name in ["pubdate", "date", "DC.date.issued", "dc.date", "article:modified_time"]:
        meta = soup.find("meta", attrs={"name": name})
        if meta and meta.get("content"):
            try:
                return dateparser.parse(meta["content"])
            except Exception:
                continue

    # Tekstowe daty na stronie
    # Szukamy czegokolwiek z klasą wskazującą na datę
    date_like = soup.find(
        lambda tag: tag.name in ("time", "span", "div", "p")
        and tag.get("class")
        and any("date" in " ".join(tag.get("class")).lower() for _ in [0])
    )
    if date_like:
        txt = clean_text(date_like.get_text(" ", strip=True))
        try:
            return dateparser.parse(txt, dayfirst=True, fuzzy=True)
        except Exception:
            pass

    # Ostatecznie None – wtedy damy datę pobrania
    return None


def first_paragraph(soup: BeautifulSoup) -> str:
    """
    Weź pierwszy sensowny akapit artykułu.
    Szukamy <article>, <div class*='content'>, <div id*='content'> itp.
    """
    # preferowany kontener
    candidates = []
    for sel in [
        "article",
        "div.article",
        "div.post",
        "div#content",
        "div.content",
        "section.article",
        "div#article",
    ]:
        for c in soup.select(sel):
            candidates.append(c)

    if not candidates:
        candidates = [soup]

    for root in candidates:
        for p in root.find_all("p"):
            txt = clean_text(p.get_text(" ", strip=True))
            if txt and len(txt) > 40:  # unikaj bardzo krótkich
                return txt

    # fallback: meta description
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        return clean_text(meta_desc["content"])

    # fallback 2: og:description
    ogd = soup.find("meta", attrs={"property": "og:description"})
    if ogd and ogd.get("content"):
        return clean_text(ogd["content"])

    return ""


def og_image(soup: BeautifulSoup) -> str | None:
    m = soup.find("meta", attrs={"property": "og:image"})
    if m and m.get("content"):
        return m["content"]
    # pierwszy sensowny <img>
    img = soup.find("img", src=True)
    if img:
        return img["src"]
    return None


def og_title(soup: BeautifulSoup) -> str | None:
    m = soup.find("meta", attrs={"property": "og:title"})
    if m and m.get("content"):
        return m["content"]
    if soup.title and soup.title.string:
        return clean_text(soup.title.string)
    h1 = soup.find("h1")
    if h1:
        return clean_text(h1.get_text(" ", strip=True))
    return None


# ------------------ LISTING: ZBIERANIE LINKÓW ------------------

def collect_links_from_listing(listing_url: str) -> list[str]:
    """
    Z listingu zbierz linki do artykułów.
    Struktura piotrkow.pl bywa różna – działamy heurystycznie:
    - bierzemy wszystkie <a href> prowadzące do tej samej domeny,
    - odrzucamy linki do tej samej podstrony/paginacji,
    - preferujemy linki dłuższe (z dodatkowymi segmentami) i zawierające slug artykułu.
    """
    resp = get(listing_url)
    if not resp:
        logging.warning("No response for listing %s", listing_url)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    base = "https://www.piotrkow.pl"

    anchors = soup.find_all("a", href=True)
    urls = []
    for a in anchors:
        href = a["href"].strip()
        # Relatywne -> absolutne
        href = absolute(base, href)

        # tylko nasza domena
        p = urlparse(href)
        if not p.netloc.endswith("piotrkow.pl"):
            continue

        # pomijamy linki do paginacji/sekcji oraz pliki
        if "page=" in href:
            continue
        if href.endswith((".pdf", ".jpg", ".png", ".webp", ".gif")):
            continue

        # pomiń sam nagłówek działu
        if href.rstrip("/") == listing_url.rstrip("/"):
            continue

        # heurystyka: artykuły mają zwykle dodatkowy segment po /aktualnosci-.../
        # więc wymagamy przynajmniej 1 dodatkowego segmentu po bazowym dziale
        if re.search(r"/aktualnosci-[aA]\d+/", href):
            urls.append(href)
        else:
            # Dopuszczamy inne ścieżki, jeśli wyglądają na artykuły (dłuższy slug)
            # i zawierają „/wiadomosci/”, „/sport/”, „/kultura/” itp.
            if len(p.path.strip("/").split("/")) >= 4:
                urls.append(href)

    # deduplikacja i zachowanie kolejności
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)

    logging.info("Collected %d links from %s", len(out), listing_url)
    return out


def paginated_urls(section_url: str, pages: int) -> list[str]:
    """
    Zwraca listę URL-i listingów z paginacją.
    Najczęściej działa query `?page=2` itd.
    """
    urls = [section_url]
    for p in range(2, pages + 1):
        sep = "&" if "?" in section_url else "?"
        urls.append(f"{section_url}{sep}page={p}")
    return urls


# ------------------ PARSOWANIE ARTYKUŁU ------------------

def parse_article(url: str) -> dict | None:
    resp = get(url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    title = og_title(soup) or url
    title = clean_text(title)

    # data publikacji
    dt = parse_date_candidates(soup)
    if not dt:
        dt = datetime.now(timezone.utc)
    else:
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)

    # lead
    lead = first_paragraph(soup)
    if lead:
        lead = truncate(lead, MAX_LEAD_LEN)
    else:
        lead = title

    # obraz
    img = og_image(soup)
    # Czasem og:image jest względny
    if img and img.startswith("/"):
        img = urljoin(url, img)

    return {
        "title": title,
        "link": url,
        "guid": unique_id(url),
        "pubDate": format_datetime(dt),
        "lead": lead,
        "image": img,
    }


# ------------------ GENEROWANIE RSS ------------------

def build_rss(items: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    last_build = format_datetime(now)

    # atom self-link – zmień tu URL na swój GitHub Pages z repo
    SELF_LINK = "https://<twoj-login>.github.io/<twoje-repo>/feed.xml"

    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss xmlns:media="http://search.yahoo.com/mrss/" xmlns:atom="http://www.w3.org/2005/Atom" version="2.0">')
    parts.append("<channel>")
    parts.append("<title>Piotrkow.pl – Aktualności (miasto, gospodarka, kultura, sport)</title>")
    parts.append("<link>https://www.piotrkow.pl/</link>")
    parts.append("<description>Zbiorczy RSS z działów Aktualności (Miasto/Gospodarka/Kultura/Sport) portalu piotrkow.pl.</description>")
    parts.append(f'<atom:link rel="self" type="application/rss+xml" href="{SELF_LINK}"/>')
    parts.append("<language>pl-PL</language>")
    parts.append(f"<lastBuildDate>{last_build}</lastBuildDate>")
    parts.append("<ttl>60</ttl>")

    for it in items:
        title = it["title"]
        link = it["link"]
        guid = it["guid"]
        pub = it["pubDate"]
        lead = it["lead"]
        img = it.get("image")

        # description: miniatura + lead w HTML
        desc_html = ""
        if img:
            desc_html += f'<p><img src="{img}" alt="miniatura"/></p>'
        if lead:
            desc_html += f"<p>{lead}</p>"

        parts.append("<item>")
        parts.append("<title><![CDATA[" + title + "]]></title>")
        parts.append(f"<link>{link}</link>")
        parts.append(f'<guid isPermaLink="false">{guid}</guid>')
        parts.append(f"<pubDate>{pub}</pubDate>")
        parts.append("<description><![CDATA[" + desc_html + "]]></description>")
        if img:
            parts.append(f'<enclosure url="{img}" type="image/*"/>')
            parts.append(f'<media:content url="{img}" medium="image"/>')
            parts.append(f'<media:thumbnail url="{img}"/>')
        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")
    return "\n".join(parts)


# ------------------ MAIN ------------------

def main():
    logging.info("Start collecting links…")
    all_links = []

    for section in SECTIONS:
        for listing in paginated_urls(section, PAGES_PER_SECTION):
            logging.info("Listing -> %s", listing)
            links = collect_links_from_listing(listing)
            all_links.extend(links)

    # deduplikacja i ucinanie do MAX_ITEMS*2 (bo nie każdy link może się sparsować)
    seen = set()
    uniq = []
    for u in all_links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)

    logging.info("Collected %d unique candidates", len(uniq))

    items = []
    for u in uniq:
        if len(items) >= MAX_ITEMS:
            break
        art = parse_article(u)
        if not art:
            continue
        items.append(art)

    logging.info("Parsed %d items", len(items))

    rss = build_rss(items)
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)

    logging.info("Wrote feed.xml (%d items)", len(items))


if __name__ == "__main__":
    main()
