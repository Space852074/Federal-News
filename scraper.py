from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import io
import json
import os
import re
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin, urlparse
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "vendor"))
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 GovernmentNewsAggregator/1.0"
DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])\b"),
    re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})\b", re.I),
    re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(20\d{2})\b", re.I),
]
MONTHS = {m.lower(): i for i, m in enumerate(["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}
MONTHS.update({m[:3].lower(): i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
NON_NEWS_TITLES = {
    "clear all filters",
    "frontline digital magazine",
    "office of the spokesperson",
    "state department home state department home state department home",
}
RED_KEYWORDS = re.compile(r"\b(?:china|chinese)\b", re.I)
YELLOW_KEYWORDS = re.compile(r"\b(?:virus|covid-?19|vaccine|fentanyl)\b", re.I)
DOJ_API = "https://www.justice.gov/api/v1/press_releases.json?sort=created&direction=DESC&pagesize=100&page={}"
DOJ_BODY_CACHE = None
DOJ_BODY_LOCK = threading.Lock()
GOOGLE_TRANSLATE_ENDPOINTS = (
    "https://translate.googleapis.com/translate_a/single",
    "https://translate.google.com/translate_a/single",
)


def apply_keyword_markers(row):
    """Prefix the display title when the English title or article body matches monitored terms."""
    haystack = "\n".join((row.get("title", ""), row.get("content", "")))
    markers = ("🔴" if RED_KEYWORDS.search(haystack) else "") + ("🟡" if YELLOW_KEYWORDS.search(haystack) else "")
    title_zh = (row.get("title_zh") or "").lstrip("🔴🟡")
    if title_zh:
        row["title_zh"] = markers + title_zh
    row["keyword_markers"] = markers
    return row


def build_session(retries=2):
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    })
    retry = Retry(
        total=retries, connect=retries, read=retries, status=retries, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]), raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=6, pool_maxsize=6)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = build_session()
QUICK_SESSION = build_session(1)


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.stack = [], []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "svg", "nav", "footer"}:
            self.skip += 1
        self.stack.append({"tag": tag, "href": attrs.get("href"), "text": []})

    def handle_data(self, data):
        if self.skip:
            return
        text = " ".join(data.split())
        if text:
            for item in self.stack[-5:]:
                item["text"].append(text)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        item = self.stack.pop()
        if item["href"] and tag == "a":
            self.links.append((item["href"], " ".join(item["text"])))
        if tag in {"script", "style", "svg", "nav", "footer"} and self.skip:
            self.skip -= 1


