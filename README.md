# X 发布板（X Publish Board）

一套以**飞书多维表格为唯一数据源**的 X（Twitter）自媒体发布与复盘系统。内容在飞书里写，发布板负责「照着发」，回采负责「发完自动收数据」。

> 这是从一套已上线实跑的系统脱敏整理成的可自部署版本：原作者的域名、X 账号、飞书 Base ID、令牌全部已抽成配置，换成你自己的即可跑。

## 它解决什么

- 写帖、排期、配图都堆在聊天记录和备忘录里，发的时候要来回复制，还容易发错图、漏首条回复；
- 发完之后曝光、点赞、收藏全靠手动回 X 后台抄，1 小时 / 24 小时 / 72 小时三个时间点根本记不全；
- 复盘没有结构：哪类选题、哪种打法真的带来涨粉，看不出来。

## 系统组成

```
飞书 Base「X 运营」                    服务器（任意一台 Linux + Caddy/nginx）
├─ 发布库（31+1 字段）      ──渲染──▶  board/render_board.py ──▶ 静态发布板 /x/
├─ 选题池（10 字段）                      │ 每 5 分钟自动渲染一次
├─ 账号日快照（9 字段）                   └─ board/state_server.py ──▶ 点「发完了」回写飞书
├─ 对标账号（7 字段）
└─ 数据看板（仪表盘）          浏览器扩展（extension/）
                                  │ 只读采集本人帖子 + Analytics
                                  ▼
                               collect/ingest_server.py ──▶ 回写发布库 1h/24h/72h 数据
                               collect/fx_poll.py（每小时，FxTwitter 公开数据兜底）
```

| 模块 | 目录 | 说明 |
|---|---|---|
| 发布板渲染 | `board/render_board.py` | 从飞书发布库全量渲染静态板页：待发布 / 已发布分区、按内容类型分开的复制动线、X 仿真预览、图片保存到相册 |
| 状态服务 | `board/state_server.py` | 板上点「发完了 / 撤销」，实时回写飞书状态与发布时间（127.0.0.1:8791，经反代 `/x-api/`） |
| 回采核心 | `collect/core.py` | 帖子链接自动匹配（正文相似度 + 发布时间 ±24h）、1h / 24h / 72h 三窗口回填、幂等账本、粉丝数来源防护 |
| 回采接收端 | `collect/ingest_server.py` | 接收扩展上报（127.0.0.1:8795，经反代 `/collect-api/`） |
| 公开兜底 | `collect/fx_poll.py` | 每小时用 FxTwitter 公开接口补数据；粉丝总数只认这条官方口径，不认扩展抄来的数 |
| 浏览器扩展 | `extension/` | Chromium MV3，只读：采集主页帖子列表、单帖指标、Analytics；只认本人账号，不是本人直接跳过 |
| 飞书结构 | `docs/feishu-base.md` + `feishu/schema.json` | 四张表的完整字段、选项、公式与仪表盘定义，附 `lark-cli` 建表脚本 |

## 发布板怎么用（日常动线）

1. 在飞书「发布库」新建一条：标题/首句、正文、图片（最多 4 张，X 单帖上限）、内容类型、账号，状态选「待发布」。
2. 最多 5 分钟后它自动出现在发布板上，并拿到一个板上编号。
3. 在手机或电脑打开发布板：
   - **短帖 / 图帖 / 视频 / 转帖**：「复制正文」→「打开 X 发帖」（真实 intent 链接，预填正文）→ 在 X 里贴图、发送；
   - **线程**：「复制整串」或逐条复制，首条用 intent 预填，后续回复自己；
   - **长文**：「去发布长文」（自动复制标题并打开 X 长文编辑器）→ 粘贴标题 → 回板「复制正文」→ 粘贴发布；
   - 发之前可点「预览发布效果」看仿真排版（短帖版式 / 长文版式是两套）。
4. 发完把帖子链接贴回飞书（或等回采自动匹配），在板上点「发完了」——状态和发布时间自动回写飞书。
5. 之后不用管：扩展 + FxTwitter 兜底会在 1h / 24h / 72h 三个窗口自动回填曝光、点赞、收藏、转发、回复、主页访问、链接点击，公式字段自动算互动率、收藏率、千次曝光涨粉和爆文判定。

## 设计红线（踩过坑才有的）

