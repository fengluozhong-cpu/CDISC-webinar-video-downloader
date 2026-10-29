---
name: CDISC-webinar-video-downloader
description: 专为 CDISC（cdisc.org）webinar 打造的视频批量下载 + Excel 清单流水线：抓取 /events/webinars/public 全量列表、绕过 Vimeo embed-only 与会员门禁假阴性、下载带全片解码校验、产出 4-sheet 带图索引。下载引擎平台无关（Vimeo / YouTube / Wistia / HLS / mp4），--proxy auto 自动适配无代理直连 / HTTP 代理 / SOCKS5 三种网络环境。核心坑：embed-only 必须传承载页、ffmpeg 不支持 SOCKS5、HLS 分片触发批量删除钩子。触发词：CDISC webinar、下载 webinar 视频、抓活动列表生成清单、Vimeo embed 视频下载、批量下载会议录像。
agent_created: true
---

# CDISC Webinar 视频批量下载 + 清单索引

把「抓 CDISC 活动列表 → 定位视频源 → 绕过网络限制下载 → 生成 Excel 清单」做成一条流水线。
为 CDISC（cdisc.org）webinar **开箱即用**；下载引擎平台无关，换站时按
`references/site-profiles.md` 的画像步骤适配。

## 何时使用

「下载 CDISC webinar 视频」「抓 CDISC 活动列表生成清单」「Vimeo 录像报 embed-only 错误」
「批量下载某站点会议录像」——以及视频站被代理挡住（403 / 连不上）时。

命令块首行的 `cd` 已指向 skill 目录，后面用相对路径调用。
`recalc.py` / `aigc_label.py` 在带版本号的插件缓存目录，**不要硬编码** ——
用 `python "scripts/resolve_sheet_scripts.py" --shell` 取路径。## 执行流程

### 第 0 步：探测网络出口（不要跳过）

视频站点常被代理挡住，且**页面域与视频 CDN 域的可达性常常不同**（如 cdisc.org 走
白名单代理可达，而 player.vimeo.com 被同一代理挡掉）。**先探测再动手**，否则会浪费时间在反复重试上。

```bash
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/probe_network.py" https://player.vimeo.com/ https://vimeo.com/
```

脚本自动读注册表 `ProxyServer` 找 SOCKS5 端口、扫常见端口、分别测「环境 HTTP_PROXY」
和「SOCKS5」两条路径，末尾给出 ACTION 建议与 **AUTO 行**（机器可读选路结论；
`--json` 输出含 `recommendation` 字段）。

**三种网络环境**（其他人的机器不一定是 SOCKS5）：

| 环境 | 判别 | 走法 |
|---|---|---|
| 无代理直连 | 无 `HTTP(S)_PROXY` 且裸连 200 | 不传 `--proxy` |
| HTTP 代理 | `HTTPS_PROXY` 存在且探测 200 | ambient 自然生效，或显式 `--proxy http://...` |
| 本地 SOCKS5（VPN / Clash） | ambient 被挡 + SOCKS5 探测 200 | `--proxy socks5://127.0.0.1:<port>` |

**推荐直接 `--proxy auto`**：下载 / 抓取脚本自动探测并选路，且会把**页面内嵌的
视频平台 CDN 域一并纳入探测**（`classify_platform_urls()` 识别 Vimeo / YouTube /
Wistia iframe）——只探页面域会选错协议（实测：cdisc.org 经 ambient 可达但 Vimeo
CDN 被挡 → 正确答案是 socks5，auto 能自己纠正）。全部出口失败时退出并给出诊断。

### 第 1 步：抓列表 → manifest.json

```bash
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/harvest_events.py" \
  --listing "https://www.cdisc.org/events/webinars/public" \
  --base "https://www.cdisc.org" \
  --proxy auto \
  --out manifest.json --pages-dir pages --workers 6
```

> `--proxy auto` 推荐直接带上：目标站直连可达时它等价于不传；
> 被代理挡时自动选路，不会报 `WinError 10061`。换站后若 auto 判断不符预期，
> 再按第 2 步手动指定。

