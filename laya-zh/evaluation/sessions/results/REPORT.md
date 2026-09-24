# Laya 本地 sessions Agent 调度评测报告

- 运行时间（UTC）：2026-09-21T09:44:01+00:00
- 服务地址：http://127.0.0.1:8765
- 数据源：已授权的本地 sessions 摘要，仅 20 条；输入已脱敏。
- 模型输入：每条请求只发送 input_text；其余字段只用于标签和分析。实际最大输入字符数：55。
- 高置信阈值：0.9；HTTP/解析失败：0；平均/最大延迟：105.2 / 772.9 ms。

## 总体指标

| 指标 | 结果 |
| --- | ---: |
| micro precision / recall / F1 | 0.2410 / 0.7692 / 0.3670 |
| macro F1 | 0.3407 |
| exact role-set match | 0.0500 (1/20) |
| main_agent fallback accuracy | 0.7000 (6 个 gold fallback) |
| 0.9 高置信覆盖率 / 覆盖集准确率 | 0.2000 / 0.3333 |
| HTTP failures | 0 |

## 各角色 Precision / Recall / F1

| 角色 | Gold 数 | 预测正例 | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| evidence_researcher | 10 | 7 | 0.5714 | 0.4000 | 0.4706 |
| solution_designer | 7 | 18 | 0.3889 | 1.0000 | 0.5600 |
| implementation_worker | 5 | 16 | 0.3125 | 1.0000 | 0.4762 |
| backend_tester | 2 | 19 | 0.1053 | 1.0000 | 0.1905 |
| e2e_tester | 1 | 15 | 0.0667 | 1.0000 | 0.1250 |
| incremental_reviewer | 1 | 8 | 0.1250 | 1.0000 | 0.2222 |

## 实际角色与推荐标签差异

历史 actual_roles 与规则复核 recommended_roles 不同的 case：11 条；角色差异总数：14。
gold 是规则复核标签，不是历史绝对事实；实际历史派发遗漏或角色不完全匹配不会被当作 gold。

| Case | 实际角色 | 推荐角色 | 对称差异 |
| --- | --- | --- | --- |
| s01 | evidence_researcher | evidence_researcher,solution_designer | solution_designer |
| s03 | evidence_researcher | evidence_researcher,solution_designer | solution_designer |
| s07 | evidence_researcher,solution_designer | evidence_researcher,implementation_worker,solution_designer | implementation_worker |
| s09 | 无 | evidence_researcher | evidence_researcher |
| s10 | 无 | implementation_worker,solution_designer | implementation_worker,solution_designer |
| s11 | e2e_tester,evidence_researcher,implementation_worker,solution_designer | backend_tester,evidence_researcher,implementation_worker,solution_designer | backend_tester,e2e_tester |
| s12 | incremental_reviewer | implementation_worker,incremental_reviewer | implementation_worker |
| s14 | evidence_researcher | backend_tester,evidence_researcher | backend_tester |
| s16 | e2e_tester | e2e_tester,implementation_worker | implementation_worker |
| s19 | 无 | solution_designer | solution_designer |
| s20 | 无 | evidence_researcher,solution_designer | evidence_researcher,solution_designer |

## 典型误判或低置信样本

| Case | 角色 | Gold | 预测 | 概率 | 置信度 |
| --- | --- | --- | --- | ---: | ---: |
| s02 | evidence_researcher (role_misprediction) | True | False | 0.1661 | 0.8339 |
| s02 | solution_designer (role_misprediction) | False | True | 0.7575 | 0.7575 |
| s02 | implementation_worker (role_misprediction) | False | True | 0.7843 | 0.7843 |
| s02 | backend_tester (role_misprediction) | False | True | 0.8817 | 0.8817 |
| s02 | e2e_tester (role_misprediction) | False | True | 0.9144 | 0.9144 |
| s02 | incremental_reviewer (role_misprediction) | False | True | 0.7233 | 0.7233 |
| s03 | evidence_researcher (role_misprediction) | True | False | 0.4617 | 0.5383 |
| s03 | implementation_worker (role_misprediction) | False | True | 0.5182 | 0.5182 |
| s03 | backend_tester (role_misprediction) | False | True | 0.7586 | 0.7586 |
| s03 | e2e_tester (role_misprediction) | False | True | 0.959 | 0.959 |

## 解读与限制

- 本评测只有 20 条，来自已授权的本地 sessions 摘要；所有输入均已脱敏，不能代表完整历史任务分布。
- recommended_roles 是人工/规则复核的 gold，不是历史 actual_roles 的无条件真值；s08、s11、s12 等记录保留了标签噪声说明。
- 固定六类角色之外的专用 Skill、权限决策、需求澄清和最终验收统一按 main_agent fallback 处理。
- 当前模型概率未针对这些 sessions 标签做校准；0.9 覆盖率和覆盖准确率只是本次样本上的观察，不能直接作为生产阈值。
- 评测输入只发送 input_text，避免把 actual_roles、recommended_roles、结果和标签说明泄漏给模型。

机器可读明细：results/latest.json
