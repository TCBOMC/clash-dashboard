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
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 共享 httpx 客户端，避免每次请求重建连接池
_http_client: httpx.AsyncClient | None = None


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
_logger.setLevel(logging.DEBUG)
_logger.addHandler(_file_handler)
_logger.addHandler(_stream_handler)

logger = _logger  # module-level alias

# ---------------------------------------------------------------------------
# Configuration (must be defined before the startup banner below)
# ---------------------------------------------------------------------------
# 支持 CLASH_API_URL 或 CLASH_API_BASE 任一环境变量
CLASH_API_BASE = os.getenv("CLASH_API_URL") or os.getenv("CLASH_API_BASE", "http://127.0.0.1:9090")
CLASH_SECRET = os.getenv("CLASH_SECRET", "")

# Resolve CONFIG_DIR: use env var, or compute relative to this file's location
# This ensures the backend finds config files regardless of working directory
if os.getenv("CONFIG_DIR"):
    CONFIG_DIR = Path(os.getenv("CONFIG_DIR"))
else:
    # Resolve relative to the backend directory (where main.py lives)
    _backend_dir = Path(__file__).resolve().parent          # backend/
    CONFIG_DIR = _backend_dir.parent / "clash-config"        # project/clash-config/
SUBSCRIPTIONS_FILE = CONFIG_DIR / "subscriptions.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# ── Startup banner ────────────────────────────────────────────────────────────
logger.info("=" * 60)
logger.info("Clash Dashboard Backend starting")
logger.info(f"  Python: {_sys.version.split()[0]}")
logger.info(f"  CONFIG_DIR: {CONFIG_DIR}")
logger.info(f"  CLASH_API_BASE: {CLASH_API_BASE}")
logger.info(f"  subscriptions.json: {SUBSCRIPTIONS_FILE}")
logger.info(f"  subscriptions.json exists: {SUBSCRIPTIONS_FILE.exists()}")
logger.info(f"  config.yaml: {CONFIG_DIR / 'config.yaml'}")
logger.info("=" * 60)

app = FastAPI(title="Clash Dashboard API", version="1.0.0")


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
    import traceback
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


async def clash_get(path: str) -> Any:
    url = f"{CLASH_API_BASE}{path}"
    client = get_client()
    logger.debug(f"[→ GET] {url}")
    resp = await client.get(url, headers=clash_headers())
    logger.debug(f"[← GET] {url} → {resp.status_code}")
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


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
        return json.loads(path.read_text(encoding="utf-8"))
    return default if default is not None else {}


def save_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    update_interval: int = 3600  # seconds


class SubscriptionUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    auto_update: bool | None = None
    update_interval: int | None = None


class RuleItem(BaseModel):
    type: str
    payload: str
    proxy: str


