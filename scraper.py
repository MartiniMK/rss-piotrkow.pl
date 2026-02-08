#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator


LIST_URL = "https://www.piotrkow.pl/nasze-miasto-t70/aktualnosci-a75"
SITE_ROOT = "https://www.piotrkow.pl"
OUT_FILE = "feed.xml"

MAX_ITEMS = 50
TIMEOUT = 25
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RSSBot/1.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Link do artykułu na piotrkow.pl zwykle kończy się "-r1234"
ARTICLE_PATH_RE = re.compile(r"^/nasze-miasto-t70/aktualnosci-a75/.*-r\d+$", re.IGNORECASE)


def http_get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_date_dd_mm_yyyy(s: str) -> datetime | None:
    s = norm_ws(s)
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    # bez godziny -> stabilnie: południe UTC
    return datetime(yyyy, mm, dd, 12, 0, 0, tzinfo=timezone.utc)


def stable_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def pick_meta_description(soup: BeautifulSoup) -> str:
    for sel in [
        ('meta', {"property": "og:description"}),
        ('meta', {"name": "description"}),
    ]:
        tag = soup.find(sel[0], sel[1])
        if tag and tag.get("content"):
            val = norm_ws(tag["content"])
            if val:
                return val
    return ""


def pick_article_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        t = norm_ws(h1.get_text(" "))
        if t:
            return t
    if soup.title:
        return norm_ws(soup.title.get_text(" "))
    return ""


def pick_article_date(soup: BeautifulSoup) -> datetime | None:
    # szukamy pierwszego wystąpienia daty w formacie dd-mm-rrrr
    text = soup.get_text("\n")
    m = re.search(r"\b(\d{2}-\d{2}-\d{4})\b", text)
    if m:
        return parse_date_dd_mm_yyyy(m.group(1))
    return None


def pick_article_lead(soup: BeautifulSoup) -> str:
    meta = pick_meta_description(soup)
    if meta:
        return meta

    for p in soup.find_all("p"):
        txt = norm_ws(p.get_text(" "))
        if len(txt) >= 60:
            return txt
    return ""


def list_from_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")

    # 1) zbierz linki do artykułów
    links = []
    seen_url = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ARTICLE_PATH_RE.match(href):
            url = urljoin(SITE_ROOT, href)
            if url in seen_url:
                continue
            seen_url.add(url)
            links.append({"url": url, "title_hint": norm_ws(a.get_text(" "))})

    # 2) heurystyka: h2 jako tytuły + data tuż przed + lead tuż po
    items = []
    seen = set()

    for h2 in soup.find_all("h2"):
        title = norm_ws(h2.get_text(" "))
        if not title:
            continue

        # data zwykle przed nagłówkiem
        published = None
        prev = h2
        for _ in range(8):
            prev = prev.find_previous(string=True)
            if not prev:
                break
            d = parse_date_dd_mm_yyyy(norm_ws(str(prev)))
            if d:
                published = d
                break

        # lead zwykle pierwszy <p> po h2
        summary = ""
        nxt = h2
        for _ in range(10):
            nxt = nxt.find_next()
            if not nxt:
                break
            if nxt.name in ("h2", "h1"):
                break
            if nxt.name == "p":
                summary = norm_ws(nxt.get_text(" "))
                if summary:
                    break

        # dopasuj URL najlepiej po identycznym tytule z linku
        url = ""
        for lk in links:
            if lk["title_hint"] and lk["title_hint"] == title:
                url = lk["url"]
                break
        if not url:
            a = h2.find("a", href=True)
            if a and ARTICLE_PATH_RE.match(a["href"].strip()):
                url = urljoin(SITE_ROOT, a["href"].strip())

        if url and url not in seen:
            seen.add(url)
            items.append({
                "url": url,
                "title": title,
                "published": published,
                "summary": summary,
            })

    # 3) fallback: jeśli nie złapaliśmy h2, weź same linki
    if not items and links:
        for lk in links:
            items.append({
                "url": lk["url"],
                "title": lk["title_hint"] or lk["url"],
                "published": None,
                "summary": "",
            })

    return items[:MAX_ITEMS]


def build_feed(items: list[dict]) -> bytes:
    fg = FeedGenerator()
    fg.title("Piotrków Trybunalski — Nasze miasto / Aktualności")
    fg.link(href=LIST_URL, rel="alternate")
    fg.description("RSS wygenerowany automatycznie z piotrkow.pl (Nasze miasto → Aktualności).")
    fg.language("pl")
    fg.generator("GitHub Actions + Python (feedgen)")

    now = datetime.now(timezone.utc)
    fg.lastBuildDate(now)

    # sortuj po dacie malejąco (brak daty na dół)
    def key(it):
        return it.get("published") or datetime(1970, 1, 1, tzinfo=timezone.utc)

    for it in sorted(items, key=key, reverse=True):
        url = it["url"]
        title = it.get("title") or ""
        published = it.get("published")
        summary = it.get("summary") or ""

        # jeśli braki, dociągnij z artykułu
        if not published or not summary or len(summary) < 40 or not title:
            try:
                art_html = http_get(url)
                art = BeautifulSoup(art_html, "lxml")
                if not title:
                    title = pick_article_title(art) or title
                if not published:
                    published = pick_article_date(art) or published
                if not summary or len(summary) < 40:
                    summary = pick_article_lead(art) or summary
            except Exception:
                # feed ma się wygenerować mimo pojedynczych błędów
                pass

        if not title:
            title = url

        fe = fg.add_entry()
        fe.id(stable_id(url))
        fe.title(title)
        fe.link(href=url)
        if published:
            fe.published(published)
            fe.updated(published)
        else:
            fe.updated(now)
        fe.description(summary or title)

    return fg.rss_str(pretty=True)


def main():
    html = http_get(LIST_URL)
    items = list_from_listing(html)
    if not items:
        raise RuntimeError("Nie udało się wyciągnąć żadnych pozycji z listingu.")

    rss_bytes = build_feed(items)
    with open(OUT_FILE, "wb") as f:
        f.write(rss_bytes)

    print(f"OK: wrote {OUT_FILE}, entries={len(items)}")


if __name__ == "__main__":
    main()
