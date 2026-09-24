# Laya 中文单模型本地部署

本目录是 Laya 的本地 HTTP 服务封装，不是模型训练源码。它使用 `convaiinnovations/laya-multilingual`，不启用模型内置 Router，也不包含 English 或 typed-decisions 模型。权重不在 Git 中；首次部署时下载到本目录被忽略的 `model/`，之后推理不需要联网。游戏的整体安装顺序见[仓库根 README](../README.md)。

Laya 的定位是本地或服务端调用的实时结构化决策中间件：输入 `state` 和 `questions`，输出 `choice`、`score`、`noul` 等判断结果，不是生成式聊天模型。本部署当前提供 loopback-only 的中文 HTTP 服务；应用场景、阈值用法和产品方向见 [APPLICATION_SCENARIOS.md](APPLICATION_SCENARIOS.md)。

模型来源固定为 Hugging Face revision `052592a15d198d9ad47da779604259b10b47b7aa`。两个大文件的官方 SHA-256 记录在 `checksums.sha256`，可在本目录执行以下命令复核：

```sh
shasum -a 256 -c checksums.sha256
```

## 已验证环境

- Python 3.12、Laya 0.3.4、PyTorch 2.11.0、Transformers 5.17.0
- 原机器的 `.venv/` 约 568 MB，`model/` 约 658 MB；新克隆需要重新创建
- 已在 Apple M2 Pro 上以 MPS 跑通中文 `choice`、`score` 和 `noul`
- 已设置离线模式复跑通过，推理阶段不依赖 Hugging Face 网络

## 效果评测

- [评测总说明](evaluation/README.md)
- [48 条合成业务场景报告](evaluation/results/REPORT.md)
- [20 条脱敏本地 sessions Agent 调度报告](evaluation/sessions/results/REPORT.md)
- [公开中文数据集候选与许可证边界](evaluation/PUBLIC_DATASETS.md)

当前评测仅用于判断方向和发现误差，不是生产精度承诺。尤其是 Agent 多标签调度和概率阈值，现阶段仍需要业务微调、温度校准和更大规模人工复核数据。

## 环境

从新克隆部署时，在本目录执行：

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/hf download convaiinnovations/laya-multilingual \
  --revision 052592a15d198d9ad47da779604259b10b47b7aa \
  --local-dir model
