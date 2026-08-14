from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
DEFAULT_DATABASE_ID = "3b893e41daef805e8d84e4ea188657ac"
CA_FILE = ROOT / "vendor" / "certifi" / "cacert.pem"


def compact_id(value: str) -> str:
    return value.replace("-", "").strip()


def request(token: str, method: str, path: str, payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = Request(
        API + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "FederalSignal/1.0",
        },
    )
    context = ssl.create_default_context(cafile=str(CA_FILE)) if CA_FILE.exists() else ssl.create_default_context()
    last_error = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=45, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Notion API {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, ssl.SSLError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"无法连接 Notion API：{last_error}") from last_error


def find_property(schema: dict, expected: str, wanted_type: str | None = None):
    for name, config in schema.items():
        if name.casefold() == expected.casefold() and (not wanted_type or config.get("type") == wanted_type):
            return name, config
    return None, None


def title_property(schema: dict):
    for name, config in schema.items():
        if config.get("type") == "title":
            return name, config
    raise RuntimeError("目标数据源没有 title 类型字段")


def text_value(value: str, max_chars=10000):
    value = (value or "")[:max_chars]
    return {"rich_text": [{"type": "text", "text": {"content": value[i:i+1900]}} for i in range(0, len(value), 1900)]}


def title_value(value: str):
    return {"title": [{"type": "text", "text": {"content": value[:2000]}}]}


