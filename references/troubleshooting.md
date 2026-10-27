# 排障手册

按「症状 → 原因 → 解法」组织。遇到本文未覆盖的问题，先跑 `probe_network.py` 定位网络层。

---

## 网络层

### 症状：所有视频站点都 403 / 连接超时

**原因**：本机两套代理，环境变量那套是白名单代理，挡掉了大部分视频站。

**解法**：
```bash
python scripts/probe_network.py https://vimeo.com/ https://www.youtube.com/
```
找出可用的 SOCKS5 端口（脚本会读注册表 `ProxyServer` + 扫常见端口）。

### 症状：`curl -x http://127.0.0.1:7890` 返回 000，但 `--socks5` 正常

**原因**：7890 是 SOCKS5 端口，不是 HTTP 代理端口。协议形式写错。

| 写法 | 适用 |
|---|---|
| `curl --socks5 127.0.0.1:7890` | SOCKS5 端口 ✅ |
| `curl -x http://127.0.0.1:7890` | HTTP 代理端口 |
| `curl -x socks5h://127.0.0.1:7890` | SOCKS5 + 远端 DNS |

### 症状：`PermissionError: [WinError 10013]` 绑定端口失败

**原因**：低位端口（<1024）需要管理员权限，或端口被占用。

**解法**：换高位端口，如 39411 / 39412。

### 症状：yt-dlp 警告 `ffmpeg does not support SOCKS proxies` 并报 403

**原因**：ffmpeg 只会说 HTTP 代理，不会说 SOCKS5。它悄悄直连了 → 目标站拒绝。

**解法**：起 `socks_bridge.py`，把 SOCKS5 包装成 HTTP 代理：
```bash
python scripts/socks_bridge.py --listen-port 39411 --socks-port 7890
# 之后
python scripts/download_webinar.py URL --proxy http://127.0.0.1:39411
```

### 症状：改了 `HTTP_PROXY` 但某些站点反而挂了

**原因**：白名单代理是按域名白名单放行的，改全局代理会连带影响别的任务。

**解法**：**不要改全局环境变量**。用命令级 `--proxy` 只给单次下载指定出口。

---

## 下载层

### 症状：`Cannot download embed-only video without embedding URL`

**原因**：Vimeo 的 embed-only 视频禁止直接用 player URL 下载。

**解法**：传**承载 iframe 的页面 URL**：
```
✅ https://site.org/events/webinar/foo
❌ https://player.vimeo.com/video/123456
```
`download_webinar.py` 会自动探测平台并设置 `--referer`（站点根域名）。

### 症状：下载到第 50 个分片被中断，日志有 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`

**原因**：yt-dlp 的 native HLS 下载器产生数百个 `.part-FragN` 文件，
收尾批量删除时触发安全钩子拦截。

**解法**：改用 ffmpeg 下载器，输出单文件：
```bash
--downloader ffmpeg
```
`download_webinar.py` 已默认开启。

### 症状：下载出 0 字节文件但退出码是 0

**原因**：某些编码器（如 edge-tts）会静默产出空文件。视频下载同理。

**解法**：**必须查文件大小**，不能只看 returncode：
```bash
ls -la out.mp4
ffprobe -v error -show_entries format=duration,size -of default=nw=1 out.mp4
```

### 症状：`Requested format is not available`

**原因**：HLS master playlist 里该清晰度不单独存在，只有分片变体。

**解法**：先看可用格式再选：
```bash
python scripts/download_webinar.py URL --list-formats
```
脚本的 `choose_format()` 已做「优先带音轨 → 最高分辨率 → 最高帧率」排序。

### 症状：下载的视频没有声音

**原因**：选了 video-only 的 rendition，HLS 把音频放在独立流。

**解法**：脚本会检测 `acodec == video_only` 并自动追加 audio-only 格式后合流。
也可手动指定：`--fmt "hls-xxx-1080+hls-xxx-audio-high-English"`。

