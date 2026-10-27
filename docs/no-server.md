# 没有服务器 / 没有域名怎么办

先说清这套系统到底「需要」什么。服务器和域名只是手段，真正缺一不可的是三件事：

1. **一台在用的机器**跑渲染脚本和两个小服务，并存着 lark-cli 的飞书登录态；
2. **一个稳定的访问地址**：发布板要能打开，浏览器扩展要往一个固定地址上报数据（生成扩展时地址就写进去了，地址一变就得重装扩展）；
3. **定时跑得起来**：每 5 分钟渲染、每小时 FxTwitter 兜底，不然 1h / 24h / 72h 三个回采窗口会错过。

所以没买服务器、没买域名完全能跑，只是把这三件事换个出处。两档方案，功能一套不少，差别只在「手机能不能打开」和「电脑关了还转不转」：

| 方案 | 地址 | 花钱 | 手机打开板 | 电脑关机后 |
|---|---|---|---|---|
| A. 纯本地 | `http://127.0.0.1:8799/x/` | 0 | ✗ 只能本机 | 全停 |
| B. 本地 + Tailscale Funnel | `https://<机器名>.<你的tailnet>.ts.net/x/` | 0 | ✓ | 全停 |
| （对照）服务器版 | 你自己的域名 | VPS + 域名钱 | ✓ | 照常 |

原理：`local/gateway.py` 是一个零依赖的本地网关，把服务器版里 Caddy 干的活（发静态板页 + 把 `/x-api/`、`/collect-api/` 反代给两个 Python 服务）搬到你自己的电脑上，只绑本机 8799 端口。方案 B 再在网关外面套一层 Tailscale Funnel：Funnel 所有套餐都可用（免费 Personal 就行，仍是 beta），会给你一个**稳定的** `*.ts.net` https 地址、证书自动签，正好补上「域名 + HTTPS + 公网可达」这一件。

## 方案 A：纯本地模式

适合先试跑、或者只在电脑前发帖的人。

1. 飞书 Base 照旧建（`docs/feishu-base.md`），这一步和服务器版没有区别。
2. lark-cli 装在**你自己的电脑上**并登录你的飞书账号（服务器版是装在服务器上，本地版装本机，仅此差别）。
3. 复制本地配置并填值：
   ```bash
   cp local/config.local.example config.env
   ```
   飞书、X 账号两节照服务器版填；路径都落在仓库的 `data/` 目录，不用改。
4. 启动（三件套 + 首次渲染一条命令）：
   ```bash
   # macOS / Linux
   bash local/start_local.sh
   ```
   ```powershell
   # Windows（PowerShell 里）
   powershell -ExecutionPolicy Bypass -File local\start_local.ps1
   ```
5. 浏览器打开 `http://127.0.0.1:8799/x/`，能看到测试帖卡片就是通了。
6. 生成回采扩展（注意用 `--base-url`，不是 `--domain`）：
   ```bash
   python3 extension/configure_extension.py --handle your_handle --base-url http://127.0.0.1:8799 --out /tmp/my-extension
   ```
   安装方法同 `docs/extension.md`；popup 里填的回采令牌在 `data/collect/collect-token.txt`（服务首次启动自动生成）。

## 方案 B：本地 + Tailscale Funnel（推荐的零成本全功能版）

先按方案 A 在本机跑通，然后：

