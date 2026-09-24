# 弈境 · Laya / Jev / Kev 象棋服务

本目录提供一个只绑定 `127.0.0.1:8768` 的象棋 HTTP 服务。每局可选 Laya、Jev 或本地 Kev-0.8B，默认 Laya；human 模式由玩家执红、选定模型执黑，selfplay 模式由同一选定模型分别为红黑双方决策，duel 模式让两个不同模型各执一方，支持 Laya × Jev、Laya × Kev 和 Jev × Kev。Laya 使用本地 `http://127.0.0.1:8765/v1/decide`，Jev 使用 TypeSafe System One API，Kev 使用本地 `http://127.0.0.1:8009/v1/systemone`。三者接收同一份当前 FEN 与 Pikafish MultiPV 候选，并经过同一套合法性检查、评分守门和棋局裁决。模型原选、守门与实际落子在日志中分开记录。

官方引擎资源来自 [official-pikafish/Pikafish](https://github.com/official-pikafish/Pikafish/releases/tag/Pikafish-2026-09-06) 的 `Pikafish.2026-09-06.7z`。服务自动发现 `engine/Pikafish-MacOS-universal` 或兼容的官方二进制，并以二进制所在目录作为工作目录加载 `pikafish.nnue`。下载时会校验 GitHub API 提供的大小和 SHA-256；该发行包的 SHA-256 为 `41952bbfe2520faceb5902c69e6ab4845cc999841d2b49a95cc1be7867a25e5b`。引擎配置为 4 threads、256 MB hash，不设置棋力降档。网页默认每步搜索 **0.3 秒**，可选 0.3、0.6、1、2、8、15 秒，并在浏览器中记住选择；较短搜索能加快响应，但可能降低复杂局面的搜索深度。搜索时限不包含 Laya 推理耗时。API 未提供 `seconds` 时使用 0.3 秒，显式请求支持 0.1—30 秒。

极速更新取消了自对弈落子后的 650ms 停顿；一次响应完成即请求下一步。两种模型接收局面、行棋方、候选评分与各候选前 3 步变化，完整搜索路径仍保留在日志和决策树中。日志的 `timings` 展示搜索、模型调用及总耗时。原始响应按需展开，隐藏的日志和残局选择面板不再每步重复构建。搜索时限的个人选择会记住。

## 启动

网页的「决策模型」可切换 Laya / Jev / Kev；切换时按当前残局与模式新建一局，旧局存档仍保留。duel 模式可分别指定红黑模型，默认「Jev × Laya」，且禁止同模型对局；可按「红黑换边」重新开局。自对弈与混合对弈都可连续观战或单步推进；暂停会等待当前一手完成，页面进入后台时也会暂停。双方各次决策保留行棋方视角、模型原始输出与决策因子。右侧显示模型原选、引擎守门后的执行着法、分支评分和双方原选采纳次数；这些数据不代表模型独立棋力。

点击候选分支中的后续着法，可以在棋盘上预览变化，实际棋局不会改变。「选择残局」提供难度筛选、棋盘预览和人机／自对弈入口。10 个实战型残局由引擎自弈采样，保留 15—22 枚棋子和双方进攻子力；来源轨迹和验证数据见 [残局分析](validation/endgames-analysis.md)，新版验收见 [自对弈验收](validation/selfplay-checks.md)。

先确保已有 Laya 服务处于就绪状态：

```sh
cd deliverables/laya-zh
./start.sh
```

再启动象棋服务：

```sh
cd deliverables/laya-xiangqi
./start.sh
```

Jev 与 [贪吃蛇项目](../laya-snake/README.md)共用**同一份密钥配置**：进程环境变量 `TYPESAFE_API_KEY` 优先，否则读取仓库外 `~/.config/jev-snake/typesafe_api_key`。不需要、也不应在象棋目录复制密钥。未配置时 Jev 新局会明确拒绝创建，Laya / Kev 棋局仍可使用。Jev 请求使用 `jev-latest`，远程网络耗时不计入 Pikafish 的搜索秒数。Kev 请求使用 `kev-latest`，只访问本机服务，不发送 API 密钥；`GET /api/health` 会实际探测 `http://127.0.0.1:8009/v1/models`，并校验已加载的 run 为 `jaredpalmer/kev-0.8b@...`。
`GET /api/health` 中的 Jev `ready` 只表示本机依赖与密钥已配置，不检测远端连通性；Kev `ready` 会实际探测本机服务并核对已加载模型。真正的网络或推理失败会在本步日志和接口错误中说明。

`start.sh` 会把 PID 和日志写入本目录的 `runtime/`，重复执行会复用已运行的进程；默认优先使用相邻 `laya-zh/.venv` 的 Python（包含 Jev 客户端所需的 `httpx`），缺失时回退到系统 Python。缺少官方引擎时，`server.py` 会通过 GitHub Releases API 查找最新官方 `.7z` 包并解压；已存在的引擎不会重复下载。也可以直接运行前台服务；需要 Jev 时请选择有 `httpx` 的 Python：

```sh
python3 server.py --host 127.0.0.1 --port 8768
```

`--no-download` 只用于离线检查，缺少引擎时会返回明确的 503。

## HTTP 接口

所有成功的棋局响应都是 `game` 对象；前端也兼容把它包在 `{game: ...}` 中。棋局包含：

```json
{
  "id": "...",
  "fen": "...",
  "board": [["r", null]],
  "turn": "red",
  "legalMoves": ["a0a1"],
  "moves": [],
  "logs": [],
  "archiveLogs": [],
  "mode": "human",
  "model": "laya",
  "opponentModel": null,
  "status": "playing",
  "inCheck": false,
  "version": 0,
  "puzzle": {"id": "standard"}
}
```

接口如下：

```text
GET  /api/health
GET  /api/puzzles
POST /api/game       {"puzzle":"standard" | "endgame-01", "mode":"human" | "selfplay" | "duel", "model":"laya" | "jev" | "kev", "opponentModel":"laya" | "jev" | "kev"}
GET  /api/game?id=...
POST /api/move       {"gameId":"...","move":"a0a1","version":0}
POST /api/think      {"gameId":"...","seconds":8,"version":1}
POST /api/undo       {"gameId":"...","version":2}
```

duel 模式用 `model` 指定红方、`opponentModel` 指定黑方，两个值必须不同；其他模式无需提交 `opponentModel`。`/api/move` 只接受 human 模式红方的一步；selfplay 与 duel 模式手动调用会返回 409。界面通过串行的 `/api/think` 请求推进模型对弈，每次请求只推进当前行棋方的一步；human 模式的 `/api/think` 仍只接受黑方回合。任一模型失败时不会用 Pikafish 最佳着法静默替代：服务返回 503、保留当前回合，客户端可以重试。每局有锁和递增 `version`，请求携带旧版本时返回 409。旧存档没有 `model` 字段时默认按 Laya 恢复。

每条模型日志都保留实际决策树所需的字段：`model`/`provider`、`fenBefore`/`positionBefore`、`legalMoveCount`、`candidates`、`modelChoice`、`finalMove`、`guarded`、`guardReason`、搜索深度、评分、PV，以及按模型区分的 `raw.laya`、`raw.jev` 或 `raw.kev` 和 `raw.uci`。旧 Laya 日志的 `layaChoice`/`layaRank` 继续可读；新日志另有通用 `modelRank`。`scorePerspective` 和候选 `score` 对应当前行棋方；`engineBestMove`、`choiceScore`、`scoreLossCp` 用于比较模型原选与引擎守门结果。候选 `score` 同时提供 `{cp, type, value}` 或 `{mate, type, value}`。对非首选候选，只有与引擎 `bestmove` 同深度、无边界、同分才可直接执行；较弱、无效或无法解析的选择会被守门拦截，并在日志中说明原因。`archiveLogs` 保留悔棋撤回的完整日志并标记 `undone`；selfplay 悔棋撤回一 ply，human 悔棋撤回一组红方与黑方回合。

`puzzles.json` 中的十篇是本项目原创练习局面，标题和难度为编辑分级，不冒用古谱名称。服务将最近 128 局以 `runtime/games.json` 原子替换保存，启动时恢复 `mode`、版本、局面、着法、日志、归档日志和悔棋快照；悔棋快照只保存局面与 moves/logs 计数，不复制完整决策树。旧的完整 round 或只含 `Game.to_dict` 的迁移快照会自动压缩或按初始 FEN 与着法记录重建悔棋边界。重复局面会核对最近两轮循环的每一步：单方持续将军判该方负；双方互将或未检测到单方长将时按重复练习规则判和。非重复的连续将军不会被处罚。120 个连续无吃子/无兵半回合仍使用简化和棋规则；完整长捉等其余竞赛裁决尚未实现。长将处理参照 [WXF 官方规则](https://www.wxf-xiangqi.org/images/wxf-rules/2018_World_XiangQi_Rules_English2018.pdf)。

搜索会向 Pikafish 传入初始 FEN 和完整实际走子历史，使引擎能够识别循环。只有同深度、无评分边界且同分的候选之间，才优先选择历史出现次数较少的局面；不会为了强行分出胜负而选择明显劣着。日志保留 `engineBestMove`、Laya 原选、`historyPlyCount`、`repetitionAvoided` 和每个候选的 `repetitionCount`。裁决事件的 `adjudication.cycleMoves` 列出每一步将军事实，便于复查。

## 测试

Pikafish 对 Rule60 计数 120 的直接查询会主动退出。本服务在合法走法与将军检查中仅将临时查询计数限制到 119，真实棋局计数、棋谱和裁决不变；达到上限时按既定优先级完成终局判断，不再继续搜索。引擎异常会记录命令、退出码和原始输出；落子或悔棋刷新失败会还原棋局状态，并返回独立的失败日志。对应复现及验收见 [Rule60 边界修复](validation/rule60-fix.md)。

无需安装第三方 Python 包即可运行静态单元检查：

```sh
python3 -m unittest discover -s . -p 'test*.py' -v
python3 -m py_compile engine.py server.py
```

引擎联机测试需要本机 arm64 官方 Pikafish 和 Laya 服务已就绪；没有它们时，服务会报告 503，而不会伪造模型决策。
