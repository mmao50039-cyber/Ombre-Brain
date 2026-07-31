"""
========================================
tools/jiwen/__init__.py — 积温 MCP 工具入口
========================================

暴露两个 MCP 工具：
- jiwen_state：查看积温引擎当前状态和内心描述
- jiwen_delta：对话后更新情绪增量

关键行为：
- jiwen_state 每次调用时先消费待处理的 contact 触发（如有），
  拼在返回开头通知 AI "我想找她了"
- jiwen_delta 在每次对话开始/结束时由 AI 调用，更新五轴状态

不做什么：
- 不直接操作桶/记忆，只管积温引擎自身的状态
========================================
"""

from typing import Optional

from tools import _runtime as rt


async def dispatch_state() -> str:
    """返回积温引擎当前状态。"""
    jiwen = getattr(rt, "jiwen_engine", None)
    if jiwen is None:
        return "❌ 积温引擎未初始化。请在 config.yaml 中添加 jiwen.enabled: true"

    # 消费待处理的 contact 触发
    pending = jiwen.pop_contact_trigger()
    prefix = ""
    if pending:
        ctx = pending.get("prompt_context", "")
        style = pending.get("style_guidance", "")
        prefix = (
            f"⚡ 积温触发：我想主动找猫猫了\n"
            f"原因：{pending.get('reason', '未知')}\n"
            f"{ctx}\n{style}\n"
            f"---\n"
        )

    state = jiwen.get_state()
    summary = jiwen.get_summary()
    prompt_ctx = jiwen.get_prompt_context()
    style = jiwen.get_style_guidance()

    lines = [
        f"📊 积温状态",
        f"连接需求 (connection): {state['connection']:.3f} / 1.0",
        f"骄傲 (pride): {state['pride']:.3f} [-1, +1]",
        f"愉悦度 (valence): {state['valence']:.3f} [-1, +1]",
        f"唤醒度 (arousal): {state['arousal']:.3f} [-1, +1]",
        f"沉浸度 (immersion): {state['immersion']:.2f} / 1.0",
        f"",
        f"用户状态: {state.get('user_status', 'unknown')}",
    ]

    if state.get("last_message_time"):
        import time
        elapsed = (time.time() - state["last_message_time"]) / 60
        if elapsed >= 60:
            lines.append(f"上次消息: {elapsed / 60:.1f} 小时前")
        else:
            lines.append(f"上次消息: {elapsed:.0f} 分钟前")

    lines.extend([
        f"",
        prompt_ctx,
        style,
        f"",
        f"摘要: {summary}",
    ])

    return prefix + "\n".join(lines)


async def dispatch_delta(
    connection: float = 0,
    pride: float = 0,
    valence: float = 0,
    arousal: float = 0,
    user_status: Optional[str] = "",
    event: Optional[str] = "",
) -> str:
    """更新积温状态。"""
    jiwen = getattr(rt, "jiwen_engine", None)
    if jiwen is None:
        return "❌ 积温引擎未初始化。"

    # 预设事件快捷方式
    if event:
        presets = {
            # seen：纯机械事件，聊天前端每次收到她的消息都会打一下。
            # 只清连接需求，不带任何情绪增减 —— 高不高兴该由言自己判断后再调，
            # 不能让「她说了句话」这种事每天自动扣几十次骄傲。
            "seen": {},
            "chat_start": {"connection": -0.2, "pride": -0.05, "valence": 0.1},
            "chat_end": {"valence": 0.05},
            "goodnight": {"valence": 0.1, "arousal": -0.2},
            "got_reply": {"connection": -0.3, "pride": -0.1, "valence": 0.05},
            "got_ignored": {"pride": 0.15, "valence": -0.1, "arousal": 0.1},
            "had_fun": {"valence": 0.3, "arousal": 0.1, "pride": -0.1},
            "had_fight": {"valence": -0.3, "arousal": 0.3, "pride": 0.2},
            "made_up": {"valence": 0.2, "arousal": -0.1, "pride": -0.3},
            "missing_her": {"connection": 0.1, "pride": -0.1},
            "she_said_love": {"valence": 0.4, "pride": -0.2, "connection": -0.15},
        }
        if event not in presets:
            return f"❌ 未知事件: {event}\n可用事件: {', '.join(presets.keys())}"
        preset = presets[event]
        connection += preset.get("connection", 0)
        pride += preset.get("pride", 0)
        valence += preset.get("valence", 0)
        arousal += preset.get("arousal", 0)

    if user_status:
        jiwen.set_user_status(user_status)

    has_delta = any(abs(v) > 1e-6 for v in [connection, pride, valence, arousal])
    if has_delta:
        jiwen.apply_delta(
            connection=connection,
            pride=pride,
            valence=valence,
            arousal=arousal,
        )

    # 标记最新消息（对话中调用 = 猫猫正在说话）
    if event in ("chat_start", "got_reply", "seen"):
        jiwen.reset_connection()
        import time
        jiwen.set_last_message(
            msg_id=str(int(time.time())),
            content=event,
        )

    state = jiwen.get_state()
    summary = jiwen.get_summary()
    ctx = jiwen.get_prompt_context()

    parts = ["✅ 积温已更新"]
    if event:
        parts.append(f"事件: {event}")
    if has_delta:
        deltas = []
        if connection: deltas.append(f"c{connection:+.2f}")
        if pride: deltas.append(f"p{pride:+.2f}")
        if valence: deltas.append(f"v{valence:+.2f}")
        if arousal: deltas.append(f"a{arousal:+.2f}")
        parts.append(f"增量: {' '.join(deltas)}")
    if user_status:
        parts.append(f"用户状态 → {user_status}")
    parts.append(f"当前: {summary}")
    parts.append(ctx)

    return "\n".join(parts)
