# 制造工序知识库

系统将工序能力分成两层，避免把“知识库中存在”误认为“已经可以自动生成刀路”。

- 制造路线层：98 项工序，覆盖切削、特种加工、热处理与表面处理、检验质控，并保留适用条件、前序准备、后序衔接、质量放行、风险和标准依据。
- CAM 执行层：现有 FreeCAD 适配器的 19 类加工策略。制造工序通过 `cam_operation_ids` 显式映射到执行能力；没有映射的工序只能参与路线规划和缺口提示。

运行时数据位于 `apps/api/data/manufacturing_process_library.json`。它由源工作簿生成，不需要生产容器读取 Excel。

```powershell
python apps/api/tools/import_process_library.py "C:\path\机加工工序库.xlsx"
```

导入器会记录源文件 SHA-256，并校验工序编码唯一性和统计数量。更新工作簿后应重新生成 JSON 并运行 API 测试。

主要接口：

- `GET /api/v1/manufacturing-processes`：查询制造工序，可使用 `family`、`query` 和 `compact`。
- `GET /api/v1/manufacturing-processes/{code}`：读取工序的完整工艺与质量约束。
- `GET /api/v1/operation-library`：读取当前 CAM 执行策略。这不是完整制造工序库。
- `GET /api/v1/jobs/{job_id}/manufacturing-route`：为新旧任务即时生成制造路线和知识完整性评估。

每个新方案带有 `manufacturing_route` 和 `knowledge_assessment`。路线步骤区分必选/条件工序、CAM/人工/外协/检验执行方式、前序依赖、来源特征以及阻塞信息；评估报告工序映射率、可执行数量、未验证能力和缺失工程输入。当前仅上传 STEP 时，尺寸公差、基准、粗糙度、热处理、毛坯和现场资源等信息不足，系统必须保持“需复核”，不能宣称可直接生产。

Qwen 审查采用“确定性路线检索 → 相关工序子集 → 大模型审查”的方式。模型只接收当前零件族、路线、替代工艺及 CAM 能力相关的工序，而不是每次发送完整 98 项正文；输出中的 `route_recommendations.process_code` 必须通过工序库编码校验。

下一阶段的扩展顺序：二维图纸/PMI 结构化、特征到候选工序检索、工序依赖图与约束求解、设备刀具夹具资源匹配、案例回归评分、最后才是更多 CAM 适配器。