class ArticleTextParser(HTMLParser):
    """Extract readable paragraphs from a news detail page without navigation chrome."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip = 0
        self.capture = 0
        self.current = []
        self.paragraphs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        marker = " ".join((attrs.get("class", ""), attrs.get("id", ""))).lower()
        if tag in {"script", "style", "svg", "nav", "footer", "header", "form", "aside"}:
            self.skip += 1
        if not self.skip and (tag in {"article", "main"} or any(x in marker for x in ("article-body", "field--name-body", "entry-content", "node__content", "content-body", "press-release"))):
            self.capture += 1
        if not self.skip and self.capture and tag in {"p", "li", "h2", "h3", "blockquote"}:
            self.depth += 1
            self.current = []

    def handle_data(self, data):
        if self.depth and not self.skip:
            text = " ".join(data.split())
            if text:
                self.current.append(text)

    def handle_endtag(self, tag):
        if tag in {"p", "li", "h2", "h3", "blockquote"} and self.depth:
            text = clean_title(" ".join(self.current))
            if len(text) >= 25:
                self.paragraphs.append(text)
            self.depth -= 1
            self.current = []
        if tag in {"article", "main"} and self.capture:
            self.capture -= 1
        if tag in {"script", "style", "svg", "nav", "footer", "header", "form", "aside"} and self.skip:
            self.skip -= 1


def fetch(url, timeout=25, session=None, outer_attempts=2):
    session = session or SESSION
    ca_file = ROOT / "vendor" / "certifi" / "cacert.pem"
    verify = str(ca_file) if ca_file.exists() else True
    last_error = None
    for attempt in range(1, outer_attempts + 1):
        try:
            res = session.get(url, timeout=(10, timeout), verify=verify)
            if res.status_code >= 400:
                raise HTTPError(res.url, res.status_code, res.reason, res.headers, io.BytesIO(res.content))
            if not res.encoding or res.encoding.lower() == "iso-8859-1":
                res.encoding = res.apparent_encoding or "utf-8"
            return res.status_code, res.url, res.text[:4_000_000]
        except HTTPError:
            raise
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            if attempt < outer_attempts:
                time.sleep(1.5 * attempt)
    raise URLError(last_error)


def parse_date(text):
    for idx, pattern in enumerate(DATE_PATTERNS):
        m = pattern.search(text)
        if not m:
            continue
        try:
            if idx in {0, 1}:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return date(int(m.group(3)), MONTHS[m.group(1).rstrip('.').lower()], int(m.group(2)))
        except ValueError:
            pass
    return None


def parse_any_date(text):
    parsed = parse_date(text or "")
    if parsed:
        return parsed
    # NIH RSS 使用 "Fri, 08/07/2026 - 15:00" 这类非标准日期。
    slash_date = re.search(r"(?<!\d)(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(20\d{2})(?!\d)", text or "")
    if slash_date:
        try:
            return date(int(slash_date.group(3)), int(slash_date.group(1)), int(slash_date.group(2)))
        except ValueError:
            pass
    try:
        return email.utils.parsedate_to_datetime(text).date()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def clean_title(text):
    text = html.unescape(re.sub(r"\s+", " ", text)).strip(" |-–—\t\r\n")
    return text[:350]


def nearby_text(body, href):
    pos = body.find(href)
    if pos < 0:
        pos = body.find(html.escape(href, quote=True))
    if pos < 0:
        return ""
    window = body[max(0, pos-700):pos+900]
    return html.unescape(re.sub(r"<[^>]+>", " ", window))


def make_row(source, published, title, link, summary=""):
    key = hashlib.sha256(link.split("#")[0].encode()).hexdigest()[:24]
    return {
        "source_id": source["id"], "agency": source["agency"], "group": source["group"],
        "date": published.isoformat(), "title": clean_title(title), "title_zh": "",
        "summary": clean_title(summary)[:1000], "summary_zh": "", "content": "", "content_zh": "", "url": link,
        "unique_key": key, "state": "待翻译", "created_time": datetime.now(timezone.utc).isoformat()
    }


def block_contexts(body):
    contexts = {}
    for match in re.finditer(r"<(article|li)\b[^>]*>(.*?)</\1>", body, re.I | re.S):
        block = match.group(2)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", block))
        if not parse_date(plain):
            continue
        for href in re.findall(r"href=[\"']([^\"']+)", block, re.I):
            previous = contexts.get(href)
            if previous is None or len(plain) < len(previous):
                contexts[href] = plain
    return contexts


def html_candidates(source, final_url, body, cutoff):
    parser = LinkParser(); parser.feed(body)
    blocks, candidates, seen = block_contexts(body), [], set()
    for href, context in parser.links:
        title = clean_title(context)
        if len(title) < 12 or len(title) > 500:
            continue
        if title.casefold() in NON_NEWS_TITLES:
            continue
        link = urljoin(final_url, href)
        if urlparse(link).netloc != urlparse(final_url).netloc or link in seen:
            continue
        # 白宫 News 页面会把分类导航放在新闻日期附近；真实文章 URL 均包含年月路径。
        if source.get("id") == "whitehouse" and not re.search(r"/20\d{2}/\d{2}/", urlparse(link).path):
            continue
        if title.lower() in {"regulations and guidance", "sanctions list updates", "secretary statements & remarks"}:
            continue
        published = parse_date(link) or parse_date(context + " " + (blocks.get(href, "") or nearby_text(body, href)))
        if not published or published < cutoff or published > date.today() + timedelta(days=1):
            continue
        seen.add(link); candidates.append(make_row(source, published, title, link))
    return candidates


def feed_candidates(source, feed_url, body, cutoff):
    root = ElementTree.fromstring(body)
    rows, seen = [], set()
    for entry in [x for x in root.iter() if x.tag.rsplit("}", 1)[-1] in {"item", "entry"}]:
        values, link = {}, ""
        for child in entry.iter():
            name = child.tag.rsplit("}", 1)[-1]
            if name == "link":
                link = child.attrib.get("href") or (child.text or link)
            elif name in {"title", "pubDate", "published", "updated", "date", "description", "summary"} and child.text:
                values.setdefault(name, child.text)
        published = parse_any_date(values.get("pubDate") or values.get("published") or values.get("updated") or values.get("date") or "")
        title, link = clean_title(values.get("title", "")), urljoin(feed_url, link.strip())
        if published and published >= cutoff and title and link and link not in seen:
            seen.add(link); rows.append(make_row(source, published, title, link, values.get("description") or values.get("summary", "")))
    return rows


def wordpress_candidates(source, body, cutoff):
    rows = []
    for item in json.loads(body):
        published = parse_any_date(item.get("date_gmt") or item.get("date") or "")
        title = html.unescape((item.get("title") or {}).get("rendered", ""))
        summary = re.sub(r"<[^>]+>", " ", html.unescape((item.get("excerpt") or {}).get("rendered", "")))
        link = item.get("link", "")
        if published and published >= cutoff and title and link:
            rows.append(make_row(source, published, title, link, summary))
    return rows


def doj_candidates(source, body, cutoff):
    rows = []
    for item in json.loads(body).get("results", []):
        component_match = source.get("component_match", "").lower()
        components = " | ".join(
            str(value.get("name", "")) if isinstance(value, dict) else str(value)
            for value in (item.get("component") or [])
        ).lower()
        if component_match and component_match not in components:
            continue
        raw_date = item.get("date") or item.get("created") or ""
        try:
            published = datetime.fromtimestamp(int(raw_date), timezone.utc).date()
        except (TypeError, ValueError, OSError, OverflowError):
            published = parse_any_date(str(raw_date))
        title = html.unescape(item.get("title") or "")
        summary = re.sub(r"<[^>]+>", " ", html.unescape(item.get("teaser") or item.get("body") or ""))
        link = item.get("url") or ""
        if published and published >= cutoff and title and link:
            rows.append(make_row(source, published, title, link, summary))
    return rows


def commerce_candidates(source, body, cutoff):
    rows = []
    for item in json.loads(body).get("data", []):
        news_types = " | ".join(
            str(value.get("label", "")) if isinstance(value, dict) else str(value)
            for value in (item.get("news_type") or [])
        ).lower()
        if "press release" not in news_types:
            continue
        published = parse_any_date(item.get("post_date_formatted") or "")
        if not published and item.get("created"):
            try:
                published = datetime.fromtimestamp(int(item["created"]), timezone.utc).date()
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        title = html.unescape(item.get("label") or "")
        summary = re.sub(r"<[^>]+>", " ", html.unescape(item.get("body") or ""))
        link = item.get("href") or item.get("self") or ""
        if published and published >= cutoff and title and link:
            rows.append(make_row(source, published, title, link, summary))
    return rows


def json_candidates(source, body, cutoff):
    if source.get("json_type") == "doj":
        return doj_candidates(source, body, cutoff)
    if source.get("json_type") == "commerce":
        return commerce_candidates(source, body, cutoff)
    return wordpress_candidates(source, body, cutoff)


def verified_snapshot(source, cutoff):
    """Use a same-day, human-verified official-page snapshot after live transports fail."""
    verified_at = parse_any_date(source.get("verified_at", ""))
    if not verified_at or verified_at < date.today() - timedelta(days=1):
        return [], None
    snapshot_items = list(source.get("snapshot_items", []))
    title_range = source.get("snapshot_translation_range")
    if title_range and (DATA / "translations.json").exists():
        translation_titles = list(json.loads((DATA / "translations.json").read_text(encoding="utf-8")))
        try:
            start = translation_titles.index(title_range["start"])
            end = translation_titles.index(title_range["end"], start) + 1
            for title in translation_titles[start:end]:
                snapshot_items.append({
                    "date": title_range["date"], "title": title,
                    "url": f"{source['url']}?search_api_fulltext={quote_plus(title)}",
                })
        except ValueError:
            pass
    rows = []
    for item in snapshot_items:
        published = parse_any_date(item.get("date", ""))
        if not published or published < cutoff:
            continue
        row = make_row(source, published, item.get("title", ""), item.get("url") or source["url"], item.get("summary", ""))
        if item.get("title_zh"):
            row.update({"title_zh": item["title_zh"], "state": "已翻译"})
        rows.append(row)
    empty_through = parse_any_date(source.get("verified_empty_through", ""))
    if rows:
        return rows, "ok"
    if empty_through and empty_through >= date.today():
        return [], "empty"
    return [], None


def scrape_source(source, cutoff):
    started = time.time()
    status = {**source, "checked_at": datetime.now(timezone.utc).isoformat(), "health": "error", "items": 0}
    attempts, last_error = [], None
    strategies = []
    if source.get("json_url"):
        strategies.append(("official_json", source["json_url"]))
    if source.get("feed_url") and source.get("prefer_feed"):
        strategies.append(("official_rss", source["feed_url"]))
    strategies.append(("official_html", source["url"]))
    strategies.extend(("alternate_html", url) for url in source.get("alternatives", []))
    if source.get("feed_url") and not source.get("prefer_feed"):
        strategies.append(("official_rss", source["feed_url"]))
    for strategy, target in strategies:
        try:
            if strategy == "official_json" and source.get("json_pages", 1) > 1:
                candidates, final_url, code = [], target, 200
                for page in range(int(source["json_pages"])):
                    separator = "&" if "?" in target else "?"
                    page_url = f"{target}{separator}page={page}"
                    code, final_url, body = fetch(page_url, timeout=20, session=QUICK_SESSION, outer_attempts=1)
                    page_candidates = json_candidates(source, body, cutoff)
                    candidates.extend(page_candidates)
                    attempts.append({"strategy": strategy, "url": page_url, "http_status": code, "items": len(page_candidates)})
                    payload = json.loads(body)
                    raw_results = payload.get("results", []) if isinstance(payload, dict) else payload
                    if len(raw_results) < 50:
                        break
                if candidates:
                    status.update({"http_status": code, "final_url": final_url, "items": len(candidates), "elapsed_ms": round((time.time()-started)*1000), "health": "ok", "strategy": "official_json_paged", "attempts": attempts, "message": "通过 DOJ 官方分页 API 获取最近 5 天内容"})
                    return candidates, status
                status.setdefault("http_status", code); status.setdefault("final_url", final_url)
                continue
            code, final_url, body = fetch(target, timeout=35)
            if strategy == "official_json":
                candidates = json_candidates(source, body, cutoff)
            elif strategy == "official_rss":
                candidates = feed_candidates(source, final_url, body, cutoff)
            else:
                candidates = html_candidates(source, final_url, body, cutoff)
            attempts.append({"strategy": strategy, "url": target, "http_status": code, "items": len(candidates)})
            if candidates:
                status.update({"http_status": code, "final_url": final_url, "items": len(candidates), "elapsed_ms": round((time.time()-started)*1000), "health": "ok", "strategy": strategy, "attempts": attempts, "message": f"通过 {strategy} 获取最近 5 天内容"})
                return candidates, status
            status.setdefault("http_status", code); status.setdefault("final_url", final_url)
        except HTTPError as e:
            last_error = str(e); attempts.append({"strategy": strategy, "url": target, "http_status": e.code, "error": str(e)}); status["http_status"] = e.code
        except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError, ElementTree.ParseError) as e:
            last_error = str(e); attempts.append({"strategy": strategy, "url": target, "error": str(e)})
    snapshot_rows, snapshot_health = verified_snapshot(source, cutoff)
    if snapshot_health:
        status.update({"http_status": 200, "final_url": source["url"], "items": len(snapshot_rows), "elapsed_ms": round((time.time()-started)*1000), "health": snapshot_health, "strategy": "verified_official_snapshot", "attempts": attempts, "message": "实时入口失败；已使用当日核验的官方页面快照"})
        return snapshot_rows, status
    status.update({"elapsed_ms": round((time.time()-started)*1000), "attempts": attempts})
    if any(a.get("http_status") == 200 for a in attempts):
        status.update({"health": "empty", "message": "备用策略均已尝试；页面可访问，但仍未识别到最近 5 天新闻"})
    else:
        status.update({"health": "error", "message": last_error or "所有抓取策略均失败"})
    return [], status


def valid_chinese_translation(source, translated):
    """Reject cached English text that older runs incorrectly treated as Chinese."""
    translated = (translated or "").lstrip("🔴🟡").strip()
    return bool(
        translated
        and translated.casefold() != (source or "").strip().casefold()
        and re.search(r"[\u3400-\u9fff]", translated)
    )


def google_translate_text(text):
    """Translate one string through Google's no-key web endpoint with throttling retries."""
    endpoints = [
        value.strip() for value in os.environ.get("GOOGLE_TRANSLATE_ENDPOINT", "").split(",")
        if value.strip()
    ] or list(GOOGLE_TRANSLATE_ENDPOINTS)
    last_error = None
    for attempt in range(4):
        endpoint = endpoints[attempt % len(endpoints)]
        try:
            response = QUICK_SESSION.get(
                endpoint,
                params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
                timeout=(10, 45),
            )
            response.raise_for_status()
            payload = response.json()
            translated = "".join(
                part[0] for part in (payload[0] or []) if part and part[0]
            ).strip()
            if translated:
                return translated
            last_error = "Google 返回空翻译"
        except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
            last_error = str(exc)
        if attempt < 3:
            time.sleep(min(2 ** (attempt + 1), 12))
    raise RuntimeError(f"Google 免费翻译调用失败：{last_error}")


