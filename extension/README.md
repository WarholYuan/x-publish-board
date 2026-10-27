# X 数据回采扩展（@YOUR_X_HANDLE）

只读采集本人 X 账号的公开页面数据与 Analytics 页面数据，上报到
`https://YOUR_DOMAIN/collect-api/ingest`，由服务端回写飞书发布库。

## 安装（Chrome / Edge 等 Chromium 内核）

1. 下载 `https://YOUR_DOMAIN/xcollect/extension.zip` 并解压。
2. 打开 `chrome://extensions`（Edge 为 `edge://extensions`）。
3. 打开右上角「开发者模式」。
4. 点「加载已解压的扩展程序」，选择解压后的 `extension` 目录。
5. 点扩展图标打开 popup，在「采集令牌」里填入部署者提供的令牌，点保存。
   令牌只存本机 `chrome.storage.local`，不会写进扩展文件，也不会发往第三方。

## 使用

- **立即采集**：先在浏览器里打开以下任意一种页面，再点 popup 的「立即采集」：
  - `https://x.com/YOUR_X_HANDLE`（主页帖子列表，用于自动匹配帖子链接）
  - 任意一条本人帖子的详情页（可见指标）
  - `https://x.com/i/account_analytics`（认证粉丝数等）
  - Analytics 单帖活动页（曝光/互动/主页访问/链接点击等）
- **每小时自动**：装好后扩展用 `alarms` 每小时唤醒一次，在已打开的 X 标签页上
  触发采集。没有打开的 X 标签页时不会自动开新页。
- 扩展只在确认当前登录账号是 `@YOUR_X_HANDLE` 时采集；不是则跳过并在 popup 提示。
- 只读：不调用发帖/点赞/关注等写接口，不读取 cookies，不拦截网络请求。

## 权限说明

- `storage`：保存令牌与上次采集结果。
- `alarms`：每小时自动采集。
- host：`x.com` / `twitter.com`（页面内容）与 `YOUR_DOMAIN`（上报端点）。
