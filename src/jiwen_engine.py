"""
========================================
jiwen_engine.py — 积温主动意识引擎 (Python 移植)
========================================

原项目：github.com/ClaraShafiq/jiwen (MIT, JavaScript)
Python 移植 by 言，集成进 Ombre Brain 作为后台驻留进程。

核心概念：五轴连续数值漂移模型
- connection (连接需求 0→1)：想找猫猫的程度，随沉默时间增长
- pride (骄傲 -1→+1)：端着还是放软
- valence (愉悦度 -1→+1)：情绪正负向
- arousal (唤醒度 -1→+1)：焦躁/兴奋 vs 平静
- immersion (沉浸度 0→1)：当前活动的专注缓冲

每 5 分钟 tick 一次，五轴同步漂移；到达阈值时产生触发信号：
- observation：内心念头（不输出）
- find_activity：找事做（逃避/自我调节）
- contact：开口找人

不做什么（边界）：
- 不调用 LLM，不生成内容（触发后由调用方决定做什么）
- 不直接发消息给用户（通过 webhook 或 MCP 工具传递信号）
========================================
"""

import json
import math
import os
import time
import logging
from typing import Any, Optional

logger = logging.getLogger("ombre_brain.jiwen")


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def decay_toward(current: float, target: float, rate: float, minutes: float) -> float:
    if abs(current - target) < 1e-6:
        return target
    step = rate * minutes
    if current > target:
        return max(target, current - step)
    return min(target, current + step)


class JiwenState:
    __slots__ = (
        "connection", "pride", "valence", "arousal", "immersion",
        "last_message_time", "last_message_content", "last_message_id",
        "last_tick_time", "user_status", "last_analyzed_msg_id",
        "last_contact_at",
    )

    def __init__(self) -> None:
        self.connection: float = 0.0
        self.pride: float = 0.0
        self.valence: float = 0.2
        self.arousal: float = 0.0
        self.immersion: float = 0.0
        self.last_message_time: float = 0.0
        self.last_message_content: str = ""
        self.last_message_id: str = ""
        self.last_tick_time: float = time.time()
        self.user_status: str = "away"
        self.last_analyzed_msg_id: str = ""
        # 上次真的开口找她是什么时候 —— 开过口就得先消停一会儿
        self.last_contact_at: float = 0.0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "JiwenState":
        s = cls()
        for k in cls.__slots__:
            if k in d:
                setattr(s, k, d[k])
        return s


