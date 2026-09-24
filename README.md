# 假期游戏

保留原来的 `deliverables/` 目录结构：

| 目录 | 内容 |
| --- | --- |
| `deliverables/laya-snake/` | 贪吃蛇游戏与网页 |
| `deliverables/laya-xiangqi/` | 象棋游戏与网页 |
| `deliverables/laya-zh/` | 两个游戏使用的本地 Laya 服务 |
| `deliverables/kev/` | 可选的本地 Kev 服务 |

仓库保存游戏和服务源码、依赖清单、启动脚本，以及当前 Mac 使用的 Pikafish 引擎和棋力文件。Laya 和 Kev 权重、Python 虚拟环境、运行存档和实验数据未提交。其他平台需要按象棋目录的说明部署兼容的 Pikafish。各服务的模型部署说明见 `laya-zh/README.md` 和 `kev/LOCAL_DEPLOYMENT.md`。

在 `deliverables/laya-zh/` 创建 Python 3.12 虚拟环境，按该目录的 `requirements.txt` 安装依赖，并将 Laya 模型部署到 `model/`。之后可运行：

```sh
cd deliverables/laya-snake
./start.sh
```

贪吃蛇打开 <http://127.0.0.1:8766>。象棋先启动 `deliverables/laya-zh/start.sh`，再运行 `deliverables/laya-xiangqi/start.sh`，打开 <http://127.0.0.1:8768>。可选的 Kev 参照 `deliverables/kev/LOCAL_DEPLOYMENT.md` 部署。Jev 的 `TYPESAFE_API_KEY` 通过环境变量或仓库外的 `~/.config/jev-snake/typesafe_api_key` 提供；不要将密钥放入仓库。
