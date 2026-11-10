#!/usr/bin/env python3
"""PapersBot: search Europe PMC for new papers and publish them as a static site with RSS feeds.

Usage:
    python papersbot.py fetch            # query Europe PMC, store new papers in data/
    python papersbot.py build            # render _site/ (HTML pages + RSS feeds)
    python papersbot.py run              # fetch, then build

No third-party dependencies (Python 3.11+).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
USER_AGENT = "PapersBot/1.0 (static RSS site generator)"


# --------------------------------------------------------------------------- config

def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    site = cfg.setdefault("site", {})
    site.setdefault("title", "PapersBot")
    site.setdefault("description", "New papers from Europe PMC")
    site.setdefault("base_url", "")
    defaults = cfg.setdefault("defaults", {})
    defaults.setdefault("lookback_days", 7)
    defaults.setdefault("max_new_per_run", 25)
    defaults.setdefault("require_abstract", True)
    defaults.setdefault("feed_items", 50)
    defaults.setdefault("page_items", 200)
    defaults.setdefault("keep_items", 1000)
    feeds = cfg.get("feeds", [])
    if not feeds:
        sys.exit("config: no [[feeds]] defined")
    seen = set()
    for feed in feeds:
        for key in ("slug", "title", "query"):
            if key not in feed:
                sys.exit(f"config: feed is missing '{key}': {feed}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", feed["slug"]):
            sys.exit(f"config: slug must be lowercase letters, digits and dashes: {feed['slug']!r}")
        if feed["slug"] in seen:
            sys.exit(f"config: duplicate slug {feed['slug']!r}")
        seen.add(feed["slug"])
        for k, v in defaults.items():
            feed.setdefault(k, v)
    return cfg


# --------------------------------------------------------------------------- Europe PMC

def epmc_search(query: str, page_size: int = 100, max_results: int = 300) -> list[dict]:
    """Return Europe PMC 'core' records for a query, newest first."""
    results: list[dict] = []
    cursor = "*"
    while len(results) < max_results:
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": str(page_size),
            "cursorMark": cursor,
            "sort": "P_PDATE_D desc",
        }
        url = f"{API}?{urllib.parse.urlencode(params)}"
        data = _get_json(url)
        batch = data.get("resultList", {}).get("result", [])
        results.extend(batch)
        next_cursor = data.get("nextCursorMark")
        if not batch or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(0.3)
    return results[:max_results]


def _get_json(url: str, retries: int = 3) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:  # network hiccups, 5xx
            if attempt == retries - 1:
                raise
            print(f"  request failed ({exc}); retrying", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return {}


def record_to_item(rec: dict) -> dict:
    source, rid = rec.get("source", ""), rec.get("id", "")
    doi = rec.get("doi", "")
    journal = (rec.get("journalInfo") or {}).get("journal", {}).get("title") or rec.get("journalTitle", "")
    if not journal and source == "PPR":
        journal = (rec.get("bookOrReportDetails") or {}).get("publisher", "") or "Preprint"
    epmc_url = f"https://europepmc.org/article/{source}/{rid}"
    return {
        "id": f"{source}:{rid}",
        "title": _clean_title(rec.get("title", "")),
        "authors": rec.get("authorString", ""),
        "journal": journal,
        "pub_date": rec.get("firstPublicationDate") or rec.get("pubYear", ""),
        "doi": doi,
        "pmid": rec.get("pmid", ""),
        "url": f"https://doi.org/{doi}" if doi else epmc_url,
        "epmc_url": epmc_url,
        "abstract": sanitize_abstract(rec.get("abstractText", "")),
        "preprint": source == "PPR",
        "open_access": rec.get("isOpenAccess") == "Y",
    }


def _clean_title(t: str) -> str:
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip().rstrip(".")


class _AbstractSanitizer(HTMLParser):
    """Keep only a tiny whitelist of inline tags; turn headings into bold labels."""

    INLINE = {"i", "b", "em", "strong", "sup", "sub"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.INLINE:
            self.out.append(f"<{tag}>")
        elif tag in self.HEADINGS:
            self.out.append("\n\n<strong>")
        elif tag in {"p", "br", "div"}:
            self.out.append("\n\n")

    def handle_endtag(self, tag):
        if tag in self.INLINE:
            self.out.append(f"</{tag}>")
        elif tag in self.HEADINGS:
            self.out.append("</strong> ")

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))


def sanitize_abstract(raw: str) -> str:
    if not raw:
        return ""
    p = _AbstractSanitizer()
    p.feed(raw)
    p.close()
    text = "".join(p.out)
    paras = [re.sub(r"\s+", " ", para).strip() for para in re.split(r"\n\s*\n", text)]
    return "".join(f"<p>{para}</p>" for para in paras if para)


# --------------------------------------------------------------------------- state

def data_path(slug: str) -> Path:
    return ROOT / "data" / f"{slug}.json"


def load_items(slug: str) -> list[dict]:
    p = data_path(slug)
    return json.loads(p.read_text("utf-8")) if p.exists() else []


def save_items(slug: str, items: list[dict]) -> None:
    p = data_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n", "utf-8")


def newest_first(items: list[dict]) -> list[dict]:
    """Most recently published first (RSS readers still see each item dated by when it was added)."""
    return sorted(items, key=lambda it: (it.get("pub_date", ""), it.get("added", "")), reverse=True)


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def fetch(cfg: dict) -> int:
    total_new = 0
    now = dt.datetime.now(dt.timezone.utc)
    for feed in cfg["feeds"]:
        slug = feed["slug"]
        items = load_items(slug)
        seen_ids = {it["id"] for it in items}
        seen_dois = {it["doi"].lower() for it in items if it.get("doi")}
        seen_titles = {_norm_title(it["title"]) for it in items}

        since = (now - dt.timedelta(days=feed["lookback_days"])).date().isoformat()
        until = now.date().isoformat()
        query = f"({feed['query']}) AND FIRST_PDATE:[{since} TO {until}]"
        print(f"[{slug}] {query}")
        try:
            records = epmc_search(query)
        except Exception as exc:
            print(f"[{slug}] ERROR: {exc}", file=sys.stderr)
            continue

        new: list[dict] = []
        for rec in records:
            item = record_to_item(rec)
            if item["id"] in seen_ids:
                continue
            if item["doi"] and item["doi"].lower() in seen_dois:
                continue
            if _norm_title(item["title"]) in seen_titles:  # same paper indexed twice (e.g. MED + PMC)
                continue
            if feed["require_abstract"] and not item["abstract"]:
                continue
            item["added"] = now.isoformat(timespec="seconds")
            new.append(item)
            seen_ids.add(item["id"])
            if item["doi"]:
                seen_dois.add(item["doi"].lower())
            seen_titles.add(_norm_title(item["title"]))
            if len(new) >= feed["max_new_per_run"]:
                break

        items = newest_first(new + items)[: feed["keep_items"]]
        save_items(slug, items)
        print(f"[{slug}] {len(records)} matches, {len(new)} new, {len(items)} stored")
        total_new += len(new)
    return total_new


# --------------------------------------------------------------------------- site

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--accent:#1f5f8b;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#9c9a92;--line:#2c2b28;--accent:#7cb7e0;--card:#1c1b19}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 Georgia,"Iowan Old Style",serif}
main,header,footer{max-width:46rem;margin:0 auto;padding:0 16px}
header{padding-top:2.5rem;padding-bottom:1rem;border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:1.8rem;line-height:1.2}
header p{margin:.4rem 0 0;color:var(--muted)}
nav{font:14px/1.4 system-ui,sans-serif;margin-bottom:.8rem}
a{color:var(--accent)}
.meta,.small,nav,footer,.badge,.rss{font-family:system-ui,-apple-system,sans-serif}
.rss{display:inline-block;margin-top:.8rem;font-size:14px;text-decoration:none;border:1px solid var(--line);padding:.2rem .6rem;border-radius:4px}
article{padding:1.4rem 0;border-bottom:1px solid var(--line)}
article h2{font-size:1.15rem;line-height:1.35;margin:0 0 .35rem}
article h2 a{color:var(--fg);text-decoration:none}
article h2 a:hover{text-decoration:underline}
.meta{font-size:13px;color:var(--muted);margin:0 0 .5rem}
.badge{font-size:11px;text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--line);border-radius:3px;padding:0 .3rem;margin-right:.3rem}
details summary{cursor:pointer;font:14px system-ui,sans-serif;color:var(--muted)}
details p{margin:.6rem 0}
.feeds{list-style:none;padding:0}
.feeds li{padding:1rem 0;border-bottom:1px solid var(--line)}
.feeds h2{margin:0;font-size:1.2rem}
.small{font-size:13px;color:var(--muted)}
footer{padding:2rem 16px;font-size:13px;color:var(--muted)}
"""


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _page(title: str, body: str, *, rel_root: str, rss: str | None = None, rss_title: str = "") -> str:
    alt = f'<link rel="alternate" type="application/rss+xml" title="{esc(rss_title)}" href="{esc(rss)}">' if rss else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{rel_root}style.css">{alt}