def translate_texts(texts):
    """Translate sequentially to reduce 429 responses from the free endpoint."""
    delay = max(0.0, float(os.environ.get("GOOGLE_TRANSLATE_DELAY_SECONDS", "0.35")))
    results = []
    for index, text in enumerate(texts):
        results.append(google_translate_text(text))
        if delay and index + 1 < len(texts):
            time.sleep(delay)
    return results


def translate_google(rows):
    """Translate pending titles with Google and leave invalid cached text pending."""
    for row in rows:
        if row.get("title_zh") and not valid_chinese_translation(row.get("title"), row.get("title_zh")):
            row["title_zh"] = ""
            row["state"] = "待翻译"
    pending = [row for row in rows if not row.get("title_zh")]
    if not pending:
        return rows
    results = translate_texts([row["title"] for row in pending])
    invalid = []
    for row, translated in zip(pending, results):
        if valid_chinese_translation(row["title"], translated):
            row.update({"title_zh": translated, "state": "已翻译"})
        else:
            invalid.append(row["title"])
    if invalid:
        examples = " | ".join(invalid[:3])
        raise RuntimeError(f"Google 未返回有效中文标题翻译（{len(invalid)} 条）：{examples}")
    return rows


def extract_article_content(row):
    url = row.get("url", "")
    if not url or "search_api_fulltext=" in url or "combine=" in url:
        return row["unique_key"], ""
    try:
        _, _, body = fetch(url, timeout=30, session=QUICK_SESSION, outer_attempts=1)
        parser = ArticleTextParser(); parser.feed(body)
        seen, paragraphs = set(), []
        for paragraph in parser.paragraphs:
            normalized = paragraph.casefold()
            if normalized in seen or paragraph == row.get("title"):
                continue
            if any(x in normalized for x in ("subscribe", "privacy policy", "accessibility", "all rights reserved", "share this page")):
                continue
            seen.add(normalized); paragraphs.append(paragraph)
        content = "\n\n".join(paragraphs)[:8000]
        if content:
            return row["unique_key"], content
        if urlparse(url).netloc.lower().endswith("justice.gov"):
            return row["unique_key"], extract_doj_api_content(row)
        return row["unique_key"], ""
    except Exception:
        if urlparse(url).netloc.lower().endswith("justice.gov"):
            return row["unique_key"], extract_doj_api_content(row)
        return row["unique_key"], ""


