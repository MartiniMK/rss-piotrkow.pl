#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator


LIST_URL = "https://www.piotrkow.pl/nasze-miasto-t70/aktualnosci-a75"
SITE_ROOT = "https://www.piotrkow.pl"
OUT_FILE = "feed.xml"

MAX_ITEMS = 40
FETCH_ARTICLES = 40
TIMEOUT = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RSSBot/1.1; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ART_R_RE = re.compile(r"-r\d+\b", re.IGNORECASE)
SECTION_RE = re.compile(r"/nasze-miasto-t70/aktualnosci-a75/", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{2}-\d{2}-\d{4})\b")


def http_get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.text


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_date_dd_mm_yyyy(s: str):
    s = norm_ws(s)
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    return datetime(yyyy, mm, dd, 12, 0, 0, tzinfo=timezone.utc)


def stable_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def pick_meta(soup: BeautifulSoup, name=None, prop=None) -> str:
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return norm_ws(tag["content"])
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return norm_ws(tag["content"])
    return ""


def extract_article(url: str) -> dict:
    html = http_get(url)
    soup = BeautifulSoup(html, "lxml")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = norm_ws(h1.get_text(" "))
    if not title and soup.title:
        title = norm_ws(soup.title.get_text(" "))

    published = None
    txt = soup.get_text("\n")
    m = DATE_RE.search(txt)
    if m:
        published = parse_date_dd_mm_yyyy(m.group(1))

    lead = pick_meta(soup, prop="og:description") or pick_meta(soup, name="description")

    if not lead or len(lead) < 40:
        for p in soup.find_all("p"):
            ptxt = norm_ws(p.get_text(" "))
            if len(ptxt) >= 60:
                lead = ptxt
                break

    return {
        "url": url,
        "title": title or url,
        "published": published,
        "summary": lead or (title or url),
    }


def extract_listing_urls(list_html: str) -> list[str]:
    soup = BeautifulSoup(list_html, "lxml")

    urls = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        full = urljoin(SITE_ROOT, href)

        host = urlparse(full).netloc.lower()
        if "piotrkow.pl" not in host:
            continue

        if not SECTION_RE.search(full):
            continue
        if not ART_R_RE.search(full):
            continue

        if full not in seen:
            seen.add(full)
            urls.append(full)

    return urls


def build_feed(entries: list[dict]) -> bytes:
    fg = FeedGenerator()
    fg.title("Piotrków Trybunalski — Nasze miasto / Aktualności")
    fg.link(href=LIST_URL, rel="alternate")
    fg.description("RSS generowany automatycznie z piotrkow.pl.")
    fg.language("pl")
    fg.generator("GitHub Actions + Python (feedgen)")

    now = datetime.now(timezone.utc)
    fg.lastBuildDate(now)

    def sort_key(e):
        return e.get("published") or datetime(1970, 1, 1, tzinfo=timezone.utc)

    for e in sorted(entries, key=sort_key, reverse=True)[:MAX_ITEMS]:
        fe = fg.add_entry()
        fe.id(stable_id(e["url"]))
        fe.title(e["title"])
        fe.link(href=e["url"])
        if e.get("published"):
            fe.published(e["published"])
            fe.updated(e["published"])
        else:
            fe.updated(now)
        fe.description(e.get("summary") or e["title"])

    return fg.rss_str(pretty=True)


def main():
    list_html = http_get(LIST_URL)
    urls = extract_listing_urls(list_html)

    if not urls:
        raise RuntimeError("Brak linków artykułów na listingu (parser nie trafił w HTML).")

    urls = urls[:FETCH_ARTICLES]

    entries = []
    for u in urls:
        try:
            entries.append(extract_article(u))
        except Exception as ex:
            print(f"[WARN] {u}: {ex}")

    if not entries:
        raise RuntimeError("Wyciągnięto URL-e, ale nie udało się sparsować żadnego artykułu.")

    rss = build_feed(entries)
    with open(OUT_FILE, "wb") as f:
        f.write(rss)

    print(f"OK: wrote {OUT_FILE}, urls={len(urls)}, entries={len(entries)}")


if __name__ == "__main__":
    main()
