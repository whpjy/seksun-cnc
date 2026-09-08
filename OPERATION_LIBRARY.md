# 工序库

工序库是 Seksun CNC 的人工 CAM 编程基础。自动规划器和人工创建工序使用同一份定义，FreeCAD 仅作为刀路执行引擎，不作为产品数据模型。

## 分层

```text
OperationDefinition（版本化工序能力与参数 Schema）
    -> Operation（项目中的可编辑工序实例）
        -> FreeCAD adapter（引擎参数映射）
            -> 原生 FreeCAD CAM 对象、刀路和 G-code
```

定义位于 `apps/api/app/operation_library.py`。每个定义包含：

- 分类、说明、版本和成熟度；
- 允许的几何类型和最小选择数量；
- 允许的刀具类型和默认刀具；
- 参数类型、单位、默认值和上下限；
- FreeCAD 工序名称及 Dressup；
- 是否允许从人工工作台创建。

## 成熟度

- `planned`：已经进入路线图，当前不能执行。
- `experimental`：存在底层能力，但几何映射或参数映射尚未完成。
- `generated`：能够生成真实原生刀路，仍需扩大样件验证。
- `validated`：已通过项目标准样件和回归测试。
- `production`：经过指定机床、后处理器和现场切削验证。

前端只允许创建 `manual_enabled=true` 的工序。未开放工序仍显示在库中，以明确能力边界。

## API

- `GET /api/v1/operation-library`：读取完整工序库。
- `GET /api/v1/operation-library/{definition_id}`：读取单个工序定义。
- `POST /api/v1/jobs/{job_id}/setups/{setup_id}/operations`：人工创建工序。
- `PATCH /api/v1/jobs/{job_id}/setups/{setup_id}/operations/{operation_id}`：修改参数、刀具、几何或启用状态。
- `DELETE /api/v1/jobs/{job_id}/setups/{setup_id}/operations/{operation_id}`：删除工序。
- `POST /api/v1/jobs/{job_id}/setups/{setup_id}/operations/reorder`：调整当前 Setup 的工序顺序。

任何人工修改都会删除旧 CAM、仿真和碰撞产物，并把工序标记为需要重新批准和生成。

## 当前边界

第一阶段聚焦三轴 2.5D。面铣、钻孔、轮廓、型腔、槽、桥位和倒角已经建立定义；自适应、雕刻、螺旋铣孔、3D 曲面与水线保留为未开放能力，必须完成真实 FreeCAD 几何映射和样件验证后才能启用。

工艺知识库以后只能通过工序定义 ID 创建工序实例，不得绕过本工序库直接构造引擎对象。