- **不自动发帖。** 板子只做到「复制 + 打开 X」，最后的发布点击永远由号主在 X 页面里完成。自动发帖是封号高危区，这条不要改。
- **扩展只读。** 不调用 X 任何写接口、不读 cookies、不拦截网络请求，只读已打开页面上的可见数据。
- **粉丝总数只认兜底口径。** 扩展从页面抄来的粉丝总数一律忽略（曾把认证粉丝数误抄成总数），只接受 FxTwitter / 官方接口读到的数；扩展只负责认证粉丝数与帖子私有数据（主页访问、链接点击等）。
- **回填幂等。** 同一条帖子每个窗口只回填一次（本地账本），重跑、重复上报都不会重复写。

## 没有服务器 / 没有域名？

也能跑，零成本。服务器和域名只是手段，真正需要的是「一台在用的机器 + 一个稳定的访问地址 + 定时任务」：用你自己的电脑跑全部服务（`local/` 本地网关替代 Caddy），地址用本机 `127.0.0.1`（纯本地版），或再套一层 Tailscale Funnel 拿到一个免费稳定的 `*.ts.net` https 地址（手机也能打开）。功能一套不少，代价只是电脑关机就停。完整步骤、定时常驻与验收清单见 [docs/no-server.md](docs/no-server.md)。

## 一句话部署

不想自己一步步来的，直接把 [DEPLOY_PROMPT.md](DEPLOY_PROMPT.md) 里那段话复制给你的 AI 助手（附上服务器、域名、X 账号信息），它会照本仓库的文档部署并逐条验收。前置条件清单（需要备齐什么、参考部署实况、成本预估）在 [docs/prerequisites.md](docs/prerequisites.md)。

## 快速开始

0. 先看 [docs/prerequisites.md](docs/prerequisites.md) 备齐：Linux 服务器（1 核 1GB 够）、域名 + HTTPS、飞书账号、装好并登录的 lark-cli、要运营的 X 账号。
1. 按 [docs/feishu-base.md](docs/feishu-base.md) 建飞书 Base（手动照表建，或跑 `feishu/create_base.sh` 用 lark-cli 半自动建），拿到 Base token 和四张表的 table_id。
2. 复制 `config.env.example` 为服务器上的 `/opt/xboard/config.env` 与 `/opt/xcollect/config.env`（或同一份），填飞书、账号、域名、路径。
3. 按 [docs/deployment.md](docs/deployment.md) 部署 board 与 collect 两个服务（systemd + Caddy 片段都在仓库里）。
4. 按 [docs/extension.md](docs/extension.md) 生成并安装你的回采扩展（把占位符换成你的 X 账号和域名）。
5. 在飞书发布库放一条测试帖，走一遍「上板 → 复制 → 打开 X → 发完了 → 等 1h 回填」，全程通了再正式用。

## 前置条件

- 一台能跑 Python 3.10+ 的 Linux 服务器，有 HTTPS 域名（Caddy 最省事，仓库给了片段；nginx 同理）；**或者**你自己的电脑 + Tailscale 账号，走零成本的本地模式，见 [docs/no-server.md](docs/no-server.md)；
- [lark-cli](https://github.com/larksuite/cli) 已以你的飞书账号登录（`--as user`），服务器上可调用；
- 一个飞书账号（Base 免费版够用）；
- 一个 X 账号；长文功能需要 X Premium；
- 浏览器为 Chromium 内核（Chrome / Edge / Arc 等）以装回采扩展。

## 配置一览

所有私密与个性化配置都走环境变量，见 `config.env.example`：飞书 Base / 表 ID、你的 X 账号、显示昵称、站点域名、板页输出目录、lark-cli 路径。代码里没有任何写死的个人值。

## 目录

```
board/       发布板渲染 + 状态服务（含 systemd/ 与 caddy/ 模板）
collect/     回采核心 + 接收端 + FxTwitter 兜底（含 systemd/、cron/、tests/）
local/       无服务器模式：本地网关 + 一键启动脚本（sh / ps1）+ 本地配置示例 + launchd 模板
extension/   回采浏览器扩展（占位符模板 + configure_extension.py 生成脚本）
feishu/      Base 结构定义 schema.json + 建表脚本
docs/        飞书建表、部署、扩展三份手册
scripts/     服务器一键安装脚本 + doctor.py 自检（卡住先跑它，五项逐项给人话结论与诊断码）
```

## 测试

`collect/tests/` 是原系统留下的离线测试：回填窗口边界、粉丝数来源防护、扩展 DOM 解析与纯函数、扩展包校验。部署后建议先跑一遍再改代码。本地已验证通过：`core_window_test.py`、`core_account_guard_test.py`、`content_pure_test.js`；`content_dom_test.js` 需要 `jsdom`，`validate_extension.py` 需要服务器上已打包的扩展 zip 与令牌文件。

## License

MIT，见 `LICENSE`。拿去用、改、给别人都可以；X 与飞书的商标归各自公司。
