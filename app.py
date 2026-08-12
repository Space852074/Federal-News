from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import subprocess
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "static"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class Handler(SimpleHTTPRequestHandler):
    def send_json(self, obj, code=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/news":
            rows = load_json(DATA / "news.json", [])
            q = parse_qs(parsed.query)
            agency = q.get("agency", [""])[0]
            text = q.get("q", [""])[0].lower()
            if agency:
                rows = [r for r in rows if r.get("source_id") == agency]
            if text:
                rows = [r for r in rows if text in (r.get("title", "") + r.get("title_zh", "") + r.get("summary_zh", "")).lower()]
            return self.send_json(rows)
        if parsed.path == "/api/sources":
            return self.send_json(load_json(DATA / "source_status.json", load_json(ROOT / "sources.json", [])))
        if parsed.path == "/api/meta":
            return self.send_json(load_json(DATA / "meta.json", {"status": "not_run"}))
        if parsed.path == "/api/notion-status":
            return self.send_json(load_json(DATA / "notion-sync.json", {"status": "not_run"}))
        if parsed.path == "/api/notion.csv":
            rows = load_json(DATA / "news.json", [])
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=["State", "SortDate", "Title", "txt", "link", "UniqueKey"])
            writer.writeheader()
            for r in rows:
                writer.writerow({"State": r.get("state", "待审核"), "SortDate": r.get("date", ""), "Title": r.get("title_zh") or r.get("title"), "txt": r.get("summary_zh") or r.get("summary") or r.get("title_zh") or r.get("title", ""), "link": r.get("url", ""), "UniqueKey": r.get("unique_key", "")})
            payload = stream.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=notion-import.csv")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        path = STATIC / ("index.html" if parsed.path == "/" else parsed.path.lstrip("/"))
        if not path.is_file() or STATIC not in path.resolve().parents:
            self.send_error(404)
            return
        payload = path.read_bytes()
        self.send_response(200)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            mime += "; charset=utf-8"
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        action = urlparse(self.path).path
        if action not in {"/api/refresh", "/api/translate", "/api/notion-sync"}:
            return self.send_json({"error": "not found"}, 404)
        if action == "/api/notion-sync":
            if (DATA / "notion-sync.lock").exists():
                return self.send_json({"status": "running"}, 202)
            length = min(int(self.headers.get("Content-Length", "0") or 0), 16384)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self.send_json({"error": "invalid json"}, 400)
            token = body.get("token") or os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN")
            if not token:
                return self.send_json({"error": "需要 Notion 内部集成令牌"}, 400)
            child_env = os.environ.copy()
            child_env["NOTION_API_KEY"] = token
            if body.get("database_id"):
                child_env["NOTION_DATABASE_ID"] = body["database_id"]
            flags = 0x08000000 if sys.platform == "win32" else 0
            subprocess.Popen([sys.executable, str(ROOT / "notion_sync.py")], cwd=ROOT, creationflags=flags, env=child_env)
            return self.send_json({"status": "started"}, 202)
        if (DATA / "refresh.lock").exists():
            return self.send_json({"status": "running"}, 202)
        flags = 0x08000000 if sys.platform == "win32" else 0
        # 网页刷新应执行完整流程：抓取后立即翻译。没有 OpenAI 密钥时，
        # scraper 会自动使用与 Congress Monitor 相同的 Google 翻译兜底。
        args = [sys.executable, str(ROOT / "scraper.py"), "--translate"]
        if action == "/api/translate":
            args.append("--translate-only")
        subprocess.Popen(args, cwd=ROOT, creationflags=flags)
        self.send_json({"status": "started"}, 202)


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    print("新闻聚合器：http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