产出 `manifest.json`（`events[]` 数组，每项含 `slug/title/date/videos/presenters/attachments`）
与 `pages/`（详情页 HTML 缓存，可重复利用）。视频识别**平台无关**：
Vimeo / YouTube / Wistia / 原生 `<video>` / `.m3u8` / `.mp4` 都能抓到。

> 🔴 **CHECKPOINT 1 · 改代码前**
> 抓到结果后先核对数量与基线。**若 `found 0 detail links` 或数量明显偏少，
> 必须先把「改 `extract_links()` 正则」告诉用户并等确认**，再改脚本。
> 改完用**双向差集**验收（漏抓=基线有而 manifest 没有；误抓=manifest 有而基线没有），
> 两者都应为 0。详见 `references/site-profiles.md`。
>
> 漏抓比误抓危险 —— 误抓只多几行，漏抓会让清单静默不完整。

#### 详情页有门禁 / 无 iframe → 回列表页找嵌入

有些站的**详情页是访问受限的**（Drupal 的 `webinar-restricted-message`、登录墙、会员门禁），
iframe 根本不渲染；但**列表页**为了做缩略图，会把 74 个录像 iframe 全量内嵌给匿名用户。
此时 `harvest_events.py` 只能给出 `has_video: false`，纯属假阴性。

```bash
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/harvest_listing_embeds.py" \
  --listing "https://www.cdisc.org/events/webinars/public" \
  --manifest manifest.json --proxy auto --dry-run   # 确认命中数后再去掉 --dry-run
```

它按**文档顺序**把 `player.vimeo.com/video/<id>` 与紧随其后的详情链接配对（卡片内 iframe→标题），
给缺视频的条目补 `vimeo_id / video_url / has_video / video_source=listing`，并写 `.bak` 备份。
**判定门禁是否真存在，看 `has_video`，不要看页面文案** —— 文案常对所有人显示。

### 第 2 步：按协议选路（别把协议当限制）

**首选 `--proxy auto`**（第 0 步的探测逻辑内置于所有下载 / 抓取脚本）。
手动选路时遵守协议表 —— **yt-dlp 会说 SOCKS5，ffmpeg 只会说 HTTP**，
谁去抓流，取决于你传什么协议：

| 探测结果 | `--proxy` | 下载器 | 分片文件 |
|---|---|---|---|
| **不确定网络环境** | **`auto`（推荐）** | 自动 | 自动 |
| ambient 200（直连 / HTTP 代理可达） | 不传 | ffmpeg | 无，单文件 |
| ambient 403/0，SOCKS5 200 | `socks5://127.0.0.1:7890` | native | 有 `.part-FragN`（脚本自动清） |
| 需 HTTP 代理口 | `http://127.0.0.1:39411` | ffmpeg | 无，单文件 |
| 全部失败 | —— | —— | 停下来查网络，不要硬下 |

传 SOCKS5 时**不需要**起桥接。但**如果 ffmpeg 合流那步要联网**（如 `--download-sections`），
它拿到的仍是 socks5 → 403，这时**必须**给合流单独一个 HTTP 口（`--mux-proxy`），
桥接也要起好：

```bash
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/socks_bridge.py" --listen-port 39411 --socks-port 7890
```

- 端口用**高位**（≥1024），低位在 Windows 上 PermissionError；该脚本**会阻塞**，用后台任务跑。
- 验证：`curl -x http://127.0.0.1:39411 -o /dev/null -w "%{http_code}" https://vimeo.com/`
- 桥接起好后，**回到第 1 步给 `harvest_events.py` 补上 `--proxy` 重跑**（它同样支持 socks5 与 http 两种）。

### 第 3 步：下载

**单个视频**（也写 `--report`，多次运行会**合并**进同一份 report）：

```bash
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/download_webinar.py" "https://www.cdisc.org/events/webinar/foo" \
  --proxy auto --out videos --report verify_report.json
```

**批量**：

```bash
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/download_webinar.py" --manifest manifest.json \
  --proxy auto --out videos \
  --report verify_report.json --skip-existing --height 720
```

