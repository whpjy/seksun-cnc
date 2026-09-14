# 二维图纸与 PMI 制造要求接入

## 已实现链路

`seksun-cnc` 的新建任务要求同时上传 PDF 工程图和 STEP/STP 三维模型。PDF 与 STEP 会发送到 `seksun-meas` 的 `/api/v1/comparisons`，其 `manufacturing_specification` 结果经标准化和特征重绑定后进入工艺规划。

每个任务保存：

- `drawing.pdf`：原始二维图纸。
- `manufacturing-specification.json`：图纸识别服务的原始结构化输出。
- `manufacturing-requirements.json`：CNC 系统使用的统一制造要求。
- `measurement-link.json`：两个系统的任务关联和处理信息。

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

`compose.yaml` 已增加内部 `measurement` 服务，API 通过 `CNC_MEAS_API_BASE_URL=http://measurement:8080` 访问。如使用外部部署，只需将该环境变量替换为对应地址。图纸服务失败时，任务仍会以纯 STEP 模式完成，并在方案警告中保留错误原因。
