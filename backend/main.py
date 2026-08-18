"""
Clash Dashboard Backend
FastAPI service that proxies and extends Clash's REST API
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Any

import aiofiles
import httpx
import yaml
import uvicorn
import traceback

import base64 as _base64
import datetime as _dt
import asyncio as _asyncio
import httpx as _httpx
import socket as _socket
import uvicorn.config as _uc


# Custom string class so yaml.dump always outputs single-quoted values.
# Prevents YAML from misinterpreting "127.0.0.1:9090" as a key-value mapping.
class _ForceQuotedStr(str):
    pass


def _force_quoted_str(s: str) -> _ForceQuotedStr:
    return _ForceQuotedStr(s)


yaml.add_representer(
    _ForceQuotedStr,
    lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:str", data, style="'"),
)
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 共享 httpx 客户端，避免每次请求重建连接池
_http_client: httpx.AsyncClient | None = None
LAUNCHER_CMD_PORT=9099

# ---------------------------------------------------------------------------
# ── subscriptions.json 串行写入锁 ───────────────────────────────────────────
# ---------------------------------------------------------------------------

# 所有对 subscriptions.json 的写操作必须经过这个锁。
# asyncio.Lock 可以保证同一个 FastAPI 进程内的并发请求按顺序完成。
SUBSCRIPTIONS_WRITE_LOCK = asyncio.Lock()

# 用于区分：
#   active_subscription 没有要求修改
# 和：
#   active_subscription 明确要求设置为 None
_UNSET = object()

# ---------------------------------------------------------------------------
# ── subscriptions.json 统一更新函数 ────────────────────────────────────────
# ---------------------------------------------------------------------------

_UNSET = object()

SUBSCRIPTIONS_WRITE_LOCK = asyncio.Lock()


async def update_subscription_storage(
    *,
    sub_id: str | None = None,
    updates: dict | None = None,
    active_subscription=_UNSET,
    add_subscription: dict | None = None,
    delete_subscription: bool = False,
    clear_active_if_sub_id: str | None = None,
):
    """
    subscriptions.json 唯一写入口。

    所有写操作：
        - 串行执行
        - 每次操作前重新读取最新文件
        - 只修改明确指定的字段
        - 不允许业务函数直接保存 subscriptions.json
    """

    async with SUBSCRIPTIONS_WRITE_LOCK:

        data = load_json_file(
            SUBSCRIPTIONS_FILE,
            {
                "subscriptions": [],
                "active_subscription": None,
            }
        )

        if not isinstance(data, dict):
            data = {
                "subscriptions": [],
                "active_subscription": None,
            }

        if not isinstance(data.get("subscriptions"), list):
            data["subscriptions"] = []

        # ===============================================================
        # 新增订阅
        # ===============================================================

        if add_subscription is not None:

            new_sub = dict(add_subscription)

            if not new_sub.get("id"):
                raise ValueError(
                    "新增订阅必须包含 id"
                )

            if any(
                str(item.get("id")) == str(new_sub["id"])
                for item in data["subscriptions"]
            ):
                raise ValueError(
                    f"Subscription already exists: "
                    f"{new_sub['id']}"
                )

            data["subscriptions"].append(new_sub)

        # ===============================================================
        # 修改 / 删除订阅
        # ===============================================================

        if sub_id is not None:

            sub = next(
                (
                    item
                    for item in data["subscriptions"]
                    if str(item.get("id")) == str(sub_id)
                ),
                None
            )

            if sub is None:
                raise KeyError(
                    f"Subscription not found: {sub_id}"
                )

            if delete_subscription:

                data["subscriptions"] = [
                    item
                    for item in data["subscriptions"]
                    if str(item.get("id")) != str(sub_id)
                ]

            else:

                if updates:
                    for key, value in updates.items():

                        if key == "id":
                            raise ValueError(
                                "不允许修改 subscription id"
                            )

                        sub[key] = value

        # ===============================================================
        # 如果删除的是当前激活订阅，自动清空 active_subscription
        # ===============================================================

        if clear_active_if_sub_id is not None:

            if (
                data.get("active_subscription")
                == clear_active_if_sub_id
            ):
                data["active_subscription"] = None

        # ===============================================================
        # 修改顶层 active_subscription
        # ===============================================================

        if active_subscription is not _UNSET:
            data["active_subscription"] = active_subscription

        # ===============================================================
        # 唯一写入口
        # ===============================================================

        save_json_file(
            SUBSCRIPTIONS_FILE,
            data
        )

        logger.info(
            "[SUBS:WRITE] "
            f"sub_id={sub_id!r}, "
            f"updates={list(updates.keys()) if updates else []}, "
            f"add={add_subscription is not None}, "
            f"delete={delete_subscription}, "
            f"active_changed="
            f"{active_subscription is not _UNSET}, "
            f"clear_active_if="
            f"{clear_active_if_sub_id!r}"
        )

        return data


def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _http_client

# Configure structured logging to backend.log (same dir as launcher.py)
import sys as _sys
_backend_root = Path(__file__).resolve().parent.parent   # project root
_log_file = _backend_root / "backend.log"
_file_handler = logging.FileHandler(_log_file, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
_stream_handler = logging.StreamHandler(_sys.stdout)
_stream_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
))

_logger = logging.getLogger("clash-dashboard")
_logger.setLevel(logging.DEBUG)  # 临时；实际级别在下面 read_settings() 后重设
_logger.addHandler(_file_handler)
_logger.addHandler(_stream_handler)

# ── Reuse the same formatter for uvicorn loggers ───────────────────────────
_uvicorn_fmt = _file_handler.formatter

uvicorn_access = logging.getLogger("uvicorn.access")
uvicorn_access.handlers.clear()
uvicorn_access.addHandler(logging.FileHandler(_log_file, encoding="utf-8"))
uvicorn_access.handlers[-1].setFormatter(_uvicorn_fmt)
uvicorn_access.propagate = False
uvicorn_access.setLevel(logging.DEBUG)  # 始终 DEBUG，由 handler formatter 过滤

uvicorn_error = logging.getLogger("uvicorn.error")
uvicorn_error.handlers.clear()
uvicorn_error.addHandler(logging.FileHandler(_log_file, encoding="utf-8"))
uvicorn_error.handlers[-1].setFormatter(_uvicorn_fmt)
uvicorn_error.propagate = False

logger = _logger  # module-level alias

# ---------------------------------------------------------------------------
# ── Resolve paths (needed by _read_settings below) ─────────────────────────
# ---------------------------------------------------------------------------
_backend_dir = Path(__file__).resolve().parent          # backend/
CONFIG_DIR = Path(os.getenv("CONFIG_DIR")) if os.getenv("CONFIG_DIR") else _backend_dir.parent / "clash-config"
SUBSCRIPTIONS_FILE = CONFIG_DIR / "subscriptions.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# ---------------------------------------------------------------------------
# ── Load settings.json BEFORE logger level is finalized ────────────────────
# ---------------------------------------------------------------------------

def _apply_log_level(level_str: str) -> None:
    """Set backend + uvicorn_error logger levels from a level string (silent/info/debug/...).
    Note: uvicorn_access is always DEBUG; its handler formatter filters by the backend level.
    """
    _LEVEL_MAP = {
        "silent": logging.CRITICAL,
        "error":  logging.ERROR,
        "warning": logging.WARNING,
        "info":   logging.INFO,
        "debug":  logging.DEBUG,
    }
    lvl = _LEVEL_MAP.get(level_str, logging.INFO)
    _logger.setLevel(lvl)
    # uvicorn_access stays at DEBUG; its handler uses the same formatter so
    # output respects the configured level without the INFO: prefix.
    uvicorn_error.setLevel(lvl)


def _read_settings() -> dict:
    """Load settings.json, apply log_level, then return the full dict."""
    try:
        local = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        local = {}
    # Apply backend log level from settings (mihomo log-level and backend log level share the field)
    _apply_log_level(local.get("log_level", "info"))
    return local


# Read settings early so log level is set before the startup banner fires
local_settings = _read_settings()

# ---------------------------------------------------------------------------
# ── Environment / CLI overrides ────────────────────────────────────────────
# ---------------------------------------------------------------------------
# 支持 CLASH_API_URL 或 CLASH_API_BASE 任一环境变量
CLASH_API_BASE = os.getenv("CLASH_API_URL") or os.getenv("CLASH_API_BASE", "http://127.0.0.1:9090")
CLASH_SECRET = os.getenv("CLASH_SECRET", "")
# UI settings (stored in settings.json) take priority
if local_settings.get("clash_api_base"):
    CLASH_API_BASE = local_settings["clash_api_base"]
if local_settings.get("clash_secret"):
    CLASH_SECRET = local_settings["clash_secret"]

# ── Startup banner ────────────────────────────────────────────────────────────
logger.info("=" * 60)
logger.info("Clash Dashboard Backend starting")
logger.info(f"  Python: {_sys.version.split()[0]}")
logger.info(f"  CONFIG_DIR: {CONFIG_DIR}")
logger.info(f"  backend log-level: {_logger.level} ({logging.getLevelName(_logger.level)})")
logger.info(f"  CLASH_API_BASE: {CLASH_API_BASE}")
logger.info(f"  subscriptions.json: {SUBSCRIPTIONS_FILE}")
logger.info(f"  subscriptions.json exists: {SUBSCRIPTIONS_FILE.exists()}")
logger.info(f"  config.yaml: {CONFIG_DIR / 'config.yaml'}")
logger.info("=" * 60)

app = FastAPI(title="Clash Dashboard API", version="1.0.0")


# ---------------------------------------------------------------------------
# ── Subscription Auto-Update Scheduler ─────────────────────────────────────
# ---------------------------------------------------------------------------

async def _auto_update_scheduler():
    """Background task: check and trigger subscription updates based on schedule."""
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute

            data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
            now = time.time()

            for sub in data.get("subscriptions", []):
                if not sub.get("auto_update") or not sub.get("url"):
                    continue

                interval_minutes = sub.get("update_interval", 0)
                if interval_minutes <= 0:
                    continue

                last_updated = sub.get("last_updated")
                if last_updated:
                    try:
                        last_ts = _dt.datetime.strptime(last_updated, "%Y-%m-%dT%H:%M:%S").timestamp()
                    except Exception:
                        last_ts = 0
                else:
                    last_ts = 0

                interval_seconds = interval_minutes * 60
                if now - last_ts >= interval_seconds:
                    sub_id = sub["id"]
                    
                    # Skip if already being updated
                    if sub_id in _updating_subs:
                        logger.debug(f"[AUTO-UPDATE] Skipping {sub['name']}: already being updated")
                        continue
                    
                    # Add to updating set
                    _updating_subs.add(sub_id)
                    logger.info(f"[AUTO-UPDATE] Triggering auto-update for subscription: {sub['name']} (id={sub_id})")
                    try:
                        # Reuse the update logic but don't raise on failure
                        await _update_subscription_content(sub_id)
                        logger.info(f"[AUTO-UPDATE] Successfully updated: {sub['name']}")
                    except Exception as e:
                        logger.error(f"[AUTO-UPDATE] Failed to update {sub['name']}: {e}")
                    finally:
                        # Always remove from updating set
                        _updating_subs.discard(sub_id)

        except asyncio.CancelledError:
            logger.info("[AUTO-UPDATE] Scheduler cancelled, shutting down")
            break
        except Exception as e:
            logger.error(f"[AUTO-UPDATE] Scheduler error: {e}")


async def _update_subscription_content(sub_id: str):
    """Internal function to update subscription content without HTTP error raising."""

    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": [], "active_subscription": None})
    sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
    if not sub or not sub.get("url"):
        return

    url = sub["url"]
    raw = None

    # Try direct download
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "clash-verge/1.0.0"})
            resp.raise_for_status()
            raw = resp.text
    except Exception:
        pass

    # Try via proxy if direct failed
    if raw is None:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy="http://127.0.0.1:7890") as client:
                resp = await client.get(url, headers={"User-Agent": "clash-verge/1.0.0"})
                resp.raise_for_status()
                raw = resp.text
        except Exception:
            pass

    if raw is None:
        sub["status"] = "error"
        save_json_file(SUBSCRIPTIONS_FILE, data)
        return

    # Parse YAML or base64
    content = raw.strip()
    try:
        cfg = yaml.safe_load(content)
        proxies = cfg.get("proxies", []) or []
        node_count = len(proxies)
    except Exception:
        try:
            decoded = _base64.b64decode(content).decode("utf-8")
            cfg = yaml.safe_load(decoded)
            proxies = cfg.get("proxies", []) or []
            node_count = len(proxies)
            content = decoded
        except Exception:
            sub["status"] = "error"
            save_json_file(SUBSCRIPTIONS_FILE, data)
            return

    # 提取原始规则
    original_rules = cfg.get("rules", [])

    # Save to sub file
    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
    sub_file.write_text(content, encoding="utf-8")

    # Update subscription metadata
    sub["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    sub["node_count"] = node_count
    sub["status"] = "ok" if node_count > 0 else "error"
    # sub["original_rules"] = original_rules  # 保存原始规则
    save_json_file(SUBSCRIPTIONS_FILE, data)

    # Re-apply to mihomo if this is the active subscription
    if sub_id == data.get("active_subscription"):
        await _apply_sub_to_mihomo(sub_id)


# Start the scheduler as a background task
_scheduler_task: asyncio.Task | None = None

# Track subscriptions currently being updated to prevent duplicate triggers
_updating_subs: set[str] = set()


@app.on_event("startup")
async def start_scheduler():
    global _scheduler_task
    _scheduler_task = asyncio.create_task(_auto_update_scheduler())
    logger.info("[STARTUP] Subscription auto-update scheduler started")


@app.middleware("http")
async def log_all_requests(request: Request, call_next):
    """Log every incoming request and its response status."""
    logger.debug(f"[HTTP] {request.method} {request.url.path}")
    try:
        response = await call_next(request)
        logger.debug(f"[HTTP] {request.method} {request.url.path} → {response.status_code}")
        return response
    except Exception as exc:
        logger.exception(
            f"[HTTP:UNHANDLED] {request.method} {request.url.path} "
            f"raised {type(exc).__name__}: {exc}"
        )
        raise


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all for any unhandled exception — log it and return a 500."""
    tb = traceback.format_exc()
    logger.exception(
        f"[EXCEPTION] {request.method} {request.url.path} "
        f"{type(exc).__name__}: {exc}\n--- TRACEBACK ---\n{tb}--- END ---"
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "path": str(request.url.path)},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clash_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if CLASH_SECRET:
        h["Authorization"] = f"Bearer {CLASH_SECRET}"
    return h


