"""
web/health_data.py — 健康数据接收端点（iOS 快捷指令 → Ombre Brain）

POST /api/health  接收 HealthKit 数据（心率、步数、睡眠等）
GET  /api/health   读取最近的健康数据

鉴权：使用 hook token（同 breath-hook / jiwen 端点）
"""

import json as _json
import os
import time
import logging

from starlette.responses import JSONResponse

from . import _shared as sh
from .hooks import _is_hook_request_authorized

logger = logging.getLogger("ombre_brain")

_HEALTH_FILE = None


def _get_health_file():
    global _HEALTH_FILE
    if _HEALTH_FILE is None:
        buckets_dir = getattr(sh, "config", {}).get("buckets_dir", "./buckets")
        _HEALTH_FILE = os.path.join(buckets_dir, ".health_data.json")
    return _HEALTH_FILE


def _load_health_data() -> list:
    path = _get_health_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return []


def _save_health_data(data: list):
    path = _get_health_file()
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)


def register(mcp) -> None:

    @mcp.custom_route("/api/health", methods=["POST"])
    async def health_post(request):
        if not _is_hook_request_authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        entry = {
            "timestamp": body.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S")),
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        if "heart_rate" in body:
            entry["heart_rate"] = body["heart_rate"]
        if "steps" in body:
            entry["steps"] = body["steps"]
        if "sleep" in body:
            entry["sleep"] = body["sleep"]
        if "calories" in body:
            entry["calories"] = body["calories"]
        if "distance" in body:
            entry["distance"] = body["distance"]
        if "exercise_minutes" in body:
            entry["exercise_minutes"] = body["exercise_minutes"]

        for k, v in body.items():
            if k not in entry and k != "timestamp":
                entry[k] = v

        records = _load_health_data()
        records.append(entry)
        # 只保留最近 200 条
        if len(records) > 200:
            records = records[-200:]
        _save_health_data(records)

        logger.info(f"Health data received: {len(entry)} fields")
        return JSONResponse({"ok": True, "stored_fields": list(entry.keys())})

    @mcp.custom_route("/api/health", methods=["GET"])
    async def health_get(request):
        if not _is_hook_request_authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        params = request.query_params
        limit = int(params.get("limit", "10"))

        records = _load_health_data()
        recent = records[-limit:] if records else []

        return JSONResponse({
            "total_records": len(records),
            "records": recent,
        })
