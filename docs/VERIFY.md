# 验收与冒烟

部署后按本文逐项核对。所有命令都可直接复制执行，`$BASE` 指服务地址。

```bash
# 本机直连
BASE=http://127.0.0.1:8000
# 经 Nginx（需要基础认证）
BASE=http://<公网IP>; AUTH="-u pap-admin:<密码>"
```

## 1. 基础健康

```bash
curl -s $BASE/health
curl -s $BASE/ready
```

`/ready` 的 `checks` 必须全为 `true`：`database` / `checkpointer` / `model_configured` / `tool_registry`。
任一为 `false` 都属部署未完成，不要继续后续步骤。

## 2. 工具注册表

```bash
curl -s $AUTH $BASE/api/v1/tools
```

期望 5 个工具（`get_platform_time` / `get_station_snapshot` / `search_knowledge` /
`create_maintenance_ticket` / `execute_code`，后两个 `requires_approval=true`）。
**未注册的工具不会出现在这里，模型也看不到**——白名单是安全边界，不是过滤。

## 3. 知识检索与引用

```bash
curl -s $AUTH -X POST $BASE/api/v1/knowledge/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"审批有效期是多少秒"}'
```

期望：`found=true`，`citations` 非空，每条含 `document_id` / `chunk_id` / `title` / `section` /
`source_path` / `updated_at` / `score`。引用可回溯是硬要求，回答不能没有出处。

## 4. 对话

```bash
curl -s $AUTH -X POST $BASE/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"verify-chat","message":"用一句话说明你能做什么"}'
```

期望 `status=completed`，中文回答，`error_category` 为 `null`。

## 5. 写操作审批闭环

```bash
# 5.1 触发：应返回 awaiting_approval，且参数被完整抽取
curl -s $AUTH -X POST $BASE/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"thread_id":"verify-write","message":"给 station-001 创建维护工单，标题「验收工单」，描述「部署验收」"}'
# → 记下返回的 approval.approval_id

# 5.2 确认：应返回 VERIFIED，verified=true
curl -s $AUTH -X POST $BASE/api/v1/approvals/<approval_id>/confirm \
  -H 'Content-Type: application/json' -d '{}'

# 5.3 幂等：重复确认不得二次执行
curl -s $AUTH -X POST $BASE/api/v1/approvals/<approval_id>/confirm \
  -H 'Content-Type: application/json' -d '{}'
# → message 应包含「本次不会重复执行」
```

## 6. 安全反例（都要能挡住）

```bash
# 6.1 越权站点：必须 error/not_authorized，且不产生审批记录
curl -s $AUTH -X POST $BASE/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"thread_id":"verify-deny","message":"给 station-999 创建维护工单，标题「越权」，描述「越权测试」"}'
# → approval 必须为 null

# 6.2 参数篡改：必须 422
curl -s -o /dev/null -w '%{http_code}\n' $AUTH \
  -X POST $BASE/api/v1/approvals/<approval_id>/confirm \
  -H 'Content-Type: application/json' \
  -d '{"parameters":{"station_id":"station-001","title":"被篡改","description":"x"}}'

# 6.3 取消后确认：必须拒绝执行
curl -s $AUTH -X POST $BASE/api/v1/approvals/<approval_id>/cancel \
  -H 'Content-Type: application/json' -d '{}'
# → status=REJECTED；再 confirm 应提示当前状态不能执行

# 6.4 身份伪造（仅经 Nginx 时验证）：伪造头必须被丢弃
curl -s $AUTH -H 'X-Authenticated-Subject: attacker' -X POST $BASE/api/v1/chat \
  -H 'Content-Type: application/json' -d '{"thread_id":"verify-spoof","message":"你好"}'
# → 仍以认证用户身份处理，不得新建 attacker 用户
```

## 7. 审计链

```sql
SELECT event_type, count(*) FROM audit_logs GROUP BY 1 ORDER BY 2 DESC;
```

一次成功的写操作应留下 `APPROVAL_CREATED → APPROVAL_APPROVED → TOOL_SUCCEEDED → APPROVAL_VERIFIED`。
缺少任一环，说明链路被绕过。

被拒绝的调用也要有账。造一次越界调用（用一个 `allowed_tools` 不含目标工具的
Agent 发起对话），然后：

```sql
SELECT resource_id, metadata_json->>'denial_reason', metadata_json->>'agent_name'
FROM audit_logs WHERE event_type = 'TOOL_DENIED' ORDER BY created_at DESC LIMIT 5;
```

`denial_reason` 应落在 `scope` / `role` / `tenant` / `agent` 之一。
**只看到 `TOOL_FAILED` 而没有 `TOOL_DENIED`，说明拒绝被错误归类**——这两类
一个该改配置、一个该查代码，混在一起运营就分不出来了。

## 8. 多进程/多副本前置（Redis 限流、索引移出进程、维护选主）

```bash
# 8.1 限流确实落在 Redis：计数键存在且带 TTL
redis-cli -u "$REDIS_URL" --scan --pattern 'rate-limit:*' | head
redis-cli -u "$REDIS_URL" ttl "$(redis-cli -u "$REDIS_URL" --scan --pattern 'rate-limit:*' | head -1)"

# 8.2 就绪检查里 rate_limit_shared 必须为 true
curl -s http://127.0.0.1:8000/ready | python -m json.tool

# 8.3 知识索引在数据库里，不在进程内存/本地文件
curl -s -u pap-admin:<密码> http://<公网IP>/api/v1/knowledge/status
# → backend=postgres，chunks>0，version 形如 vN

# 8.4 重新入库后不重启即可生效：查一次 status 记下 version，
#     跑 uv run python -m scripts.ingest_knowledge，再查一次，version 必须 +1

# 8.5 维护选主：两个实例同时起，日志里每轮只应有一个 maintenance_completed
```

跨进程恢复（checkpoint 落 PG）见 `docs/DEPLOYMENT.md` 的升级章节与
`docs/STUDIO.md` 的观测流程。

## 9. 多租户隔离

需要一个**非默认租户**才能真正验证隔离：只在 `default` 里测，等于没测。

```bash
# 9.1 建租户与成员（走管理通道，写 ADMIN_ACTION 审计）
uv run python -m scripts.create_tenant --tenant test --name "测试租户" \
  --member test-user --role dev
uv run python -m scripts.seed_dev_data --tenant test --subject test-user --station station-001

# 9.2 非成员申请切换：必须 403
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Authenticated-Subject: pap-admin' \
  -H 'X-Authenticated-Tenant: test' $BASE/api/v1/me

# 9.3 成员不带租户头：落在 default 且非其成员，也必须 403
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Authenticated-Subject: test-user' $BASE/api/v1/me

# 9.4 成员显式申请：必须返回 tenant_id=test
curl -s -H 'X-Authenticated-Subject: test-user' -H 'X-Authenticated-Tenant: test' $BASE/api/v1/me
```

再用另一个租户的审批 / 附件 / 会话 ID 去读，必须一律 404（不是 403——不泄露
"这个 ID 存在"）。最后在数据库侧确认 RLS 兜底：

```sql
-- 用应用角色（非超级用户）连接，不设 app.tenant_id
SELECT count(*) FROM conversations;              -- 必须是 0
BEGIN; SELECT set_config('app.tenant_id','default',true);
SELECT count(*) FROM conversations;              -- 必须是 default 的真实行数
ROLLBACK;
```

`seed_dev_data` 的 `tenant_scope` 必须包在事务外层：`app.tenant_id` 在事务
`begin` 时就写入，事务开完再改上下文对 RLS 无效，表现为
`new row violates row-level security policy`。