async def clash_get(path: str, silent: bool = False) -> Any:
    url = f"{CLASH_API_BASE}{path}"
    client = get_client()
    if not silent:
        logger.debug(f"[→ GET] {url}")
    try:
        resp = await client.get(url, headers=clash_headers())
        if not silent:
            logger.debug(f"[← GET] {url} → {resp.status_code}")
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}
    except Exception as e:
        if path == "/version":
            logger.warning(f"[clash_get] /version failed: {e}")
        else:
            logger.debug(f"[← GET] {url} → {type(e).__name__}: {e}")
        raise


async def clash_put(path: str, data: dict) -> Any:
    url = f"{CLASH_API_BASE}{path}"
    client = get_client()
    logger.debug(f"[→ PUT] {url} body={data}")
    resp = await client.put(url, json=data, headers=clash_headers())
    logger.debug(f"[← PUT] {url} → {resp.status_code}")
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


async def clash_patch(path: str, data: dict) -> Any:
    url = f"{CLASH_API_BASE}{path}"
    client = get_client()
    logger.debug(f"[→ PATCH] {url} body={data}")
    resp = await client.patch(url, json=data, headers=clash_headers())
    logger.debug(f"[← PATCH] {url} → {resp.status_code}")
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


async def clash_post(path: str, data: dict) -> Any:
    url = f"{CLASH_API_BASE}{path}"
    client = get_client()
    logger.debug(f"[→ POST] {url} body={data}")
    resp = await client.post(url, json=data, headers=clash_headers())
    logger.debug(f"[← POST] {url} → {resp.status_code}")
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


