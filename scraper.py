#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import html
import os
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import tz


BASE_URL = "https://www.piotrkow.pl"
HOMEPAGE_URL = "https://www.piotrkow.pl"
NEWS_PATH_FRAGMENT = "/nasze-miasto-t70/aktualnosci-a75/"
USER_AGENT = "piotrkowpl-rssbot/1.0 (+https://github.com/; contact: you@example.com)"


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s


def fetch_html(session: requests.Session, url: str, timeout: int = 30) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def is_valid_article_url(u: str) -> bool:
    # Akceptujemy URL-e z aktualnościami; zwykle mają końcówkę -rNNNN
    if NEWS_PATH_FRAGMENT not in u:
        return False
    # odfiltruj PDF-y i inne akcje
    if u.lower().endswith(".pdf"):
        return False
    # Heurystyka: artykuły zwykle mają "-r" z numerem na końcu
    if re.search(r"-r\d+/?$", u):
        return True
    # fallback: jeśli jest w ścieżce aktualności i nie jest katalogiem
    parsed = urlparse(u)
    if parsed.path.startswith(NEWS_PATH_FRAGMENT) and len(parsed.path.strip("/").split("/")) >= 4:
        return True
    return False


def extract_article_links_from_homepage(home_html: str, limit: int = 30) -> List[str]:
    soup = BeautifulSoup(home_html, "lxml")

    links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        abs_url = urljoin(BASE_URL, href)
        if is_valid_article_url(abs_url):
            links.append(abs_url)

    # unikalne, zachowując kolejność
    seen = set()
    uniq = []
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    return uniq[:limit]


def pick_first(soup: BeautifulSoup, selectors: List[str]):
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            return node
    return None


def parse_date_from_text(text: str) -> Optional[datetime]:
    """
    Na piotrkow.pl data często jest w formacie dd-mm-YYYY (np. 09-01-2026).
    Czasu zwykle brak, więc ustawiamy 00:00 w strefie PL.
    """
    m = re.search(r"\b(\d{2})-(\d{2})-(\d{4})\b", text)
    if not m:
        return None
    dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
    try:
        dt_naive = datetime(int(yyyy), int(mm), int(dd), 0, 0, 0)
    except ValueError:
        return None
    pl_tz = tz.gettz("Europe/Warsaw")
    dt_local = dt_naive.replace(tzinfo=pl_tz)
    return dt_local


def clean_content_node(node):
    # usuń śmieci: skrypty, style, formularze (captcha), przyciski, menu itd.
    for sel in ["script", "style", "noscript", "form", "nav", "aside"]:
        for x in node.select(sel):
            x.decompose()

    # często na stronach są sekcje typu "zgłoś błąd / wyślij / drukuj" — wycinamy po klasach/tekstach
    # (robimy delikatnie: szukamy kontenerów z linkami pdf/drukuj/wyślij)
    for x in node.find_all(["a", "div", "span", "p", "li"]):
        t = (x.get_text(" ", strip=True) or "").lower()
        if t in {"zgłoś błąd", "wyślij", "drukuj", "pdf"}:
            # usuń rodzica jeśli to mały bloczek akcji
            parent = x.parent
            if parent and parent.name in {"div", "ul", "p"}:
                parent.decompose()

    return node


def node_to_html_fragment(node) -> str:
    # Upewniamy się, że obrazki mają absolutne URL-e
    for img in node.select("img[src]"):
        img["src"] = urljoin(BASE_URL, img["src"])
    for a in node.select("a[href]"):
        a["href"] = urljoin(BASE_URL, a["href"])

    frag = str(node)
    return frag


