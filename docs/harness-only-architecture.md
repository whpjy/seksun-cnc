# Harness 唯一入口架构

## 产品边界

- DeepSeek Harness 是 STEP/STP 上传、用户对话和工艺规划的唯一入口。
- Harness 中的 Qwen 负责按需观察模型、确定装夹与阶段策略，并逐道决定工序。
- CNC 服务只提供确定性几何解析、设备/刀具目录、工序草案操作、刀路、仿真、证据归档和安全门禁。
- CNC Web 只用于查看已由 Harness 创建的任务、模型、工序和仿真证据；不再创建规划任务。
- 所有方案保持 `DRAFT`，通过仿真也不代表生产放行。

## 唯一正式流程

1. 用户在 Harness 页面上传 STEP/STP。
2. Harness 调用 `create_job_from_step`，CNC 后端只进行几何解析。
3. Harness 调用 `inspect_job_progress` 等待解析完成。
4. Harness 按需调用 `inspect_geometry`、`observe_model`、`inspect_machine`。
5. Harness 调用 `initialize_process_draft` 建立零工序草案。
6. Harness 循环执行 `add_process_operation -> trial_l32_operation -> accept/repair`。
7. 全部目标覆盖后调用 `finalize_harness_process_plan` 做整件验证。

`planning_mode=autonomous` 和 `planning_mode=backend_baseline` 不再对 Harness 暴露。CNC
内部的旧规划器与自主执行器只保留为兼容代码或未来候选工具，不能拥有正式方案的规划权。

## 运行保护

部署环境默认设置 `CNC_HARNESS_ONLY=true`。此时 `/api/v1/jobs/start` 返回 409，防止
CNC Web 或其他客户端绕过 Harness 创建并规划任务。Harness MCP 使用专用的
`/api/v1/jobs/intake` 上传入口。

Harness 地址由 `VITE_HARNESS_URL` 配置，CNC 页面中的“打开 Harness”按钮只负责跳转。
