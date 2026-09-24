# 本地验收记录

2026-09-22，在 Apple Silicon 本机使用实际 Laya MPS 服务和 Pikafish 2026-09-06 验证。

## 浏览器检查

- 标准局 32 个棋子、44 个初始合法着法，10 个残局入口。
- 点击红兵 `e3` → `e4`，自动触发 8 秒搜索、真实 Laya 调用与黑方合法应手。
- 本次搜索深度 23，返回 5 个候选。侧栏可查看原始模型输出与候选 PV；决策树逐层展示候选、选择、守门和执行结果。
- 悔棋恢复初始局面，被撤回的决策仍可查看和导出。
- 第 1、10 个残局切换成功；棋盘可翻转。
- 390px 窄屏无横向溢出，主流程无 JavaScript 异常。

浏览器环境是独立 Headless Chrome 153。用户现有 Chrome 的 browser-skill 扩展未连接，CUA Chrome 通道超时，因此这份记录不代表已完成用户真实 Chrome Profile 的验收。

截图：`desktop.png`、`decision-tree.png`、`mobile.png`；机器可读结果：`ui-results.json`。

## 引擎及源码检查

- 官方发行包 SHA-256：`41952bbfe2520faceb5902c69e6ab4845cc999841d2b49a95cc1be7867a25e5b`，与 GitHub release asset 的 digest 一致。
- 10 个原创残局均通过引擎严格局面校验，合法着法数量依次为 17、18、11、21、13、22、23、17、17、28；均红方先行且未被将军。
- `python3 -m unittest discover -s . -p 'test*.py' -v`：5 个检查通过。
- Python 编译检查、`sh -n start.sh`、`node --check web/app.js` 通过。

## 独立后端验证

- 非法走法 `e3e5`、错误回合请求、旧版本请求均返回 409，棋局保持不变。
- 真实 Laya 返回 `laya-rl-agent` 的 `answers.move.choice`，日志保留原始模型响应与 UCI 搜索输出。
- 实测一次较弱模型选择被最优评分守门拦截，一次模型直接选择最优着法。
- 分别构造将死与困毙局面，引擎均返回 0 个合法着法，后端正确判负。
- 独立坏端口客户端会明确报告 Laya 不可用，共享模型服务未被修改或停止。
- 最终服务在 8768 启动，health 中端口、模型及引擎状态一致；启动脚本可重复执行。