async def clash_delete(path: str) -> Any:
    url = f"{CLASH_API_BASE}{path}"
    client = get_client()
    logger.debug(f"[→ DELETE] {url}")
    resp = await client.delete(url, headers=clash_headers())
    logger.debug(f"[← DELETE] {url} → {resp.status_code}")
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


def load_json_file(path: Path, default: Any = None) -> Any:
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:  # 空文件或只含空白
                return default if default is not None else {}
            return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            # 文件损坏或无法解析，返回默认值
            return default if default is not None else {}
    return default if default is not None else {}


def save_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        f"[JSON:SAVE] path={path} "
        f"active_subscription={data.get('active_subscription') if isinstance(data, dict) else 'N/A'}"
    )
    logger.info(
        f"[JSON:SAVE] subscriptions：{data.get('subscriptions') if isinstance(data, dict) else []}"
    )

    if isinstance(data, dict):
        for sub in data.get("subscriptions", []):
            logger.info(
                f"[JSON:SAVE] sub={sub.get('id')} "
                f"name={sub.get('name')} "
                f"pre_rules={len(sub.get('pre_rules', []))} "
                f"original_rules={len(sub.get('original_rules', []))} "
                f"post_rules={len(sub.get('post_rules', []))}"
            )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ProxySelect(BaseModel):
    name: str


class SubscriptionCreate(BaseModel):
    name: str
    url: str
    auto_update: bool = False
    update_interval: int = 60  # minutes


class SubscriptionUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    auto_update: bool | None = None
    update_interval: int | None = None  # minutes


class RuleItem(BaseModel):
    type: str
    payload: str
    proxy: str


class SettingsUpdate(BaseModel):
    clash_api_base: str | None = None
    clash_secret: str | None = None
    proxy_mode: str | None = None  # "mixed" or "separated"
    mixed_port: int | None = None
    http_port: int | None = None
    socks_port: int | None = None
    allow_lan: bool | None = None
    log_level: str | None = None
    mode: str | None = None
    ipv6: bool | None = None


# ---------------------------------------------------------------------------
# ── Dashboard / Overview ────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/overview")
async def overview():
    """Return aggregated overview data for the home dashboard."""
    version_data = {"version": "unknown"}
    config_data = {}
    connections_data = {"downloadTotal": 0, "uploadTotal": 0, "connections": []}
    proxies_data = {"proxies": {}}

    # 并行请求所有端点（/traffic 是 SSE 流，不在这里调用）

    async def _fetch_all():
        nonlocal version_data, config_data, connections_data, proxies_data
        results = await _asyncio.gather(
            clash_get("/version"),
            clash_get("/configs"),
            clash_get("/connections"),
            clash_get("/proxies"),
            return_exceptions=True,
        )
        for i, (label, res) in enumerate(zip(
            ["version", "configs", "connections", "proxies"], results
        )):
            if isinstance(res, Exception):
                logger.warning(f"[OVERVIEW:fetch] /{label} failed: {type(res).__name__}: {res}")
            else:
                logger.debug(f"[OVERVIEW:fetch] /{label} OK")
        if not isinstance(results[0], Exception):
            version_data = results[0]
        if not isinstance(results[1], Exception):
            config_data = results[1]
        if not isinstance(results[2], Exception):
            connections_data = results[2]
        if not isinstance(results[3], Exception):
            proxies_data = results[3]

    await _fetch_all()

    proxy_count = len(proxies_data.get("proxies", {}))
    active_connections = len(connections_data.get("connections") or [])

    # 读取本地配置，获取端口模式和相关端口
    local_settings = load_json_file(SETTINGS_FILE, {})
    proxy_mode = local_settings.get("proxy_mode", "mixed")
    mixed_port = local_settings.get("mixed_port", 7890)
    http_port = local_settings.get("http_port", 7890)
    socks_port = local_settings.get("socks_port", 7891)

    return {
        "version": version_data.get("version", "unknown"),
        "mode": config_data.get("mode", "rule"),
        "proxy_mode": proxy_mode,
        "mixed_port": mixed_port,
        "http_port": http_port,
        "socks_port": socks_port,
        "allow_lan": config_data.get("allow-lan", False),
        "log_level": config_data.get("log-level", "info"),
        "download_total": connections_data.get("downloadTotal", 0),
        "upload_total": connections_data.get("uploadTotal", 0),
        "active_connections": active_connections,
        "proxy_count": proxy_count,
    }


# ---------------------------------------------------------------------------
# ── Traffic stream ──────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/traffic/stream")
async def traffic_stream():
    """Server-Sent Events stream of live traffic data."""

    async def event_generator():
        while True:
            try:
                # traffic 是 Clash 的 SSE 流，用独立客户端读取（不占用共享池）
                async with _httpx.AsyncClient(timeout=_httpx.Timeout(5.0)) as client:
                    async with client.stream(
                        "GET", f"{CLASH_API_BASE}/traffic", headers=clash_headers()
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if line:
                                yield f"data: {line}\n\n"
            except Exception:
                yield f"data: {json.dumps({'up': 0, 'down': 0})}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# ── Proxies / Nodes ─────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/proxies")
async def get_proxies():
    return await clash_get("/proxies")


@app.get("/api/proxies/{name}")
async def get_proxy(name: str):
    return await clash_get(f"/proxies/{name}")


@app.put("/api/proxies/{group}/select")
async def select_proxy(group: str, body: ProxySelect):
    return await clash_put(f"/proxies/{group}", {"name": body.name})


@app.get("/api/proxies/{name}/delay")
async def get_proxy_delay(name: str, url: str = "http://www.gstatic.com/generate_204", timeout: int = 5000):
    return await clash_get(f"/proxies/{name}/delay?url={url}&timeout={timeout}")


# ---------------------------------------------------------------------------
# ── Proxy Groups ────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/groups")
async def get_groups():
    data = await clash_get("/proxies")
    proxies = data.get("proxies", {})
    groups = {
        k: v for k, v in proxies.items()
        if v.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance")
    }
    return {"groups": groups}


# ---------------------------------------------------------------------------
# ── Rules ───────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/rules")
async def get_rules():
    return await clash_get("/rules")