def extract_doj_api_content(row):
    """Use DOJ's official JSON API when Akamai serves an interstitial page."""
    global DOJ_BODY_CACHE
    with DOJ_BODY_LOCK:
        if DOJ_BODY_CACHE is None:
            DOJ_BODY_CACHE = {}
            for page in range(8):
                try:
                    response = QUICK_SESSION.get(DOJ_API.format(page), timeout=35)
                    response.raise_for_status()
                    for item in response.json().get("results", []):
                        body = item.get("body") or ""
                        if not body:
                            continue
                        parser = ArticleTextParser()
                        parser.feed("<article>" + body + "</article>")
                        content = "\n\n".join(dict.fromkeys(parser.paragraphs))[:8000]
                        entry = {"title": clean_title(item.get("title", "")), "content": content}
                        item_url = (item.get("url") or "").split("#")[0].rstrip("/")
                        if item_url:
                            DOJ_BODY_CACHE[item_url] = entry
                except Exception:
                    continue
    normalized_url = (row.get("url") or "").split("#")[0].rstrip("/")
    match = DOJ_BODY_CACHE.get(normalized_url)
    if not match:
        wanted = clean_title(row.get("title", "")).casefold()
        match = next((item for item in DOJ_BODY_CACHE.values() if item["title"].casefold() == wanted), None)
    return (match or {}).get("content", "")


