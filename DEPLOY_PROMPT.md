# 一句话部署

## 给 AI 助手的一句话（推荐，复制整段发给你的 Muse / Codex / 其他能操作服务器的 AI）

> 请把 X 发布板部署到我的服务器上：仓库是 https://github.com/WarholYuan/x-publish-board ，先完整读 README.md、docs/prerequisites.md、docs/feishu-base.md 和 docs/deployment.md，再按顺序执行——①帮我在飞书建好「X 运营」Base 的四张表（字段名严格照 feishu/schema.json，一字不改），把 Base token 和四个 table_id 填进 config.env；②把 board 和 collect 部署到服务器（bash scripts/install_server.sh），按 board/caddy/xboard.caddy 把反代并进 Caddy 并 reload；③用 extension/configure_extension.py 以我的 X 账号和域名生成回采扩展并打包；④最后按 docs/deployment.md 的验收清单逐条实测（手动渲染、板上点「发完了」回写飞书、扩展采集上报、fx_poll --dry-run），每条把实测结果报给我，有一步没通就停下来告诉我卡在哪，不要跳过验收直接说部署完成。我的服务器、域名、飞书账号和 X 账号信息我接下来发你。

发完这句，把这四样信息补给它就行：**服务器 IP 与登录方式、域名、你的 X handle 与昵称、飞书已登录确认**（lark-cli 要先在服务器上以你的飞书账号登录，这一步需要你本人配合扫码/授权）。

## 给人的一条命令（已备齐前置、填好 config.env 之后，在服务器上跑）

```bash
git clone https://github.com/WarholYuan/x-publish-board.git && cd x-publish-board && cp config.env.example config.env && nano config.env && sudo bash scripts/install_server.sh
```

跑完只剩两件手动事：①按 `board/caddy/xboard.caddy` 把反代片段并进你的 Caddyfile 并 reload；②按 `docs/extension.md` 生成并安装浏览器扩展、在 popup 里填回采令牌。然后按 `docs/deployment.md` 第 4 节验收。
