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

    @mcp.custom_route("/api/jiwen/seen", methods=["POST"])
    async def api_jiwen_seen(request):
        """猫猫说话了 —— 由聊天前端（tg.js）直接报进来。

        在这之前，connection 只有言自己想起来调 jiwen_delta 时才会归零；
        她回了话但没人告诉引擎，需求就一直卡在满值上，同一件事会被反复推给她。
        """
        if not _is_hook_request_authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        from tools import _runtime as rt
        jiwen = getattr(rt, "jiwen_engine", None)
        if jiwen is None:
            return JSONResponse({"enabled": False})

        try:
            body = await request.json()
        except Exception:
            body = {}
        content = str(body.get("content", ""))[:200]
        msg_id = str(body.get("msg_id") or "")

        import time as _time
        jiwen.reset_connection()
        jiwen.set_last_message(msg_id=msg_id or str(int(_time.time())), content=content)
        jiwen.set_user_status("active")

        return JSONResponse({"ok": True, "summary": jiwen.get_summary()})
