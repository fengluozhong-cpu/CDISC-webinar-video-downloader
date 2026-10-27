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
- Members Only 页面**没有** `field-webinar-recording` 字段 → 判定为「无公开录像」

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