批量模式每场下载到 `videos/<slug>/` 独立子目录，单场失败不影响其余，末尾打印 `ok=N skipped=N failed=N`。

- `--report` 落盘校验结论，第 4 步读它：**校验失败的文件不算「已下载」**；report 缺失时第 4 步**报错退出**。
- `--skip-existing` 跳过前**重新校验**，坏文件移到 `videos/_failed/` 并重下；跳过的文件也写进 report。
- `--height 720` 是**偏好不是硬过滤** —— 无该档位时自动回退最佳可用档。

脚本自动处理：embed-only 页面识别、referer 设置、格式选择、音视频合流、**全片解码校验**。

> 🔴 **CHECKPOINT 2 · 批量下载前**
> 场次多时（如 70+ 场、每场 500MB ≈ 35GB），**先把待下载清单与预计体积报给用户确认**，
> 或先 `--limit 2` 试跑一场验证链路，确认无误再放开跑。
> 默认选最高分辨率 + 逐个全片解码校验，**大清单务必先降档**（`--height 720`）。
### 第 4 步：生成 Excel 清单

```bash
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/build_index_xlsx.py" \
  --manifest manifest.json --out index.xlsx \
  --title "XX Webinar 视频清单" --keyword "SDTM" --videos-dir videos \
  --report verify_report.json
```

产出 4 个 sheet：**汇总**（KPI + 年度/主题分布 + 柱状图）、**专题**（按 keyword 过滤）、
**下载说明**、**明细**（全字段 + 可点链接 + 冻结表头 + 筛选）。统计列是**活公式**。
传 `--report` 时校验失败的文件不计入「已下载」。

**必须按顺序做后两步**（顺序反了会被覆盖）：

```bash
# ① 公式重算 —— openpyxl 写的公式无缓存值，外部预览器显示空白
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/resolve_sheet_scripts.py" --shell
#   → 用输出的 recalc 命令，再跑 aigc_label 命令（② 必须在重算之后，否则标识被抹）
```

判定标准只看 JSON：`status=success` 且 `total_errors=0`。**不要看退出码** —— `errors_found` 的退出码也是 0。

**交付前自检**：视频 > 100KB 且 `ffprobe` 读得出时长 · 全片解码零错误
（`ffmpeg -v error -i out.mp4 -f null -`）· Excel `total_errors=0` · 已写 AIGC 标识 ·
`present_files` 交付视频 + 表格。

---

## 进阶场景：YouTube（平台级门槛，非脚本缺陷）

下载引擎平台无关（yt-dlp 封装），但 YouTube 有自己的反爬门槛。实测（2026-10）：

| 门槛 | 现象 | 解法 |
|---|---|---|
| bot 检测 | `Sign in to confirm you're not a bot`（默认 / web_embedded / tv_embedded 三种 client 均拦） | 带 cookie：`--cookies-from-browser chrome` 或 `edge`（**浏览器必须关闭**，否则 cookie 库被锁，报 `Could not copy Chrome cookie database`） |
| JS runtime | `No supported JavaScript runtime could be found`，缺省会少格式 | 装 deno（yt-dlp 默认仅启用 deno） |
| 网络出口 | 无代理环境直连不通 | 走第 0 步探测；`--proxy auto` 会把 `https://www.youtube.com/` 一并纳入探测 |

对比：CDISC / Vimeo 场景**开箱即用**，YouTube 需上述两步额外配置 —— 这不是脚本
bug，是 YouTube 对数据中心 IP / 无 cookie 请求的策略。带 cookie 下载时同样遵守第 2 步协议选路。

---

## 🚫 不要做的动作