1. 在这台电脑上装 [Tailscale](https://tailscale.com/download) 并登录（免费账号即可）。
2. 开 Funnel（只需一次）：
   ```bash
   tailscale funnel --bg 8799
   ```
   它会打印出你的地址，形如 `https://your-machine.your-tailnet.ts.net`。第一次跑会引导你在 Tailscale 管理页给 tailnet 开 Funnel 与 HTTPS 证书，照着点即可。
3. 把 `config.env` 里站点两行换成这个地址（`local/config.local.example` 里有现成的注释段）：
   ```
   SITE_DOMAIN=your-machine.your-tailnet.ts.net
   SITE_URL=https://your-machine.your-tailnet.ts.net/x/
   ```
   重启 start_local 脚本让配置生效。
4. 重新生成扩展（地址变了必须重装，这是一次性的）：
   ```bash
   python3 extension/configure_extension.py --handle your_handle --base-url https://your-machine.your-tailnet.ts.net --out /tmp/my-extension
   ```
5. 手机浏览器打开 `https://your-machine.your-tailnet.ts.net/x/`，能看到板子即成。

注意事项：

- **电脑就是服务器**：关机、休眠、断网，板子和回采就一起停。长期用请关掉系统休眠（至少接电源时不休眠），并把三件套设为开机自启（见下节）。
- **这条 https 地址不要公开分享**：板页里嵌着状态令牌、两个接口靠令牌鉴权，暴露面和服务器版相同——当自家地址用，别发到公开帖子里。
- Funnel 只开放 443 等少数端口、带宽有限制：这套系统只有静态页和几个小接口，完全够用。
- 国内网络下 Tailscale 的中继速度因人而异；只是打不开板时慢一点，不影响扩展在本机回采（扩展走的也是这个地址，若 Funnel 不稳，可让扩展继续用方案 A 的 `127.0.0.1` 地址，板子走 Funnel——两边生成两个扩展目录、装哪个用哪个即可，令牌是同一个）。

## 定时与常驻（两方案共用）

start_local 脚本是前台手动版。想让它像服务器版一样自动转，再补三样：

**macOS（launchd，模板在 `local/launchd/`）**

```bash
# 三个 plist 先把 __REPO__ 换成仓库绝对路径，再装载
for f in local/launchd/*.plist; do sed -i '' "s|__REPO__|$PWD|g" "$f"; done
cp local/launchd/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.xpublishboard.local.plist    # 三件套常驻（开机自启）
launchctl load ~/Library/LaunchAgents/com.xpublishboard.render.plist   # 每 5 分钟渲染
launchctl load ~/Library/LaunchAgents/com.xpublishboard.fx.plist       # 每小时第 17 分兜底
```

**Windows（任务计划）**

三件套常驻：把 start_local.ps1 的快捷方式放进启动文件夹（`shell:startup`），或建一个「登录时」触发的任务。定时两条：

```bat
schtasks /Create /TN "XBoard Render" /SC MINUTE /MO 5 /TR "powershell -ExecutionPolicy Bypass -File \"C:\你的路径\x-publish-board\local\run_once.ps1\" -Task render" /F
schtasks /Create /TN "XBoard Fx" /SC HOURLY /MO 1 /ST 00:17 /TR "powershell -ExecutionPolicy Bypass -File \"C:\你的路径\x-publish-board\local\run_once.ps1\" -Task fx" /F
```

**Linux 桌面机（cron）**

```cron
*/5 * * * * set -a; . /你的路径/x-publish-board/config.env; set +a; /usr/bin/python3 /你的路径/x-publish-board/board/render_board.py >> /你的路径/x-publish-board/data/logs/render.log 2>&1
17 * * * * set -a; . /你的路径/x-publish-board/config.env; set +a; /usr/bin/python3 /你的路径/x-publish-board/collect/fx_poll.py >> /你的路径/x-publish-board/data/logs/fx_poll.log 2>&1
```

三件套本身用 `bash local/start_local.sh` 手动开，或照 systemd 模板改成 `--user` 服务。

## 卡住时先跑自检（doctor）

本地版和服务器版通用。任何一步不对劲，先别猜，跑：

```bash
python3 scripts/doctor.py
```

它按这套系统真实出过事的顺序查五项——飞书授权（是否过期）、配置值（是否抄错/格式坑）、运行环境（干净环境下实读一次飞书，专治「手动通、定时不通」）、回采链路（FxTwitter 是否可达、兜底日志与最近一轮匹配结果）、令牌与服务（服务是否活着、鉴权是否在工作）。全程只读，不改任何数据。每项给一句人话结论和修法，最后生成一行**诊断码**（形如 `XPB-DOC1 1:OK 2:FAIL ...`）和一份 `doctor-report.txt`——把这两样发给帮你的人，对方不用远程连你的机器就知道坏在哪。

## 验收（本地版）

按这个顺序走完才算通：

1. `http://127.0.0.1:8799/x/`（方案 B 再验一遍 https 地址）能打开，飞书里的 TEST 记录出现在板上、且被写上板上编号；
2. 在板上点「发完了」，飞书里状态变「已发布」、发布时间是当前时间；再点「撤销已发布」能退回；
3. `curl http://127.0.0.1:8799/collect-api/health` 返回 `{"ok": true, ...}`；
4. 装好扩展后打开你的 X 主页，点扩展「立即采集」，`data/logs/ingest.log` 出现一条上报记录；
5. 手动跑一次 `python3 collect/fx_poll.py --dry-run`（先加载 config.env），能拉到已发布帖子的公开数据。

## 什么时候升级到服务器版

出现下面任意一条，就说明该花那几十块钱了：电脑没法常开、1h 回采窗口总丢、想要板子在任何网络下都秒开。升级不需要改代码也不用重建飞书：同一份 config.env（把路径与站点换回服务器版的值）、同一套验收，按 `docs/deployment.md` 搬到 VPS 上即可，扩展按新域名重新生成一次。

## 为什么不用其他免费隧道凑合

- **Cloudflare Quick Tunnel（trycloudflare）**：地址每次启动随机变，扩展里的地址写死，等于每次开机都要重装扩展——只适合临时演示。它的正式版 Tunnel 又要求域名在 Cloudflare 托管，没域名时用不了。
- **ngrok 免费版**：地址是稳定的，但免费版会给网页流量插一层提示页，板页体验和扩展上报都容易出岔子，不推荐当长期方案。