@app.post("/api/rules/reload")
async def reload_rules():
    """Force Clash to reload the config (reloads rules)."""
    try:
        result = await clash_put("/configs?force=true", {})
        return {"success": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _reload_mihomo_config():
    """Reload mihomo config via REST API."""
    try:
        await clash_put("/configs?force=true", {})
        logger.info("[RELOAD] Mihomo config reloaded")
    except Exception as e:
        logger.warning(f"[RELOAD] Could not reload clash config: {e}")


# ---------------------------------------------------------------------------
# ── Connections ─────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/connections")
async def get_connections():
    return await clash_get("/connections")


@app.delete("/api/connections")
async def close_all_connections():
    return await clash_delete("/connections")


@app.delete("/api/connections/{conn_id}")
async def close_connection(conn_id: str):
    return await clash_delete(f"/connections/{conn_id}")


# ---------------------------------------------------------------------------
# ── Logs ────────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/logs/stream")
async def log_stream(level: str = "info"):
    """SSE stream for Clash logs."""

    async def generator():
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream(
                    "GET",
                    f"{CLASH_API_BASE}/logs?level={level}",
                    headers=clash_headers(),
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line:
                            yield f"data: {line}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'payload': str(exc)})}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# ── Config (Clash core config) ──────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config():
    return await clash_get("/configs")


@app.patch("/api/config")
async def patch_config(request: Request):
    body = await request.json()
    return await clash_patch("/configs", body)


@app.get("/api/config/raw")
async def get_raw_config():
    cfg_path = CONFIG_DIR / "config.yaml"
    if cfg_path.exists():
        return {"content": cfg_path.read_text(encoding="utf-8")}
    return {"content": ""}


@app.post("/api/config/raw")
async def save_raw_config(request: Request):
    body = await request.json()
    content = body.get("content", "")
    cfg_path = CONFIG_DIR / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(content, encoding="utf-8")
    # Reload Clash with new config
    try:
        await clash_put("/configs?force=true", {"path": str(cfg_path)})
    except Exception as e:
        logger.warning(f"Could not reload clash config: {e}")
    return {"success": True}


# ---------------------------------------------------------------------------
# ── Subscriptions ───────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/subscriptions")
async def list_subscriptions():
    logger.info(f"[SUBS:LIST] Loading from {SUBSCRIPTIONS_FILE}  exists={SUBSCRIPTIONS_FILE.exists()}")
    try:
        data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": [], "active_subscription": None})
        logger.info(f"[SUBS:LIST] Returns active_subscription={data.get('active_subscription')}  "
                    f"subs_count={len(data.get('subscriptions', []))}")
        return data
    except Exception as e:
        logger.exception(f"[SUBS:LIST] ERROR: {e}\n{traceback.format_exc()}")
        raise


@app.post("/api/subscriptions")
async def create_subscription(sub: SubscriptionCreate):

    new_sub = {
        "id": str(int(time.time() * 1000)),
        "name": sub.name,
        "url": sub.url,
        "auto_update": sub.auto_update,
        "update_interval": sub.update_interval,
        "last_updated": None,
        "node_count": 0,
        "status": "pending",
        "pre_rules": [],
        "original_rules": [],
        "post_rules": [],
    }

    try:
        await update_subscription_storage(
            add_subscription=new_sub
        )
    except ValueError as e:
        raise HTTPException(
            status_code=409,
            detail=str(e)
        )

    return new_sub


@app.post("/api/subscriptions/file")
async def create_subscription_from_file(
    name: str = Form(...),
    url: str | None = Form(None),
    update_interval: int = Form(0),
    auto_update: bool = Form(False),
    file: UploadFile = File(...),
):
    """
    Upload a YAML config file to create a subscription.
    """
    name = name.strip()
    url = url.strip() if url else None
    auto_update = auto_update and bool(url)

    if not name:
        raise HTTPException(status_code=400, detail="订阅名称不能为空")
    if not file:
        raise HTTPException(status_code=400, detail="请选择配置文件")

    # Read and parse the uploaded YAML file
    content = await file.read()
    try:
        raw_text = content.decode("utf-8")
    except Exception:
        try:
            raw_text = content.decode("gbk")
        except Exception:
            raise HTTPException(status_code=400, detail="文件编码不支持，请使用 UTF-8 编码的 YAML 文件")

    try:
        cfg = yaml.safe_load(raw_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML 解析失败: {e}")

    proxies = cfg.get("proxies", []) or []
    proxy_count = len(proxies)

    # Create subscription entry first so we have the ID for file naming
    sub_id = str(int(time.time() * 1000))

    # Save sub_{id}.yaml so activate can find it later
    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
    try:
        sub_file.write_text(raw_text, encoding="utf-8")
        logger.info(f"[FILE:SUB] Saved sub config with {proxy_count} proxies to {sub_file}")
    except Exception as e:
        logger.error(f"[FILE:SUB] Failed to save sub config: {e}")
        raise HTTPException(status_code=500, detail=f"保存配置文件失败: {e}")

    # Create subscription entry
    new_sub = {
        "id": sub_id,
        "name": name,
        "url": url,
        "auto_update": auto_update,
        "update_interval": update_interval if url else 0,
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "node_count": proxy_count,
        "status": "ok" if proxy_count > 0 else "pending",
        "pre_rules": [],
        "original_rules": [],
        "post_rules": [],
    }

    try:
        await update_subscription_storage(
            add_subscription=new_sub
        )
    except ValueError as e:
        raise HTTPException(
            status_code=409,
            detail=str(e)
        )

    return new_sub


@app.put("/api/subscriptions/{sub_id}")
async def update_subscription(
    sub_id: str,
    sub: SubscriptionUpdate
):
    updates = {}

    if sub.name is not None:
        updates["name"] = sub.name

    if sub.url is not None:
        updates["url"] = sub.url

    if sub.auto_update is not None:
        updates["auto_update"] = sub.auto_update

    if sub.update_interval is not None:
        updates["update_interval"] = sub.update_interval

    if not updates:
        raise HTTPException(
            status_code=400,
            detail="没有需要更新的参数"
        )

    try:
        data = await update_subscription_storage(
            sub_id=sub_id,
            updates=updates
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Subscription not found"
        )

    # 这里只从统一更新函数返回的最新数据中获取结果。
    # 不执行任何修改。
    updated_sub = next(
        s for s in data["subscriptions"]
        if str(s["id"]) == str(sub_id)
    )

    return updated_sub


@app.put("/api/subscriptions/{sub_id}/file")
async def update_subscription_from_file(
    sub_id: str,
    name: str | None = Form(None),
    url: str | None = Form(None),
    update_interval: int = Form(0),
    auto_update: bool = Form(False),
    file: UploadFile | None = File(None),
):
    """
    Update subscription metadata and optionally its YAML config.

    subscriptions.json 的修改全部通过
    update_subscription_storage() 完成。
    """

    # ---------------------------------------------------------------
    # 如果没有文件，只更新基本信息
    # ---------------------------------------------------------------

    if not file:

        updates = {}

        if name is not None and name.strip():
            updates["name"] = name.strip()

        if url is not None:
            clean_url = url.strip() if url.strip() else None
            updates["url"] = clean_url
            updates["auto_update"] = auto_update and bool(clean_url)
            updates["update_interval"] = (
                update_interval if clean_url else 0
            )
        else:
            if auto_update is not None:
                updates["auto_update"] = auto_update

            if update_interval is not None:
                updates["update_interval"] = update_interval

        try:
            data = await update_subscription_storage(
                sub_id=sub_id,
                updates=updates
            )
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail="Subscription not found"
            )

        return next(
            s for s in data["subscriptions"]
            if str(s["id"]) == str(sub_id)
        )

    # ---------------------------------------------------------------
    # 有文件：先读取文件，不碰 subscriptions.json
    # ---------------------------------------------------------------

    content = await file.read()

    try:
        raw_text = content.decode("utf-8")
    except Exception:
        try:
            raw_text = content.decode("gbk")
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="文件编码不支持，请使用 UTF-8 编码的 YAML 文件"
            )

    try:
        cfg = yaml.safe_load(raw_text)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"YAML 解析失败: {e}"
        )

    proxies = cfg.get("proxies", []) or []
    node_count = len(proxies)

    # ---------------------------------------------------------------
    # 写 sub_xxx.yaml
    # ---------------------------------------------------------------

    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"

    try:
        sub_file.write_text(
            raw_text,
            encoding="utf-8"
        )

        logger.info(
            f"[UPDATE:FILE] Saved config with "
            f"{node_count} proxies to {sub_file}"
        )

    except Exception as e:
        logger.error(
            f"[UPDATE:FILE] Failed to save sub config: {e}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"保存配置文件失败: {e}"
        )

    # ---------------------------------------------------------------
    # 这里只更新自己负责的字段
    # ---------------------------------------------------------------

    updates = {
        "auto_update": auto_update and bool(url.strip() if url else None),
        "update_interval": (
            update_interval
            if (url and url.strip())
            else 0
        ),
        "node_count": node_count,
        "status": "ok" if node_count > 0 else "pending",
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if name is not None and name.strip():
        updates["name"] = name.strip()

    if url is not None:
        updates["url"] = url.strip() if url.strip() else None

    try:
        data = await update_subscription_storage(
            sub_id=sub_id,
            updates=updates
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Subscription not found"
        )

    updated_sub = next(
        s for s in data["subscriptions"]
        if str(s["id"]) == str(sub_id)
    )

    # 重新确认是否是当前激活订阅。
    # 注意：这里不能使用旧 data。
    is_active = (
        data.get("active_subscription") == sub_id
    )

    if is_active:
        await _apply_sub_to_mihomo(sub_id)

    return updated_sub


@app.delete("/api/subscriptions/{sub_id}")
async def delete_subscription(sub_id: str):

    try:
        await update_subscription_storage(
            sub_id=sub_id,
            delete_subscription=True,
            clear_active_if_sub_id=sub_id,
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Subscription not found"
        )

    return {
        "success": True
    }


@app.post("/api/subscriptions/{sub_id}/rules/reset")
async def reset_subscription_rules(sub_id: str):

    try:
        data = await update_subscription_storage(
            sub_id=sub_id,
            updates={
                "pre_rules": [],
                "original_rules": [],
                "post_rules": [],
            }
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Subscription not found"
        )

    logger.info(
        f"[RESET_RULES] Reset all rule modifications "
        f"for subscription id={sub_id}"
    )

    if data.get("active_subscription") == sub_id:
        try:
            await _apply_sub_to_mihomo(sub_id)

            logger.info(
                f"[RESET_RULES] Successfully re-applied "
                f"original rules for subscription {sub_id}"
            )

        except Exception as e:
            logger.warning(
                f"[RESET_RULES] Failed to re-apply subscription: {e}"
            )

            return {
                "success": True,
                "warning": (
                    f"规则已重置但应用配置时出错: {str(e)}"
                )
            }

    return {
        "success": True,
        "message": "规则已重置，恢复到订阅原始配置"
    }


@app.post("/api/subscriptions/{sub_id}/update")
async def update_subscription_now(sub_id: str):
    """Download the subscription URL and merge proxies into config."""
    logger.info(f"[UPDATE] Starting update of subscription {sub_id}")

    data = load_json_file(
        SUBSCRIPTIONS_FILE,
        {"subscriptions": [], "active_subscription": None}
    )

    sub = next(
        (s for s in data["subscriptions"] if s["id"] == sub_id),
        None
    )

    if not sub:
        raise HTTPException(
            status_code=404,
            detail="Subscription not found"
        )

    logger.info(
        f"[UPDATE:1] Found subscription: {sub['name']}, URL: {sub['url']}"
    )

    url = sub["url"]
    raw = None
    direct_err = None

    # -----------------------------------------------------------------------
    # Step 1: Try direct download
    # -----------------------------------------------------------------------
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True
        ) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "clash-verge/1.0.0",
                },
            )
            resp.raise_for_status()
            raw = resp.text

            logger.info(
                f"[UPDATE:1] Direct fetch OK ({len(raw)} chars)"
            )

    except Exception as e:
        direct_err = e
        logger.warning(
            f"[UPDATE:1] Direct fetch failed: {e}, "
            f"trying via proxy..."
        )

    # -----------------------------------------------------------------------
    # Step 2: If direct failed, try via mihomo proxy
    # -----------------------------------------------------------------------
    if raw is None:

        def _port_open(host: str, port: int) -> bool:
            try:
                with _socket.create_connection(
                    (host, port),
                    timeout=2
                ):
                    return True
            except Exception:
                return False

        if _port_open("127.0.0.1", 7890):
            try:
                async with httpx.AsyncClient(
                    timeout=30,
                    follow_redirects=True,
                    proxy="http://127.0.0.1:7890",
                ) as client:
                    resp = await client.get(
                        url,
                        headers={
                            "User-Agent": "clash-verge/1.0.0"
                        },
                    )
                    resp.raise_for_status()
                    raw = resp.text

                    logger.info(
                        f"[UPDATE:1] Proxy fetch OK ({len(raw)} chars)"
                    )

            except Exception as proxy_err:
                logger.error(
                    f"[UPDATE:1] Proxy fetch also failed: {proxy_err}"
                )

                # 🔥 只更新 status
                await update_subscription_storage(
                    sub_id=sub_id,
                    updates={
                        "status": "error"
                    }
                )

                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Failed to fetch subscription "
                        f"(direct: {direct_err}, "
                        f"proxy: {proxy_err})"
                    )
                )

        else:
            logger.error(
                "[UPDATE:FAIL] Direct fetch failed, "
                "no proxy available"
            )

            # 🔥 只更新 status
            await update_subscription_storage(
                sub_id=sub_id,
                updates={
                    "status": "error"
                }
            )

            raise HTTPException(
                status_code=502,
                detail=f"Failed to fetch subscription: {direct_err}"
            )

    if raw is None:
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch subscription: unknown error"
        )

    logger.debug(
        f"[UPDATE:2] Fetched {len(raw)} chars from {url}"
    )

    # -----------------------------------------------------------------------
    # Step 3: Parse subscription content
    # -----------------------------------------------------------------------
    content = raw.strip()
    parse_method = "unknown"

    try:
        cfg = yaml.safe_load(content)
        proxies = cfg.get("proxies", []) or []
        node_count = len(proxies)
        parse_method = "plain_yaml"

        logger.info(
            f"[UPDATE:3] Parsed as plain YAML: "
            f"{node_count} proxies"
        )

    except Exception as yaml_err:
        logger.warning(
            f"[UPDATE:3] YAML parse failed ({yaml_err}) "
            f"— trying base64"
        )

        try:
            decoded = _base64.b64decode(content).decode("utf-8")

            try:
                cfg2 = yaml.safe_load(decoded)
                decoded_cfg = cfg2
            except Exception:
                decoded_cfg = None

            if decoded_cfg is None:
                try:
                    double_decoded = _base64.b64decode(
                        decoded.strip()
                    ).decode("utf-8")

                    decoded = double_decoded

                    logger.info(
                        "[UPDATE:3] Double base64 decode OK"
                    )

                except Exception:
                    pass

            cfg = yaml.safe_load(decoded)
            proxies = cfg.get("proxies", []) or []
            node_count = len(proxies)
            content = decoded
            parse_method = "base64_yaml"

            logger.info(
                f"[UPDATE:3] Base64 decode OK: "
                f"{node_count} proxies"
            )

        except Exception as b64_err:
            logger.error(
                f"[UPDATE:FAIL] YAML failed ({yaml_err}), "
                f"base64 failed ({b64_err}) — rejecting content"
            )

            # 🔥 这里只更新本次更新操作负责的三个元数据字段
            await update_subscription_storage(
                sub_id=sub_id,
                updates={
                    "last_updated": time.strftime(
                        "%Y-%m-%dT%H:%M:%S"
                    ),
                    "node_count": 0,
                    "status": "error",
                }
            )

            raise HTTPException(
                status_code=422,
                detail=(
                    "无法解析订阅内容：YAML解析失败，"
                    "base64解码也失败。"
                    "请检查订阅URL是否正确。"
                )
            )

    # -----------------------------------------------------------------------
    # Step 4: Save downloaded subscription config
    # -----------------------------------------------------------------------
    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"

    sub_file.write_text(
        content,
        encoding="utf-8"
    )

    logger.info(
        f"[UPDATE:4] Saved {len(content)} chars to {sub_file} "
        f"(method={parse_method})"
    )

    # -----------------------------------------------------------------------
    # Step 5: Update subscription metadata
    # -----------------------------------------------------------------------
    last_updated = time.strftime("%Y-%m-%dT%H:%M:%S")
    status = "ok" if node_count > 0 else "error"

    # 🔥 只更新本函数负责的字段
    #
    # 不更新：
    #   pre_rules
    #   original_rules
    #   post_rules
    #   name
    #   url
    #   auto_update
    #   update_interval
    #   active_subscription
    #
    await update_subscription_storage(
        sub_id=sub_id,
        updates={
            "last_updated": last_updated,
            "node_count": node_count,
            "status": status,
        }
    )

    logger.info(
        f"[UPDATE:5] Done. "
        f"node_count={node_count}, "
        f"status={status}"
    )

    # -----------------------------------------------------------------------
    # Step 6: Re-apply config if this subscription is active
    # -----------------------------------------------------------------------
    is_active = data.get("active_subscription") == sub_id

    if is_active:
        await _apply_sub_to_mihomo(sub_id)

    return {
        "success": True,
        "node_count": node_count
    }


