# Snake Lab

本地十关贪吃蛇。玩家可以在网页上观察 Laya、Jev 或 Kev 的逐步选择；同一套规划器校验候选方向，并在模型失败或提议不满足安全约束时接管。目标顺序是**存活、当前食物的最短安全路线、能量**。

## 启动

先按[安装说明](../docs/setup.md)部署同级的 `laya-zh/`，然后从本目录运行：

```sh
./start.sh
```

打开 <http://127.0.0.1:8766>。脚本使用 `../laya-zh/.venv/bin/python`，若默认的 Laya 服务尚未运行，会尝试自动启动它。终端按 `Ctrl+C` 停止本次脚本启动的服务。网页和 API 仅监听本机。

## 模型接入

| 模型 | 游戏中的作用 | 准备工作 |
| --- | --- | --- |
| Laya | 默认模型，提出候选方向 | `../laya-zh/model/` 和 `127.0.0.1:8765` 服务就绪 |
| Jev | 通过 TypeSafe System One API 提出方向 | 配置仓库外的 `TYPESAFE_API_KEY` |
| Kev | 通过本地 System One 兼容服务提出方向 | 外部 Kev 服务在 `127.0.0.1:8009` 加载 `kev-latest` |

可以在启动前用 `SNAKE_LAYA_URL` 和 `SNAKE_KEV_URL` 改变本地服务地址；详情和 Kev 启动命令见[安装说明](../docs/setup.md)。无论选择哪个模型，游戏仍使用 Laya tokenizer 校验输入长度，因此 `../laya-zh/model/tokenizer/` 必须存在。切换模型时以同一 seed 重新开局，方便对照。

## 玩法与记录

- 默认自动运行十关；可以暂停、单步、调整播放速度、重开或用 `?seed=<整数>` 重放。
- 规划器检查蛇身碰撞、存活边界和已证安全路线。模型原选、实际执行方向、接管原因分别记入日志；规划器跳过模型的步骤不会计作模型成功。
- 游戏状态在内存中，逐步决策和元数据保存在 `runtime/games/`。该目录被 Git 忽略；重新开始不会清除已有磁盘日志。
- `web/` 是静态前端，`app.py` 提供 HTTP API，`engine.py` 管理游戏规则，`planner.py` 和 `certified_routes.py` 负责路线规划及校验。

## 检查

```sh
../laya-zh/.venv/bin/python -m unittest discover -s . -p 'test*.py'
node --check web/app.js
```

部分测试需要 `../laya-zh/model/tokenizer/`；完整运行还需要 Laya 服务。模型和规划器各自的贡献应根据每步的 `modelCalled`、`modelChoice`、`executed` 与 `executionSource` 字段判断，通关不代表模型每步都选对。
