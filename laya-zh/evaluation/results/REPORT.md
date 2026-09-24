# Laya 中文场景评测报告

- 运行时间（UTC）：2026-09-21T09:23:07+00:00
- 服务地址：http://127.0.0.1:8765
- 阈值：0.9（noul 高置信覆盖条件为 p >= threshold 或 p <= 1-threshold）
- 数据集：合成中文业务集，共 48 条；Agent、游戏、文档各 16 / 16 / 16 条
- HTTP/解析失败：0；平均单 case 请求耗时：96.8 ms，最大：766.0 ms

## 总体指标

| 指标 | 结果 |
| --- | ---: |
| choice exact accuracy | 0.5625 |
| noul accuracy@0.5 | 0.6042 |
| noul 高置信覆盖率 | 0.6458 |
| 高置信覆盖集准确率 | 0.6774 |
| score MAE | 0.6699 |
| score round accuracy | 0.3750 |

## 按场景指标

| 场景 | 样本 | choice exact | noul@0.5 | noul 高置信覆盖 / 准确 | score MAE / round |
| --- | ---: | ---: | ---: | ---: | ---: |
| Agent 智能调度 | 16 | 0.5000 | 0.5000 | 0.2500 / 0.7500 | 0.5353 / 0.5000 |
| 游戏高层决策 | 16 | 0.3125 | 0.6875 | 0.8750 / 0.7143 | 0.5818 / 0.4375 |
| 文档流程决策 | 16 | 0.8750 | 0.6250 | 0.8125 / 0.6154 | 0.8924 / 0.1875 |

## 典型误判或低置信样本

以下样本用于定位问题，不代表完整错误列表：

| Case | 场景/问题 | 期望 | 模型结果 | 置信/概率 |
| --- | --- | --- | --- | ---: |
| agent-001 | route (choice) | "research" | "implementation" | 0.114 |
| agent-003 | route (choice) | "backend_test" | "implementation" | 0.1437 |
| agent-003 | priority (score) | 2 | 1 | 0.4733 |
| agent-003 | needs_human (noul) | false | true | 0.5058 |
| agent-004 | priority (score) | 2 | 1 | 0.4501 |
| agent-005 | needs_human (noul) | true | false | 0.2143 |
| agent-006 | route (choice) | "review" | "implementation" | 0.2481 |
| agent-007 | priority (score) | 2 | 1 | 0.4182 |
| agent-007 | needs_human (noul) | true | false | 0.0056 |
| agent-008 | needs_human (noul) | false | true | 0.6512 |

## 解读与限制

- 这是为当前 Laya 中文服务准备的合成业务集，标签是人工设计的场景期望，不是生产标注，也不是生产精度结论。
- 每个 suite 使用固定问题模板；choice 以选项完全匹配计分，score 用模型 score 四舍五入到等级，noul 以 0.5 为分类边界。
- 高置信覆盖率只表示模型愿意在阈值下自动给出结论的比例；覆盖集准确率才反映这部分自动结论的正确性。
- 当前服务仍是本机 1024 context、MPS 推理，概率未针对本业务集做温度校准；高风险动作不能只依据模型概率执行。
- 文档和游戏样本使用合成名称、编号和金额，不含真实个人或敏感资料。要判断生产可用性，还需要真实分布、人工复核和分层校准集。

机器可读明细：results/latest.json
