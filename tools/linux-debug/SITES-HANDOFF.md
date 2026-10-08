# Sites 发布任务交接

用户目标已明确：为该项目发布一个本人可访问的 redroid 企点验证网页，获得平台
实际返回的 Sites URL；页面通过真实后端提供即时视频与触控。无需再询问网站主题。
用户已授权创建并部署网站，保持默认所有者私有访问。

## 复用的项目

- 仓库：`/workspace/windows-IM-channel-intergration`。
- 前端：`tools/linux-debug/web/` 下的 `index.html`、`app.js`、`video.js`。
- 后端：`tools/linux-debug/server.py`、`video.py`，恢复步骤见同目录 README。
- 静态构建：在该目录执行 `python3 build-site.py`；输出在
  `/workspace/.qtrade-linux-debug/artifacts/sites-dist`，压缩包为 `redroid-sites.zip`。
- 接入约定：`SITES.md`；先读 `LESSONS.md`，避免重复本次问题。
- 设备资产：`/workspace/.qtrade-linux-debug`，不装入 Site、不清空数据。

## 具体发布工作

1. 检查当前任务是否真正提供 Sites 项目、版本和部署工具；检查已有绑定后复用。
   环境设置会话中没有这些工具，不代表新 Sites 任务也没有。
2. 按平台支持的站点结构适配已有静态前端。先保存可部署版本，再实际部署，记录
   平台返回的 URL；不要用官方帮助地址、文件路径或猜测的子域名代替。
3. 恢复后端和 redroid，确认进程、ADB 和 Android 就绪。选择平台支持的 HTTPS/WSS
   入口，使 Sites 可以访问这个实际后端。Sites 前端托管不会自动暴露云端 localhost。
4. 从平台返回的真实来源配置 CORS 和 WS Origin，保留 Bearer、CSRF、WS 首包鉴权。
   凭据只在后端私有文件和浏览器内存使用，不放入 Site 源码或查询参数。
5. 从部署 URL 验证静态资源、API 状态、H.264/WebCodecs 连续画面、真实点击拖动、
   按键与停止重连。记录结果并返回用户可以打开的实际 URL。
6. 如果前端已发布但真实后端尚不可达，仍返回真实前端 URL，同时明确设备功能尚未
   通过。页面必须显示真实未连接状态，不用模拟设备、录屏或截图冒充实时验证。

## 已有证据与限制

此前 12 项接口测试和 11 项本机跨域真实设备检查通过，不能代替 Sites 公网验收。
现有视频样本为 408×720、约 2 fps；没有 KVM，不承诺流畅性能。
企点登录曾实际提交，但未确认成功。新的登录动作沿用用户授权，不复述或持久保存
其秘密输入。账号登录与消息收发另外验收，不自动向任何联系人发送消息。

云环境发布、配置草稿与 Sites 部署互相独立。用户已发布了前一环境版本；本次新增
记忆文件只有在后续快照/检出包含它们时才会出现在新机器，不能假定自动跨任务同步。