| # | 不要 | 后果 | 改用 |
|---|---|---|---|
| 1 | 改全局 `HTTP_PROXY` | 白名单代理按域名放行，改全局会搞坏别的任务 | 命令级 `--proxy` |
| 2 | 直传 `player.vimeo.com/video/<id>` | `Cannot download embed-only video` | 承载 iframe 的**页面 URL** |
| 3 | 让 **ffmpeg** 去连 SOCKS5 | ffmpeg 静默忽略 socks5 → 直连 → 403 | 按第 2 步协议表选路；curl 用 `--socks5` |
| 4 | 用 native HLS 下载器却不管分片 | 数百 `.part-FragN` 触发安全钩子 | 脚本自动清理；或 `--downloader ffmpeg` |
| 5 | 用 `COUNTIF(range,"*关键词*")` 通配符 | 跨引擎不一致，与专题 sheet 口径不符 | 明细加**显式枚举列**做精确 `COUNTIF` |
| 6 | 用前缀匹配做 slug 黑名单 | 误伤 `2025-ai-innovation-challenge` | 精确匹配**完整 slug** |
| 7 | 先打 AIGC 标再 recalc | 重算重新序列化，标识被抹 | 顺序固定：recalc → aigc_label |
| 8 | shell 调 `reg.exe` 读注册表 | 本机安全策略拦截 `reg.exe` | Python `winreg`（脚本已内置） |
| 9 | 只看退出码判断重算 | `errors_found` 退出码也是 0，会误判通过 | 只看 JSON `status` + `total_errors` |
| 10 | 改 `extract_links()` 不做双向差集 | 静默漏抓或误抓 | 人工基线差集验收，两边都应为 0 |
| 11 | 批量下载不降档 | 70 场 1080p ≈ 35GB + 逐个解码极慢 | `--height 720` + `--limit 2` 试跑 |
| 12 | 只按「文件存在」判断已下载 | 坏文件会被算成已下载并交付 | 必须用 `--report` 贯通校验结论 |

## 🩺 失败模式：症状 → 一线修复 → 兜底

高频 6 条（完整见 `references/troubleshooting.md`）：

| 症状 | 一线修复 | 仍失败时 |
|---|---|---|
| `found 0 detail links` | 看真实 `<a href>` 形态改 `extract_links()` | SPA/JSON 接口站 → 改用 API |
| 场次比基线少 | 查漏抓四因：a href 正则贪婪 / 日期层级 / slug 过宽 / 黑名单前缀误伤 | 加分页 |
| yt-dlp 报 403 | 按第 2 步协议表选路 | 查 `probe_network.py` 的 ACTION 行 |
| yt-dlp 报 embed-only | 换成承载 iframe 的页面 URL | 无公开页 → 记为「无公开录像」 |
| 详情页无 iframe / 显示「会员专属」 | **视频多半内嵌在列表页**，跑 `harvest_listing_embeds.py` 回填 | 仍无 → 才是真无公开录像 |
| 0 字节但退出码 0 | **查文件大小**，不只看 returncode | 降档重试 |
| yt-dlp 报 `Sign in to confirm you're not a bot` | YouTube bot 检测：**关闭浏览器**后 `--cookies-from-browser`，并装 deno | 见「进阶场景：YouTube」 |
| recalc 返回 `error` 键 | 装 `formulas` 引擎 | 提示用户装 LibreOffice |

## 脚本清单

`scripts/` 下 8 个：`probe_network.py`（探测出口 + `--proxy auto` 选路引擎：
`resolve_auto_proxy()` / `classify_platform_urls()`，`--json` 含 `recommendation` 字段）·
`harvest_events.py`（抓列表→manifest）·
`harvest_listing_embeds.py`（列表页嵌入回填，救详情页门禁假阴性）·
`socks_bridge.py`（SOCKS5→HTTP 桥）· `download_webinar.py`（下载/批量）·
`build_index_xlsx.py`（Excel）· `resolve_sheet_scripts.py`（解析 recalc/aigc）·
`report_store.py`（report 合并的唯一实现）。

回归测试在 `tests/`（改脚本后必跑）：`test_date_parse.py` · `test_verify_report.py` ·
`test_batch_isolation.py` · `test_report_store.py` · `test_cross_key_retry.py` ·
`test_proxy_transport.py` · `test_harvest_socks5.py` · `test_judge_*.py`。

排障见 `references/troubleshooting.md`；换站见 `references/site-profiles.md`。