### 症状：下载中途断流 / 反复重试

**原因**：代理链路不稳。

**解法**：
- 降低并发：`--concurrent-fragments 1`（脚本默认）
- 换低清晰度：`--height 720`
- 用 HTTP 桥接而非直连 socks5（桥接有重试与缓冲）

### 症状：ffmpeg 报 `Error during demuxing: I/O error` 但文件看着正常

**原因**：HLS 末尾分片丢失，常见于网络抖动。

**解法**：**以完整解码为准**，别只看文件大小：
```bash
ffmpeg -v error -i out.mp4 -f null -   # 无输出 = 完整
```
`download_webinar.py` 的 `verify()` 已内置这一步。

---

## 抓取层

### 症状：`found 0 detail links`

**原因**：该站列表页的 `<a href>` 形态与预设正则不符。

**解法**：
1. 打开 `pages/` 下的列表页 HTML（或直接 `curl` 一次存下来）
2. 搜 `href=` 看真实路径长什么样
3. 改 `harvest_events.py` 的 `extract_links()`，重跑

### 症状：抓到的场次比人工统计少（漏抓）

**原因**（按实际发生频率排序）：

1. **正则里的 `[^"#?]+` 贪婪** —— 遇到 `<a href="..." hreflang="en">` 这种 href 后还有别的属性时，
   匹配会出问题。改用 `<a\s[^>]*href="([^"]+)"` 逐个解析 `<a>` 标签。
2. **日期容器取错层级** —— Drupal 的 daterange 把日期放在
   `<span class="daterange__date">` 里，取 `</div>` 前的内容会拿到空串。
   空日期会让条目被当成 hub 页过滤掉 → 表现为「漏抓」。
3. **slug 过滤规则过宽** —— `2023-cosa-dataset-json-hackathon-ii` 这类
   「年份+多词」的真实 webinar 会被「年份开头」的 hub 规则误杀。
   规则应收紧为只排除 `2026` 和 `2026-cosis` 这类**裸年份/年份+单词**形式。
4. **前缀黑名单误伤** —— `ai-innovation-challenge` 放进黑名单会连带干掉
   `2025-ai-innovation-challenge`。黑名单必须**精确匹配**完整 slug。

**验证方法**：拿人工统计的基线做双向差集，漏抓和误抓都应为 0。
详见 `site-profiles.md` 的「抓取结果自检建议」。

### 症状：子进程输出抛 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb`

**原因**：Windows 命令行工具（`netstat`、`reg`、ffmpeg）输出的是 GBK/CP936 字节，
用 `text=True` 会强制按 UTF-8 解码而炸掉。炸掉后 `subprocess.run(...).stdout` 是 `None`，
后续 `.splitlines()` 报 `AttributeError: 'NoneType'`，**掩盖了真正的错误**。

**解法**：统一走容错解码（本 skill 的 `run()` / `_run_text()` 已实现）：

```python
def dec(b):
    if b is None: return ""
    if isinstance(b, str): return b
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try: return b.decode(enc)
        except UnicodeDecodeError: continue
    return b.decode("latin-1", "ignore")
```

### 症状：`reg.exe` 被安全策略拦截（Program Blacklist）

**原因**：本机安全策略禁止启动 `reg.exe`（`PROGRAM BLOCKED BY SECURITY POLICY`）。
读注册表时应改用 Python 的 `winreg` 模块，不要 shell out。

```python
import winreg
with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
    val, _ = winreg.QueryValueEx(k, "ProxyServer")
```

### 症状：抓到的日期不对 / 演讲人是空

**原因**：该站的 DOM 结构与 Drupal 风格不同。

**解法**：
- 日期：`harvest_events.py` 的 `parse_date()` 支持 `12 March 2026` 和 `2026-03-12` 两种格式，
  新的就扩展 `MONTHS` 或补一条正则
