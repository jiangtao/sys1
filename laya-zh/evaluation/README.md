# Laya 中文业务场景评测

本目录提供一套可重复运行的合成中文测试集，覆盖 Agent 智能调度、游戏高层决策和文档流程决策。它用于观察当前本地模型在固定问题模板下的输出，不是生产精度承诺。

公开数据集候选、许可证边界、场景映射和最小采集路线见 [PUBLIC_DATASETS.md](PUBLIC_DATASETS.md)。候选数据不会自动下载或并入当前合成 `cases.json`。

## 从本地 Sessions 构建 Agent 调度集

对 Agent 调度场景，真实的本地任务与往来对话通常比人工合成样本更有代表性。可以从历史 sessions 中提取“派发前输入”，再用后续实际发生的 Agent 调用生成金标准标签。

推荐的数据构造方式：

- **模型输入**：当前用户请求、最近 1–3 轮必要上下文、项目类型和已知约束。
- **金标准标签**：后续实际调用的 `evidence_researcher`、`implementation_worker`、`backend_tester`、`e2e_tester`、`solution_designer`、`incremental_reviewer`，以及是否最终由主 Agent 直接处理。
- **多阶段任务**：分别记录调查、实现、验证和 Review，可视为多标签或有序执行链，而不是强行压成单一角色。
- **辅助标签**：是否修改文件、是否浏览器验证、是否需要联网、是否涉及高风险授权、任务是否成功完成。

必须避免答案泄漏：模型输入不能包含后续 Assistant 的分发说明、`spawn_agent` 调用、工具结果或最终答案；这些内容只用于生成标签。否则评测得到的高准确率没有意义。

数据治理建议：

- 默认只在本机处理，不上传原始 session。
- 对密钥、账号、邮箱、客户名、绝对路径和业务数据做脱敏或占位符替换。
- 按 session 或时间切分 train/dev/test，不能把同一任务的不同轮次分散到不同集合。
- 去除重复模板、自动化心跳和没有明确任务意图的短消息。
- 保留人工复核字段，纠正“历史上实际派发了某角色，但该派发并不合理”的噪声标签。
- 记录模型输入快照、实际调用链、人工修正标签和任务结果，便于回放。

当前 multilingual 服务默认只有约 768 tokens 可用于 state。Agent 调度不应输入完整 session；优先使用当前请求加最近几轮上下文，较长会话先做检索或摘要，再将紧凑上下文送入 `/v1/decide`。

真实 sessions 集应与本目录的合成集分开报告：合成集用于覆盖边界和回归，sessions 集用于衡量真实分布上的路由准确率、角色覆盖率和阈值可靠性。

最近半年 443 条本地 sessions 的评测数据与报告未随游戏仓库提交。该批评测只发送脱敏 `input_text` 与固定六角色问题；推荐角色来自三片互斥数据的 blind single-annotator silver labels，历史实际角色只作对照。

## 运行

### 本地 sessions Agent 调度集

上层合成集之外，sessions/ 保存 20 条已授权、已脱敏的本地历史任务评测。它只把每条记录的 input_text 发送给模型，其他历史字段仅用于规则复核标签和差异分析：

~~~sh
.venv/bin/python evaluation/sessions/run_sessions_eval.py \
  --base-url http://127.0.0.1:8765 \
  --threshold 0.9 \
  --output-dir evaluation/sessions/results
~~~

详见 sessions/README.md。sessions 评测与下面的 48 条合成 Agent/游戏/文档评测独立报告。

先确保本地服务已启动：

~~~sh
cd laya-zh
./start.sh
~~~

使用当前默认服务运行全部 48 条 case：

~~~sh
.venv/bin/python evaluation/run_eval.py
~~~

也可以指定服务地址、noul 高置信阈值和输出目录：

~~~sh
.venv/bin/python evaluation/run_eval.py \
  --base-url http://127.0.0.1:8765 \
  --threshold 0.9 \
  --output-dir evaluation/results
~~~

脚本只使用 Python 标准库，通过 POST /v1/decide 调用已有服务；不访问网络、不新增依赖。成功后生成：

- results/latest.json：机器可读的完整指标、逐 case 结果和失败记录。
- results/REPORT.md：中文汇总报告，包含总体/分场景指标、阈值表现和典型误判。

## 数据与标签

templates.json 定义每个 suite 固定的问题模板；同一 suite 的每个 case 使用完全相同的 questions。cases.json 中的 expected 是运行前由人工根据场景定义的期望标签，不能根据模型输出反向修改。

当前数据集分布：

- Agent 调度：16 条；route 覆盖 research、implementation、backend_test、e2e_test、solution_design、review、main_agent，并测试 priority 与 needs_human。
- 游戏：16 条；测试 NPC action、danger 和 use_item。
- 文档：16 条；测试 document_type、risk 和 needs_review。

每个 suite 都包含清晰正例、清晰反例和带有不完整信息/临界条件的边界样本。游戏、文档数据使用合成名称、编号和金额，不含真实个人或敏感信息。每条 state 经过脚本的 700 字符保守限制；在当前 max_len=1024、head_max_len=256 配置下，数据刻意保持远低于窗口上限。

## 指标定义

- choice exact accuracy：模型返回的 choice 与期望标签完全相同的比例。
- noul accuracy@0.5：noul >= 0.5 判为真，否则判为假，再与期望布尔标签比较。
- noul 高置信覆盖率：满足 p >= threshold 或 p <= 1-threshold 的 noul 样本比例。默认阈值为 0.9。
- 高置信覆盖集准确率：只在上述覆盖集内计算的 noul 分类准确率。
- score MAE：模型 score 与期望等级（0、1、2）的平均绝对误差。
- score round accuracy：将模型 score 按四舍五入映射到等级后，与期望等级相同的比例。

报告中的“典型误判或低置信样本”最多展示 10 条，优先列出误判，再列出置信度低于 0.6 的结果；如果本次没有这些样本会明确写出。HTTP 或响应 schema 错误会计入失败并使脚本退出码为 1，模型预测错误本身不会使评测失败。

## 解读边界

这是小规模合成集，不代表真实业务分布、跨行业泛化能力或生产安全性。当前服务概率没有针对这些业务标签做温度校准；阈值表现只能作为方案讨论和后续采集真实标注的起点。付款、权限、删除、发布、退款、封禁等不可逆或高风险动作仍需硬规则和人工确认。
