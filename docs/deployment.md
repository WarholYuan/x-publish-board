# 部署手册（服务器）

目标形态：一台 Linux 服务器上跑两个 Python 服务 + 一个静态板页，前面用 Caddy（或 nginx）做 HTTPS 反代。全程只有 Python 标准库，没有 pip 依赖。

## 0. 前置

- Python 3.10+；lark-cli 已装好并以你的飞书账号登录（`lark-cli base +base-get --base-token <你的> --as user` 能通）。两个服务以 root（或能读到 lark-cli 登录态的用户）运行，`HOME` 要指向登录态所在目录。
- 域名已解析到服务器，Caddy 已在跑（nginx 的等价配置照着片段改 location 即可）。
- 飞书 Base 已按 `docs/feishu-base.md` 建好。

## 1. 放代码与配置

```bash
sudo mkdir -p /opt/xboard /opt/xcollect
sudo cp board/render_board.py board/state_server.py /opt/xboard/
sudo cp collect/core.py collect/ingest_server.py collect/fx_poll.py collect/package_extension.py /opt/xcollect/
sudo cp -r extension /opt/xcollect/extension   # 先按 docs/extension.md 生成你自己的版本再拷
cp config.env.example config.env               # 填好你的值
sudo cp config.env /opt/xboard/config.env
sudo cp config.env /opt/xcollect/config.env
sudo chmod 600 /opt/xboard/config.env /opt/xcollect/config.env
sudo mkdir -p /var/www/x-publish-board/x /var/www/x-publish-board/xcollect
```

令牌不用手填：状态服务与接收端首次启动时会在 `/opt/xboard/state-token.txt`、`/opt/xcollect/collect-token.txt` 自动生成（权限 600）。渲染脚本会把状态令牌嵌进板页 JS，接收令牌要填进浏览器扩展的 popup（见扩展手册）。

## 2. systemd

模板在 `board/systemd/` 与 `collect/systemd/`，都用 `EnvironmentFile` 读上面的 config.env。装法：

```bash
sudo cp board/systemd/xboard-state.service board/systemd/xboard-render.service board/systemd/xboard-render.timer /etc/systemd/system/
sudo cp collect/systemd/xcollect.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xboard-state xcollect xboard-render.timer
sudo systemctl status xboard-state xcollect
```

- `xboard-state`：127.0.0.1:8791，板页「发完了 / 撤销」回写；
- `xcollect`：127.0.0.1:8795，接收扩展上报；
- `xboard-render.timer`：每 5 分钟渲染一次板页。

FxTwitter 兜底用 cron（`collect/cron/xcollect-fx.cron`），每小时第 17 分跑：

```bash
sudo cp collect/cron/xcollect-fx.cron /etc/cron.d/xcollect-fx
```

cron 不读 systemd 的 EnvironmentFile，所以该文件里用 `set -a; . /opt/xcollect/config.env; set +a` 先加载配置再跑脚本，模板里已写好。

## 3. Caddy

把 `board/caddy/xboard.caddy` 的片段并进你的站点段（把 `example.com` 换成你的域名）：板页静态目录 `/x/`、状态接口 `/x-api/` → 8791、回采接口 `/collect-api/` → 8795，都带 `no-store` 与 `noindex`。改完：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

## 4. 验收（按顺序走完才算部署成功）

1. `python3 /opt/xboard/render_board.py` 手动渲染一次，无报错；飞书发布库里放的那条 TEST 记录应被写上板上编号、状态变「已上板」。
2. 浏览器打开 `https://你的域名/x/`，能看到 TEST 卡片。
3. 在板上点「发完了」，回飞书看：状态 = 已发布、发布时间 = 当前时间；再点「撤销已发布」，应回到「已上板」、发布时间清空。
4. `curl https://你的域名/collect-api/health`（经反代）或本机 `curl 127.0.0.1:8795/health` 返回 ok。
5. 装好扩展后打开你的 X 主页，点扩展「立即采集」，服务端日志 `/opt/xcollect/ingest.log` 应出现一条上报记录。
6. 手动跑一次 `python3 /opt/xcollect/fx_poll.py --dry-run`，能拉到你已发布帖子的公开数据（前提是飞书里已有带帖子链接的已发布记录）。

补充：仓库带了一个自检脚本 `scripts/doctor.py`，按「授权 → 配置 → 运行环境 → 回采 → 令牌与服务」五项逐项验并生成诊断码，验收卡住或日后板子不动了，先跑它再排查（本地模式同样适用，见 `docs/no-server.md`）。

## 5. 日常运维

- 板页是静态渲染：飞书里改了内容，最多等 5 分钟自动上板；等不及就手动跑一次渲染脚本。渲染前会自动备份旧板页为 `index.html.bak-<时间戳>`。
- 日志：渲染 `/opt/xboard/render.log`；回采 `/opt/xcollect/ingest.log`、`fx_poll.log`。
- 已发布区最多显示最近 30 条；单帖最多取飞书图片前 4 张上板。
- 升级：覆盖 `/opt` 下的 py 文件后 `systemctl restart xboard-state xcollect` 并手动渲染一次即可；config.env 与令牌文件不要覆盖。