def get_modified_original_rules(sub_id: str) -> list:
    """
    从订阅文件中提取原始规则，并应用 stored 的修改操作
    返回修改后的原始规则列表（字符串格式，不带 '- ' 前缀）
    """

    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
    if not sub_file.exists():
        return []

    try:
        content = sub_file.read_text(encoding="utf-8").strip()
        # 尝试解码
        try:
            cfg = yaml.safe_load(content)
        except Exception:
            try:
                decoded = _base64.b64decode(content).decode("utf-8")
                cfg = yaml.safe_load(decoded)
            except Exception:
                return []

        original_rules = cfg.get("rules", [])
        if not original_rules:
            return []

        # 加载修改记录
        subs_data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
        sub_meta = next((s for s in subs_data["subscriptions"] if s["id"] == sub_id), {})
        modifications = sub_meta.get("original_rules", [])

        # 构建结果列表
        result = []

        for rule in original_rules:
            # 解析规则字符串，提取 payload
            rule_str = rule
            if isinstance(rule, str):
                rule_str = rule
            else:
                # 如果是对象格式，转为字符串
                if rule.get("payload"):
                    rule_str = f"{rule['type']},{rule['payload']},{rule['proxy']}"
                else:
                    rule_str = f"{rule['type']},{rule['proxy']}"

            # 清理前缀
            if rule_str.startswith('- '):
                rule_str = rule_str[2:]

            # 提取 payload 用于匹配
            parts = rule_str.split(',')
            if len(parts) < 2:
                result.append(rule_str)
                continue

            # payload 是中间部分
            payload = parts[1] if len(parts) > 2 else ''
            rule_type = parts[0]

            # 检查是否有删除操作
            is_deleted = False
            for mod in modifications:
                if mod.get("operation") == "delete" and mod.get("payload") == payload:
                    is_deleted = True
                    break

            if is_deleted:
                continue  # 跳过已删除的规则

            # 检查是否有编辑操作
            edited = False
            for mod in modifications:
                if mod.get("operation") == "edit" and mod.get("payload") == payload:
                    # 应用编辑
                    edited = True
                    if mod.get("payload"):
                        result.append(f"{mod['type']},{mod['payload']},{mod['proxy']}")
                    else:
                        result.append(f"{mod['type']},{mod['proxy']}")
                    break

            if not edited:
                # 没有修改，保留原规则
                result.append(rule_str)

        return result

    except Exception as e:
        logger.error(f"[get_modified_original_rules] Error: {e}")
        return []


