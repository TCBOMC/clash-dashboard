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
import time
from pathlib import Path
from typing import Any

import aiofiles
import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CLASH_API_BASE = os.getenv("CLASH_API_BASE", "http://clash:9090")
CLASH_SECRET = os.getenv("CLASH_SECRET", "")
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/clash-config"))
SUBSCRIPTIONS_FILE = CONFIG_DIR / "subscriptions.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

app = FastAPI(title="Clash Dashboard API", version="1.0.0")

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
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{CLASH_API_BASE}{path}", headers=clash_headers())
        resp.raise_for_status()
        return resp.json()


async def clash_put(path: str, data: dict) -> Any:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.put(
            f"{CLASH_API_BASE}{path}", json=data, headers=clash_headers()
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}


async def clash_patch(path: str, data: dict) -> Any:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.patch(
            f"{CLASH_API_BASE}{path}", json=data, headers=clash_headers()
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}


async def clash_post(path: str, data: dict) -> Any:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CLASH_API_BASE}{path}", json=data, headers=clash_headers()
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}


async def clash_delete(path: str) -> Any:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.delete(f"{CLASH_API_BASE}{path}", headers=clash_headers())
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
    try:
        version_data = await clash_get("/version")
    except Exception:
        version_data = {"version": "unknown"}

    try:
        config_data = await clash_get("/configs")
    except Exception:
        config_data = {}

    try:
        traffic_data = await clash_get("/traffic")
    except Exception:
        traffic_data = {}

    try:
        connections_data = await clash_get("/connections")
    except Exception:
        connections_data = {"downloadTotal": 0, "uploadTotal": 0, "connections": []}

    try:
        proxies_data = await clash_get("/proxies")
    except Exception:
        proxies_data = {"proxies": {}}

    proxy_count = len(proxies_data.get("proxies", {}))
    active_connections = len(connections_data.get("connections", []))

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

    async def event_generator():
        while True:
            try:
                data = await clash_get("/traffic")
                yield f"data: {json.dumps(data)}\n\n"
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
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    return data


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


@app.delete("/api/subscriptions/{sub_id}")
async def delete_subscription(sub_id: str):
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    data["subscriptions"] = [s for s in data["subscriptions"] if s["id"] != sub_id]
    save_json_file(SUBSCRIPTIONS_FILE, data)
    return {"success": True}


@app.post("/api/subscriptions/{sub_id}/update")
async def update_subscription_now(sub_id: str):
    """Download the subscription URL and merge proxies into config."""
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    url = sub["url"]
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.text
    except Exception as e:
        sub["status"] = "error"
        save_json_file(SUBSCRIPTIONS_FILE, data)
        raise HTTPException(status_code=502, detail=f"Failed to fetch subscription: {e}")

    # Try to parse as YAML (Clash config format)
    try:
        cfg = yaml.safe_load(raw)
        proxies = cfg.get("proxies", []) or []
        node_count = len(proxies)
    except Exception:
        proxies = []
        node_count = 0

    # Save raw subscription content
    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
    sub_file.write_text(raw, encoding="utf-8")

    sub["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    sub["node_count"] = node_count
    sub["status"] = "ok"
    save_json_file(SUBSCRIPTIONS_FILE, data)

    return {"success": True, "node_count": node_count}


@app.post("/api/subscriptions/{sub_id}/activate")
async def activate_subscription(sub_id: str):
    """Activate a subscription as the primary Clash config."""
    data = load_json_file(SUBSCRIPTIONS_FILE, {"subscriptions": []})
    sub = next((s for s in data["subscriptions"] if s["id"] == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub_file = CONFIG_DIR / f"sub_{sub_id}.yaml"
    if not sub_file.exists():
        raise HTTPException(status_code=400, detail="Subscription not downloaded yet. Please update first.")

    # Copy as main config
    cfg_path = CONFIG_DIR / "config.yaml"
    cfg_path.write_text(sub_file.read_text(encoding="utf-8"), encoding="utf-8")

    try:
        await clash_put("/configs?force=true", {"path": str(cfg_path)})
    except Exception as e:
        logger.warning(f"Could not reload: {e}")

    data["active_subscription"] = sub_id
    save_json_file(SUBSCRIPTIONS_FILE, data)
    return {"success": True}


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

static_dir = Path("/app/frontend")
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="frontend")
