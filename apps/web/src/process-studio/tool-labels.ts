/** Presentation only: preserve the original names in the Harness protocol and journal. */
export const toolLabels: Readonly<Record<string, string>> = {
  create_job_from_step: "导入模型并创建任务",
  inspect_job_progress: "查询模型解析进度",
  initialize_process_draft: "建立工艺草案",
  inspect_operation_catalog: "查询加工工序库",
  inspect_tool_catalog: "查询刀具目录",
  add_process_operation: "添加加工工序",
  trial_l32_operation: "计算刀路并试算工序",
  accept_l32_operation_trial: "接受工序试算结果",
  inspect_l32_operation_trial_state: "检查工序试算状态",
  evaluate_l32_operation_candidates: "比较候选工序方案",
  auto_repair_l32_operation: "自动修复加工工序",
  inspect_l32_candidate_tool_requirements: "检查候选方案刀具需求",
  bind_l32_candidate_inventory_tool: "绑定候选方案库存刀具",
  apply_l32_operation_candidate: "应用候选工序方案",
  finalize_harness_process_plan: "完成整件工艺验证",
  revise_process_operation: "修改加工工序",
  remove_process_operation: "删除加工工序",
  reorder_process_operations: "调整工序顺序",
  open_job_context: "读取加工任务上下文",
  inspect_job: "查看加工任务",
  inspect_geometry: "识别零件几何特征",
  inspect_l32_profile_review_request: "检查轮廓复核要求",
  provisionally_accept_l32_profile: "暂时接受轮廓方案",
  inspect_machine: "检查机床加工能力",
  inspect_l32_tool_inventory: "查看机床刀具库存",
  record_l32_physical_tool: "登记实体刀具",
  observe_model: "观察零件模型",
  inspect_validation: "查看仿真验证结果",
  validate_l32_plan: "验证加工工艺方案",
  inspect_l32_operation_loop: "检查工序执行流程",
  advance_l32_operation: "推进下一道工序",
  inspect_l32_repair_options: "查询工序修复方案",
  select_l32_repair_candidate: "选择工序修复方案",
  read_file: "读取文件", read: "读取文件", write_file: "写入文件", write: "写入文件",
  edit_file: "编辑文件", edit: "编辑文件", apply_patch: "修改文件",
  list_directory: "查看目录", list_files: "查看文件列表", glob: "查找文件", grep: "搜索文件内容",
  exec: "执行辅助脚本", exec_command: "执行辅助命令", shell: "执行辅助命令", bash: "执行辅助命令",
  write_stdin: "读取命令执行进度", wait: "等待执行结果",
  tool_search: "查找可用功能", search_tools: "查找可用功能",
  read_resource: "读取工程资源", read_mcp_resource: "读取工程资源",
  list_resources: "查看工程资源", list_mcp_resources: "查看工程资源",
  list_mcp_resource_templates: "查看工程资源模板",
  get_goal: "查看任务目标", create_goal: "建立任务目标", update_goal: "更新任务目标",
  update_plan: "更新执行计划", skill: "加载工艺技能", load_skill: "加载工艺技能",
  web_search: "搜索参考资料", web_fetch: "读取参考资料", fetch: "读取参考资料",
  request_user_input: "请求补充信息", view_image: "查看图像",
  ask_user_question: "请求用户确认与补充信息",
  read_image: "读取图像", pwsh: "执行辅助命令", str_replace_editor: "编辑文件内容",
  job_list: "查看后台任务", job_output: "读取后台任务输出", job_kill: "停止后台任务",
  load_workspace_dependencies: "加载工程依赖", lsp: "查询代码信息",
  subagent: "启动协作智能体", cordis_inspect_list: "查看扩展组件", cordis_inspect_query: "查询扩展组件",
  present: "展示工程成果", todo_write: "更新任务清单",
  terminal_open: "打开辅助终端", terminal_send: "执行终端命令", terminal_read: "读取终端输出",
  terminal_signal: "控制终端任务", terminal_close: "关闭辅助终端", terminal_list: "查看终端列表",
  session_search: "查找历史任务", session_event_search: "搜索执行记录", session_trace: "查看任务执行轨迹",
  session_event_trace: "查看事件执行轨迹", session_event_read: "读取执行记录",
  spawn_agent: "启动协作智能体", spawn_teammate: "启动协作智能体", send_message: "发送协作消息",
  list_agents: "查看协作智能体", wait_agent: "等待协作结果", interrupt_agent: "停止协作任务",
  list_subagent_models: "查看协作模型", team_task_create: "建立协作任务", team_task_list: "查看协作任务",
  team_task_get: "读取协作任务", team_task_update: "更新协作任务",
};

export function toolLabel(name?: string): string {
  if (!name?.trim()) return "准备下一步操作";
  // Both mcp__cnc__tool and the deployed mcp__cnc_tool spelling occur.
  const normalized = name.trim().toLowerCase().replace(/^mcp__cnc(?:__|_)/, "");
  const bare = normalized.split(/__|[.:/]/).at(-1)!;
  if (toolLabels[bare]) return toolLabels[bare];
  // Some remote servers flatten their namespace to a single underscore.
  if (normalized.startsWith("mcp__")) {
    const match = Object.keys(toolLabels).sort((a, b) => b.length - a.length).find(key => bare.endsWith(`_${key}`));
    if (match) return toolLabels[match];
  }
  return normalized !== name.trim().toLowerCase() ? "执行加工辅助操作" : "执行辅助操作";
}