def parse_article(session: requests.Session, url: str) -> Tuple[str, datetime, str, Optional[str]]:
    """
    Zwraca: (title, pubdate, content_html, first_image_url)
    """
    html_text = fetch_html(session, url)
    soup = BeautifulSoup(html_text, "lxml")

    title_node = pick_first(soup, ["h1", "meta[property='og:title']"])
    if title_node is None:
        title = url
    else:
        if title_node.name == "meta":
            title = title_node.get("content", "").strip() or url
        else:
            title = title_node.get_text(" ", strip=True) or url

    # data: próbujemy z elementów typowych, a jak nie to regexem z tekstu okolicy nagłówka
    date_node = pick_first(soup, [".news-inside-date", ".date", "time", "meta[property='article:published_time']"])
    pub_dt = None
    if date_node:
        if date_node.name == "meta":
            # ISO czasem bywa w og:title/published_time
            content = date_node.get("content", "").strip()
            try:
                # spróbuj ISO
                pub_dt = datetime.fromisoformat(content.replace("Z", "+00:00"))
            except Exception:
                pub_dt = parse_date_from_text(content)
        else:
            pub_dt = parse_date_from_text(date_node.get_text(" ", strip=True))

    if pub_dt is None:
        # fallback: szukamy dd-mm-YYYY w całym dokumencie (ale to ryzyko, więc ograniczamy do okolicy body)
        pub_dt = parse_date_from_text(soup.get_text("\n", strip=True))

    if pub_dt is None:
        # ostateczność: teraz
        pub_dt = datetime.now(tz.gettz("Europe/Warsaw"))

    # treść artykułu: kilka popularnych kontenerów + fallback na main
    content_node = pick_first(soup, [
        ".news-inside-content",
        ".news-inside-text",
        ".article-content",
        "article",
        "main",
        "#content",
    ])
    if content_node is None:
        content_node = soup.body or soup

    content_node = clean_content_node(content_node)

    # wyciągnij pierwsze zdjęcie z treści (opcjonalnie do <enclosure> / og:image)
    first_img = None
    img = content_node.select_one("img[src]")
    if img:
        first_img = urljoin(BASE_URL, img.get("src"))

    content_html = node_to_html_fragment(content_node)

    return title, pub_dt, content_html, first_img


def build_rss(
    items: List[Tuple[str, str, datetime, str, Optional[str]]],
    feed_title: str,
    feed_link: str,
    feed_description: str,
) -> str:
    now = datetime.now(timezone.utc)

    # RSS 2.0
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss version="2.0">')
    parts.append("<channel>")
    parts.append(f"<title>{html.escape(feed_title)}</title>")
    parts.append(f"<link>{html.escape(feed_link)}</link>")
    parts.append(f"<description>{html.escape(feed_description)}</description>")
    parts.append(f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>")

    for title, link, pub_dt, content_html, first_img in items:
        # RSS pubDate najlepiej w RFC2822 i w UTC
        pub_dt_utc = pub_dt.astimezone(timezone.utc)

        parts.append("<item>")
        parts.append(f"<title>{html.escape(title)}</title>")
        parts.append(f"<link>{html.escape(link)}</link>")
        parts.append(f"<guid isPermaLink=\"true\">{html.escape(link)}</guid>")
        parts.append(f"<pubDate>{format_datetime(pub_dt_utc)}</pubDate>")

        # description: CDATA
        # Uwaga: content_html bywa duży — to normalne dla RSS; czytnik i tak sobie skróci.
        parts.append("<description><![CDATA[")
        parts.append(content_html)
        parts.append("]]></description>")

        # opcjonalne: enclosure z obrazkiem (jeśli czytnik wspiera)
        if first_img:
            parts.append(f'<enclosure url="{html.escape(first_img)}" type="image/jpeg" />')

        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/feed.xml", help="Output RSS path (default: docs/feed.xml)")
    ap.add_argument("--max-items", type=int, default=30, help="Max items in feed (default: 30)")
    args = ap.parse_args()

    session = get_session()

    home_html = fetch_html(session, HOMEPAGE_URL)
    article_urls = extract_article_links_from_homepage(home_html, limit=args.max_items)

    items = []
    for url in article_urls:
        try:
            title, pub_dt, content_html, first_img = parse_article(session, url)
            items.append((title, url, pub_dt, content_html, first_img))
        except Exception as e:
            # nie przerywamy całego feeda przez 1 artykuł
            print(f"[WARN] Failed to parse {url}: {e}")

    # sortuj po dacie malejąco
    items.sort(key=lambda x: x[2], reverse=True)

    rss = build_rss(
        items=items[: args.max_items],
        feed_title="Piotrków Trybunalski – Aktualności (piotrkow.pl)",
        feed_link=BASE_URL + NEWS_PATH_FRAGMENT,
        feed_description="Kanał RSS generowany z piotrkow.pl (blok 'Aktualności' ze strony głównej).",
    )

    out_path = args.out
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(rss)

    print(f"[OK] Wrote {out_path} with {len(items[: args.max_items])} items.")


if __name__ == "__main__":
    main()
