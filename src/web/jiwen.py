"""
web/jiwen.py — 积温状态 HTTP API（供外部定时任务查询）
"""

import json as _json

from starlette.responses import JSONResponse

from . import _shared as sh
from .hooks import _is_hook_request_authorized


def register(mcp) -> None:

    @mcp.custom_route("/api/jiwen", methods=["GET"])
    async def api_jiwen(request):
        if not _is_hook_request_authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        from tools import _runtime as rt
        jiwen = getattr(rt, "jiwen_engine", None)
        if jiwen is None:
            return JSONResponse({"enabled": False})

        pending = jiwen.pop_contact_trigger()
        state = jiwen.get_state()
        summary = jiwen.get_summary()

        return JSONResponse({
            "enabled": True,
            "contact_triggered": pending is not None,
            "trigger": pending,
            "state": {
                k: round(v, 4) if isinstance(v, float) else v
                for k, v in state.items()
            },
            "summary": summary,
            "prompt_context": jiwen.get_prompt_context(),
            "style_guidance": jiwen.get_style_guidance(),
        })
