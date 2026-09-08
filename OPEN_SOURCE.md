# 开源内核与边界

本项目采用“成熟内核 + 产品适配层”，不自行重写 CAD、CAM 和材料去除内核。

| 项目 | 固定版本 | 许可证 | 本项目用途 |
| --- | --- | --- | --- |
| FreeCAD / CAM | 1.1.1，官方 AppImage，SHA256 固定 | LGPL-2.0-or-later | 原生 CAM 工序、刀具、Job、GRBL 后处理、FCStd 工程 |
| CAMotics / cbang | 源码提交固定，headless 构建 | GPL-2.0+ / LGPL-2.1+ | 按装夹执行 G-code 材料去除并输出 STL |
| Open CASCADE Technology | Debian 12 系统包 | LGPL-2.1 + OCCT exception | STEP/B-Rep、拓扑分析和显示网格 |
| Three.js | npm lockfile 固定 | MIT | Web 三维显示和交互 |
| React / Vite | npm lockfile 固定 | MIT | Web 工作台 |
| FastAPI / Uvicorn | pip requirements 固定 | MIT / BSD-3-Clause | API 与引擎编排 |

FreeCAD 和 CAMotics 均作为独立命令行进程调用，通过 STEP、JSON、FCStd、G-code 和 STL 交换数据。多个装夹分别生成 NC 文件和 CAMotics 表面，禁止把不同工件坐标系直接拼成一次材料去除仿真。

当前 CAMotics 使用 G-code 自动推导毛坯范围，能够替代自研高度场作为真实去除内核，但还不是机床级数字孪生。下一阶段应生成 `.camotics` 工程，明确写入毛坯尺寸、刀具表和坐标偏置。

候选但未纳入关键路径：

- OpenCAMLib：FreeCAD 的部分 CAM 算法已经间接使用；暂不重复部署另一套编排层。
- AAGNet：可作为复杂加工特征识别的辅助模型，但预测必须经几何规则验证。
- OpenVDB / Manifold：可用于更复杂的体素或网格布尔，不在本阶段替换 CAMotics。
- LinuxCNC：适合后处理和虚拟控制器验证；不能取代 CAM 规划。

许可证说明：分发或商业部署前仍需正式审查第三方许可证、NOTICE、源代码提供义务以及 FreeCAD/CAMotics 的进程与文件边界。
