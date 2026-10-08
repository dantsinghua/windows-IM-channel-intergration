# Sites 与 redroid 真实验证：前后端接入约定

官方 Sites 文档：https://learn.chatgpt.com/docs/sites?surface=app
Sites 入口：https://chatgpt.com/sites

## 架构与当前边界

```text
Sites 私有网页（平台实际返回的 HTTPS 地址）
  ├─ HTTPS /api/debug/*：状态、启动、截图及兜底输入
  └─ WSS /api/debug/stream：H.264 视频 + 原生触控
               ↓ 同一个后端 HTTPS 域名
         TLS 入口 / 反向代理
               ↓ loopback 17622
     FastAPI：Origin + 访问凭据 + 操作鉴权
               ↓ Docker exec / ADB（不暴露到公网）
     本机 QEMU → redroid → scrcpy-server → 企点
```

前端使用静态 HTML / JavaScript，无 CDN、无模拟数据。`web/video.js` 用 WebCodecs
解码 H.264 并绘制 canvas；WebSocket 控制消息转换成 scrcpy 原生触控/按键协议，
无需为每次拖动启动 ADB 命令。截图仅作诊断和无视频时的输入兜底。

Sites 支持 HTTPS/WebSocket，不提供原始 TCP；因此 ADB 与 scrcpy TCP 都留在云端。
Sites 部署和云环境发布是独立步骤。当前会话尚无 Sites 创建/保存/部署工具，
也没有已配置的公网后端 TLS 入口：未取得 Site URL，未验证公网 HTTPS/WSS。
双来源浏览器实测通过不等于 Sites 已部署。

## 前后端共同配置

| 配置 | 来源及使用方 |
| --- | --- |
| Site origin | 由 Sites 部署返回；完整 `https://域名`，填入后端 `QTRADE_SITE_ORIGIN` |
| API origin | 由真实 TLS 入口提供；填入后端 `QTRADE_API_ORIGIN` 和网页“后端 HTTPS 地址” |
| 访问凭据 | 随机 base64url 字符串，至少 32 字符；后端读取 `QTRADE_REMOTE_TOKEN_FILE`（权限 600），操作者在页面输入 |
| 后端端口 | `start-remote.sh` 默认 loopback 17622；必须经 TLS 入口转发 |

三个远程配置必须同时存在，否则拒绝启动。凭据不写入 Site 包、URL、日志或浏览器
持久存储；在页面内存中使用，断开或刷新即清除。当前控制台用于所有者私有验证，
尚未实现多人账号权限体系。首次部署保留 Sites 默认私有访问范围。

前端只接受 HTTPS 根地址，不接受带路径、查询参数或嵌入凭据的 URL。请求禁止跳转，
避免把访问凭据带到其他地址。仅两端都是 loopback 时允许 HTTP，用于本机集成测试。

## CORS、WebSocket 与 HTTPS

- HTTP CORS 只允许配置的准确 Site origin，不使用 `*`。
- 允许 `GET/POST/OPTIONS`；允许 `Authorization`、`Content-Type`、`X-Debug-CSRF`。
- 页面使用 `credentials: omit`，不依赖跨站 Cookie；401/403 等响应也保留准确 CORS 头。
- 状态与截图同样要求访问凭据；POST 还要求状态接口返回的操作令牌。
- WebSocket 不依赖 HTTP CORS 中间件：升级时单独验证 Host、Origin 与子协议。
- WS 接受后 5 秒内必须收到鉴权首包；验证成功之前不启动设备流，不发送视频。
- 前端 CSP 必须允许真实 API 的 HTTPS 和 WSS 地址；上线后检查实际响应头，不能仅验证源文件。
- TLS 入口必须正确转发 WebSocket Upgrade、Host 与协议头；不要把明文 API 或设备端口暴露公网。
- Origin 校验不能代替鉴权。即使非浏览器客户端伪造 Origin，仍需持有正确凭据。

`Caddyfile.example` 提供同机 TLS 反向代理模板，Caddy 原生支持 WebSocket。
它不是已创建的域名或已启动的入口。若入口与云环境不在同一网络，需要平台支持的
网络转发；修改 CORS 无法让公网网站访问云环境的 localhost。

## 接口约定

所有路径都相对 API origin，不相对 Sites 的页面域名。

