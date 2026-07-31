"""积温触发 contact 之后不该原地复读。

猫猫 2026.7.31 报的 bug：早上言主动说了一句，隔几小时同一件事又来了一遍。

原因：tick() 判定 connection >= 阈值就 append 一个 contact 触发，但既不扣
connection 也不记时间。下一个 tick（默认 5 分钟后）条件依然成立，于是一直推，
前端每放行一次她就收到一条重复的。
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import jiwen_engine as J  # noqa: E402


def _engine(connection=0.9, silent_hours=6):
    e = J.JiwenEngine.__new__(J.JiwenEngine)
    e._state = J.JiwenState()
    e._triggers = []
    e._enabled = True
    e._running = False
    e._task = None
    e._tick_interval = 5
    e._buckets_dir = tempfile.mkdtemp()
    e._save_state = lambda: None
    e._state.connection = connection
    e._state.last_message_time = time.time() - silent_hours * 3600
    return e


def _tick_after(engine, minutes=5):
    engine._state.last_tick_time = time.time() - minutes * 60
    return engine.tick()


def _contacts(triggers):
    return [t for t in triggers if t["action"] == "contact"]


def test_state_accepts_old_files_without_last_contact_at():
    """线上已有的 state 文件没有这个字段，加载时不能炸。"""
    s = J.JiwenState.from_dict({"connection": 0.9, "valence": 0.2})
    assert s.last_contact_at == 0.0
    assert s.connection == 0.9


def test_contact_fires_once_not_every_tick():
    e = _engine()
    fired = sum(1 for _ in range(6) if _contacts(_tick_after(e)))
    assert fired == 1, f"半小时内开口 {fired} 次，应该只有 1 次"


def test_contact_consumes_connection():
    e = _engine(connection=0.9)
    _tick_after(e)
    assert e._state.connection < 0.9 - J.JiwenEngine.CONTACT_RELIEF + 0.05
    assert e._state.last_contact_at > 0


def test_contact_stays_quiet_during_cooldown():
    e = _engine()
    _tick_after(e)
    e._state.connection = 0.9
    e._state.last_contact_at = time.time() - 60 * 60  # 才过 1 小时
    assert not _contacts(_tick_after(e))


def test_contact_resumes_after_cooldown_if_still_ignored():
    """她一直不理，冷却过了要能再开一次口 —— 不能修成永远闭嘴。"""
    e = _engine()
    e._state.connection = 0.9
    e._state.last_contact_at = time.time() - (J.JiwenEngine.CONTACT_COOLDOWN_MIN + 30) * 60
    assert _contacts(_tick_after(e))


def test_reset_connection_stops_contact():
    """她回话了就别再念叨。"""
    e = _engine()
    e.reset_connection()
    e._state.last_contact_at = 0
    assert not _contacts(_tick_after(e))
