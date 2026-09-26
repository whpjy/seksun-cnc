# DeepSeek Harness CNC MCP Bridge

该桥接层让 DeepSeek Harness 中的模型通过 MCP 调用 Seksun CNC 的真实领域能力。CNC API 仍是机床约束、工艺草案和仿真产物的唯一真源；MCP 只负责提供紧凑、可审计的智能体工具。

## 工具

| Harness 工具 | 用途 | 是否计算 |
|---|---|---|
| `mcp__cnc__create_job_from_step` | 从 Harness 已上传附件路径创建 CNC 任务并绑定 `job_id` | 上传并启动后台分析 |
| `mcp__cnc__inspect_job_progress` | 读取模型解析、规划阶段和错误；可短暂等待 | 否 |
| `mcp__cnc__initialize_process_draft` | 创建只有毛坯/装夹、没有任何工序的 Harness DRAFT | 确定性初始化 |
| `mcp__cnc__inspect_operation_catalog` | 读取工序定义、几何约束、默认刀具和参数边界 | 否 |
| `mcp__cnc__add_process_operation` | 由 Harness 每次向草案增加一道候选工序 | 更新 DRAFT |
| `mcp__cnc__trial_l32_operation` | 独立编译下一道 L32 工序，并从上一道已接受的真实余料状态继续仿真 | 是 |
| `mcp__cnc__auto_repair_l32_operation` | 根据失败证据自动生成安全修复候选并在隔离沙箱中逐一试算 | 是 |
| `mcp__cnc__accept_l32_operation_trial` | 接受已通过门禁的单道证据并推进累计余料；不代表生产放行 | 更新 DRAFT 验证状态 |
| `mcp__cnc__inspect_l32_operation_trial_state` | 恢复已接受工序前缀、下一工序及最近一次门禁结果 | 否 |
| `mcp__cnc__evaluate_l32_operation_candidates` | 隔离试算最多五个参数、换刀或几何绑定候选，不修改正式草案 | 是 |
| `mcp__cnc__apply_l32_operation_candidate` | 显式应用一个已经通过隔离试算的候选；应用后仍需正式重试 | 更新 DRAFT |
| `mcp__cnc__finalize_harness_process_plan` | 全部工序接受后检查制造覆盖率并触发整件连续材料验证 | 是 |
| `mcp__cnc__revise_process_operation` | 修改候选工序的刀具、参数、几何或启用状态 | 更新 DRAFT |
| `mcp__cnc__remove_process_operation` | 删除尚未放行的候选工序 | 更新 DRAFT |
| `mcp__cnc__reorder_process_operations` | 重排一个装夹内的完整工序序列 | 更新 DRAFT |
| `mcp__cnc__open_job_context` | 绑定任务上下文；省略编号时选择最近完成的本地任务 | 否 |
| `mcp__cnc__inspect_job` | 读取任务、覆盖率和工序清单 | 否 |
| `mcp__cnc__inspect_geometry` | 读取制造特征和回转轮廓摘要 | 否 |
| `mcp__cnc__inspect_machine` | 读取绑定机床快照与刀具库存 | 否 |
| `mcp__cnc__observe_model` | 把真实 STL 的四视图作为图片交给多模态模型 | 仅渲染 |
| `mcp__cnc__inspect_validation` | 读取已有逐工序验证和阻断证据 | 否 |
| `mcp__cnc__validate_l32_plan` | 编译当前 L32 草案并执行逐工序连续材料仿真与审核 | 是 |
| `mcp__cnc__inspect_l32_operation_loop` | 读取已接受工序、当前阻断和下一动作 | 否 |
| `mcp__cnc__advance_l32_operation` | 复用真实连续材料仿真证据，每次只审核并推进一道工序 | 首次调用计算，后续复用同版证据 |
| `mcp__cnc__inspect_l32_repair_options` | 读取阻断归因、候选工艺和每个候选缺少的安全证据 | 否 |
| `mcp__cnc__select_l32_repair_candidate` | 显式确认一个修复候选；只有通过确定性门禁的候选才允许修改 DRAFT 方案 | 视候选而定 |

所有验证结果固定标记为 `DRAFT` 且 `production_ready=false`。仿真通过不等于完成整机碰撞、后处理器认证或生产放行。

## 启动

先启动 CNC API：

```powershell
docker compose up -d --build api
```

再把 [cnc-mcp.patch.yml](../integrations/deepseek-harness/cnc-mcp.patch.yml) 的 insert 块加入模型 patch，或把它作为额外 patch 使用。启动前设置 MCP 工作目录：

```powershell
$env:CNC_MCP_CWD = (Resolve-Path .\apps\api).Path
$env:CNC_MCP_IMPORT_ROOTS = "E:\允许上传模型的目录;E:\DeepSeek-Harness工作区"
```

在 Harness 网页上传 STEP/STP 后，把附件的本地路径交给
`create_job_from_step`。该工具只允许读取 `CNC_MCP_IMPORT_ROOTS` 下的
`.step/.stp` 文件，避免模型工具获得任意本地文件读取权限。创建后用
`inspect_job_progress(wait_seconds=30)` 跟踪任务，完成后再进入模型观察和逐工序验证。