def enrich_contents(rows, old_rows):
    cached = {r.get("unique_key"): (r.get("content", ""), r.get("content_zh", "")) for r in old_rows}
    by_key = {r["unique_key"]: r for r in rows}
    pending = []
    for row in rows:
        content, content_zh = cached.get(row["unique_key"], ("", ""))
        row["content"], row["content_zh"] = content, content_zh
        if not content:
            pending.append(row)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, content in pool.map(extract_article_content, pending):
            if content:
                by_key[key]["content"] = content
    return rows


def translate_contents(rows):
    jobs = []
    for row in rows:
        if row.get("content") and not row.get("content_zh"):
            chunks, current = [], ""
            for paragraph in row["content"].split("\n\n"):
                if current and len(current) + len(paragraph) + 2 > 1800:
                    chunks.append(current); current = paragraph
                else:
                    current = (current + "\n\n" + paragraph).strip()
            if current:
                chunks.append(current)
            for index, chunk in enumerate(chunks):
                jobs.append((row["unique_key"], index, chunk))

    translations = {}
    results = translate_texts([chunk for _, _, chunk in jobs])
    for (key, index, _), translated in zip(jobs, results):
        if translated and re.search(r"[\u3400-\u9fff]", translated):
            translations.setdefault(key, {})[index] = translated
        else:
            raise RuntimeError(f"Google 未返回有效中文正文翻译：{key} 第 {index + 1} 段")
    for row in rows:
        pieces = translations.get(row["unique_key"], {})
        if pieces:
            row["content_zh"] = "\n\n".join(pieces[i] for i in sorted(pieces))[:10000]
    return rows


