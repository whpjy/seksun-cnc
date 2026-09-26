# CNC 通用工艺策略框架

该框架把“修改某个固定工序编号”改为“根据加工意图绑定工艺角色”。智能体、规则规划器和 Harness 可以使用同一份候选与验证契约。

## 核心对象

- `MachiningIntent`：描述要将哪些几何变成什么制造状态。
- `ProcessCandidate`：某个策略对意图的具体实现候选。
- `ProcessDependency`：通过 `material_separation`、`back_workholding` 等角色表示前后依赖，不依赖 `OP40` 等显示编号。
- `ValidationRequirement`：声明 DRAFT 落库与生产放行分别需要的证据。
- `CandidateValidationSummary`：对声明的验证器逐项收口，没有声明证据不能被隐式视为通过。

## 执行流程

```text
制造缺口/加工目标
        ↓
MachiningIntent
        ↓
策略注册表选择 ProcessCandidate
        ↓
语义角色绑定 + 机床能力门禁
        ↓
派生参数 + 候选 ProcessPlan
        ↓
刀路/连续余料/三维扫掠/成品保护/工序证据
        ↓
evaluate_validation_contract
        ↓
所有 DRAFT 必需证据通过后才原子提交方案
```

## 首个策略

`BackLiveToolFaceStrategy` 是首个接入策略。它：

1. 按 `turn_cutoff` 类型解析材料分离角色，不查找 `OP40`。
2. 按背面/副轴装夹语义选择目标 setup，不查找 `SETUP-L32-SUB`。
3. 生成基于策略、意图和依赖的稳定 `AUTO-*` 工序编号。
4. 使用 `clamp(tool_diameter_mm * 0.25, 0.15, 0.50)` 计算背面轴向余量。
5. 将整机夹具碰撞、干运行和首件明确标记为生产阶段未完成证据。

## 新增策略的最小步骤

1. 定义意图、所需机床能力、工艺依赖和验证契约。
2. 实现 `describe()` 与 `apply()`，且 `apply()` 只返回深拷贝的候选方案。
3. 在 `PROCESS_STRATEGIES` 中注册候选 ID。
4. 将刀路和仿真证据按验证器 ID 交给 `evaluate_validation_contract()`。
5. 添加任意工序编号、能力缺失、证据缺失和旧方案兼容测试。

当前仅背面动力刀具策略已完成这套迁移。槽加工、孔加工和非回转轮廓策略将按同一契约继续接入。