</head><body>
{body}
<footer>Generated by PapersBot from <a href="https://europepmc.org/">Europe PMC</a>. Updated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC.</footer>
</body></html>
"""


def _fmt_date(item: dict) -> str:
    return item.get("pub_date", "")


def _article_html(item: dict) -> str:
    badges = ""
    if item.get("preprint"):
        badges += '<span class="badge">preprint</span>'
    if item.get("open_access"):
        badges += '<span class="badge">open access</span>'
    authors = item.get("authors", "")
    if len(authors) > 220:
        authors = authors[:220].rsplit(",", 1)[0] + ", et al."
    bits = [esc(x) for x in (item.get("journal", ""), _fmt_date(item)) if x]
    links = []
    if item.get("doi"):
        links.append(f'<a href="{esc(item["url"])}">doi:{esc(item["doi"])}</a>')
    if item.get("pmid"):
        links.append(f'<a href="https://pubmed.ncbi.nlm.nih.gov/{esc(item["pmid"])}/">PubMed</a>')
    links.append(f'<a href="{esc(item["epmc_url"])}">Europe PMC</a>')
    abstract = item.get("abstract") or "<p><em>No abstract available.</em></p>"
    anchor = re.sub(r"[^A-Za-z0-9]", "-", item["id"])
    return f"""<article id="{anchor}">
