"""Источники новостей.

kind="rss"  — обычная RSS-лента.
kind="html" — страница-рубрика; ссылки на статьи достаются регуляркой link_pattern
              (для сайтов, у которых RSS не работает, например Báo Khánh Hòa).

Ленты, отмеченные verified=True, проверены вручную 15.10.2026 и отдают свежие новости.
Остальные — стандартные адреса этих сайтов; если какая-то лента отдаёт 0 записей,
бот напишет об этом в лог, просто удали её или поправь адрес.
"""
from dataclasses import dataclass


@dataclass
class Source:
    name: str
    url: str
    kind: str = "rss"
    link_pattern: str = ""
    verified: bool = False


SOURCES: list[Source] = [
    # --- Общенациональные, рубрика «Право/криминал» ---
    Source("VnExpress", "https://vnexpress.net/rss/phap-luat.rss", verified=True),
    Source("VnExpress", "https://vnexpress.net/rss/thoi-su.rss"),
    Source("VnExpress", "https://vnexpress.net/rss/du-lich.rss"),
    Source("Thanh Niên", "https://thanhnien.vn/rss/thoi-su/phap-luat.rss", verified=True),
    Source("Tuổi Trẻ", "https://tuoitre.vn/rss/phap-luat.rss", verified=True),
    Source("Dân Trí", "https://dantri.com.vn/rss/phap-luat.rss"),

    # --- Местная пресса Нячанга (RSS у них не работает, берём страницы рубрик) ---
    Source(
        "Báo Khánh Hòa",
        "https://baokhanhhoa.vn/phap-luat/",
        kind="html",
        # только статьи самой рубрики, без ссылок на «женсовет» и политику из боковых колонок
        link_pattern=r"^https://baokhanhhoa\.vn/phap-luat/([a-z0-9\-]+/)?20\d{4}/[a-z0-9\-]+/?$",
        verified=True,
    ),
    Source(
        "Báo Khánh Hòa",
        "https://baokhanhhoa.vn/xa-hoi/",
        kind="html",
        link_pattern=r"^https://baokhanhhoa\.vn/xa-hoi/([a-z0-9\-]+/)?20\d{4}/[a-z0-9\-]+/?$",
    ),

    # --- Англоязычная версия VnExpress: хорошо ловит истории про иностранцев ---
    Source("VnExpress International", "https://e.vnexpress.net/rss/news.rss"),
    Source("VnExpress International", "https://e.vnexpress.net/rss/travel.rss"),
]
