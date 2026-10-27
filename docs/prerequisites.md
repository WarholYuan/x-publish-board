# 前置条件与部署实况

## 一、你需要先备齐的东西（缺一不可的标 ★）

| # | 东西 | 具体要求 | 说明 / 参考 |
|---|---|---|---|
| 1 ★ | 一台 Linux 服务器 | Ubuntu 22.04 / 24.04 或 Debian 12；**1 核 1GB 内存、10GB 磁盘就够**（这套只有两个 Python 标准库服务 + 一个静态页，没有数据库）；有公网 IP，能用 root 或 sudo；能正常出网访问飞书、x.com、api.fxtwitter.com | 任意 VPS 厂商都行。原作者的实例就是和别的服务共用一台 2GB 小机器跑的，单独跑这套用最小档即可，行情 roughly 几美元/月起。**没有服务器也行**：用你自己的电脑跑（Mac / Windows / Linux 桌面均可），见 `docs/no-server.md`，代价是电脑关机就停 |
| 2 ★ | 一个域名 | 任意一个你能改 DNS 的域名或子域名（如 `x.example.com` 或直接用主域名路径），A 记录解析到服务器 | 必须走 **HTTPS**：板页要嵌令牌、扩展要往你的域名上报数据，没证书跑不通。用 Caddy 的话证书是自动免费签的（Let's Encrypt），不用另买。**没有域名也行**：Tailscale Funnel 免费给一个稳定的 `*.ts.net` https 地址，或纯本地用 `127.0.0.1`，见 `docs/no-server.md` |
| 3 ★ | 飞书账号 | 免费版就行，不用企业版 | 整套系统的数据都存在飞书多维表格里：发布库、选题池、账号日快照、对标账号。按 `docs/feishu-base.md` 建一个 Base 即可 |
| 4 ★ | lark-cli | 装在**服务器上**，并以你的飞书账号登录（`--as user` 能读到上面那个 Base） | 这是服务器读写飞书的唯一通道。lark-cli 本身需要 Node.js 18+ 环境来安装运行（服务器上装一个 Node 即可，业务代码本身不需要 Node） |
| 5 ★ | 要运营的 X 账号 | 就是你要发帖的号本人 | 回采只认这一个号：扩展会校验当前浏览器登录的账号，不是它直接跳过 |
| 6 ★ | 一台自己日常用的电脑 + Chromium 浏览器 | Chrome / Edge / Arc 任一 | 用来装回采扩展、打开发布板。发布板本身手机也能用（Safari 打开板页即可），但扩展只能装在电脑浏览器上 |
| 7 | X Premium（可选） | 只有你要发**长文**时才需要 | 短帖、线程、图帖都不需要。长文编辑器是 Premium 功能，没开就用不了长文动线。价格以 X 官方为准 |
| 8 | Python 3.10+ | 服务器系统自带一般都满足 | 全部代码只用 Python 标准库，**零 pip 依赖**，不用装任何包 |

**明确不需要的东西**：不用买 X API（数据靠扩展读页面 + FxTwitter 公开接口兜底）、不用数据库（飞书就是数据库）、不用 Docker、不用对象存储（帖子图片就存服务器的静态目录）、不用单独的推送/消息服务。

## 二、部署时你要填的就 6 个值

部署前把这几个值准备好，填进 `config.env`，其余全有默认值：

1. `FEISHU_BASE_TOKEN`——飞书 Base 链接里 `/base/` 后面那一段；
2. 四张表的 table_id——发布库、选题池、账号日快照、对标账号各一个（`lark-cli base +table-list` 一条命令查全）；
3. `X_TARGET_ACCOUNT`——你的 X handle，不带 @；
4. `X_DISPLAY_NAMES_JSON`——预览里显示的昵称，如 `{"@your_handle": "你的昵称"}`；
5. `SITE_DOMAIN` / `SITE_URL`——你的域名；
6. `LARK_CLI`——服务器上 lark-cli 的实际路径（`which lark-cli`）。

两个服务令牌（板页状态令牌、回采令牌）**不用准备**：服务首次启动自动生成，存在服务器上权限 600 的文件里；回采令牌部署后从服务器文件里复制一份，填进浏览器扩展的 popup 就行。

## 三、参考部署实况（原作者线上就是这么跑的）

朋友照这个拓扑部署即可，不用自己设计：

| 部件 | 跑法 | 调度 / 端口 |
|---|---|---|
| 发布板渲染 `render_board.py` | systemd timer 定时跑一次性任务 | **每 5 分钟**渲染一次静态板页 |
| 状态服务 `state_server.py` | systemd 常驻服务 `xboard-state` | 只绑 `127.0.0.1:8791`，经 Caddy `/x-api/` 反代 |
| 回采接收 `ingest_server.py` | systemd 常驻服务 `xcollect` | 只绑 `127.0.0.1:8795`，经 Caddy `/collect-api/` 反代 |
| 公开兜底 `fx_poll.py` | cron | **每小时第 17 分**跑一次 |
| 板页本体 | 静态文件 | Web 根目录下 `/x/`，图片在 `/x/assets/<板上编号>/` |
| 扩展安装包 | 静态文件 | 打包成 zip 放在 `/xcollect/extension.zip` 供自己下载 |
| 配置 | 服务器上两份同内容 `config.env` | `/opt/xboard/config.env` 与 `/opt/xcollect/config.env`，权限 600 |
| 日志 | 文件 | 渲染 `/opt/xboard/render.log`；回采 `/opt/xcollect/ingest.log`、`fx_poll.log`；回采状态账本是 `/opt/xcollect/` 下的 jsonl 文件，别删 |

对外只暴露 80/443（Caddy），两个 Python 服务都不直接对外。飞书里改完内容，最多等 5 分钟自动上板；等不及手动跑一次渲染脚本立即生效。

## 四、时间和成本预估

- **一次性部署耗时**：把下面《一句话部署》交给你的 AI 助手（Muse / Codex 等能操作你服务器的都行），大约 30–60 分钟走完验收；纯手工照 `docs/deployment.md` 做，预留 1–2 小时，主要时间花在飞书建表上。
- **持续成本**：服务器 + 域名是仅有的固定开销（合计 roughly 人民币几十元/月以内，取决于你选的 VPS 档位）；飞书免费；X Premium 只有发长文才需要，按需开。走 `docs/no-server.md` 的本地模式则固定开销为 0。
- **日常维护**：基本零维护。要看的就是三个日志文件；飞书表结构别改字段名（改名即断链），其余随便用。