def load_key_file(path):
    if not path:
        return set()
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = payload.get("keys", [])
    return {str(value) for value in payload}


def process_new_content(key_file):
    """Inspect new pages, but translate and return bodies only when monitored keywords match."""
    rows = json.loads((DATA / "news.json").read_text(encoding="utf-8"))
    target_keys = load_key_file(key_file)
    targets = [row for row in rows if row.get("unique_key") in target_keys]
    targets = enrich_contents(targets, [])
    for row in targets:
        apply_keyword_markers(row)
    matched = [row for row in targets if row.get("keyword_markers")]
    matched_keys = {row["unique_key"] for row in matched}
    # Avoid translating or syncing full text for ordinary news. The extracted
    # English body is retained only long enough to perform keyword matching.
    for row in targets:
        if row["unique_key"] not in matched_keys:
            row["content"] = ""
            row["content_zh"] = ""
    matched = translate_contents(matched)
    Path(key_file).write_text(json.dumps({"keys": sorted(matched_keys)}, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "news.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {
        "requested": len(target_keys),
        "inspected": len(targets),
        "keyword_matched": len(matched),
        "content_extracted": sum(bool(row.get("content")) for row in matched),
        "content_translated": sum(bool(row.get("content_zh")) for row in matched),
    }
    (DATA / "content-stage.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--translate", action="store_true"); ap.add_argument("--translate-only", action="store_true"); ap.add_argument("--retry-errors", action="store_true"); ap.add_argument("--content-keys"); args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    lock = DATA / "refresh.lock"; lock.write_text(str(os.getpid()), encoding="ascii")
    try:
        if args.content_keys:
            process_new_content(args.content_keys)
            return
        previous_meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8")) if (DATA / "meta.json").exists() else {}
        running_meta = {**previous_meta, "status": "running", "phase": "scraping", "started_at": datetime.now(timezone.utc).isoformat()}
        (DATA / "meta.json").write_text(json.dumps(running_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.translate_only:
            rows = json.loads((DATA / "news.json").read_text(encoding="utf-8")) if (DATA / "news.json").exists() else []
            title_translations = json.loads((DATA / "translations.json").read_text(encoding="utf-8")) if (DATA / "translations.json").exists() else {}
            pending = sum(not row.get("title_zh") for row in rows)
            progress_meta = {**running_meta, "phase": "translating", "translation_total": pending, "translated": 0}
            (DATA / "meta.json").write_text(json.dumps(progress_meta, ensure_ascii=False, indent=2), encoding="utf-8")
            rows = translate_google(rows)
            for row in rows:
                row["state"] = "已翻译" if row.get("title_zh") else "待翻译"
                if row.get("title_zh"):
                    title_translations[row["title"]] = row["title_zh"]
            (DATA / "news.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            (DATA / "translations.json").write_text(json.dumps(title_translations, ensure_ascii=False, indent=2), encoding="utf-8")
            meta = {**previous_meta, "status": "complete", "refreshed_at": datetime.now(timezone.utc).isoformat(), "translated": sum(bool(r.get("title_zh")) for r in rows), "pending_translation": sum(not r.get("title_zh") for r in rows)}
            (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
        enabled = [s for s in sources if s.get("enabled", True)]
        cutoff = date.today() - timedelta(days=4)
        old = json.loads((DATA / "news.json").read_text(encoding="utf-8")) if (DATA / "news.json").exists() else []
        old_statuses = json.loads((DATA / "source_status.json").read_text(encoding="utf-8")) if (DATA / "source_status.json").exists() else []
        target_sources = enabled
        news, statuses = [], []
        if args.retry_errors and old_statuses:
            error_ids = {s["id"] for s in old_statuses if s.get("health") == "error"}
            target_sources = [s for s in enabled if s["id"] in error_ids]
            statuses = [s for s in old_statuses if s.get("health") != "error"]
            news = [row for row in old if row.get("source_id") not in error_ids and row.get("date", "") >= cutoff.isoformat()]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(scrape_source, s, cutoff): s for s in target_sources}
            for future in as_completed(futures):
                items, status = future.result(); news.extend(items); statuses.append(status)
        failed_ids = {s["id"] for s in statuses if s.get("health") == "error" and s.get("http_status") != 403}
        if failed_ids:
            retry_results = []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(scrape_source, s, cutoff): s for s in target_sources if s["id"] in failed_ids}
                for future in as_completed(futures):
                    items, status = future.result(); news.extend(items); retry_results.append(status)
            retry_by_id = {s["id"]: s for s in retry_results}
            statuses = [retry_by_id.get(s["id"], s) for s in statuses]
        old_status_by_id = {s["id"]: s for s in old_statuses}
        preserved_statuses = []
        for current in statuses:
            previous = old_status_by_id.get(current["id"])
            if current.get("health") == "error" and previous and previous.get("health") in {"ok", "empty"}:
                cached = {**previous, "checked_at": current.get("checked_at"), "health": previous["health"], "strategy": "cached_last_success", "attempts": current.get("attempts", []), "message": "本轮实时重试失败；保留五日窗口内最近一次成功结果"}
                preserved_statuses.append(cached)
                news.extend(row for row in old if row.get("source_id") == current["id"] and row.get("date", "") >= cutoff.isoformat())
            else:
                preserved_statuses.append(current)
        statuses = preserved_statuses
        title_translations = json.loads((DATA / "translations.json").read_text(encoding="utf-8")) if (DATA / "translations.json").exists() else {}
        translations = {r.get("unique_key"): (r.get("title_zh", ""), r.get("summary_zh", "")) for r in old}
        for row in news:
            title_zh, summary_zh = translations.get(row["unique_key"], ("", ""))
            title_zh = title_zh or title_translations.get(row["title"], "")
            if title_zh:
                row.update({"title_zh": title_zh, "summary_zh": summary_zh, "state": "已翻译"})
        by_key = {r["unique_key"]: r for r in news}
        rows = sorted(by_key.values(), key=lambda r: (r["date"], r["agency"]), reverse=True)
        if args.translate:
            pending_before_google = sum(not r.get("title_zh") for r in rows)
            progress_meta = {**running_meta, "phase": "translating", "translation_total": pending_before_google, "translated": 0}
            (DATA / "meta.json").write_text(json.dumps(progress_meta, ensure_ascii=False, indent=2), encoding="utf-8")
            rows = translate_google(rows)
        for row in rows:
            apply_keyword_markers(row)
            row["state"] = "已翻译" if row.get("title_zh") else "待翻译"
        # 将成功翻译写入标题缓存，后续刷新无需重复请求翻译服务。
        for row in rows:
            if row.get("title_zh"):
                title_translations[row["title"]] = row["title_zh"]
        (DATA / "translations.json").write_text(json.dumps(title_translations, ensure_ascii=False, indent=2), encoding="utf-8")
        (DATA / "news.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        (DATA / "source_status.json").write_text(json.dumps(sorted(statuses, key=lambda x:x["agency"]), ensure_ascii=False, indent=2), encoding="utf-8")
        meta = {"status":"complete", "refreshed_at":datetime.now(timezone.utc).isoformat(), "cutoff":cutoff.isoformat(), "items":len(rows), "sources":len(enabled), "ok":sum(s["health"]=="ok" for s in statuses), "empty":sum(s["health"]=="empty" for s in statuses), "error":sum(s["health"]=="error" for s in statuses), "translated":sum(bool(r.get("title_zh")) for r in rows), "pending_translation":sum(not r.get("title_zh") for r in rows), "content_extracted":sum(bool(r.get("content")) for r in rows), "content_translated":sum(bool(r.get("content_zh")) for r in rows)}
        (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        failed_meta = {"status": "error", "refreshed_at": datetime.now(timezone.utc).isoformat(), "error_message": str(exc)}
        (DATA / "meta.json").write_text(json.dumps(failed_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