async def _apply_sub_to_mihomo(sub_id: str):
    """
    Read sub_{id}.yaml, decode (if base64), write to config.yaml, and restart mihomo.
    Merges pre_rules, modified original_rules, and post_rules from subscription metadata.
    """
    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
    if not sub_file.exists():
        logger.warning(f"[_APPLY] Sub file not found: {sub_file}")
        raise HTTPException(status_code=400, detail="订阅配置文件不存在，请先点击「更新」下载或通过文件上传配置")

    sub_content = sub_file.read_text(encoding="utf-8").strip()
    logger.debug(f"[_APPLY] Read {len(sub_content)} chars from {sub_file}")

    content_to_write = sub_content
    try:
        yaml.safe_load(sub_content)
    except Exception as yaml_err:
        logger.warning(f"[_APPLY] YAML parse failed: {yaml_err} — trying base64 decode")
        try:
            decoded = _base64.b64decode(sub_content).decode("utf-8")
            try:
                yaml.safe_load(decoded)
                content_to_write = decoded
                logger.info(f"[_APPLY] Single base64 decode OK")
            except Exception:
                try:
                    double_decoded = _base64.b64decode(decoded.strip()).decode("utf-8")
                    yaml.safe_load(double_decoded)
                    content_to_write = double_decoded
                    logger.info(f"[_APPLY] Double base64 decode OK")
                except Exception as dd_err:
                    logger.error(f"[_APPLY] Decode failed: {dd_err}")
                    raise HTTPException(status_code=400, detail="订阅内容不是有效的 YAML（尝试了 base64 单次和双次解码）")
        except Exception as b64_err:
            logger.error(f"[_APPLY] YAML failed ({yaml_err}), base64 also failed ({b64_err})")
            raise HTTPException(status_code=400, detail="订阅内容不是有效的 YAML 或 base64 格式")

    # 加载订阅元数据
    subs_data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    sub_meta = next((s for s in subs_data["subscriptions"] if s["id"] == sub_id), {})
    pre_rules = sub_meta.get("pre_rules", [])
    post_rules = sub_meta.get("post_rules", [])

    # 🔥 获取修改后的原始规则
    modified_original_rules = get_modified_original_rules(sub_id)

    cfg_path = CONFIG_DIR / "config.yaml"

    # Apply settings from settings.json to subscription config
    local = load_json_file(SETTINGS_FILE, {})
    sub_cfg = yaml.safe_load(content_to_write)
    if sub_cfg is None:
        sub_cfg = {}

    # ─── 合并规则 ───────────────────────────────────────────────────────────────
    new_rules = []

    # 1. 添加前置规则 - 构建完整的规则字符串
    for rule in pre_rules:
        if isinstance(rule, dict):
            if rule.get("payload"):
                rule_str = f"{rule['type']},{rule['payload']},{rule['proxy']}"
            else:
                rule_str = f"{rule['type']},{rule['proxy']}"
            new_rules.append(rule_str)
        elif isinstance(rule, str):
            rule_str = rule.strip()
            if rule_str.startswith('- '):
                rule_str = rule_str[2:]
            new_rules.append(rule_str)

    # 2. 添加原始规则（已应用修改）
    for rule in modified_original_rules:
        if isinstance(rule, str):
            rule_str = rule.strip()
            if rule_str.startswith('- '):
                rule_str = rule_str[2:]
            new_rules.append(rule_str)
        elif isinstance(rule, dict):
            if rule.get("payload"):
                new_rules.append(f"{rule['type']},{rule['payload']},{rule['proxy']}")
            else:
                new_rules.append(f"{rule['type']},{rule['proxy']}")

    # 3. 添加后置规则
    for rule in post_rules:
        if isinstance(rule, dict):
            if rule.get("payload"):
                rule_str = f"{rule['type']},{rule['payload']},{rule['proxy']}"
            else:
                rule_str = f"{rule['type']},{rule['proxy']}"
            new_rules.append(rule_str)
        elif isinstance(rule, str):
            rule_str = rule.strip()
            if rule_str.startswith('- '):
                rule_str = rule_str[2:]
            new_rules.append(rule_str)

    sub_cfg["rules"] = new_rules
    # ───────────────────────────────────────────────────────────────────────────

    # 应用其他设置...
    sub_cfg["allow-lan"] = local.get("allow_lan", True)
    sub_cfg["ipv6"] = local.get("ipv6", False)
    sub_cfg["mode"] = local.get("mode", "rule")
    sub_cfg["log-level"] = local.get("log_level", "info")
    _api_base = local.get("clash_api_base", CLASH_API_BASE)
    _api_host = _api_base.replace("http://", "").replace("https://", "").rstrip("/")
    sub_cfg["external-controller"] = _force_quoted_str(_api_host)
    if local.get("proxy_mode") == "mixed":
        sub_cfg["mixed-port"] = local.get("mixed_port", 7890)
        sub_cfg.pop("http-port", None)
        sub_cfg.pop("socks-port", None)
    else:
        sub_cfg["http-port"] = local.get("http_port", 7890)
        sub_cfg["socks-port"] = local.get("socks_port", 7891)
        sub_cfg.pop("mixed-port", None)

    _secret = local.get("clash_secret", "") or CLASH_SECRET
    if _secret:
        sub_cfg["secret"] = _secret
    else:
        sub_cfg.pop("secret", None)

    sub_cfg.pop("external-ui", None)

    content_to_write = yaml.dump(sub_cfg, allow_unicode=True, sort_keys=False)
    cfg_path.write_text(content_to_write, encoding="utf-8")
    logger.info(f"[_APPLY] Written {len(content_to_write)} chars to {cfg_path}")

    logger.info(f"[_APPLY] Calling mihomo POST /restart ...")
    try:
        result = await clash_post("/restart", {})
        logger.info(f"[_APPLY] mihomo restart response: {result}")
    except Exception as restart_err:
        logger.warning(f"[_APPLY] mihomo restart failed: {restart_err}")
        # ============================================================
        # mihomo API 不可连接
        #
        # 很可能是 mihomo 已经因为配置错误退出。
        # 此时通过 launcher 请求重新启动 mihomo。
        # ============================================================
        launcher_url = (
            f"http://127.0.0.1:{LAUNCHER_CMD_PORT}"
            "/restart-mihomo"
        )
        logger.info(
            f"[_APPLY] Requesting launcher to restart mihomo: "
            f"{launcher_url}"
        )
        try:
            launcher_response = await get_client().post(
                launcher_url,
                timeout=20
            )
            if launcher_response.status_code != 200:
                logger.error(
                    f"[_APPLY] Launcher failed to restart mihomo: "
                    f"{launcher_response.status_code} "
                    f"{launcher_response.text}"
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "mihomo 无法连接，且 launcher "
                        "重新启动 mihomo 失败"
                    )
                )
            logger.info(
                "[_APPLY] Launcher successfully restarted mihomo"
            )
        except HTTPException:
            raise
        except Exception as launcher_err:
            logger.error(
                f"[_APPLY] Failed to contact launcher: "
                f"{launcher_err}"
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    f"mihomo 重启失败，且无法联系 launcher: "
                    f"{launcher_err}"
                )
            )

    # Wait for mihomo REST API to be ready
    _api_base = local.get("clash_api_base", CLASH_API_BASE)
    logger.info(f"[_APPLY] Waiting for mihomo API at {_api_base} to be ready ...")
    for i in range(20):
        await asyncio.sleep(0.5)
        try:
            r = await get_client().get(f"{_api_base}/version", headers=clash_headers(), timeout=1)
            if r.status_code == 200:
                logger.info(f"[_APPLY] mihomo API ready after ~{(i + 1) * 0.5:.1f}s")
                break
        except Exception:
            continue
    else:
        raise HTTPException(status_code=503, detail="mihomo REST API 未在 10 秒内就绪，请检查 mihomo 日志")


