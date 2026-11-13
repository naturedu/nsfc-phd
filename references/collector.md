# 检索计划与元数据整理工具

`scripts/collect.py` 使用 Python 3.10+ 标准库，不访问网络、不读取浏览器Cookie。它生成可复用检索计划，并把agent、授权连接器、浏览器或用户合法导出的元数据整理成可审计结果。不能把运行成功描述为“完成四平台爬取”。

## 生成计划

在技能目录运行：

```bash
python scripts/collect.py queries --year 2025 --year 2026 --output-dir work/search-plan
```

默认覆盖小红书、抖音、微博、微信公众号，组合项目同义词、申请经验/条件/申请书/校内遴选/答辩/获批/未获资助复盘等主题。省略 `--year` 表示不加年份，不表示已覆盖完整历史。可重复使用 `--platform`、`--alias`、`--topic` 选择平台或替换默认关键词，例如：

```bash
python scripts/collect.py queries --platform weibo --platform wechat --alias 国自然博士生项目 --topic 答辩 --output-dir work/focused-plan
```

输出 `query-plan.json` 和可在Excel打开的UTF-8 BOM `query-plan.csv`。所有查询初始状态均为 `planned`。执行后另行记录时间、排序方式、页数/滚动批次、命中数、访问失败及停止原因。搜索引擎可能不严格遵守 `site:` 限制，须核对实际域名和正文。

## 输入记录

接受JSON数组、包含 `posts` 或 `records` 数组的JSON对象、单条JSON对象，以及逐行JSONL/NDJSON；可合并多个文件。每条至少有实际单帖 `url`（兼容 `original_url`、`source_url`）。其他字段可缺失，但标题、读取层级、观察时间及查询来源缺失会记入覆盖报告。

以下是**合成字段示例，不是真实采集结果**：

```json
{
  "platform": "douyin",
  "url": "https://www.douyin.com/video/12345",
  "title": "合成示例：博士生项目答辩",
  "summary": "这里只填写实际读到内容的简短摘要",
  "published_at": null,
  "project_year": null,
  "accessed_at": "2026-01-01T12:00:00+08:00",
  "evidence_level": "search_snippet",
  "relevance": "unreviewed",
  "limitations": "仅搜索摘要；尚未观看，不能提炼为实操经验",
  "queries": ["国自然博士生项目 答辩"],
  "evidence": [{"note": "仅看到搜索摘要，尚未打开原帖"}]
}
```

支持层级与 [social-research.md](social-research.md) 一致：`search_snippet`（搜索摘要）、`page_text`（原页正文/简介）、`image_text`（实际查看并提取图文）、`watched`（实际观看）、`mirror`（镜像标记）、`unknown`（未确认读取深度）。另兼容 `post_fulltext`、`lead_only`、`user_export`，以及 `reading_level`/`access_level` 字段和“平台正文”“搜索摘要”“仅线索”等值。无法识别的层级降为 `unknown`，同时保留原值；不会依据URL自动声称已读正文。

镜像属于来源性质，不能据此判断内容完整性。使用 `evidence_level=mirror` 时，`source_type`标为镜像，`reading_depth`默认为`unknown`；如实际读取了部分内容，可另外提供 `reading_depth=page_text`（也兼容 `reading_level`）。`image_text`和`watched`不表示整组图片、完整视频或全部评论已读取，实际范围写入 `reading_scope`/`limitations`。`user_export`只代表授权导出来源，仍须记录实际完整性。

可选 `author`、`author_name` 只记录公开署名；不添加真实身份推断。`published_at`未知则留空，勿用抓取日期替代。`accessed_at`、`relevance`、`limitations`、`project_year`同时保留在顶层和每次观察的证据中；观察日期也兼容 `collected_at`、`observed_at`。缺少 `topic_status` 时以 `relevance`作为兼容值，但保留原字段名称。`query_id`、`notes`、`metrics`、`access_status`、`reading_scope`、`original_post_url`、`evidence`及`sources`保留在每次观察中。互动数字仅是观察时快照。

相关性标记：`excluded_*`（例如`excluded_comment_only`、`excluded_generic_grant_tools`）及`excluded`表示排除；`relevant`、`confirmed_related`、`directly_related`、`included`表示确认主题相关；只有明确标记`included_experience`才计入已纳入经验数。其他值均为待审。这个标记由实际阅读和判断赋予，工具不会依据关键词或URL自动纳入。

## 整理与输出

```bash
python scripts/collect.py normalize work/social-posts.json work/additions.jsonl --output-dir work/normalized
python -B -m unittest discover -s tests -v
```

输出：

- `posts.json`：每个已识别平台ID一条候选记录，包括已排除候选，保留所有原URL、查询式、读取层级、相关性、限制、单次观察和补充证据。
- `posts.csv`：便于筛选的摘要表，含`relevance`、`limitations`、`review_status`；对危险公式前缀加单引号，JSON中的原文本保持原样。
- `coverage.json`：格式检查通过的观察数、候选数、排除/待审/相关/矛盾/已纳入经验数、平台分布、镜像计数、拒收原因及未测量的覆盖范围。`unique_candidates`（兼容字段`unique_posts`）只是不同候选链接数；`accepted_observations`只表示格式通过。`included_experience_posts`才是明确标记纳入的经验数，不能用候选数替代。

URL规则：小红书以24位笔记ID去重，公共URL移除签名等参数，原URL保留在本地JSON；抖音以作品ID去重；微博将base62状态ID转为十进制MID，可与已确认的新浪镜像ID合并，仍保留镜像身份；公众号长链接按 `__biz`、`mid`、`idx` 识别文章，公共URL保留这些字段及 `sn`，仅清理已知跟踪字段。公众号短文章链接和长链接的对应关系不能离线确认，因此不猜测合并。

短分享链接必须先在授权访问环境中解析到单帖URL。搜索结果页、账号主页、话题/聚合页被拒收，不能靠手填 `post_id` 让聚合页变成帖子。新浪镜像可登记，但未出现原站观察前不能声称已核验微博原帖。若声明的平台与URL不一致，记录会拒收。

顶层读取层级选择观察中较强的一项仅为方便筛选；镜像标记不提升读取强度，也不会删除较弱/矛盾证据。合并时`limitations`、`relevance`、`accessed_at`、`project_year`等不同值保留为列表，各次原值仍保存在证据内；更深的读取记录不能覆盖旧排除原因。同一候选同时出现排除和纳入/相关标记时，`review_status=conflict`，不计入已纳入经验，须另行核查。主题相关性仍由人工或agent复核，链接可识别不等于主题已确定。输入中的补充证据原样保留，因此发布GitHub前须额外检查；原URL可能带临时签名、结果可能含公开署名或用户资料，默认留在本地，不把结果目录整体提交。

覆盖报告中的 `queries_with_accepted_results` 仅表示输入里有结果的查询，**不是已执行查询全集**。零结果、访问失败和搜索终止情况必须用独立检索日志保存。工具未统计平台总量或未导入内容；所有全量标记保持false。
