# 本地 sessions Agent 调度评测

本目录与上层合成业务评测独立，使用 20 条已授权、已脱敏的本地历史任务摘要观察六类 SubAgent 角色路由。它不是完整历史分布，也不是生产精度结论。

## 运行

确保本地 Laya 服务已启动后，在部署目录执行：

~~~sh
.venv/bin/python evaluation/sessions/run_sessions_eval.py
~~~

可选参数：

~~~sh
.venv/bin/python evaluation/sessions/run_sessions_eval.py \
  --base-url http://127.0.0.1:8765 \
  --threshold 0.9 \
  --output-dir evaluation/sessions/results
~~~

成功后生成 results/latest.json 和 results/REPORT.md。脚本只使用 Python 标准库，不访问网络、不新增依赖；HTTP 或响应 schema 失败会使退出码为 1，模型预测错误本身不会使评测失败。

## 数据与防泄漏约束

session_cases.json 的每条记录包含来源线程标识、标题、脱敏输入、任务类型、结果、历史实际角色、规则复核推荐角色和标签说明。模型请求只发送 input_text，不发送标题、outcome、actual_roles、recommended_roles、label_confidence 或 label_notes。

session_templates.json 固定六个 noul 问题：

- evidence_researcher
- solution_designer
- implementation_worker
- backend_tester
- e2e_tester
- incremental_reviewer

如果 recommended_roles 不包含这六类角色中的任何一个，gold 按 main_agent fallback 处理。专用 Skill、权限判断、需求澄清和最终验收不强行映射到这六类角色。

## 标签规范与指标

recommended_roles 是规则/人工复核后的 gold，不是历史实际派发结果的绝对真值；actual_roles 只用于报告实际与推荐的差异。标签中保留了历史遗漏、角色替代和根因冲突等说明。

报告包含：

- 每个角色的 precision、recall、F1，阈值为 0.5；
- micro/macro F1；
- exact role-set match；
- main_agent fallback 二分类准确率；
- 0.9 高置信自动覆盖率及覆盖集准确率；
- HTTP 失败、延迟和典型误判/低置信样本；
- actual_roles 与 recommended_roles 的差异 case 和角色数量。

评测只有 20 条本地 sessions；s08 等记录明确存在标签噪声或历史根因冲突。当前模型概率没有针对真实任务标签做校准，0.9 只能作为观察阈值。