- 演讲人：`extract_presenters()` 找的是 `<strong>Presenters</strong>` + `<ul><li>`，
  换成该站实际的标签

### 症状：详情页抓取大量报错

**原因**：并发过高被站点限流。

**解法**：`--workers 2` 降低并发；`--pages-dir` 会缓存成功的页面，重跑时不会重复请求。

---

## 表格层

### 症状：Excel 打开后公式单元格显示空白

**原因**：openpyxl 只写公式字符串，**不写缓存值**。Excel 之外的预览器（微信、邮件附件、
大多数网页预览）读不到值就显示空白。

**解法**：跑重算脚本（路径随版本变，用解析脚本取，别硬编码）：
```bash
python "C:/Users/Administrator/.workbuddy/skills/webinar-video-downloader/scripts/resolve_sheet_scripts.py" --shell
# 输出可直接执行的两条命令（recalc + aigc_label，自动选最新版本）
```
判定只看 JSON 的 `status` 与 `total_errors`，**不要看退出码**。
失败时 JSON 返回的是 `error` 键（**没有** `status` 字段）。

### 症状：重算返回 `error` 字段

**原因**：两种可能——LibreOffice 不可用且 `formulas` 引擎缺失；或工作簿误引用了外部工作簿。

**解法**：
- 纯 Python 引擎缺失 → 提示用户手动 `pip install formulas`，或获授权后加 `--allow-install`
- 出现 `external_link_cells` → 公式里混了外部工作簿引用，回 `build.py` 去掉

### 症状：COUNTIF 统计出来的数字和预期不符

**原因**：对含通配符的字符串做 COUNTIF 通配匹配，或中文编码在两个引擎下行为不同。

**解法**：**不要用 `COUNTIF(range,"*关键词*")`**。改为：
1. 在明细表加一列**显式枚举值**（如「SDTM IG v4.0」/「SDTM 相关」/「—」）
2. 汇总表对该列做**精确匹配** `COUNTIF(range,"SDTM IG v4.0")`

`build_index_xlsx.py` 的「主题分类」「是否有录像」「是否已下载」都是这个套路。

### 症状：重算后 AIGC 标识丢失

**原因**：重算会把整个 xlsx 包重新序列化，先打的标会被覆盖。

**解法**：**顺序固定**——先 `recalc.py`，再 `aigc_label.py`。

### 症状：批量跑到某条整批停住（无 ok=N 汇总行）

**原因**：单场失败时 download() 抛 SystemExit，而 SystemExit **不是** Exception 的子类，
只 catch Exception 会让整批中断。

**现状**：脚本已改 catch BaseException，单场失败不再中断整批，末尾照常打印
`ok=N skipped=N failed=N`，失败场次列在 FAILED 清单里。

### 症状：视频没声音

**原因**：HLS 把音频放在独立 rendition，选到的格式是 video-only。

**解法**：脚本会自动挑 audio-only 并用 `video+audio` 组合格式；
也可显式 `--audio-fmt <id>` 指定，或 `--fmt "<video-id>+<audio-id>"`。

### 症状：`Requested format is not available`

**解法**：`--list-formats` 看实际可用档；指定 `--fmt`。

### 症状：`Error during demuxing: I/O error`

**解法**：以**全片解码**为准（`ffmpeg -v error -i out.mp4 -f null -`），不看文件大小；有问题就重下该场。

### 症状：抓到的场次比基线多（多出 hub 页）

**解法**：多为分类页/活动族页混入。补进 harvest_events.py 的 `SKIP_SLUGS`（必须精确匹配完整 slug）；
若是同站其他事件族（如 /events/interchange/），检查路径规则是否放得太宽。

### 症状：演讲人 / 日期为空

**解法**：该站 DOM 不同 → 按 references/site-profiles.md 改提取逻辑。
日期提取改动后必跑 `scripts/test_date_parse.py` 回归。
