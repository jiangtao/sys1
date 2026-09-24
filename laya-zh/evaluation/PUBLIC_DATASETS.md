# 公开中文评测集候选

本页记录用于补充当前 48 条合成 case 的公开数据集候选。这里只做选型和采集规划，不下载数据，也不自动改写 `cases.json`、`templates.json` 或 `results/`。许可证以官方页面为准；许可证不清或仅学术使用的候选，不视为可商用数据源。

表中的 `context` 是 Laya 服务建议的 `max_len`，不是数据集原始文本长度限制。`choice` 表示直接或转换后的类别选择，`noul` 表示从标签派生的二元判断，`score` 表示需要额外定义等级映射的有序分数。除非特别说明，`noul` 和 `score` 都不是原始数据集自带标签，转换规则必须写入 `transform` 并由人工复核。

## 立即可用

这些候选的公开许可适合先做内部评测；使用时仍需保留原作者署名、许可证和来源记录。

| 数据集（官方页） | 适用场景 | 规模 / 标签 | 许可证 | 映射到 Laya | 推荐 context | 限制 |
| --- | --- | --- | --- | --- | ---: | --- |
| [MASSIVE 1.1 zh-CN](https://huggingface.co/datasets/AmazonScience/massive) | Agent 意图路由、客服/工具路由 | `zh-CN`：60 intents、55 slots；train/validation/test 为 11,514 / 2,033 / 2,974 | CC BY 4.0 | `choice`：intent；`noul`：是否含目标 slot（派生）；`score`：不使用 | 1,024；带对话元数据时 2,048 | 单轮口语意图为主；不能直接代表多文件 Agent 调度，需要保留原始 intent/slot 对照 |
| [CAPP-130](https://github.com/EnlightenedAI/CAPP-130) | 文档隐私、外发安全、审核路由 | CAPP-130；隐私政策 13 类 | Apache-2.0 | `choice`：隐私类别；`noul`：是否涉及隐私内容；`score`：敏感等级需派生 | 1,024；政策段落拼接时 2,048 | 领域集中在隐私政策；13 类与本项目的“敏感/需审核”定义仍需对齐 |
| [COLDataset](https://github.com/thu-coai/COLDataset) | 聊天/评论安全、攻击识别 | 37,480 条中文评论；安全/攻击标签（细分标签以官方发布为准） | Apache-2.0 | `choice`：安全/攻击或攻击类型；`noul`：是否攻击/违规；`score`：风险等级需派生 | 1,024；带短上下文时 2,048 | 评论分布和攻击定义与业务安全策略可能不同；需要去重并保留官方 split |

## 需转换或非商业使用

这些候选可以帮助覆盖更贴近业务的场景，但需要先转换标签、接入 OCR/结构化流程，或受到非商业/学术限制。

| 数据集（官方页） | 适用场景 | 规模 / 标签 | 许可证 | 映射到 Laya | 推荐 context | 限制 |
| --- | --- | --- | --- | --- | ---: | --- |
| [Chinese-Ambiguous-Reference](https://github.com/ygan/Chinese-Ambiguous-Reference) | 客服多轮对话、指代消解、人工升级 | 1,000+ 条客服对话；歧义指代标注 | MIT | `choice`：歧义类型或解析结果（需转换）；`noul`：是否仍有歧义；`score`：歧义严重度需派生 | 1,024；保留前后轮时 2,048 | 原始标注要转换成业务问题；应保留歧义所在轮及其上下文，避免把整段对话无差别塞入窗口 |
| [Orak](https://huggingface.co/datasets/KRAFTON/Orak) | 游戏 NPC/轨迹结构参考 | 英文游戏轨迹；规模和字段以 HF 数据卡为准 | CC BY-NC 4.0 | `choice`：下一步动作；`noul`：动作是否满足约束（派生）；`score`：轨迹奖励/风险需派生 | 2,048–4,096 | 英文且非商业；只借鉴轨迹字段和任务结构，不能当作中文线上效果证据 |
| [SCID](https://davar-lab.github.io/dataset/scid.html) | 发票/票据 OCR 后的文档路由与字段审核 | 中文票据图像和 OCR/实体标注；官方页列出六类票据 | CC BY-NC-SA 4.0 | `choice`：票据类型；`noul`：字段是否缺失/是否需复核（需派生）；`score`：抽取风险需派生 | OCR 后 1,024；OCR 加元数据 2,048 | 先经过图像/OCR 转换；非商业且需相同方式共享，精确字段校验仍应由 OCR/规则完成 |
| [CA4P-483](https://github.com/zacharykzhao/CA4P-483) | Android 隐私政策、移动端隐私审核 | CA4P-483；中文 Android 隐私政策数据，具体标签以官方仓库为准 | 仅学术研究（按官方仓库说明；不视为可商用许可证） | `choice`：隐私类别；`noul`：是否包含目标隐私条款；`score`：风险等级需派生 | 1,024；条款带上下文时 2,048 | 只能用于获准的研究/评测；需先确认当前仓库条款和标签定义 |
| [ChineseSafe](https://huggingface.co/datasets/SUSTech/ChineseSafe) | 中文安全、违规内容筛查 | 中文安全评测数据；标签和 split 以 HF 数据卡为准 | CC BY-NC 4.0 | `choice`：安全类别；`noul`：是否不安全；`score`：风险等级需派生 | 1,024；带对话上下文时 2,048 | 非商业；安全政策、标签边界和本项目阈值需要单独对齐 |

## 暂不建议直接纳入

| 数据集（官方页） | 适用场景 | 规模 / 标签 | 许可证 | 映射到 Laya | 推荐 context | 限制 |
| --- | --- | --- | --- | --- | ---: | --- |
| [CLUE](https://github.com/CLUEbenchmark/CLUE) | 通用中文 NLU 基线 | 多任务中文理解基准；各任务规模和标签不同 | 许可不清 | 各任务可映射 `choice`；`noul`/`score` 需按任务另行定义 | 1,024–2,048 | 在许可证核验前不宜直接用于商业评测、分发或训练；任务间不能合并成单一业务指标 |
| [ProvBench / ProvData 论文页](https://aclanthology.org/2025.acl-long.312/) | 合同条款推荐、合同审核 | ACL 2025 相关数据与基准；官方论文描述任务，数据仓库当前不可依赖 | 许可不清；相关仓库曾返回 404 | `choice`：推荐条款/审核路由；`noul`：是否需审核（派生）；`score`：风险需派生 | 2,048–4,096 | 在官方数据发布、仓库和许可证恢复并核验前，不下载、不纳入评测；论文结果不能替代本地金标准 |

## 最小采集路线

第一轮不替换现有 48 条合成 case，先做一个可追溯的小型外部验证集，并保留官方 split，避免把调参集误当测试集。

1. **MASSIVE zh-CN**：先取官方 `test` 的分层子集，每个 intent 取 5–10 条（不足则全取），先覆盖 60 个 intent；保留原始 `intent` 和 `slots`，生成一个 `choice` 问题。第一轮同时跑 1,024 与 2,048，比较截断、延迟和 intent exact accuracy。
2. **CAPP-130**：按 13 类分层，每类先取 10–20 条；原始类别作为 `choice` 金标准，再派生“是否涉及隐私/是否需审核”的 `noul`，派生规则单独人工复核。政策长段落只保留标题、相关段落和来源位置。
3. **COLDataset**：按官方顶层安全/攻击标签分层，每类先取 100–200 条，去除重复或近重复评论；原始标签评估 `choice`/`noul`，不要未经确认把攻击标签直接当作统一风险分数。
4. **Chinese-Ambiguous-Reference**：先取 50–100 段对话，围绕标注的歧义轮保留前后必要轮次；将原始解析/歧义标注转换成“是否需要澄清”的 `noul`，另保留原始标签用于误判回溯。转换样本应做一次独立人工复核。
5. **游戏场景**：在 Orak 许可和语言限制未解决前，先用现有游戏字段生成合成规则金标准；用明确规则标注 `action`、`danger`、`use_item`，再比较 1,024/2,048。游戏线上效果不以 Orak 英文轨迹直接代替。

每个来源至少拆出 `train/dev/test` 或“调参/验证/留出”三部分；任何由原始标签推导出的 `noul`/`score` 都应记录规则版本，并在最终 test 前冻结。

## 统一记录格式

建议用 JSONL 作为采集层格式；导入当前评测 runner 时，再按 suite 把同模板问题合并到 `cases.json`，不要覆盖官方原始标签。

```json
{
  "id": "massive-zh-CN-test-0001",
  "source": "MASSIVE-1.1",
  "source_split": "test",
  "scenario": "agent",
  "state": "原始文本或经过记录的对话上下文",
  "gold": {
    "choice": "原始 intent",
    "noul": null,
    "score": null
  },
  "original_label": {"intent": "原始 intent", "slots": {}},
  "context_window": 1024,
  "provenance": {
    "url": "https://huggingface.co/datasets/AmazonScience/massive",
    "license": "CC BY 4.0",
    "transform": "intent 直接映射 choice；未派生 noul/score",
    "review_status": "unreviewed"
  }
}
```

`gold` 中只填已经定义并人工确认的字段；不适用字段保持 `null`，不要用模型预测补齐。`provenance.transform` 记录清洗、切分和标签派生规则，`review_status` 在独立复核后再改为 `reviewed`。当前服务的正式输入仍以 [README.md](README.md) 中的 `templates.json` 和 `cases.json` 约定为准。
