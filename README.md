# Snake & Xiangqi

两个本地网页游戏：十关贪吃蛇和中国象棋。两个游戏都可选 Laya、Jev 或 Kev 作决策；游戏规则、候选动作和安全校验由各自的游戏服务负责，模型只在这些候选中提出选择。

## 仓库结构

| 目录 | 用途 |
| --- | --- |
| [`snake/`](snake/) | 贪吃蛇网页、服务、规划器和测试；端口 `8766` |
| [`xiangqi/`](xiangqi/) | 象棋网页、服务、残局和 Pikafish 引擎；端口 `8768` |
| [`laya-zh/`](laya-zh/) | 本项目的 Laya HTTP 服务适配代码；端口 `8765` |

`laya-zh/` 是服务封装，**不是 Laya 模型的训练源码或权重**。Kev 的实现由独立的 [jaredpalmer/kev](https://github.com/jaredpalmer/kev) 仓库维护；本仓库只保留两个游戏的 Kev 客户端。两种模型的权重都不在 Git 中。

## 环境要求

- macOS：仓库内的 Pikafish 可执行文件是 Mac 通用版本。其他系统需在 `xiangqi/engine/` 放置兼容的官方引擎和 `pikafish.nnue`。
- Python 3.12 和 [`uv`](https://docs.astral.sh/uv/)；安装依赖及首次下载模型时需要网络。
- Laya 是默认决策模型。贪吃蛇的输入预算检查也使用 Laya tokenizer，因此使用 Jev 或 Kev 玩贪吃蛇时仍需部署 `laya-zh/model/`。

## 快速开始：Laya 与两个游戏

从仓库根目录执行。以下命令将 Laya 模型固定到已验证的 Hugging Face revision，下载到被 Git 忽略的 `laya-zh/model/`：

```sh
cd laya-zh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/hf download convaiinnovations/laya-multilingual \
  --revision 052592a15d198d9ad47da779604259b10b47b7aa \
  --local-dir model
shasum -a 256 -c checksums.sha256
./start.sh
cd ..
curl http://127.0.0.1:8765/health
```

服务报告 `"status":"ready"` 后，在另外的终端从仓库根目录启动游戏：

```sh
cd snake
./start.sh
# http://127.0.0.1:8766
```

```sh
cd xiangqi
./start.sh
# http://127.0.0.1:8768
```

`snake/start.sh` 会在默认地址没有 Laya 服务时尝试启动它；`xiangqi/start.sh` 需要先启动 Laya。服务只绑定本机回环地址。游戏细节见 [贪吃蛇说明](snake/README.md)和[象棋说明](xiangqi/README.md)。

## 可选模型

| 模型 | 接入方式 | 配置与检查 |
| --- | --- | --- |
| Laya | 本地 `http://127.0.0.1:8765/v1/decide` | 启动 `laya-zh/start.sh`，通过 `/health` 检查 |
| Kev 0.8B | 本地 `http://127.0.0.1:8009/v1/systemone` | 按下文启动独立 Kev 服务；游戏用 `/v1/models` 检查所加载的 run |
| Jev | TypeSafe System One API | 设置 `TYPESAFE_API_KEY`，或把密钥放在仓库外的 `~/.config/jev-snake/typesafe_api_key`（权限 `600`） |

可以在启动游戏前设置服务基础 URL，例如 `http://127.0.0.1:8009`：

| 游戏 | Laya 地址 | Kev 地址 |
| --- | --- | --- |
| 贪吃蛇 | `SNAKE_LAYA_URL` | `SNAKE_KEV_URL` |
| 象棋 | `XIANGQI_LAYA_URL` | `XIANGQI_KEV_URL` |

旧的 `LAYA_SNAKE_LAYA_URL`、`LAYA_SNAKE_KEV_URL`、`LAYA_XIANGQI_LAYA_URL` 仍兼容。更改地址后重启对应游戏服务。

### 部署 Kev

Kev 源码放在本仓库外的相邻目录。以下版本与本项目当前客户端验证过的 Kev-0.8B 配置一致；权重会下载到 Kev 目录的 `runtime/hf/`，不会进入本仓库。

```sh
git clone https://github.com/jaredpalmer/kev.git ../kev
cd ../kev
git checkout 557598fced1dada75dfbf36ed144dce309ac6ceb
uv sync --extra serve --no-dev --python 3.12
HF_HOME="$PWD/runtime/hf" HF_HUB_DISABLE_XET=1 \
  .venv/bin/python -m kev.serve \
  --run jaredpalmer/kev-0.8b@54f4f8777356cd5bbbb6c6919c657f26e6f2f6d8 \
  --port 8009
```

在另一终端用 `curl http://127.0.0.1:8009/v1/models` 检查：`models` 中应有 `name=kev-latest`，且 `run` 以 `jaredpalmer/kev-0.8b@` 开头。然后在游戏网页的“决策模型”中选择 Kev。Kev 服务不需要 Jev API 密钥。

Jev 只在选择它时需要密钥。不要把密钥写进代码、Git、游戏 URL 或示例配置文件。

## 数据与许可

Git 忽略 Laya/Kev 权重、虚拟环境、运行日志与对局存档。**克隆本仓库不会恢复原机器上的对局**；原机器的存档仍在各游戏的 `runtime/` 目录。象棋随仓库提供 Mac 版 Pikafish、NNUE 棋力文件及其[许可证](xiangqi/engine/NNUE-License.md)；使用前请阅读许可证。

## 检查

部署 Laya 依赖后，可从各游戏目录运行：

```sh
cd snake
../laya-zh/.venv/bin/python -m unittest discover -s . -p 'test*.py'
```

```sh
cd xiangqi
../laya-zh/.venv/bin/python -m unittest discover -s . -p 'test*.py'
```
