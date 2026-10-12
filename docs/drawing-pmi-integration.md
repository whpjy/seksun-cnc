# 二维图纸与 PMI 制造要求接入

## 当前链路

3082 工艺工作台的新建任务现在配对上传 STEP/STP 与 PDF 工程图。三维几何分析由本项目 OCCT 工具执行；二维图纸由 PDF 文字提取、按文字方向校正的图纸渲染与已配置的视觉模型读取，不依赖独立的 measurement 服务。旧任务和 STEP-only API 导入保留兼容。

任务保存原始 STEP、`drawing.pdf`、`drawing-analysis.json` 与几何/规划结果。`POST /api/v1/jobs/intake` 支持 multipart 的 `step` 和 `drawing`；MCP `create_job_from_step` 对应 `step_path` 和 `drawing_path`。图纸最大外径与 STEP 包络交叉检查后用于实心毛坯选择，默认直径增量为 0.2 mm，规则可以在页面修改。图纸材料及制造要求进入 Harness 草案上下文。

二维视觉标注仍是初读候选，尺寸方向、数量与 CAD 候选不一致时显示待审核，不允许自动宣称公差、粗糙度或其他质量要求已验证。`GET /api/v1/jobs/{job_id}/drawing-analysis` 读取证据；`PUT /api/v1/jobs/{job_id}/drawing-analysis/confirm` 允许人工修正最大外径、材料与尺寸依据，变更将使旧刀路/验证失效。已有外部解析器输出也仍可通过下述统一接口导入。

导入制造要求后，任务可额外保存：

- `manufacturing-specification.json`：外部解析器的原始结构化输出。
- `manufacturing-requirements.json`：CNC 系统使用的统一制造要求。

严孔径公差、形位公差和表面粗糙度已能影响铰孔、坐标基准策略、CMM 检测和粗糙度检测工序。无法唯一关联到 CNC 特征的要求会保持待复核，不允许以错误特征 ID 驱动确定性规划。

## 统一要求契约

二维图纸和原生 PMI 共用 `ManufacturingRequirements` 契约。单条要求包含：

- 类型与子类型：尺寸、孔径、螺纹、粗糙度、GD&T、热处理、表面处理等。
- 数值语义：公称值、上下偏差、上下限、单位和数量。
- 特征关联：CNC 特征 ID、匹配状态、验证状态和置信度。
- 可追溯性：原始文本、来源系统、图号、版本和来源元数据。

外部解析器或未来的 PMI 解析器可调用：

```http
POST /api/v1/jobs/{job_id}/manufacturing-requirements
Content-Type: application/json

{
  "source_system": "step-ap242-pmi",
  "specification": { "comparison_rows": [] }
}
```

## PMI 下一阶段

当前 OCCT 几何分析器还没有输出 STEP AP242 语义 PMI。后续应在几何服务增加独立 PMI 适配层，读取尺寸、公差、基准、形位公差、粗糙度和语义特征关联，然后输出相同的 `ManufacturingRequirements` 契约。优先级为：

1. AP242 语义 PMI 与精确 B-Rep 面绑定。
2. 二维图纸与 CNC 特征的稳定 ID/几何指纹重绑定。
3. 材料、热处理、表面处理、通用公差和技术要求的规则映射。
4. 冲突合并：PMI 优先，二维图纸补充，冲突项必须人工复核。

## 部署

`compose.yaml` 只构建和启动 `seksun-cnc` 自身的 API 与内置 Web 工作台，不读取或构建同级的 `seksun-meas`、`seksun-web` 项目。服务器仅部署本项目即可启动核心 STEP/CAM 流程。

外部图纸或 PMI 解析器是可选的数据来源；解析完成后由调用方通过 `POST /api/v1/jobs/{job_id}/manufacturing-requirements` 导入结果，不属于本项目的部署依赖。
