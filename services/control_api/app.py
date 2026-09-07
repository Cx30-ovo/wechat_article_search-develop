"""面向 Windows Desktop Agent 的轻量宿主机 API。"""

from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from pymongo import ASCENDING, MongoClient


HOST = os.getenv("CONTROL_API_HOST", "0.0.0.0")
PORT = int(os.getenv("CONTROL_API_PORT", "8020"))
TOKEN = os.getenv("CONTROL_API_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongodb:27017/weixin")
MONGO_DATABASE = os.getenv("MONGO_DATABASE", "weixin")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_article_url(value: str) -> str:
    parsed = urlsplit((value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or parsed.netloc.lower() != "mp.weixin.qq.com":
        raise ValueError("url 必须是 mp.weixin.qq.com 的 HTTP(S) 文章链接")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, ""))


def stable_account_id(account_name: str) -> str:
    digest = hashlib.sha1(account_name.encode("utf-8")).hexdigest()[:16].upper()
    return f"RPA_{digest}"


def parse_publish_time(value: Any) -> datetime | str | None:
    """尽量沿用桌面采集端的北京时间字段；未知格式原样保留。"""
    if isinstance(value, datetime) or value is None:
        return value
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("年", "-").replace("月", "-").replace("日", "")
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue
    return text


def build_interaction(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    supplied = payload.get("interaction")
    interaction = dict(supplied) if isinstance(supplied, dict) else {}
    interaction.setdefault("collectedAt", now)
    interaction.setdefault("source", "wechat-desktop-rpa")
    return interaction


class Repository:
    def __init__(self) -> None:
        self.client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        self.database = self.client[MONGO_DATABASE]
        self.articles = self.database[os.getenv("MONGO_ARTICLE_COLLECTION", "article")]
        self.accounts = self.database[os.getenv("MONGO_TARGET_COLLECTION", "collection_target")]
        self.heartbeats = self.database["agent_heartbeat"]
        self.events = self.database["collection_runs"]
        self.articles.create_index(
            [("article.urlNormalized", ASCENDING)],
            name="article_url_normalized_unique",
            unique=True,
            sparse=True,
        )
        self.heartbeats.create_index([("agentId", ASCENDING)], unique=True)

    def ping(self) -> None:
        self.client.admin.command("ping")

    def ingest_article(self, payload: dict[str, Any]) -> tuple[str, bool]:
        source_url = str(payload.get("url") or "").strip()
        normalized_url = normalize_article_url(source_url)
        account = str(payload.get("account_name") or payload.get("account") or "").strip()
        title = str(payload.get("title") or "").strip()
        content = str(payload.get("content") or "")
        if not account or not title or not content.strip():
            raise ValueError("account_name、title 和 content 不能为空")
        now = utc_now()
        target = self.accounts.find_one({"name": account}, {"id": 1})
        account_id = str((target or {}).get("id") or "").strip() or stable_account_id(account)
        publish_date = parse_publish_time(payload.get("publish_time"))
        interaction = build_interaction(payload, now)
        existing = self.articles.find_one(
            {"$or": [{"article.urlNormalized": normalized_url}, {"article.url": source_url}]},
            {"_id": 1},
        )
        selector = {"_id": existing["_id"]} if existing else {"article.urlNormalized": normalized_url}
        result = self.articles.update_one(
            selector,
            {
                "$setOnInsert": {
                    "account.id": account_id,
                    "account.name": account,
                    "article.title": title,
                    "article.url": source_url,
                    "article.publishDate": publish_date,
                    "article.content.text": content,
                    "source.type": "wechat-desktop-rpa",
                    "firstCollectedAt": now,
                },
                "$set": {
                    "article.urlNormalized": normalized_url,
                    "source.syncedAt": now,
                    "lastUpdatedAt": now,
                },
                "$push": {"interactionHistory": {"$each": [interaction], "$slice": -90}},
            },
            upsert=True,
        )
        article = self.articles.find_one(
            {"article.urlNormalized": normalized_url}, {"_id": 1}
        )
        return str(article["_id"]), bool(result.upserted_id)

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = self.accounts.find({}, {"_id": 0}).sort([("priority", 1), ("name", 1)])
        return list(rows)

    def heartbeat(self, payload: dict[str, Any]) -> None:
        agent_id = str(payload.get("agent_id") or "").strip()
        if not agent_id:
            raise ValueError("agent_id 不能为空")
        self.heartbeats.update_one(
            {"agentId": agent_id},
            {"$set": {**payload, "agentId": agent_id, "receivedAt": utc_now()}},
            upsert=True,
        )

    def append_event(self, payload: dict[str, Any]) -> None:
        self.events.insert_one({**payload, "receivedAt": utc_now()})


REPOSITORY: Repository | None = None


def repository() -> Repository:
    global REPOSITORY
    if REPOSITORY is None:
        REPOSITORY = Repository()
    return REPOSITORY


class Handler(BaseHTTPRequestHandler):
    server_version = "WechatRPAControlAPI/1.0"

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            raise ValueError("请求体为空或超过 10MB")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _guard(self) -> bool:
        if self._authorized():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            try:
                repository().ping()
                self._json(HTTPStatus.OK, {"ok": True, "service": "control-api"})
            except Exception as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
            return
        if not self._guard():
            return
        try:
            if path == "/api/v1/accounts":
                self._json(HTTPStatus.OK, {"ok": True, "items": repository().list_accounts()})
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = urlparse(self.path).path
        try:
            payload = self._payload()
            if path == "/api/v1/articles":
                article_id, created = repository().ingest_article(payload)
                self._json(
                    HTTPStatus.CREATED if created else HTTPStatus.OK,
                    {"ok": True, "id": article_id, "created": created},
                )
                return
            if path == "/api/v1/agent/heartbeat":
                repository().heartbeat(payload)
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/v1/runs/events":
                repository().append_event(payload)
                self._json(HTTPStatus.CREATED, {"ok": True})
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"control-api {self.address_string()} {format % args}", flush=True)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("缺少 CONTROL_API_TOKEN，拒绝启动无鉴权接收接口")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"control-api listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