class SettingsUpdate(BaseModel):
    clash_api_base: str | None = None
    clash_secret: str | None = None
    mixed_port: int | None = None
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
    import asyncio as _asyncio

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

    return {
        "version": version_data.get("version", "unknown"),
        "mode": config_data.get("mode", "rule"),
        "mixed_port": config_data.get("mixed-port", config_data.get("port", 7890)),
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
    import httpx as _httpx

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
    import traceback
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
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    new_sub = {
        "id": str(int(time.time() * 1000)),
        "name": sub.name,
        "url": sub.url,
        "auto_update": sub.auto_update,
        "update_interval": sub.update_interval,
        "last_updated": None,
        "node_count": 0,
        "status": "pending",
    }
    data["subscriptions"].append(new_sub)
    save_json_file(SUBSCRIPTIONS_FILE, data)
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
    Accepts multipart form data with:
      - name: subscription name
      - url: subscription URL (optional)
      - update_interval: auto-update interval in seconds (0 = disabled)
      - auto_update: "true"/"false"
      - file: the YAML config file
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

    # Save the config to clash-config directory
    config_path = CONFIG_DIR / "config.yaml"
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(raw_text)
        logger.info(f"[FILE:SUB] Saved config with {proxy_count} proxies to {config_path}")
    except Exception as e:
        logger.error(f"[FILE:SUB] Failed to save config: {e}")
        raise HTTPException(status_code=500, detail=f"保存配置文件失败: {e}")

    # Create subscription entry
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    new_sub = {
        "id": str(int(time.time() * 1000)),
        "name": name,
        "url": url,
        "auto_update": auto_update,
        "update_interval": update_interval if url else 0,
        "last_updated": None,
        "node_count": proxy_count,
        "status": "active" if proxy_count > 0 else "pending",
    }
    data["subscriptions"].append(new_sub)
    save_json_file(SUBSCRIPTIONS_FILE, data)

    # Reload mihomo config
    await _reload_mihomo_config()

    return new_sub


@app.put("/api/subscriptions/{sub_id}")
async def update_subscription(sub_id: str, sub: SubscriptionUpdate):
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    for item in data["subscriptions"]:
        if item["id"] == sub_id:
            if sub.name is not None:
                item["name"] = sub.name
            if sub.url is not None:
                item["url"] = sub.url
            if sub.auto_update is not None:
                item["auto_update"] = sub.auto_update
            if sub.update_interval is not None:
                item["update_interval"] = sub.update_interval
            save_json_file(SUBSCRIPTIONS_FILE, data)
            return item
    raise HTTPException(status_code=404, detail="Subscription not found")


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
    Update a subscription via file upload or form data.
    Accepts multipart form data with:
      - name: subscription name (optional, keep existing if not provided)
      - url: subscription URL (optional)
      - update_interval: auto-update interval in seconds (0 = disabled)
      - auto_update: "true"/"false"
      - file: the YAML config file (optional, if provided will update the config)
    """
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Update basic info
    if name is not None and name.strip():
        sub["name"] = name.strip()
    if url is not None:
        sub["url"] = url.strip() if url.strip() else None
    sub["auto_update"] = auto_update and bool(sub["url"])
    sub["update_interval"] = update_interval if sub["url"] else 0

    # If file is provided, update the config
    node_count = sub.get("node_count", 0)
    if file:
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
        node_count = len(proxies)

        # Save the config to clash-config directory
        sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
        try:
            sub_file.write_text(raw_text, encoding="utf-8")
            logger.info(f"[UPDATE:FILE] Saved config with {node_count} proxies to {sub_file}")
        except Exception as e:
            logger.error(f"[UPDATE:FILE] Failed to save config: {e}")
            raise HTTPException(status_code=500, detail=f"保存配置文件失败: {e}")

        sub["node_count"] = node_count
        sub["status"] = "ok" if node_count > 0 else "pending"
        sub["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    save_json_file(SUBSCRIPTIONS_FILE, data)
    return sub


@app.delete("/api/subscriptions/{sub_id}")
async def delete_subscription(sub_id: str):
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    data["subscriptions"] = [s for s in data["subscriptions"] if s["id"] != sub_id]
    save_json_file(SUBSCRIPTIONS_FILE, data)
    return {"success": True}


@app.post("/api/subscriptions/{sub_id}/update")
async def update_subscription_now(sub_id: str):
    """Download the subscription URL and merge proxies into config."""
    import traceback
    logger.info(f"[UPDATE] Starting update of subscription {sub_id}")

    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    logger.info(f"[UPDATE:1] Found subscription: {sub['name']}, URL: {sub['url']}")

    url = sub["url"]
    raw = None
    direct_err = None

    # Step 1: Try direct download
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    # Some subscription servers (e.g. Inno6) return full YAML with
                    # proxy nodes only when the UA looks like a Clash client.
                    # Use clash-verge UA as the default to maximise compatibility.
                    "User-Agent": "clash-verge/1.0.0",
                },
            )
            resp.raise_for_status()
            raw = resp.text
            logger.info(f"[UPDATE:1] Direct fetch OK ({len(raw)} chars)")
    except Exception as e:
        direct_err = e
        logger.warning(f"[UPDATE:1] Direct fetch failed: {e}, trying via proxy...")

    # Step 2: If direct failed, try via mihomo proxy (if running)
    if raw is None:
        import socket as _socket
        def _port_open(host: str, port: int) -> bool:
            try:
                with _socket.create_connection((host, port), timeout=2):
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
                        headers={"User-Agent": "clash-verge/1.0.0"},
                    )
                    resp.raise_for_status()
                    raw = resp.text
                    logger.info(f"[UPDATE:1] Proxy fetch OK ({len(raw)} chars)")
            except Exception as proxy_err:
                logger.error(f"[UPDATE:1] Proxy fetch also failed: {proxy_err}")
                sub["status"] = "error"
                save_json_file(SUBSCRIPTIONS_FILE, data)
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch subscription (direct: {direct_err}, proxy: {proxy_err})"
                )
        else:
            logger.error(f"[UPDATE:FAIL] Direct fetch failed, no proxy available")
            sub["status"] = "error"
            save_json_file(SUBSCRIPTIONS_FILE, data)
            raise HTTPException(status_code=502, detail=f"Failed to fetch subscription: {direct_err}")

    if raw is None:
        raise HTTPException(status_code=502, detail="Failed to fetch subscription: unknown error")
    logger.debug(f"[UPDATE:2] Fetched {len(raw)} chars from {url}")

    # Subscription content may be plain YAML or base64-encoded
    content = raw.strip()
    parse_method = "unknown"
    try:
        cfg = yaml.safe_load(content)
        proxies = cfg.get("proxies", []) or []
        node_count = len(proxies)
        parse_method = "plain_yaml"
        logger.info(f"[UPDATE:3] Parsed as plain YAML: {node_count} proxies")
    except Exception as yaml_err:
        logger.warning(f"[UPDATE:3] YAML parse failed ({yaml_err}) — trying base64")
        try:
            import base64 as _base64
            decoded = _base64.b64decode(content).decode("utf-8")
            # Some providers double-encode: try a second decode if first yields base64-looking result
            try:
                cfg2 = yaml.safe_load(decoded)
                decoded_cfg = cfg2
            except Exception:
                decoded_cfg = None

            if decoded_cfg is None:
                # First decode didn't yield valid YAML — try double-decode
                try:
                    double_decoded = _base64.b64decode(decoded.strip()).decode("utf-8")
                    decoded = double_decoded
                    logger.info(f"[UPDATE:3] Double base64 decode OK")
                except Exception:
                    pass  # Not double-encoded, use single decode result

            cfg = yaml.safe_load(decoded)
            proxies = cfg.get("proxies", []) or []
            node_count = len(proxies)
            content = decoded  # save decoded content
            parse_method = "base64_yaml"
            logger.info(f"[UPDATE:3] Base64 decode OK: {node_count} proxies")
        except Exception as b64_err:
            # BOTH YAML and base64 failed — reject the subscription, do NOT save garbage
            logger.error(f"[UPDATE:FAIL] YAML failed ({yaml_err}), base64 failed ({b64_err}) — rejecting content")
            sub["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            sub["node_count"] = 0
            sub["status"] = "error"
            save_json_file(SUBSCRIPTIONS_FILE, data)
            raise HTTPException(
                status_code=422,
                detail=f"无法解析订阅内容：YAML解析失败，base64解码也失败。请检查订阅URL是否正确。"
            )

    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
    sub_file.write_text(content, encoding="utf-8")
    logger.info(f"[UPDATE:4] Saved {len(content)} chars to {sub_file} (method={parse_method})")

    sub["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    sub["node_count"] = node_count
    sub["status"] = "ok" if node_count > 0 else "error"
    save_json_file(SUBSCRIPTIONS_FILE, data)
    logger.info(f"[UPDATE:5] Done. node_count={node_count}, status={sub['status']}")

    return {"success": True, "node_count": node_count}


@app.post("/api/subscriptions/{sub_id}/activate")
async def activate_subscription(sub_id: str):
    """
    Activate a subscription as the primary Clash config by restarting mihomo.
    Each step is logged for debugging.
    """
    import traceback
    logger.info(f"[ACTIVATE] Starting activation of subscription {sub_id}")

    # Step 1: Load subscriptions
    logger.debug(f"[ACTIVATE:1] Loading {SUBSCRIPTIONS_FILE}")
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
    if not sub:
        logger.warning(f"[ACTIVATE:FAIL] Subscription {sub_id} not found")
        raise HTTPException(status_code=404, detail="Subscription not found")
    logger.info(f"[ACTIVATE:1] Found subscription: {sub['name']}")

    # Step 2: Check sub file exists
    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
    logger.debug(f"[ACTIVATE:2] Sub file path: {sub_file}  exists={sub_file.exists()}")
    if not sub_file.exists():
        logger.warning(f"[ACTIVATE:FAIL] Sub file not found: {sub_file}")
        raise HTTPException(status_code=400, detail="Subscription not downloaded yet. Please update first.")

    # Step 3: Read sub file content
    sub_content = sub_file.read_text(encoding="utf-8").strip()
    logger.debug(f"[ACTIVATE:3] Read {len(sub_content)} chars from {sub_file}")
    logger.debug(f"[ACTIVATE:3] First 100 chars: {repr(sub_content[:100])}")

    # Step 4: Try YAML parse; if fails, try base64 decode (with double-decode fallback)
    content_to_write = sub_content
    try:
        yaml.safe_load(sub_content)
        logger.info(f"[ACTIVATE:4] Content is valid YAML (plain text)")
    except Exception as yaml_err:
        logger.warning(f"[ACTIVATE:4] YAML parse failed: {yaml_err} — trying base64 decode")
        try:
            import base64 as _base64
            decoded = _base64.b64decode(sub_content).decode("utf-8")
            # Some providers double-encode: if decoded still isn't valid YAML, try again
            try:
                yaml.safe_load(decoded)
                content_to_write = decoded
                logger.info(f"[ACTIVATE:4] Single base64 decode OK, decoded {len(content_to_write)} chars")
            except Exception:
                # Try double-decode as fallback
                try:
                    double_decoded = _base64.b64decode(decoded.strip()).decode("utf-8")
                    yaml.safe_load(double_decoded)
                    content_to_write = double_decoded
                    logger.info(f"[ACTIVATE:4] Double base64 decode OK, decoded {len(content_to_write)} chars")
                except Exception as dd_err:
                    logger.error(f"[ACTIVATE:FAIL] Single decode failed, double decode also failed: {dd_err}")
                    raise HTTPException(
                        status_code=400,
                        detail="Subscription content is not valid YAML (tried single and double base64 decode)"
                    )
        except Exception as b64_err:
            logger.error(f"[ACTIVATE:FAIL] Not valid YAML (yaml_err={yaml_err}), "
                         f"base64 decode also failed (b64_err={b64_err})")
            raise HTTPException(status_code=400,
                               detail="Subscription content is not valid YAML or base64")

    # Step 5: Write to config.yaml
    cfg_path = CONFIG_DIR / "config.yaml"
    cfg_path.write_text(content_to_write, encoding="utf-8")
    logger.info(f"[ACTIVATE:5] Written {len(content_to_write)} chars to {cfg_path}")

    # Step 6: Trigger mihomo graceful restart
    logger.info(f"[ACTIVATE:6] Calling mihomo POST /restart ...")
    try:
        result = await clash_post("/restart", {})
        logger.info(f"[ACTIVATE:6] mihomo restart response: {result}")
    except Exception as restart_err:
        logger.error(f"[ACTIVATE:FAIL] mihomo restart failed: {restart_err}  "
                     f"type={type(restart_err).__name__}")
        raise HTTPException(status_code=502, detail=f"Failed to restart mihomo: {restart_err}")

    # Step 7: Update active subscription (always set, even if it was already this one)
    data["active_subscription"] = sub_id
    save_json_file(SUBSCRIPTIONS_FILE, data)
    logger.info(f"[ACTIVATE:7] Done. Active subscription set to {sub_id}")
    return {"success": True, "message": "Subscription activated, mihomo is restarting"}


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
        "mixed_port": clash_cfg.get("mixed-port", clash_cfg.get("port", 7890)),
        "socks_port": clash_cfg.get("socks-port", 7891),
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
    if body.mixed_port is not None:
        patch["mixed-port"] = body.mixed_port
    if body.allow_lan is not None:
        patch["allow-lan"] = body.allow_lan
    if body.log_level is not None:
        patch["log-level"] = body.log_level
    if body.mode is not None:
        patch["mode"] = body.mode
    if body.ipv6 is not None:
        patch["ipv6"] = body.ipv6

    save_json_file(SETTINGS_FILE, local)

    if patch:
        try:
            await clash_patch("/configs", patch)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to update Clash config: {e}")

    return {"success": True}


# ---------------------------------------------------------------------------
# ── Version / health ────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    try:
        data = await clash_get("/version")
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

    # ── Patch uvicorn so every socket it creates gets SO_REUSEADDR ─────────────
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

    print(f"[main] Starting backend on 0.0.0.0:{port} (SO_REUSEADDR patched)")
    config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")
    uvicorn.Server(config).run()

