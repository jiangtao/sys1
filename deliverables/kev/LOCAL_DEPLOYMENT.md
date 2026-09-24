# Kev 本地部署

本目录与 `../laya-zh` 平级。源码来自 [jaredpalmer/kev](https://github.com/jaredpalmer/kev)，当前使用 Kev-0.8B（Qwen3.5 基座）在本机提供 TypeSafe System One 兼容接口。服务只监听 `127.0.0.1:8009`，模型下载到 `runtime/hf/`，日志和 PID 保存在 `runtime/`。

Kev 权重固定为 Hugging Face revision `54f4f8777356cd5bbbb6c6919c657f26e6f2f6d8`；检查点内指定的 Qwen3.5-0.8B-Base revision 为 `dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68`。首次启动需要下载适配器和基座权重。启动脚本设置 `HF_HUB_DISABLE_XET=1`，以绕过本机网络下停滞的 Xet 下载。

基座权重从 Qwen 官方 ModelScope 仓库下载，文件大小为 `1746942600` 字节，SHA-256 为 `c2b1e5a17d9c1e27685d92ed9b382911ebb99955ecd89052d1721241adfbab6c`，与上述 Hugging Face revision 的 LFS 元数据一致。已在 2026-09-23 验证服务通过 MPS + MLX + bfloat16 加载，并成功处理同时包含 `noul`、`choice` 和 `score` 的请求。

## 管理

```sh
./control.sh start
./control.sh status
./control.sh stop
tail -f runtime/server.log
```

若需重建依赖，在本目录执行：

```sh
uv sync --extra serve --no-dev --python 3.12
```

`start` 启动独立后台进程；模型完全加载前，`status` 会显示 `starting-or-unhealthy`。服务就绪后可访问 `GET http://127.0.0.1:8009/v1/models`。例如：

```sh
curl -sS http://127.0.0.1:8009/v1/systemone \
  -H 'content-type: application/json' \
  -d '{"model":"kev-latest","state":"客户被重复扣费，需要尽快处理。","questions":{"billing":{"type":"noul","instructions":"是否涉及账单？"}}}'
```

重启时复用 `runtime/hf/` 中已下载的权重；服务运行阶段只依赖本机文件。`control.sh` 在停止进程前会校验 PID 对应的服务命令。