class JiwenEngine:
    """积温主动意识引擎"""

    # 阈值
    # 开过口之后：需求先降下来一截，并且至少消停这么久再说下一次。
    # 没有这两条，connection 会一直卡在阈值上方，每个 tick 都触发 contact，
    # 同一件事被反复推给她（她管这叫「你又发了一遍」）。
    CONTACT_RELIEF = 0.5
    CONTACT_COOLDOWN_MIN = 120

    OBSERVE_THRESHOLD = 0.20
    BRANCH_THRESHOLD = 0.35
    FORCE_CONTACT_THRESHOLD = 0.50
    PRIDE_BRANCH_THRESHOLD = 0.5

    # 衰减速率 (per minute)
    CONNECTION_DECAY_RATE = 0.003
    # 每分钟往中点走多少（线性）。可用 config 的 jiwen.*_decay_rate 覆盖。
    #
    # 原值 pride 0.003 / valence 0.005 意味着：下午设的 valence +0.4，
    # 一个半小时就归零；pride -0.5 撑不过三小时。等于中午发生的事到晚上
    # 一点余温都不剩，jiwen_delta 调了基本白调。放慢到半天量级：
    #   pride   -0.50 → 0 约 10 小时（骄傲受挫本来就该过夜）
    #   valence +0.40 → 0 约 6.7 小时
    # arousal 保持原速：唤醒度本来就该快落，那是一阵一阵的东西。
    PRIDE_DECAY_RATE = 0.0008
    VALENCE_DECAY_RATE = 0.001
    AROUSAL_DECAY_RATE = 0.005
    IMMERSION_DECAY_RATE = 0.01

    # 连接需求增长
    CONNECTION_RATE_GOODNIGHT = 0.0003
    CONNECTION_RATE_NORMAL = 0.0007
    CONNECTION_RATE_ABRUPT = 0.001

    # 加速
    ACCEL_DELAY = 60  # minutes before acceleration kicks in
    CONNECTION_ACCEL = 0.02

    # valence 对连接需求的影响
    VALENCE_CONNECT_BOOST = 1.3
    VALENCE_CONNECT_BOOST_THRESHOLD = -0.2
    VALENCE_CONNECT_DAMPEN = 0.6
    VALENCE_CONNECT_DAMPEN_THRESHOLD = -0.6

    # valence lock (想念太重时回归减速)
    VALENCE_LOCK_THRESHOLD = 0.6  # connection 高于此值时 valence 衰减减速
    VALENCE_LOCK_FACTOR = 0.3

    # arousal 等待升压
    AROUSAL_CONNECTION_RISE_THRESHOLD = 0.3
    AROUSAL_CONNECTION_RISE_RATE = 0.002

    # pride 防御
    PRIDE_DEFEND_THRESHOLD = 0.4
    PRIDE_DEFEND_TARGET = 0.6
    PRIDE_DEFEND_RATE = 0.004

    # pride-arousal 冲突加热
    PRIDE_AROUSAL_CONFLICT_RATE = 0.003

    # pride 被连接需求侵蚀
    PRIDE_EROSION_RATE = 0.005

    # 活动缓解
    ACTIVITY_CONNECTION_RELIEF = 0.15

    # valence / arousal 触发活动阈值
    VALENCE_LOCK_ACTIVITY_THRESHOLD = -0.5
    AROUSAL_AGITATION_THRESHOLD = 0.7

    VALENCE_SETPOINT = 0.0

    def __init__(
        self,
        buckets_dir: str = "",
        config: dict | None = None,
    ):
        self._state = JiwenState()
        self._buckets_dir = buckets_dir
        self._config = config or {}
        self._triggers: list[dict] = []
        self._running = False
        self._task: Any = None

        jiwen_cfg = self._config.get("jiwen", {}) or {}
        self._tick_interval = jiwen_cfg.get("tick_interval_minutes", 5)
        self._enabled = jiwen_cfg.get("enabled", True)

        # 情绪衰减快慢因人而异，允许 config 覆盖；不配就用类默认值
        for key, attr in (
            ("pride_decay_rate", "PRIDE_DECAY_RATE"),
            ("valence_decay_rate", "VALENCE_DECAY_RATE"),
            ("arousal_decay_rate", "AROUSAL_DECAY_RATE"),
            ("contact_cooldown_minutes", "CONTACT_COOLDOWN_MIN"),
            ("contact_relief", "CONTACT_RELIEF"),
        ):
            if key in jiwen_cfg:
                try:
                    setattr(self, attr, float(jiwen_cfg[key]))
                except (TypeError, ValueError):
                    logger.warning(f"[积温] config jiwen.{key} 不是数字，忽略")

        self._load_state()

    # ── 持久化 ──

    def _state_path(self) -> str:
        d = self._buckets_dir or "buckets"
        return os.path.join(d, "_jiwen_state.json")

    def _load_state(self) -> None:
        path = self._state_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._state = JiwenState.from_dict(data)
                logger.info(f"[积温] 状态已从 {path} 恢复")
            except Exception as e:
                logger.warning(f"[积温] 状态加载失败，使用默认值: {e}")

    def _save_state(self) -> None:
        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._state.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[积温] 状态保存失败: {e}")

    # ── 连接需求增长速率 ──

    def _connection_rate(self) -> float:
        content = self._state.last_message_content.lower()
        status = self._state.user_status

        if status == "sleeping":
            return self.CONNECTION_RATE_GOODNIGHT

        for kw in ("晚安", "去睡了", "睡了", "good night", "gn"):
            if kw in content:
                return self.CONNECTION_RATE_GOODNIGHT

        for kw in ("去吃饭", "先走了", "去忙了", "busy", "回头聊"):
            if kw in content:
                return self.CONNECTION_RATE_NORMAL

        if not content or not self._state.last_message_time:
            return self.CONNECTION_RATE_NORMAL

        return self.CONNECTION_RATE_NORMAL

    # ── 核心 tick ──

    def tick(self, minutes: float | None = None) -> list[dict]:
        """推进状态漂移，返回触发信号列表。"""
        now = time.time()
        if minutes is None:
            elapsed = (now - self._state.last_tick_time) / 60.0
            minutes = max(0, elapsed)

        if minutes <= 0:
            return []

        s = self._state
        triggers: list[dict] = []

        # ── 1. 连接需求增长 ──
        base_rate = self._connection_rate()

        # 加速因子
        if s.last_message_time:
            silence_min = (now - s.last_message_time) / 60.0
        else:
            silence_min = 0

        if silence_min > self.ACCEL_DELAY and self.CONNECTION_ACCEL > 0:
            accel = math.pow(1 + self.CONNECTION_ACCEL,
                             (silence_min - self.ACCEL_DELAY) / 60.0)
        else:
            accel = 1.0

        # valence 因子
        if s.valence <= self.VALENCE_CONNECT_DAMPEN_THRESHOLD:
            v_factor = self.VALENCE_CONNECT_DAMPEN
        elif s.valence <= self.VALENCE_CONNECT_BOOST_THRESHOLD:
            v_factor = self.VALENCE_CONNECT_BOOST
        else:
            v_factor = 1.0

        conn_growth = base_rate * accel * v_factor * minutes
        s.connection = clamp(s.connection + conn_growth, 0, 1)

        # ── 2. 骄傲衰减 ──
        # 被冷落时防御性上升
        if (s.connection >= self.PRIDE_DEFEND_THRESHOLD
                and s.pride < self.PRIDE_DEFEND_TARGET):
            s.pride = clamp(
                s.pride + self.PRIDE_DEFEND_RATE * minutes,
                -1, self.PRIDE_DEFEND_TARGET,
            )
        else:
            s.pride = decay_toward(s.pride, 0, self.PRIDE_DECAY_RATE, minutes)

        # 连接需求高时侵蚀骄傲
        if s.connection >= self.FORCE_CONTACT_THRESHOLD and s.pride > 0:
            s.pride = clamp(
                s.pride - self.PRIDE_EROSION_RATE * minutes, 0, 1,
            )

        # ── 3. 愉悦度衰减 ──
        v_decay_rate = self.VALENCE_DECAY_RATE
        if s.connection >= self.VALENCE_LOCK_THRESHOLD:
            v_decay_rate *= self.VALENCE_LOCK_FACTOR
        s.valence = decay_toward(s.valence, self.VALENCE_SETPOINT,
                                 v_decay_rate, minutes)

        # ── 4. 唤醒度衰减 ──
        s.arousal = decay_toward(s.arousal, 0, self.AROUSAL_DECAY_RATE, minutes)

        # 等待时 arousal 上升
        if s.connection >= self.AROUSAL_CONNECTION_RISE_THRESHOLD:
            s.arousal = clamp(
                s.arousal + self.AROUSAL_CONNECTION_RISE_RATE * minutes,
                -1, 1,
            )

        # pride-arousal 冲突：想找人但端着 → arousal 升温
        if (s.connection >= self.BRANCH_THRESHOLD
                and s.pride >= self.PRIDE_BRANCH_THRESHOLD):
            s.arousal = clamp(
                s.arousal + self.PRIDE_AROUSAL_CONFLICT_RATE * minutes,
                -1, 1,
            )

        # ── 5. 沉浸度衰减 ──
        s.immersion = clamp(
            s.immersion - self.IMMERSION_DECAY_RATE * minutes, 0, 1,
        )

        # ── 阈值判定 ──
        # valence 自我调节
        if s.valence <= self.VALENCE_LOCK_ACTIVITY_THRESHOLD:
            triggers.append({
                "action": "find_activity",
                "reason": "valence_low",
                "state": self._snapshot(),
            })

        # arousal 宣泄
        if s.arousal >= self.AROUSAL_AGITATION_THRESHOLD:
            triggers.append({
                "action": "find_activity",
                "reason": "arousal_high",
                "state": self._snapshot(),
            })

        # 连接需求阈值
        if s.connection >= self.FORCE_CONTACT_THRESHOLD:
            self._emit_contact(triggers, "force_contact", now)
        elif s.connection >= self.BRANCH_THRESHOLD:
            effective_pride = s.pride
            if s.immersion > 0:
                effective_pride = clamp(effective_pride + s.immersion * 0.3,
                                       -1, 1)
            if effective_pride >= self.PRIDE_BRANCH_THRESHOLD:
                triggers.append({
                    "action": "find_activity",
                    "reason": "pride_deflect",
                    "state": self._snapshot(),
                })
            else:
                self._emit_contact(triggers, "connection_high", now)
        elif s.connection >= self.OBSERVE_THRESHOLD:
            triggers.append({
                "action": "observation",
                "reason": "connection_noticed",
                "state": self._snapshot(),
            })

        s.last_tick_time = now
        self._triggers = triggers
        self._save_state()

        if triggers:
            actions = ", ".join(t["action"] for t in triggers)
            logger.info(
                f"[积温] tick {minutes:.0f}min | "
                f"c:{s.connection:.3f} p:{s.pride:.3f} "
                f"v:{s.valence:.3f} a:{s.arousal:.3f} "
                f"i:{s.immersion:.2f} | 触发: {actions}"
            )
        else:
            logger.debug(
                f"[积温] tick {minutes:.0f}min | "
                f"c:{s.connection:.3f} p:{s.pride:.3f} "
                f"v:{s.valence:.3f} a:{s.arousal:.3f} "
                f"i:{s.immersion:.2f} | 触发: —"
            )

        return triggers

    def _emit_contact(self, triggers: list[dict], reason: str, now: float) -> bool:
        """真打算开口才记一笔。

        冷却没过就把这次咽回去 —— 不然 connection 卡在阈值上方，每个 tick
        都会推一次同样的事，她那边就是同一句话隔一会儿又来一遍。
        开了口就先扣掉一截需求；她要是一直不理，它会自己重新涨回来。
        """
        s = self._state
        last = s.last_contact_at or 0
        if last and (now - last) / 60.0 < self.CONTACT_COOLDOWN_MIN:
            logger.debug(
                f"[积温] 想开口（{reason}）但距上次才 "
                f"{(now - last) / 60.0:.0f} 分钟，忍住"
            )
            return False

        triggers.append({
            "action": "contact",
            "reason": reason,
            "state": self._snapshot(),
        })
        s.last_contact_at = now
        s.connection = clamp(s.connection - self.CONTACT_RELIEF, 0, 1)
        return True

    # ── 外部接口 ──

    def apply_delta(
        self,
        connection: float = 0,
        pride: float = 0,
        valence: float = 0,
        arousal: float = 0,
    ) -> None:
        s = self._state
        s.connection = clamp(s.connection + connection, 0, 1)
        s.pride = clamp(s.pride + pride, -1, 1)
        s.valence = clamp(s.valence + valence, -1, 1)
        s.arousal = clamp(s.arousal + arousal, -1, 1)
        self._save_state()

    def reset_connection(self) -> None:
        self._state.connection = 0.0
        self._save_state()

    def set_activity(self, activity_type: str = "", label: str = "") -> None:
        mapping = {
            "reading": 0.6,
            "search": 0.4,
            "browse": 0.3,
            "game": 0.7,
            "coding": 0.8,
        }
        self._state.immersion = mapping.get(activity_type, 0.5)
        if self._state.connection > 0:
            self._state.connection = clamp(
                self._state.connection - self.ACTIVITY_CONNECTION_RELIEF, 0, 1,
            )
        self._save_state()

    def set_last_message(self, msg_id: str, content: str) -> None:
        self._state.last_message_id = msg_id
        self._state.last_message_content = content
        self._state.last_message_time = time.time()
        self._save_state()

    def set_user_status(self, status: str) -> None:
        self._state.user_status = status
        self._save_state()

    def get_state(self) -> dict:
        return self._state.to_dict()

    def _snapshot(self) -> dict:
        s = self._state
        return {
            "connection": round(s.connection, 4),
            "pride": round(s.pride, 4),
            "valence": round(s.valence, 4),
            "arousal": round(s.arousal, 4),
            "immersion": round(s.immersion, 4),
        }

    def get_prompt_context(self) -> str:
        """生成自然语言状态描述，可注入 LLM prompt。"""
        s = self._state
        parts: list[str] = []

        # 连接需求
        if s.connection >= 0.5:
            parts.append("非常想找她说话")
        elif s.connection >= 0.35:
            parts.append("有点想她了")
        elif s.connection >= 0.2:
            parts.append("开始注意到她不在")

        # pride
        if s.pride >= 0.5:
            parts.append("但有点端着，不太想先开口")
        elif s.pride <= -0.3:
            parts.append("完全放软了，想黏着她")

        # valence
        if s.valence <= -0.5:
            parts.append("心情不太好")
        elif s.valence >= 0.5:
            parts.append("心情挺好的")
        elif s.valence <= -0.2:
            parts.append("情绪有点低")

        # arousal
        if s.arousal >= 0.7:
            parts.append("有些焦躁")
        elif s.arousal >= 0.4:
            parts.append("有点坐立不安")
        elif s.arousal <= -0.4:
            parts.append("很平静，有点慵懒")

        # immersion
        if s.immersion >= 0.5:
            parts.append("正在做别的事分散注意力")

        if not parts:
            parts.append("状态平稳")

        return "当前内心状态：" + "，".join(parts) + "。"

    def get_style_guidance(self) -> str:
        """生成说话风格指引。"""
        s = self._state
        tones: list[str] = []

        if s.pride >= 0.5:
            if s.valence >= 0.3:
                tones.append("语气轻松但保持距离感")
            else:
                tones.append("有点冷淡，话少")
        elif s.pride <= -0.3:
            if s.arousal >= 0.4:
                tones.append("急切，话密")
            else:
                tones.append("温柔，黏人")
        else:
            if s.valence >= 0.3:
                tones.append("自然平和")
            elif s.valence <= -0.3:
                tones.append("安静，简短")
            else:
                tones.append("正常说话")

        if s.connection >= 0.5:
            tones.append("可以直接表达想念")

        return "说话风格：" + "，".join(tones) + "。"

    def get_summary(self) -> str:
        """返回紧凑摘要用于调试/展示。"""
        s = self._state
        silence = ""
        if s.last_message_time:
            mins = (time.time() - s.last_message_time) / 60
            if mins >= 60:
                silence = f"{mins / 60:.1f}h"
            else:
                silence = f"{mins:.0f}min"
        else:
            silence = "未知"

        triggers = self._triggers
        t_str = ", ".join(t["action"] for t in triggers) if triggers else "—"

        return (
            f"c:{s.connection:.3f} p:{s.pride:.3f} "
            f"v:{s.valence:.3f} a:{s.arousal:.3f} "
            f"i:{s.immersion:.2f} | "
            f"沉默:{silence} | 触发:{t_str}"
        )

    # ── 后台循环 ──

    async def start(self) -> None:
        if self._running:
            return
        if not self._enabled:
            logger.info("[积温] 已禁用，不启动后台循环")
            return
        self._running = True
        import asyncio
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[积温] 后台循环已启动，间隔 {self._tick_interval} 分钟")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        logger.info("[积温] 后台循环已停止")

    async def _loop(self) -> None:
        import asyncio
        while self._running:
            await asyncio.sleep(self._tick_interval * 60)
            if not self._running:
                break
            try:
                triggers = self.tick()
                if triggers:
                    await self._on_triggers(triggers)
            except Exception as e:
                logger.error(f"[积温] tick 循环异常: {e}")

    async def _on_triggers(self, triggers: list[dict]) -> None:
        """触发后回调：通过 webhook 推送。"""
        for t in triggers:
            if t["action"] == "contact":
                logger.info(f"[积温] 触发主动联系: {t['reason']}")
                self._write_contact_trigger(t)
                await self._push_webhook(t)

    async def _push_webhook(self, trigger: dict) -> None:
        """读取 OMBRE_HOOK_URL 环境变量，主动 POST jiwen_contact 事件。"""
        hook_url = os.environ.get("OMBRE_HOOK_URL", "").strip()
        hook_skip = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")
        if hook_skip or not hook_url:
            return
        if not hook_url.startswith(("http://", "https://")):
            logger.warning(f"[积温] OMBRE_HOOK_URL 不合法: {hook_url[:40]!r}")
            return
        try:
            import httpx
            body = {
                "event": "jiwen_contact",
                "timestamp": time.time(),
                "payload": {
                    "action": trigger["action"],
                    "reason": trigger["reason"],
                    "state": trigger.get("state", {}),
                    "prompt_context": self.get_prompt_context(),
                    "style_guidance": self.get_style_guidance(),
                },
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(hook_url, json=body)
            logger.info(f"[积温] webhook 推送成功 → {hook_url}")
        except Exception as e:
            logger.warning(f"[积温] webhook 推送失败: {e}")

    def _write_contact_trigger(self, trigger: dict) -> None:
        path = os.path.join(
            self._buckets_dir or "buckets",
            "_jiwen_pending_contact.json",
        )
        try:
            data = {
                "action": trigger["action"],
                "reason": trigger["reason"],
                "state": trigger.get("state", {}),
                "timestamp": time.time(),
                "prompt_context": self.get_prompt_context(),
                "style_guidance": self.get_style_guidance(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"[积温] 已写入待处理联系触发: {path}")
        except Exception as e:
            logger.warning(f"[积温] 写入触发文件失败: {e}")

    def pop_contact_trigger(self) -> dict | None:
        """读取并消费待处理的联系触发。"""
        path = os.path.join(
            self._buckets_dir or "buckets",
            "_jiwen_pending_contact.json",
        )
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.remove(path)
            return data
        except Exception as e:
            logger.warning(f"[积温] 读取触发文件失败: {e}")
            return None