@app.get("/api/subscriptions/{sub_id}/rules/full")
async def get_full_rules(sub_id: str):
    """
    获取订阅的完整规则列表
    返回: pre_rules, original_rules(已应用修改), post_rules
    """
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # 🔥 获取应用了修改的原始规则（用于显示）
    modified_original_rules = get_modified_original_rules(sub_id)

    return {
        "pre_rules": sub.get("pre_rules", []),
        "original_rules": modified_original_rules,  # 已应用修改的完整列表
        "post_rules": sub.get("post_rules", [])
    }


class RulesSaveRequest(BaseModel):
    pre_rules: list[dict] = []
    original_rules: list[dict] = []
    post_rules: list[dict] = []


@app.post("/api/subscriptions/{sub_id}/rules/save")
async def save_subscription_rules(sub_id: str, request: Request):
    """
    保存订阅的规则修改

    original_rules 是修改记录，格式:
    - 删除: {"operation": "delete", "payload": "example.com"}
    - 编辑: {"operation": "edit", "type": "DOMAIN", "payload": "example.com", "proxy": "PROXY"}

    注意：
    - 规则的计算和合并逻辑在锁外完成
    - 最终写入通过 update_subscription_storage()
    - 只更新当前订阅的 pre_rules / original_rules / post_rules
    - 不修改、不覆盖订阅的其他字段
    """
    try:
        body = await request.json()
        logger.info(f"[SAVE_RULES] Saving rules for subscription {sub_id}")

        # ---------------------------------------------------------------
        # 读取当前数据，仅用于计算，不在这里写回
        # ---------------------------------------------------------------
        data = load_json_file(
            SUBSCRIPTIONS_FILE,
            {"subscriptions": []}
        )

        sub = next(
            (s for s in data["subscriptions"] if s["id"] == sub_id),
            None
        )

        if not sub:
            raise HTTPException(
                status_code=404,
                detail="Subscription not found"
            )

        # ---------------------------------------------------------------
        # 保存前置和后置规则
        # ---------------------------------------------------------------
        pre_rules = body.get("pre_rules", [])
        post_rules = body.get("post_rules", [])

        # ---------------------------------------------------------------
        # 处理原始规则的修改记录
        # ---------------------------------------------------------------
        new_mods = body.get("original_rules", [])
        existing_mods = sub.get("original_rules", [])

        # 验证修改记录格式
        validated_mods = []

        for mod in new_mods:
            if mod.get("operation") == "delete":
                # 删除操作只需要 payload
                if mod.get("payload"):
                    validated_mods.append({
                        "operation": "delete",
                        "payload": mod["payload"]
                    })

            elif mod.get("operation") == "edit":
                # 编辑操作需要完整的规则信息
                if (
                    mod.get("type")
                    and mod.get("payload")
                    and mod.get("proxy")
                ):
                    validated_mods.append({
                        "operation": "edit",
                        "type": mod["type"],
                        "payload": mod["payload"],
                        "proxy": mod["proxy"]
                    })

        # ---------------------------------------------------------------
        # 合并修改记录
        # ---------------------------------------------------------------
        merged_mods = []
        new_delete_payloads = set()
        new_edit_payloads = set()

        # 收集新修改中的 payload
        for mod in validated_mods:
            if mod["operation"] == "delete":
                new_delete_payloads.add(mod["payload"])

            elif mod["operation"] == "edit":
                new_edit_payloads.add(mod["payload"])

        # 保留未被覆盖的旧修改记录
        for mod in existing_mods:
            payload = mod.get("payload")

            if (
                payload in new_delete_payloads
                or payload in new_edit_payloads
            ):
                continue

            merged_mods.append(mod)

        # 添加新修改记录
        merged_mods.extend(validated_mods)

        # ---------------------------------------------------------------
        # 到这里为止全部都是计算
        #
        # 不再：
        #   sub["pre_rules"] = ...
        #   sub["post_rules"] = ...
        #   sub["original_rules"] = ...
        #   save_json_file(...)
        #
        # 最终统一交给 update_subscription_storage()
        # ---------------------------------------------------------------

        logger.info(
            f"[SAVE_RULES] Calculated rules for subscription {sub_id}: "
            f"pre_rules={len(pre_rules)}, "
            f"post_rules={len(post_rules)}, "
            f"original_rules modifications={len(merged_mods)}"
        )

        # ---------------------------------------------------------------
        # 只写当前订阅的三个规则字段
        #
        # update_subscription_storage 内部负责串行化写入，
        # 不会覆盖 name/url/status/node_count 等其他字段。
        # ---------------------------------------------------------------
        await update_subscription_storage(
            sub_id=sub_id,
            updates={
                "pre_rules": pre_rules,
                "original_rules": merged_mods,
                "post_rules": post_rules,
            }
        )

        logger.info(
            f"[SAVE_RULES] Rules storage updated successfully for "
            f"subscription {sub_id}"
        )

        # ---------------------------------------------------------------
        # 如果是激活的订阅，重新应用配置
        #
        # 这里使用当前请求中已经读取的数据判断 active_subscription。
        # 规则存储已经完成后，再执行应用。
        # ---------------------------------------------------------------
        if data.get("active_subscription") == sub_id:
            try:
                await _apply_sub_to_mihomo(sub_id)

                logger.info(
                    f"[SAVE_RULES] Successfully applied rules "
                    f"for subscription {sub_id}"
                )

            except Exception as e:
                logger.warning(
                    f"[SAVE_RULES] Failed to re-apply subscription: {e}"
                )

                return {
                    "success": True,
                    "warning": f"规则已保存但应用配置时出错: {str(e)}"
                }

        return {
            "success": True,
            "message": "规则已保存"
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.error(
            f"[SAVE_RULES] Unexpected error: {e}",
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.post("/api/subscriptions/{sub_id}/activate")
async def activate_subscription(sub_id: str):
    """
    Activate a subscription as the primary Clash config by restarting mihomo.
    """
    logger.info(
        f"[ACTIVATE] Starting activation of subscription {sub_id}"
    )

    data = load_json_file(
        SUBSCRIPTIONS_FILE,
        {
            "subscriptions": [],
            "active_subscription": None
        }
    )

    sub = next(
        (
            s for s in data.get("subscriptions", [])
            if str(s.get("id")) == str(sub_id)
        ),
        None
    )

    if not sub:
        logger.warning(
            f"[ACTIVATE:FAIL] Subscription {sub_id} not found"
        )
        raise HTTPException(
            status_code=404,
            detail="Subscription not found"
        )

    logger.info(
        f"[ACTIVATE:1] Found subscription: {sub['name']}"
    )

    # 先应用配置
    await _apply_sub_to_mihomo(sub_id)

    # 只更新 active_subscription
    await update_subscription_storage(
        active_subscription=sub_id
    )

    logger.info(
        f"[ACTIVATE:7] Done. Active subscription set to {sub_id}"
    )

    return {
        "success": True,
        "message": "Subscription activated, mihomo is restarting",
        "active_subscription": sub_id
    }


# ---------------------------------------------------------------------------
# ── Settings ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings():
    local = load_json_file(SETTINGS_FILE, {})
    try:
        clash_cfg = await clash_get("/configs")
    except Exception:
        clash_cfg = {}
    return {
        "clash_api_base": local.get("clash_api_base", CLASH_API_BASE),
        "clash_secret": "****" if CLASH_SECRET else "",
        "proxy_mode": local.get("proxy_mode", "mixed"),
        "mixed_port": local.get("mixed_port", 7890),
        "http_port": local.get("http_port", 7890),
        "socks_port": local.get("socks_port", 7891),
        "allow_lan": clash_cfg.get("allow-lan", False),
        "log_level": clash_cfg.get("log-level", "info"),
        "mode": clash_cfg.get("mode", "rule"),
        "ipv6": clash_cfg.get("ipv6", False),
    }


@app.patch("/api/settings")
async def update_settings(body: SettingsUpdate):
    local = load_json_file(SETTINGS_FILE, {})
    patch: dict[str, Any] = {}

    if body.clash_api_base is not None:
        local["clash_api_base"] = body.clash_api_base
        global CLASH_API_BASE
        CLASH_API_BASE = body.clash_api_base
    # clash_secret: None = don't change; "" = remove; non-empty = set
    if body.clash_secret is not None:
        global CLASH_SECRET
        if body.clash_secret:
            local["clash_secret"] = body.clash_secret
            CLASH_SECRET = body.clash_secret
        else:
            local.pop("clash_secret", None)
            CLASH_SECRET = ""
    if body.proxy_mode is not None:
        local["proxy_mode"] = body.proxy_mode
        if body.proxy_mode == "mixed":
            # Clear separated ports, set mixed port
            local["mixed_port"] = body.mixed_port if body.mixed_port else 7890
            patch["mixed-port"] = local["mixed_port"]
            patch.pop("http-port", None)
            patch.pop("socks-port", None)
        elif body.proxy_mode == "separated":
            # Clear mixed port, set separated ports
            local["http_port"] = body.http_port if body.http_port else 7890
            local["socks_port"] = body.socks_port if body.socks_port else 7891
            patch.pop("mixed-port", None)
            patch["http-port"] = local["http_port"]
            patch["socks-port"] = local["socks_port"]
    elif body.mixed_port is not None:
        local["mixed_port"] = body.mixed_port
        patch["mixed-port"] = body.mixed_port
    if body.allow_lan is not None:
        local["allow_lan"] = body.allow_lan
        patch["allow-lan"] = body.allow_lan
    if body.log_level is not None:
        local["log_level"] = body.log_level
        patch["log-level"] = body.log_level
        _apply_log_level(body.log_level)  # apply to backend logger immediately
    if body.mode is not None:
        local["mode"] = body.mode
        patch["mode"] = body.mode
    if body.ipv6 is not None:
        local["ipv6"] = body.ipv6
        patch["ipv6"] = body.ipv6

    save_json_file(SETTINGS_FILE, local)

    if patch:
        try:
            await clash_patch("/configs", patch)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to update Clash config: {e}")

    # Re-apply the active subscription so config.yaml reflects the new settings
    subs_data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": [], "active_subscription": None})
    active_sub_id = subs_data.get("active_subscription")
    if active_sub_id:
        sub_file = CONFIG_DIR / f"sub_{active_sub_id}.yaml"
        if sub_file.exists():
            try:
                await _apply_sub_to_mihomo(active_sub_id)
                logger.info(f"[UPDATE_SETTINGS] Re-applied active subscription {active_sub_id} to config.yaml")
            except HTTPException:
                # Non-fatal: settings already persisted, just log
                logger.warning(f"[UPDATE_SETTINGS] Failed to re-apply subscription: {active_sub_id}")

    return {"success": True}


# ---------------------------------------------------------------------------
# ── Version / health ────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    try:
        data = await clash_get("/version", silent=True)
        return {"status": "ok", "clash": data}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ---------------------------------------------------------------------------
# ── Static frontend ─────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

static_dir = Path(os.getenv("STATIC_DIR", "/app/frontend"))
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="frontend")


# ---------------------------------------------------------------------------
# ── Standalone entry (for launcher.py) ─────────────────────────────────────
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("BACKEND_PORT", "8080"))

    # ── Patch uvicorn LOGGING_CONFIG so every socket it creates gets SO_REUSEADDR ──
    # On Windows, a port stays in TIME_WAIT for ~2 min after a process exits.
    # Without this, the first bind_socket() call inside config.load() fails
    # with EADDRINUSE (10048) and exits immediately.
    _orig_bs = uvicorn.Config.bind_socket
    def _reuse_bind_socket(self):
        if self.port and self.host in ("0.0.0.0", "127.0.0.1", ""):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.set_inheritable(False)
                s.bind((self.host or "0.0.0.0", self.port))
                s.listen(128)
                self.sockets = frozenset([s])
                return
            except OSError:
                s.close()
                raise
        _orig_bs(self)
    uvicorn.Config.bind_socket = _reuse_bind_socket

    # ── Patch uvicorn access formatter to remove INFO: level prefix ──────────
    # uvicorn's AccessFormatter reads the logger's level and injects %(levelprefix)s
    # (hardcoded to "INFO"). The only way to remove it is to patch LOGGING_CONFIG
    # BEFORE Config() is created — dictConfig() inside Config.__init__ recreates
    # handlers from the patched config, so this persists.
    _uc.LOGGING_CONFIG["formatters"]["access"]["fmt"] = (
        "%(client_addr)s - \"%(request_line)s\" %(status_code)s"
    )

    logger.info(f"Starting backend on 0.0.0.0:{port} (SO_REUSEADDR patched)")
    config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")

    # uvicorn.Config.__init__ calls dictConfig(LOGGING_CONFIG) which recreates
    # handlers on uvicorn.access/uvicorn.error AFTER our module-level setup.
    # Clear the freshly-created uvicorn.access handler so only our
    # module-level handler (set up above, using the correct formatter) is active.
    _uvicorn_access2 = logging.getLogger("uvicorn.access")
    for _h in _uvicorn_access2.handlers[:]:
        _uvicorn_access2.removeHandler(_h)

    uvicorn.Server(config).run()