def load_keys(path: str):
    key_path = Path(path)
    if not key_path.exists():
        return set()
    payload = json.loads(key_path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = payload.get("keys", [])
    return {str(value) for value in payload}


def existing_records(token: str, data_source_id: str, key_name: str, content_name: str | None):
    records, cursor = {}, None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        result = request(token, "POST", f"/data_sources/{data_source_id}/query", payload)
        for page in result.get("results", []):
            prop = page.get("properties", {}).get(key_name, {})
            parts = prop.get("rich_text", []) if prop.get("type") == "rich_text" else []
            value = "".join(x.get("plain_text", "") for x in parts)
            if value:
                content = page.get("properties", {}).get(content_name, {}) if content_name else {}
                content_parts = content.get("rich_text", []) if content.get("type") == "rich_text" else []
                records[value] = {"page_id": page.get("id"), "has_content": bool("".join(x.get("plain_text", "") for x in content_parts).strip())}
        if not result.get("has_more"):
            return records
        cursor = result.get("next_cursor")


def state_value(config: dict, desired: str):
    kind = config.get("type")
    options = config.get(kind, {}).get("options", []) if kind in {"status", "select"} else []
    names = [x.get("name") for x in options]
    if desired in names:
        return {kind: {"name": desired}}
    if kind == "select":
        return {"select": {"name": desired}}
    if kind == "status" and names:
        return {"status": {"name": names[0]}}
    return None


def build_properties(row: dict, schema: dict, include_content=True):
    title_name, _ = title_property(schema)
    # 该数据库以“部门”为标题列，新闻标题存放在 rich_text“标题”列。
    title_text_name, _ = find_property(schema, "标题", "rich_text")
    primary_value = row.get("agency") if title_text_name else (row.get("title_zh") or row.get("title") or "无标题")
    props = {title_name: title_value(primary_value or "未知机构")}

    date_name, _ = find_property(schema, "SortDate", "date")
    date_name = date_name or find_property(schema, "日期", "date")[0]
    if date_name and row.get("date"):
        props[date_name] = {"date": {"start": row["date"]}}

    txt_name, _ = find_property(schema, "txt", "rich_text")
    txt_name = txt_name or title_text_name
    if txt_name:
        value = row.get("title_zh") or row.get("title") or row.get("summary_zh") or row.get("summary", "")
        props[txt_name] = text_value(value)

    link_name, _ = find_property(schema, "link", "url")
    link_name = link_name or find_property(schema, "网址", "url")[0]
    if link_name:
        props[link_name] = {"url": row.get("url") or None}

    key_name, _ = find_property(schema, "UniqueKey", "rich_text")
    if key_name:
        props[key_name] = text_value(row["unique_key"])

    state_name, state_config = find_property(schema, "State")
    if state_name and state_config:
        value = state_value(state_config, row.get("state", "已翻译"))
        if value:
            props[state_name] = value
    content_name, _ = find_property(schema, "内容", "rich_text")
    if include_content and content_name and (row.get("content_zh") or row.get("content")):
        props[content_name] = text_value(row.get("content_zh") or row.get("content"), 10000)
    return props


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("metadata", "content"), default="metadata")
    parser.add_argument("--keys", default=str(DATA / "notion-new-keys.json"))
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    lock = DATA / "notion-sync.lock"
    meta_path = DATA / "notion-sync.json"
    if lock.exists():
        print("Notion 同步正在运行")
        return 2
    lock.write_text(str(os.getpid()), encoding="ascii")
    meta = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        token = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN")
        if not token:
            raise RuntimeError("未设置 NOTION_API_KEY 环境变量")
        token = token.strip().strip('"').strip("'")
        database_id = compact_id(os.environ.get("NOTION_DATABASE_ID", DEFAULT_DATABASE_ID))
        database = request(token, "GET", f"/databases/{database_id}")
        sources = database.get("data_sources", [])
        if not sources:
            raise RuntimeError("数据库中没有可用的数据源")
        preferred = os.environ.get("NOTION_DATA_SOURCE_ID")
        selected = next((x for x in sources if compact_id(x.get("id", "")) == compact_id(preferred or "")), sources[0])
        data_source_id = selected["id"]
        data_source = request(token, "GET", f"/data_sources/{data_source_id}")
        schema = data_source.get("properties", {})
        key_name, _ = find_property(schema, "UniqueKey", "rich_text")
        if not key_name:
            request(token, "PATCH", f"/data_sources/{data_source_id}", {"properties": {"UniqueKey": {"rich_text": {}}}})
            data_source = request(token, "GET", f"/data_sources/{data_source_id}")
            schema = data_source.get("properties", {})
            key_name, _ = find_property(schema, "UniqueKey", "rich_text")
        if not key_name:
            raise RuntimeError("无法在目标数据库创建 UniqueKey 去重字段")
        content_name, _ = find_property(schema, "内容", "rich_text")
        known = existing_records(token, data_source_id, key_name, content_name)
        rows = json.loads((DATA / "news.json").read_text(encoding="utf-8"))
        requested_keys = load_keys(args.keys) if args.stage == "content" else set()
        pending = [row for row in rows if args.stage == "metadata" and row.get("unique_key") not in known]
        updates = [row for row in rows if args.stage == "content" and row.get("unique_key") in requested_keys and row.get("unique_key") in known]
        created, updated, errors = 0, 0, []
        created_keys = []
        for row in pending:
            payload = {
                "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": build_properties(row, schema, include_content=False),
                "children": [
                    {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"机构：{row.get('agency', '')}"}}]}},
                    {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": row.get("title", "")}}]}},
                ],
            }
            try:
                request(token, "POST", "/pages", payload)
                created += 1
                created_keys.append(row["unique_key"])
            except RuntimeError as exc:
                errors.append({"unique_key": row.get("unique_key"), "title": row.get("title"), "error": str(exc)})
            time.sleep(0.38)
        for row in updates:
            try:
                request(token, "PATCH", f"/pages/{known[row['unique_key']]['page_id']}", {"properties": build_properties(row, schema, include_content=True)})
                updated += 1
            except RuntimeError as exc:
                errors.append({"unique_key": row.get("unique_key"), "title": row.get("title"), "error": str(exc)})
            time.sleep(0.38)
        meta = {
            "status": "complete" if not errors else "partial",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "database_id": database_id,
            "data_source_id": data_source_id,
            "total_local": len(rows),
            "existing": len(known),
            "created": created,
            "updated": updated,
            "stage": args.stage,
            "skipped": len(rows) - len(pending) - len(updates),
            "errors": errors,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.stage == "metadata":
            retry_marked = [
                row["unique_key"] for row in rows
                if row.get("unique_key") in known
                and not known[row["unique_key"]]["has_content"]
                and (row.get("title_zh") or "").startswith(("🔴", "🟡"))
            ]
            Path(args.keys).write_text(json.dumps({"keys": sorted(set(created_keys + retry_marked))}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0 if not errors else 1
    except Exception as exc:
        meta.update({"status": "error", "finished_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)})
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(meta["error"], file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
