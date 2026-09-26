# QTrade 控制台(`console/`)

Windows 上的 Electron 桌面程序,规格唯一出处 = `docs/01-控制台前端设计.md`(元素 ID 见 §4,
文案对照见 §2.10),端点与机器码以 `docs/02` 为准,基线口径以 `docs/00` 为准。

## 跑起来

⚠️ `/mnt/c` 上 `npm install` 很慢且符号链接易坏。**源码以本目录为准**,但请 rsync 到 WSL ext4 再装依赖与构建:

```bash
mkdir -p ~/work/qtrade-build/console ~/work/qtrade-build/docs
rsync -a --exclude node_modules --exclude dist --exclude release \
      /mnt/c/Users/anlin/Desktop/work/console/ ~/work/qtrade-build/console/
rsync -a /mnt/c/Users/anlin/Desktop/work/docs/*.md ~/work/qtrade-build/docs/   # 单测要读文档对账

cd ~/work/qtrade-build/console
export https_proxy=http://172.19.176.1:7890 http_proxy=http://172.19.176.1:7890
export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
npm install
```

| 命令 | 做什么 |
|---|---|
| `npm run dev:web` | mock 假后端(127.0.0.1:17600)+ vite dev server(:5273),**浏览器里就能跑完整界面** |
| `npm run dev` | 上面两个 + 拉起 Electron |
| `npm run typecheck` | `vue-tsc --noEmit` |
| `npm run lint` | eslint 9 flat config |
| `npm test` | vitest 单测(含「文档 ↔ 源码」对账) |
| `npm run build` | renderer(`dist/renderer`)+ main/preload(`dist/electron`,CJS) |
| `npm run build:dir` | `electron-builder --dir` → `release/win-unpacked/`(**按 03:只出目录,不出 NSIS**) |

非 Windows 上开发时,主进程取不到命名管道令牌,用 `QT_DEV_TOKEN=xxx` 走 mock 令牌。

### 浏览器调试

```bash
QT_DEV_TOKEN=<管理员令牌> npm run dev      # 然后浏览器打开 http://127.0.0.1:5273
```

- `QT_DEV_TOKEN` 由 `vite.config.ts` 的 dev server 代理层给 `/api/v1`(含画面流 WS)补 `Authorization: Bearer …`。
  **仅本机浏览器调试用,构建产物不含这一层**;正式运行时令牌只由 Electron 主进程注入,渲染进程不持有。
- ⚠️ `npm run dev` / `dev:web` 会同时起 mock(占 `127.0.0.1:17600`)。对着**真 Agent**(已占 17600)调试时,
  mock 起不来会连带 `concurrently -k` 把 vite 一起停掉 —— 这时只起 vite:`QT_DEV_TOKEN=<管理员令牌> npx vite`。
- 管理员令牌等同最高权限,别写进脚本、别提交、别贴进聊天。

## 目录

| 路径 | 说明 |
|---|---|
| `electron/main/` | 单实例锁、托盘、自启、崩溃恢复、窗口;令牌交接(命名管道)、`webRequest` 注入与阻断、文件对话框 |
| `electron/preload/` | `contextBridge` 暴露 `window.qt` 白名单(§2.2);不暴露 `ipcRenderer` 本体 |
| `src/api/` | `http.ts`(统一信封 / 401 重放 / 429 退避 / 507 `DISK_FULL`)、`ws.ts`(`seq` 去重 + `since_seq` 续传)、`client.ts`(端点清单) |
| `src/stores/` | accounts / resources / messages / mail / env / settings / commands / workflows / audit / events / jobs / session / setup / ui |
| `src/pages/` | 一页一目录,对应 00 §9 的页面 ID |
| `src/testids.ts` | **01 §4 全表的唯一出处**;页面不手写 `qt-*` 字符串 |
| `src/i18n/zh-CN/codes.ts` | 结果码 / `state_code` / 告警码 / 邮件状态 / 探测结论 / 来源 的中文单一来源 |
| `src/codec/stream.ts` | 企点画面流:WebCodecs 解码 + 三档降级(硬解 → 软解 → 静态预览,自动降不自动升) |
| `mock/` | 开发用假 Agent(不是实现),按 02 的信封回数据、按 00 §7.5 推事件 |

## 测试在验什么

- `api-client.spec.ts` —— 信封解包、HTTP→结果码映射、`X-Trace-Id`/`X-QT-Api-Min` 头、幂等键、401 重取重放、429 `Retry-After` 退避、**507 单独识别为 `DISK_FULL`**
- `ws-client.spec.ts` —— 首帧订阅、`seq` 去重与乱序不丢、重连 `since_seq`、`replay:"truncated"` 才全量拉、20s ping / 40s 判死、退避 1→2→4→…→30s
- `stores.spec.ts` —— `account_state` 归约、`error_since_ms` 算「已故障 N 分钟」、`login_required` 的可用操作集、`late`/`origin` 三字段只挂事件行且重拉即消失、槽位**非空串**判据、告警 `(code,subject)` 去重、二维码不进环形缓冲
- `codes-coverage.spec.ts` —— 从 `docs/02 §3.7` 解析告警码全表,断言 `codes.ts` **逐条登记且 severity 一致、不多不少**;再对 00 §8.3 结果码、01 §2.10 `state_code`、06 §2.3.5 邮件状态、00 §8.5 探测结论、02 §3.10 danger 十项逐一对账
- `screen-stream.spec.ts` —— Annex-B 拆分(3/4 字节起始码、防竞争字节)/ avcC / 长度前缀;降档链硬解 → 软解(强制 thumb)→ 静态预览、不自动升;`decodeQueueSize > 6` 丢非关键帧到下一个关键帧;SPS 变化重新 configure;开流前隐藏补发 pause
- `pointer-relay.spec.ts` —— 画布指针 → `touch{action,x,y,pointer}` 序列:点击、长按(按住期间零帧)、滑动 ≤ 60 Hz 且终点不丢、多点、cancel、黑边、首帧前不发;滚轮 → `scroll`;静态预览的 #35 tap/swipe 换算
- `screen-page.spec.ts` —— 画面页:任何进页路径都注册可见性监听(隐藏 pause / 恢复 resume,浏览器退回 `visibilitychange`);无 WebCodecs 真去轮询 #33;可打印字符攒进输入框回车整段发 `text`;无「旋转」
- `testids-coverage.spec.ts` —— 从 `docs/01 §4` 解析 **475 条元素条目**,断言源码里都出现;并断言作废 id(C-45/R-11/R-12)不再出现

改文档后这两个 coverage 测试会立刻变红,这是刻意的:**它们是文档与代码的对账闸**。
