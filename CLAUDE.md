# CLAUDE.md — Ombre Brain 开发笔记

## 已知问题 & 解决方案

### GitHub 集成 Session 的工具权限限制

**症状**：调用 `create_trigger`、`send_later`、`list_triggers` 等调度/推送工具时，报错：
```
MCP tool call requires approval
```
用户点了"同意"也没用，反复失败。

**原因**：通过 GitHub 集成（issue/PR 自动触发）启动的 session 是"远程执行环境"，调度类和 session 管理类工具被限制了权限，无法使用。

**解决方法**：
1. 在手机 Claude app 或网页 claude.ai/code **新开一个普通对话**
2. 把需要执行的操作复制过去
3. 在那个 session 里执行——权限完整，工具全部可用

**受限工具举例**：`create_trigger`、`send_later`、`list_triggers`、`delete_trigger`、`fire_trigger`

**不受限的 session 类型**：手机 Claude Code、网页 Claude Code 普通对话

---

## 积温引擎（Jiwen Engine）

### Railway 部署信息
- URL：`https://ombre-brain-production-d29c.up.railway.app`
- 积温接口：`GET /api/jiwen?token=20040808`

### 已配置的 Routine
- **积温主动联系检查**（`trig_01PfAp8CydZMabj5Zcv3EzLV`）
  - 每小时第 7 分钟触发（`7 * * * *`）
  - 新 session 里 GET 积温 API，`contact_triggered=true` 时发手机推送 "⚡ 言想你了"
  - 在猫猫手机 Claude Code 的普通对话里创建，云端运行，不依赖设备在线

### 触发逻辑
- `jiwen_engine.py` 的 `_on_triggers` 方法：connection 达到阈值时触发
- 触发后写 `_jiwen_pending_contact.json` 并调用 `_push_webhook`（POST 到 `OMBRE_HOOK_URL`）
- `OMBRE_HOOK_URL` 环境变量未设置时跳过 webhook，不报错
