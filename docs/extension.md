# 回采扩展：生成与安装

`extension/` 里是模板：X 账号是 `YOUR_X_HANDLE`、域名是 `YOUR_DOMAIN`，**不能直接装**，先生成你自己的版本：

```bash
python3 extension/configure_extension.py --handle your_handle --domain example.com --out /tmp/my-extension
```

参数：`--handle` 不带 @；`--domain` 是你的站点域名（回采上报到 `https://<domain>/collect-api/ingest`，板页在 `https://<domain>/x/`）。脚本会复制整个扩展目录并替换全部占位符，同时把生成结果的目录打印出来。

没有服务器 / 域名、走本地模式的，用 `--base-url` 代替 `--domain`（二选一），填站点的根地址：

```bash
# 纯本地模式
python3 extension/configure_extension.py --handle your_handle --base-url http://127.0.0.1:8799 --out /tmp/my-extension
# 本地 + Tailscale Funnel 模式
python3 extension/configure_extension.py --handle your_handle --base-url https://your-machine.your-tailnet.ts.net --out /tmp/my-extension
```

上报端点拼为 `<base-url>/collect-api/ingest`；脚本会自动把 manifest 的 host 权限校正为不带端口的 `协议://主机/*`（Chrome 的权限模式不允许写端口）。细节见 `docs/no-server.md`。

## 安装（Chrome / Edge / Arc 等 Chromium 内核）

1. 打开 `chrome://extensions`，打开右上角「开发者模式」；
2. 点「加载已解压的扩展程序」，选择上一步生成的目录；
3. 点扩展图标打开 popup，在「采集令牌」里填入服务器 `/opt/xcollect/collect-token.txt` 的内容，点保存。令牌只存本机 `chrome.storage.local`，不会写进扩展文件。

也可以先在服务器上跑 `collect/package_extension.py` 把生成好的扩展打成 zip，放在 `https://你的域名/xcollect/extension.zip` 供自己其他电脑下载（zip 里不含令牌）。

## 使用

- **立即采集**：先在浏览器打开以下任一页面，再点 popup 的「立即采集」：
  - `https://x.com/<你的handle>` 主页帖子列表（用于自动匹配帖子链接）；
  - 任意一条本人帖子的详情页（可见指标）；
  - `https://x.com/i/account_analytics`（认证粉丝数等）；
  - Analytics 单帖活动页（曝光 / 主页访问 / 链接点击等私有数据）。
- **每小时自动**：装好后扩展用 alarms 每小时唤醒一次，在已打开的 X 标签页上触发采集；没有打开的 X 标签页时不会自动开新页。
- 扩展只在确认当前登录账号是你的 handle 时才采集，不是则跳过并在 popup 提示。

## 权限与红线

- `storage`：存令牌与上次采集结果；`alarms`：每小时自动采集；host 只含 x.com / twitter.com 与你的域名。
- 只读：不调用发帖 / 点赞 / 关注等写接口，不读 cookies，不拦截网络请求。这条是设计红线，改扩展时不要破。
- 粉丝总数不由扩展决定：扩展上报的 followers 服务端会忽略，总数只认 FxTwitter 兜底（见 README）。