| 接口 | 行为 |
| --- | --- |
| GET `/api/debug/status` | 返回真实 ADB/Android/企点状态、检查时间及操作令牌 |
| POST `/api/debug/connect` | 连接本机 redroid |
| POST `/api/debug/qidian/launch` | 启动已安装的企点；不自动登录或发送消息 |
| GET `/api/debug/screenshot` | 真实 PNG 截图 |
| POST `/api/debug/tap` | 截图自然像素坐标 `{x,y}`，兜底点击 |
| POST `/api/debug/key/{key}` | `home/back/enter/delete` 白名单 |
| POST `/api/debug/text` | ASCII 输入兜底；不返回输入内容 |
| WS `/api/debug/stream` | 实时画面和原生触控；子协议 `qtrade-scrcpy-v1` |

HTTP 发送 `Authorization: Bearer <内存中的访问凭据>`；POST 增加
`X-Debug-CSRF: <状态接口返回值>`。WS 不把凭据放进 query，而使用首包：

```json
{"type":"auth","token":"<仅内存中的访问凭据>","csrf":"<操作令牌>"}
```

服务端随后发 `{codec:"h264",width,height,profile:"debug15",fps:15,seq0:0}`；
二进制消息沿用仓库基线的 **8 字节大端 PTS（毫秒）+ Annex-B H.264**，IDR 前补发
SPS/PPS。浏览器从 SPS 取得 AVC 配置；丢弃过量解码队列后等待下一个关键帧，避免
无限累积延迟。静止画面可以不产生新帧，连接存活由 WS 心跳和 socket 状态判断。

控制包：

```json
{"type":"touch","action":"down|move|up|cancel","x":100,"y":200}
{"type":"key","key":"home|back|enter|delete|select_all"}
{"type":"text","text":"<ASCII 内容>"}
```

触控坐标使用视频自然尺寸，后端按 scrcpy 协议交给 Android 映射；当前支持单指
点击和拖动。只有勾选“点击画面操作”才注入触控。控制包限制长度和速率；停止、
页面隐藏、断连时关闭流并回收对应的 server 进程和 ADB forward，不清理 Android 数据。
本调试实现是基线协议的有限子集，未实现音频、多指、在线切档或完整生产账号管理。

## 构建与启动

在 `tools/linux-debug` 中运行：

```bash
python install-video.py
python build-site.py
```

产物 `/workspace/.qtrade-linux-debug/artifacts/redroid-sites.zip` 只包含：
`index.html`、`app.js`、`video.js`。不包含账号、后端地址、视频、截图、APK 或虚拟磁盘。
这只是静态部署候选，仍需 Sites 保存版本并部署才能取得实际 URL。

完成真实 origin 和凭据文件配置后运行 `bash start-remote.sh`。
原有 `bash start.sh` 仍默认仅用于本机，端口 17620；不设置远程变量时不开放跨域。
不要同时用两个后台实例控制同一设备的实时流。

scrcpy-server 固定为 3.3.4，SHA-256 与官方 `SHA256SUMS.txt` 核对为：
`8588238c9a5a00aa542906b6ec7e6d5541d9ffb9b5d0f6e1bc0e365e2303079e`。

## 已执行验证与上线验收

```bash
python -m unittest -v test_server.py
python e2e_remote.py
```

已通过 12 项接口测试。`e2e_remote.py` 启动两个不同 loopback origin，以真实 Chromium
浏览器访问实际 redroid，验证错误凭据、CORS 预检与 POST、真实设备状态、WebCodecs
连续解码、主页按键、拖动打开 Launcher3 应用抽屉、企点前台启动、停止/重连/清理、
断开以及真实后台停止后的不可用状态；不用设备替身。

当前视频为 408×720，目标上限 15 fps；本机样本约 2 fps，不能据此承诺流畅的手机
模拟器体验。云主机没有 KVM，QEMU 软件模拟及 Android 软件编码存在性能限制。
功能链路已验证，性能、生产 HTTPS/WSS、Sites 登录访问仍需在真实部署后分别验收。

发布时必须重新从实际 Sites URL 验证上述链路，并验证未授权访问拒绝、页面 CSP、
TLS 信任、WSS Upgrade 和站点私有访问。不要以本机测试或静态预览代替这一步。
企点登录、验证码和业务收发单独报告；账号及密码不写入本文或部署包。

本次授权登录已通过实时网页输入和点击实际提交，应用显示“登录中”后回到登录页，
未确认成功。Android 域名解析失败；宿主/客体直连腾讯 HTTPS 端口被拒绝，
但通过平台 HTTP CONNECT 代理访问企点官网返回 200。需要能承载应用实际登录协议的
网络环境继续验证；不能将上述网络现象归因为密码错误。没有发送业务消息。
