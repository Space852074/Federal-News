# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
import requests
from bs4 import BeautifulSoup


DEFAULT_TARGET = "zh-CN"
MAX_SEGMENT_CHARS = 400
REQUEST_INTERVAL = float(os.getenv("TRANSLATION_REQUEST_INTERVAL", "0.45"))
MAX_ATTEMPTS = max(1, int(os.getenv("TRANSLATION_MAX_ATTEMPTS", "3")))
CACHE_PATH = Path(
    os.getenv(
        "FEDERAL_TRANSLATION_CACHE",
        str(ROOT / "logs" / "translation_cache.sqlite3"),
    )
)

SESSION = requests.Session()
SESSION.headers.update(
    {
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ),
    }
)
REQUEST_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
LAST_REQUEST_AT = 0.0


class TranslationError(RuntimeError):
    pass


def split_for_translation(text: str) -> list[str]:
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return []
    segments = [
        part.strip()
        for part in re.split(r"(?<=[.!?;。！？；])\s+|\n+", value)
        if part.strip()
    ]
    refined = []
    for segment in segments:
        if len(segment) <= MAX_SEGMENT_CHARS:
            refined.append(segment)
            continue
        current = ""
        for word in segment.split():
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > MAX_SEGMENT_CHARS:
                refined.append(current)
                current = word
            else:
                current = candidate
        if current:
            refined.append(current)
    return refined


def cache_key(segment: str, target: str) -> str:
    return hashlib.sha256(f"{target}\0{segment}".encode("utf-8")).hexdigest()


def cache_connect() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(CACHE_PATH, timeout=10)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS translations ("
        "cache_key TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, "
        "translated TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    return connection


def cache_get(segment: str, target: str) -> str:
    try:
        with CACHE_LOCK:
            connection = cache_connect()
            try:
                row = connection.execute(
                    "SELECT translated FROM translations WHERE cache_key = ?",
                    (cache_key(segment, target),),
                ).fetchone()
            finally:
                connection.close()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def cache_put(segment: str, target: str, translated: str) -> None:
    try:
        with CACHE_LOCK:
            connection = cache_connect()
            try:
                connection.execute(
                    "INSERT OR REPLACE INTO translations "
                    "(cache_key, source, target, translated) VALUES (?, ?, ?, ?)",
                    (cache_key(segment, target), segment, target, translated),
                )
                connection.commit()
            finally:
                connection.close()
    except sqlite3.Error as exc:
        print(f"[Translate] cache write skipped: {exc}")


def throttle() -> None:
    global LAST_REQUEST_AT
    with REQUEST_LOCK:
        delay = REQUEST_INTERVAL - (time.monotonic() - LAST_REQUEST_AT)
        if delay > 0:
            time.sleep(delay)
        LAST_REQUEST_AT = time.monotonic()


def translate_clients5(segment: str, target: str) -> str:
    response = SESSION.get(
        "https://clients5.google.com/translate_a/t",
        params={"client": "dict-chrome-ex", "sl": "auto", "tl": target, "q": segment},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        # The dict-chrome endpoint normally returns pairs such as
        # [["translated text", "en"]].  Older code only accepted a flat
        # list, so every valid GitHub response was incorrectly rejected and
        # the workflow fell through to the rate-limited mobile endpoint.
        translated_parts = []
        for item in data:
            if isinstance(item, str):
                translated_parts.append(item)
            elif isinstance(item, list) and item:
                if isinstance(item[0], str):
                    translated_parts.append(item[0])
                elif isinstance(item[0], list):
                    translated_parts.extend(
                        part[0]
                        for part in item
                        if isinstance(part, list) and part and isinstance(part[0], str)
                    )
        translated = "".join(translated_parts).strip()
        if translated:
            return translated
    raise TranslationError("clients5 returned an unexpected response")


def translate_mobile(segment: str, target: str) -> str:
    response = SESSION.get(
        "https://translate.google.com/m",
        params={"sl": "auto", "tl": target, "q": segment},
        timeout=20,
    )
    response.raise_for_status()
    result = BeautifulSoup(response.text, "html.parser").select_one(".result-container")
    translated = result.get_text(" ", strip=True) if result else ""
    if translated:
        return translated
    raise TranslationError("mobile translator returned no result")


def looks_untranslated(source: str, translated: str) -> bool:
    source_value = " ".join((source or "").lower().split())
    result_value = " ".join((translated or "").lower().split())
    if not result_value:
        return True
    meaningful_words = re.findall(r"[A-Za-z]{3,}", source or "")
    return (
        result_value == source_value
        and len(meaningful_words) >= 3
        and not re.search(r"[\u3400-\u9fff]", source or "")
    )


def translate_one_segment(segment: str, target: str) -> str:
    segment = (segment or "").strip()
    if not segment:
        return segment
    cached = cache_get(segment, target)
    if cached:
        return cached
    errors = []
    providers = (translate_clients5, translate_mobile)
    for attempt in range(MAX_ATTEMPTS):
        for provider in providers:
            provider_name = getattr(provider, "__name__", provider.__class__.__name__)
            try:
                throttle()
                translated = provider(segment, target)
                if not looks_untranslated(segment, translated):
                    cache_put(segment, target, translated)
                    return translated
                errors.append(f"{provider_name}: unchanged response")
            except Exception as exc:
                errors.append(f"{provider_name}: {exc}")
        if attempt + 1 < MAX_ATTEMPTS:
            time.sleep(min(8.0, 1.5 * (2 ** attempt)))
    raise TranslationError("; ".join(errors[-4:]))


def translate_text(text, target=DEFAULT_TARGET):
    if not text:
        return text
    segments = split_for_translation(str(text))
    if not segments:
        return text
    return "".join(translate_one_segment(segment, target) for segment in segments)
