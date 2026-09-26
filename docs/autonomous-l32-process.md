# L32 有界自主工艺执行器

目标不是让任意 STEP 都被强行判定为可加工，而是让每个上传任务都在有限预算内得到可审计结论：

- `verified_success`：全部启用工序逐道试算、接受，覆盖检查与整件验证通过。
- `engineer_review_required`：几何、轮廓、公差或预算需要工程师确认。
- `capability_unavailable`：现有 L32 配置、刀具库存或已实现工艺无法完成目标。

所有结果仍为 `DRAFT`，`production_ready=false`。通过仿真不代表生产 NC 放行。

## Harness 上传并自动运行

调用 `create_job_from_step`：

```text
planning_mode = autonomous
device_id = citizen-cincom-l32
```

几何分析完成后，后台自动执行：

```text
适用性判断
  -> 分层工艺草案
  -> 当前工序真实试算
  -> 通过则接受并传递余料
  -> 失败则生成候选并隔离仿真
  -> 候选通过后写回并正式重试
  -> 全工序覆盖与整件验证
  -> 三态结论
```

随后用 `inspect_autonomous_l32_process` 查看实时状态。会话中断后可以再次调用
`start_autonomous_l32_process`；执行器从已经持久化的已接受工序前缀继续，而不是从头重算。

## HTTP API

启动已有任务：

```http
POST /api/v1/jobs/{job_id}/agent/l32/autonomous/start
Content-Type: application/json

{
  "rebuild_plan": false,
  "max_operations": 30,
  "max_repair_attempts_per_operation": 2,
  "max_tool_calls": 120,
  "max_seconds": 900
}
```

查询状态：

```http
GET /api/v1/jobs/{job_id}/agent/l32/autonomous
```

过程归档在任务目录的 `agent-l32-autonomous-process.json`，前端工作区同时会收到精简后的
`orchestration.autonomous_process`，包括当前工序、试算结果、修复次数、阻断原因和能力缺口。
