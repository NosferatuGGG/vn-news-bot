"""Сбор новостей: RSS, страницы рубрик и полный текст статьи."""
import calendar
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup

from .sources import Source

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
}

_client = httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True)


@dataclass
class Entry:
    url: str
    title: str
    summary: str
    published_at: float
    source: str


def _clean(text: str) -> str:
    text = BeautifulSoup(text or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def get(url: str) -> str:
    r = _client.get(url)
    r.raise_for_status()
    return r.text


def parse_rss(xml: str, source_name: str) -> list[Entry]:
    feed = feedparser.parse(xml)
    out = []
    for e in feed.entries:
        url = e.get("link")
        if not url:
            continue
        t = e.get("published_parsed") or e.get("updated_parsed")
        ts = calendar.timegm(t) if t else time.time()
        out.append(Entry(url, _clean(e.get("title", "")), _clean(e.get("summary", ""))[:600], ts, source_name))
    return out


def parse_listing(html: str, base_url: str, pattern: str, source_name: str) -> list[Entry]:
    soup = BeautifulSoup(html, "html.parser")
    rx = re.compile(pattern)
    titles: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0]
        if not rx.match(url):
            continue
        text = _clean(a.get("title") or a.get_text(" "))
        if len(text) > len(titles.get(url, "")):
            titles[url] = text
    now = time.time()
    # на страницах рубрик даты нет — ставим «сейчас», реальную дату уточним из статьи
    return [Entry(u, t, "", now, source_name) for u, t in titles.items() if len(t) > 15]


def fetch_source(src: Source) -> list[Entry]:
    try:
        body = get(src.url)
    except Exception as e:  # noqa: BLE001
        log.warning("Источник недоступен %s: %s", src.url, e)
        return []
    if src.kind == "rss":
        entries = parse_rss(body, src.name)
    else:
        entries = parse_listing(body, src.url, src.link_pattern, src.name)
    if not entries:
        log.warning("Источник %s отдал 0 записей — проверь адрес", src.url)
    return entries


@dataclass
class Article:
    text: str
    image: str
    published_at: float | None


def fetch_article(url: str) -> Article:
    html = get(url)
    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    soup = BeautifulSoup(html, "html.parser")
    img = ""
    m = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if m and m.get("content"):
        img = m["content"]
    published = None
    t = soup.find("meta", property="article:published_time")
    if t and t.get("content"):
        try:
            from datetime import datetime

            published = datetime.fromisoformat(t["content"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return Article(text=text[:12000], image=img, published_at=published)
