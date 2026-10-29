# 站点画像与 DOM 适配

通用脚本针对 Drupal 风格的活动站做过适配。下面记录各站点的具体差异，便于换站时快速改。

## DOM 判别速查

| 站点类型 | 详情页链接 | 日期字段 | 视频 | 备注 |
|---|---|---|---|---|
| Drupal Views 列表 | `<a href="/events/webinar/<slug>" hreflang="en">` | `<span class="daterange__date">` | `<div class="video-embed-field-provider-vimeo">` 内 iframe | CDISC、BioTech、DiCTS |
| WordPress 活动 | `?p=<id>` 或 `/events/<slug>/` | `time[datetime]` | `<video>` / `iframe` | 很多协会站 |
| 自研 SPA | JSON API | API 返回 | JSON 字段 | 需用 `network` 面板抓 API |

## CDISC（已验证）

- 列表页：`https://www.cdisc.org/events/webinars/public`，**单页含全部，无分页**（实测 73 场）
- 详情页：`https://www.cdisc.org/events/webinar/<slug>`
- 视频：Vimeo iframe，字段容器 `field--name-field-webinar-recording`
- 日期：`<span class="daterange__date">31 August 2023 11:00am - 12:30pm EDT</span>`
  （**注意**：日期在 span 文本里，不在 div 文本里；早期版本按 div 取会拿到空值）
- 演讲人：`<strong>Presenters</strong><br><ul><li>Name</li>...</ul>`
- 附件：`/sites/default/files/pdf/*.pdf`
- ⚠️ **改判（2026-10 实证）**：详情页出现 `webinar-restricted-message`（文案「Members Only Webinar -
  Become a Member to Register」）且**没有** `field-webinar-recording` 字段时，**不等于无公开录像**。
  列表页 `/events/webinars/public` 为每场渲染缩略图 iframe，`player.vimeo.com/video/<id>` **对匿名用户全量可见**。
  旧结论「Members Only → 无公开录像」是错的，照着文案判断会漏掉整场。
  → 判定顺序：先 `has_video`（manifest 字段），再 `video_source`，**永远不要用页面文案判断门禁**。

### CDISC「列表页独有」录像（详情页门禁）审计

```bash
# 1) 从列表页回填缺失的 vimeo_id
cd "C:/Users/Administrator/.workbuddy/skills/CDISC-webinar-video-downloader" &&
python "scripts/harvest_listing_embeds.py" \
  --listing "https://www.cdisc.org/events/webinars/public" \
  --manifest "E:/CDISC/Webinars/index.json" --dry-run

# 2) 给每条打 detail_page_embed / video_source 标
#    判定 = pages/events/webinar/<slug> 缓存里有没有 player.vimeo.com/video/<id>
#    72 条 detail / 1 条 listing（member-webinar-new-sdtmig-v4-0-content-requested-fda，
#    vimeo 1191662752，2026-05-12）—— 截至 2026-10-28 的实测基线
```

已知同类型条目（下次可直接照抄下载方式）：

| slug | 日期 | Vimeo ID | 备注 |
|---|---|---|---|
| `member-webinar-new-sdtmig-v4-0-content-requested-fda` | 2026-05-12 | 1191662752 | 详情页全文只有门禁提示 + "Members Only Files"，无任何 iframe；录像只在列表页 |

下载这类条目**不要**把详情页 URL 交给 `download_webinar.py`（它按页面找 iframe，会报 `无视频`）；
直接把列表页当承载页，用 playlist 定位：

```bash
# 列表页是 74 个 embed 的 playlist，先定位序号再取
python -m yt_dlp --proxy http://127.0.0.1:39411 \
  --downloader ffmpeg -f "bv[height<=720]+ba" --merge-output-format mp4 \
  -o "videos/<Name>_[<vid>].%(ext)s" \
  "https://player.vimeo.com/video/<vid>?autoplay=0#__youtubedl_smuggle=%7B%22referer%22%3A+%22https%3A%2F%2Fwww.cdisc.org%2Fevents%2Fwebinars%2Fpublic%22%7D"
```

> `#__youtubedl_smuggle={"referer":...}` 是绕过 Vimeo `embed-only` 的关键：直接传 player URL 必失败，
> 传列表页 URL 又会被解析成 74 条 playlist。smuggle 片段 = 只取一个视频 + 带正确 referer。

### CDISC 特殊过滤规则

- 列表页混有 `/events/interchange/`（会议）、`/events/webinars/public`（列表页自身）→ 已被
  `extract_links()` 的 `/events?/webinar` 路径规则排除
- slug 形如 `2023-cosa-dataset-json-hackathon-ii` 的**真实** webinar 不能被「年份开头」规则误杀
  → 只排除 `2026` 和 `2026-cosis` 这种裸年份/年份+单词的形式
- 存在名为 `ai-innovation-challenge` 的 hub 页（也有视频 iframe）→ 需精确 slug 黑名单，
  不能用前缀匹配（会误伤 `2025-ai-innovation-challenge`）

## 换站时的适配步骤

1. `curl` 一次列表页存下来，看真实 `<a href>` 形态 → 改 `extract_links()`
2. 打开一个详情页，找日期容器 → 改 `parse_date()` 的输入来源
3. 找视频 iframe → 确认平台；Vimeo 需页面 URL 而非 player URL
4. 跑 `harvest_events.py`，用**已知基线**（人工统计的场次数）对照：
   - 漏抓 = 基线有而 manifest 没有
   - 误抓 = manifest 有而基线没有
   - 两者都应为 0
5. 数字对不上时，逐环节单测：`extract_links()` → `parse_date()` → `detect_videos()` → `is_hub_page()`

## 抓取结果自检建议

每次改完过滤规则，都用基线做双向差集验证。经验：
- **漏抓比误抓更危险** —— 误抓只是多了几行，漏抓会让清单静默不完整
- 日期为空的条目要重点查：往往是 hub 页，也可能是日期选择器没命中
- 视频数为 0 的条目要人工抽查：可能是解析失败，也真的是会员专属