`create_job_from_step` 固定由 Harness 持有规划权：后台只执行 STEP 几何解析，
不会调用原规则规划器、LangGraph 规划器或后台自主规划器。几何完成后，Harness 必须先调用
`initialize_process_draft` 建立空白工序草案，再根据几何、机床和工序库逐道调用
`add_process_operation`。工具不再接受 `planning_mode`，避免会话误切换到后端规划路径。

MCP 服务使用 Python SDK 1.x，是因为当前 API 固定使用 Pydantic 2.11；Harness 的 MCP 客户端会从新协议自动回退到该 SDK 支持的协议版本。

## 推荐的智能体调用顺序

1. 没有显式任务编号时先调用 `open_job_context`；后续工具自动复用已绑定上下文。
2. 新任务先用 `inspect_job_progress` 等待确定性几何解析完成，然后调用 `inspect_geometry`、`observe_model` 和 `inspect_machine` 按需感知。
3. 调用 `initialize_process_draft` 创建零工序 DRAFT；读取 `inspect_operation_catalog` 后，每次只用 `add_process_operation` 增加一道工序，并明确几何引用、刀具、参数和理由。
4. 新增后立即调用 `trial_l32_operation(operation_id)`。工具只编译当前一道，但材料仿真从已接受前序的累计余料继续；不允许跳过前序，也不允许把每一道都重置成新毛坯。
5. 仅当返回 `can_accept=true` 时调用 `accept_l32_operation_trial`，并提供具体 `rationale`。若状态为 `warning`，还必须显式传入 `acknowledge_warning=true`；当前只允许粗加工的单纯轮廓余量警告被确认，精加工、螺纹或可达性警告必须先修改方案。接受只表示该工序可作为下一步规划的输入，不代表 NC、整机碰撞或生产放行。
6. 返回 `blocked` 或不可接受的 `warning` 时，Harness 根据错误证据提出 1–5 个有实际差异的候选，调用 `evaluate_l32_operation_candidates` 做隔离编译和仿真。候选可以改变 `tool_id`、部分 `parameters` 或 `feature_ids`，不会直接修改正式草案。
7. 只有候选返回 `can_accept=true` 后，才可用 `apply_l32_operation_candidate(confirmed=true)` 写回；写回后候选试算只作为选择证据，仍必须重新调用 `trial_l32_operation` 形成正式累计余料证据。无候选通过时继续观察模型、提出新候选或删除当前工序，不能越过阻断新增后续工序。
8. 工序被阻断时优先调用 `auto_repair_l32_operation`。该工具不会通过放宽公差或伪造余量掩盖缺陷；若局部回转轮廓之外属于非回转区域，它会停止自动扩展并要求观察模型、拆分工序。
8. 页面/会话中断后使用 `inspect_l32_operation_trial_state` 恢复已接受前缀。已接受工序被编辑、删除或重排时，其本身及后续累计证据会自动失效。
9. `advance_l32_operation` 保留给已有完整草案的兼容审核；新的 Harness 自主规划优先使用 add → trial → accept 闭环。认为工艺完整时调用 `finalize_harness_process_plan`：若制造特征覆盖不完整，它会返回未覆盖目标并要求继续规划；覆盖完整后才执行跨主/副轴、同步与整件级验证。
10. 普通修复候选只允许记录为 `selected_pending_evidence`；只有通过确定性重编译/仿真门禁后，才允许智能体修改 DRAFT 工艺方案。

### 单道门禁的真实性边界

- 当前支持 L32 车削提供器覆盖的端面、外圆/内孔粗精车、切槽、螺纹、轴向钻孔/攻牙和切断。
- 动力刀具铣削、背面特殊工艺等尚无独立门禁的类型会明确返回 `unsupported_independent_trial`，不会伪造仿真成功。
- 工序引用的回转轮廓必须已被工程师接受；模型不能自行把待复核轮廓提升为正式几何证据。
- 每次接受都保存工序签名与累计 Z-R 余料。如果已接受工序的参数、刀具、顺序或几何引用发生变化，从该工序起的证据自动作废。

BUQD0017 的首次集成验证证明 Harness 可读取 9 道工序并接收 STL 四视图；L32 验证真实通过 OP10、OP20、OP30 后，在缺少 OP50 的整件计划门禁处停止并归档证据。该缺口会被归因为 `missing_back_face_process`：由于零件存在背面非回转保护区，系统不会盲目恢复整面端面车削，而会提出背面动力刀具分区精加工、二次装夹等受证据约束的候选。

`back_live_tool_face_finish` 候选不是只更改工序名称。它会按 `material_separation` 语义角色找到切断工序，根据刀具直径计算背面余量，然后生成稳定 `AUTO-*` 编号的背面动力刀具光栅刀路。已存在的旧任务仍兼容 `OP40/OP50-ALT`。正式改写 DRAFT 方案前必须同时通过：

- 机床 `back_live_tool_milling` 能力与 U151B 模块门禁；
- 整件重编译和跨主/副轴连续余料仿真；
- 基于原始 STEP 实体的刀具三维扫掠与成品侵入检查；
- 新生成工序的独立刀路、材料去除和工序证据审核。

真实任务 `4a713c2a46d04b0c9804ee7c885b5777` 已完成该路径验证：14 道光栅路径、210 个三维刀具采样，成品实体侵入为 `0 mm³`，连续余料去除为 `6.26257 mm³`。结果仍是 `DRAFT`，未覆盖整机夹具碰撞、干运行和首件验证。