shasum -a 256 -c checksums.sha256
```

`requirements.txt` 是由 `requirements.in` 编译得到的锁定依赖清单；若需重新解析依赖，可执行：

```sh
uv pip compile requirements.in --python-version 3.12 --python-platform darwin --output-file requirements.txt
```

## 中文 smoke test

```sh
./run_smoke.sh
```

脚本会从本地 `model/` 加载模型，显式请求 `device="mps"`，并输出实际设备、choice/score/noul 结果及模型位置。若 MPS 加载或推理失败，会记录异常后自动用 CPU 重试。环境变量 `USE_TF=0`、`USE_TORCH=1` 和 `TOKENIZERS_PARALLELISM=false` 由脚本设置。

查看实际占用：

```sh
du -sh .venv model
find model -type f -print | sort
```

## 本地 HTTP 服务

服务只绑定 `127.0.0.1:8765`，启动时从本目录的 `model/` 加载一次中文模型，默认优先使用 MPS；如果 MPS 初始化失败或不可用，会自动回退到 CPU。启动过程设置 `USE_TF=0`、`USE_TORCH=1`、`TOKENIZERS_PARALLELISM=false`，并强制 Hugging Face 离线模式。

安装或更新服务依赖：

```sh
uv pip install --python .venv/bin/python -r requirements.txt
```

启动、查看状态和停止：

```sh
./start.sh
./status.sh
./stop.sh
```

脚本可重复执行。PID 和日志只写入本目录的 `runtime/`：`runtime/server.pid` 和 `runtime/server.log`。停止脚本会先校验 PID 对应的命令确实是本服务，不会按端口或模糊匹配终止其他进程；没有 PID 文件时重复停止会直接返回成功。不会创建 launchd 系统项。

### 健康检查

```sh
curl http://127.0.0.1:8765/health
```

就绪时返回 `200`，并包含 `status=ready`、实际 `device`、请求设备和 CPU 回退原因（如有）。模型加载失败时返回 `503`，不会把 Python 堆栈放进 HTTP 响应。

### 推理失败诊断

每次 `POST /v1/decide` 都会生成短 `request_id`；成功响应附带 `requestId`，失败响应的 `error.details` 会包含 `request_id`、异常类型、截断后的原因、设备、耗时和输入形状统计（state 字符数、问题数及每个 `choice` 的候选数）。完整异常栈写入 `runtime/inference-errors.log`，文件按 3 MB、保留 2 个备份滚动；HTTP 响应不会回传完整 state 或 traceback。根据 `request_id` 可以把前端报错与服务端日志对应起来。

当前已确认的残局失败原因是：当 `choice` 只有一个合法候选时，Laya SDK 的 `DecisionModel.forward` 仍固定执行 `topk(2)`，触发 `selected index k out of range`。服务加载模型后会调用本地 `laya_compat.py` 的 `register_single_option_padding`，只在单候选位置补一个 masked 槽位，保留原有单候选结果和多候选输出；该兼容补丁不改 SDK 或模型文件。

### 决策接口

`POST /v1/decide` 接受 Laya 原生的 `state`（字符串、JSON 对象或数组）和 `questions` 字典。问题定义沿用 Laya 的 `choice`、`score`、`noul` 格式：

```sh
curl -sS http://127.0.0.1:8765/v1/decide \
  -H 'content-type: application/json' \
  -d '{
    "state": {"message": "我被重复收取了两次费用，请尽快退还其中一笔。"},
    "questions": {
      "department": {
        "type": "choice",
        "instructions": "这条客户消息应该由哪个部门处理？",
        "criteria": {"账单": "付款、发票、重复扣款或退款", "技术支持": "登录、功能故障或产品使用问题"}
      },
      "urgency": {
        "type": "score",
        "instructions": "这条请求有多紧急？",
        "criteria": ["不紧急", "需要尽快处理", "关键业务被阻断"]
      },
      "refund_requested": {
        "type": "noul",
        "instructions": "客户是否明确要求退款？"
      }
    }
  }'
```

成功响应透传 Laya 的 `model`、`answers` 和 `usage`，并附加 `status=ok` 与实际 `device`。缺少字段、类型错误或问题定义不合法时返回 `4xx` JSON 错误；接口不提供公网绑定、英文模型或 typed-decisions 模型。

## Codex next-action 旁路实验

`next_action_client.py` 是调用侧 shadow 客户端，不属于 `/v1/decide` 服务端，也不会启动 SubAgent 或改变 Codex 的实际路线；当前尚未自动挂接到 Codex 任务生命周期，需要调用方在阶段边界显式调用。主 Agent 提供目标摘要、上一步结果和硬规则允许的候选；客户端先建议 `main_control / research / design / implement / validate / review` 中的下一步，仅当建议 `implement` 或 `validate` 时再建议 `frontend / backend / both`。所有执行结果仍回 `main_control` 检查。

从本目录运行示例：

```sh
printf '%s' '{"goal":"确认订单页面保存失败的原因","last_actor":"main_control","last_status":"none","last_summary":"尚未调查","allowed_actions":["main_control","research","design"],"allowed_surfaces":["frontend","backend","both"]}' | python3 next_action_client.py --log runtime/next-action-shadow.jsonl --actual-action main_control
```

输出中的 `status=observed` 只表示成功记录了模型建议，不表示已派发；`status=skipped` 表示本次旁路判断跳过。Laya 未启动、超时、返回 HTTP 错误或无效响应时，客户端标记 `skipped` 和 `skip_reason`，以正常退出码结束，调用方按原路径继续。日志仅保存摘要哈希、建议/实际动作、概率和耗时等元数据，不保存目标或阶段结果原文。`allowed_actions` 是主 Agent 的硬规则输出；客户端不能凭模型建议扩大权限、跳过必做检查或代替最终验收。