<h2><a href="{esc(item['url'])}">{esc(item['title'])}</a></h2>
<p class="meta">{badges}{esc(authors)}<br>{' · '.join(bits)}<br>{' · '.join(links)}</p>
<details open><summary>Abstract</summary>{abstract}</details>
</article>"""


def _rss(feed: dict, items: list[dict], site: dict, base: str) -> str:
    page_url = f"{base}feeds/{feed['slug']}/"
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    entries = []
    for it in items:
        try:
            pub = format_datetime(dt.datetime.fromisoformat(it["added"]))
        except Exception:
            pub = now
        meta = " · ".join(x for x in (it.get("authors", ""), it.get("journal", ""), it.get("pub_date", "")) if x)
        desc = f"<p><em>{esc(meta)}</em></p>{it.get('abstract', '')}<p><a href=\"{esc(it['url'])}\">{esc(it['url'])}</a></p>"
        entries.append(f"""  <item>
    <title>{esc(it['title'])}</title>
    <link>{esc(it['url'])}</link>
    <guid isPermaLink="false">{esc(it['id'])}</guid>
    <pubDate>{pub}</pubDate>
    <description>{esc(desc)}</description>
  </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{esc(feed['title'])}</title>
  <link>{esc(page_url)}</link>
  <atom:link href="{esc(page_url)}feed.xml" rel="self" type="application/rss+xml"/>
  <description>{esc(feed.get('description') or 'New papers matching: ' + feed['query'])}</description>
  <lastBuildDate>{now}</lastBuildDate>
  <generator>PapersBot</generator>
{chr(10).join(entries)}
</channel>
</rss>
"""


def build(cfg: dict, out: Path, base_url: str | None = None) -> None:
    site = cfg["site"]
    base = (base_url if base_url is not None else site["base_url"]).rstrip("/") + "/"
    if base == "/":
        print("warning: no base_url set; RSS self-links will be relative", file=sys.stderr)
    out.mkdir(parents=True, exist_ok=True)
    (out / "style.css").write_text(CSS.strip() + "\n", "utf-8")
    (out / ".nojekyll").write_text("", "utf-8")

    index_rows = []
    for feed in cfg["feeds"]:
        slug = feed["slug"]
        items = newest_first(load_items(slug))
        d = out / "feeds" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "feed.xml").write_text(_rss(feed, items[: feed["feed_items"]], site, base), "utf-8")
        desc = feed.get("description", "")
        body = f"""<header><nav><a href="../../">← {esc(site['title'])}</a></nav>
<h1>{esc(feed['title'])}</h1>
{f'<p>{esc(desc)}</p>' if desc else ''}
<p class="small">Query: <code>{esc(feed['query'])}</code></p>
<a class="rss" href="feed.xml">RSS feed</a></header>
<main>
{chr(10).join(_article_html(it) for it in items[: feed['page_items']]) or '<p>No papers yet.</p>'}
</main>"""
        (d / "index.html").write_text(
            _page(f"{feed['title']} · {site['title']}", body, rel_root="../../", rss="feed.xml", rss_title=feed["title"]),
            "utf-8",
        )
        latest = items[0]["added"][:10] if items else "—"
        index_rows.append(
            f"""<li><h2><a href="feeds/{slug}/">{esc(feed['title'])}</a></h2>
<p class="small">{esc(desc) + ' · ' if desc else ''}{len(items)} papers · last added {latest} · <a href="feeds/{slug}/feed.xml">RSS</a></p></li>"""
        )

    body = f"""<header><h1>{esc(site['title'])}</h1><p>{esc(site['description'])}</p>
<a class="rss" href="feeds.opml">All feeds (OPML)</a></header>
<main><ul class="feeds">
{chr(10).join(index_rows)}
</ul></main>"""
    (out / "index.html").write_text(_page(site["title"], body, rel_root=""), "utf-8")

    outlines = "\n".join(
        f'    <outline type="rss" text="{esc(f["title"])}" title="{esc(f["title"])}" '
        f'xmlUrl="{esc(base)}feeds/{f["slug"]}/feed.xml" htmlUrl="{esc(base)}feeds/{f["slug"]}/"/>'
        for f in cfg["feeds"]
    )
    (out / "feeds.opml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0"><head><title>{esc(site['title'])}</title></head>
<body>
{outlines}
</body></opml>
""",
        "utf-8",
    )
    print(f"built {len(cfg['feeds'])} feed(s) into {out}")


# --------------------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "build", "run"])
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--out", default=str(ROOT / "_site"))
    ap.add_argument("--base-url", default=None, help="override site.base_url (used by GitHub Actions)")
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    if args.command in ("fetch", "run"):
        fetch(cfg)
    if args.command in ("build", "run"):
        build(cfg, Path(args.out), args.base_url)


if __name__ == "__main__":
    main()
