#!/usr/bin/env python3
"""Build search plans and normalize authorized post metadata; no network access."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PLATFORMS = ("xiaohongshu", "douyin", "weibo", "wechat")
ALIASES = ("国自然博士生项目", "国家自然科学基金博士生项目", "青年学生基础研究项目 博士研究生", "国自然 博士生基金")
TOPICS = ("申请经验", "申请条件", "申请书", "校内遴选", "答辩", "获批", "未获资助 复盘")
SCOPES = {
    "xiaohongshu": "site:xiaohongshu.com",
    "douyin": "site:douyin.com",
    "weibo": "(site:weibo.com OR site:weibo.cn OR site:m.weibo.cn)",
    "wechat": "site:mp.weixin.qq.com",
}
PLATFORM_ALIASES = {"小红书": "xiaohongshu", "抖音": "douyin", "微博": "weibo", "微信公众号": "wechat", "公众号": "wechat", "微信": "wechat"}
LEVEL_ALIASES = {
    "平台正文": "post_fulltext", "全文": "post_fulltext", "搜索摘要": "search_snippet",
    "仅线索": "lead_only", "用户导出": "user_export", "未知": "unknown",
    "fulltext": "post_fulltext", "full_text": "post_fulltext", "snippet": "search_snippet",
}
# Mirror describes provenance, not reading depth. Watched/image_text may be partial.
LEVEL_RANK = {"unknown": 0, "mirror": 0, "lead_only": 1, "search_snippet": 2,
              "page_text": 3, "image_text": 3, "watched": 3,
              "post_fulltext": 4, "user_export": 4}
TRACKING_KEYS = {
    "xsec_token", "xsec_source", "xsec_encode", "xhsshare", "app_platform", "app_version",
    "share_from_user_hidden", "share_id", "shareid", "timestamp", "share_source", "share_medium",
    "share_platform", "share_token", "sec_user_id", "sec_uid", "enter_from", "enter_method",
    "previous_page", "source", "from", "isappinstalled", "featurecode", "luicode", "lfid",
    "scene", "subscene", "ascene", "srcid", "clicktime", "enterid", "sessionid", "pass_ticket",
    "sharer_shareinfo", "sharer_shareinfo_first", "wx_header", "exportkey", "mpshare",
    "forceh5", "session_id", "share_app_name", "share_iid", "share_link_id",
}
BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


class RecordError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def unique(values):
    result, seen = [], set()
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def as_list(value):
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def review_status(evidence: list[dict]) -> str:
    """Keep a discovered URL separate from reviewed, relevant experience."""
    decisions = set()
    for observation in evidence:
        for key in ("relevance", "topic_status"):
            for raw in as_list(observation.get(key)):
                value = str(raw).strip().lower()
                if value.startswith("excluded") or value in {"irrelevant", "not_relevant", "排除"}:
                    decisions.add("excluded")
                elif value in {"included_experience", "纳入经验"}:
                    decisions.add("included_experience")
                elif value in {"included", "relevant", "confirmed_related", "directly_related", "明确相关", "纳入"}:
                    decisions.add("related")
    if "excluded" in decisions and decisions.intersection({"related", "included_experience"}):
        return "conflict"
    if "excluded" in decisions:
        return "excluded"
    if "included_experience" in decisions:
        return "included_experience"
    if "related" in decisions:
        return "related"
    return "pending"


def clean_pairs(query: str):
    return [(key, value) for key, value in parse_qsl(query, keep_blank_values=True)
            if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")]


def weibo_mid(value: str) -> str:
    """Map a Weibo base62 status ID and a decimal MID to the same identity."""
    if value.isdigit():
        return value
    if not re.fullmatch(r"[0-9A-Za-z]{5,16}", value):
        raise RecordError("invalid_post_id", "微博状态ID格式不明确")
    chunks = []
    while value:
        part, value = value[-4:], value[:-4]
        number = 0
        for char in part:
            number = number * 62 + BASE62.index(char)
        chunks.append(str(number))
    chunks.reverse()
    return chunks[0] + "".join(chunk.zfill(7) for chunk in chunks[1:])


def identify_url(original: str) -> dict:
    if not isinstance(original, str) or not original.strip():
        raise RecordError("missing_url", "缺少非空URL")
    try:
        parts = urlsplit(original.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise RecordError("invalid_url", "URL无法解析") from exc
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password or port not in (None, 80, 443):
        raise RecordError("invalid_url", "仅支持无嵌入凭据的公开HTTP(S)链接")
    path = parts.path.rstrip("/") or "/"
    source_type = "original"
    if host in ("xiaohongshu.com", "www.xiaohongshu.com"):
        match = re.fullmatch(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})", path)
        if not match:
            raise RecordError("not_post_url", "小红书搜索、用户或聚合页不是单帖")
        platform, post_id = "xiaohongshu", match.group(1).lower()
        public_url = "https://www.xiaohongshu.com/explore/" + post_id
    elif host in ("douyin.com", "www.douyin.com"):
        match = re.fullmatch(r"/(video|note)/([0-9]+)", path)
        if not match:
            raise RecordError("not_post_url", "抖音搜索、用户或聚合页不是单帖")
        platform, post_id = "douyin", match.group(2)
        public_url = "https://www.douyin.com/" + match.group(1) + "/" + post_id
    elif host in ("weibo.com", "www.weibo.com", "m.weibo.cn", "weibo.cn", "www.weibo.cn"):
        if re.match(r"/(?:u|n|p|search|hot|newlogin|login)(?:/|$)", path):
            raise RecordError("not_post_url", "微博搜索、用户或聚合页不是单帖")
        match = re.fullmatch(r"/(?:detail|status)/([0-9A-Za-z]+)", path)
        if not match:
            match = re.fullmatch(r"/[0-9]+/([0-9A-Za-z]+)", path)
        if not match and host.endswith("weibo.cn"):
            match = re.fullmatch(r"/comment/([0-9A-Za-z]+)", path)
        if not match:
            raise RecordError("not_post_url", "无法从微博URL确认单帖ID")
        platform, post_id = "weibo", weibo_mid(match.group(1))
        public_url = "https://m.weibo.cn/detail/" + post_id
    elif host in ("sina.cn", "www.sina.cn"):
        match = re.fullmatch(r"/news/detail/([0-9]+)\.html", path)
        if not match:
            raise RecordError("not_post_url", "新浪账号/新闻聚合页不作为微博单帖")
        platform, post_id, source_type = "weibo", match.group(1), "sina_mirror"
        public_url = "https://www.sina.cn/news/detail/" + post_id + ".html"
    elif host == "mp.weixin.qq.com":
        platform = "wechat"
        short = re.fullmatch(r"/s/([0-9A-Za-z_-]+)", path)
        pairs = clean_pairs(parts.query)
        if short:
            post_id = "short:" + short.group(1)
        elif path == "/s":
            params = dict(pairs)
            if not all(params.get(key) for key in ("__biz", "mid", "idx")):
                raise RecordError("incomplete_post_url", "公众号长链接需保留__biz、mid及idx")
            post_id = "long:" + ":".join(params[key] for key in ("__biz", "mid", "idx"))
        else:
            raise RecordError("not_post_url", "公众号菜单、搜索和聚合页不是文章")
        public_url = urlunsplit(("https", host, path, urlencode(pairs), ""))
    elif host in ("xhslink.com", "www.xhslink.com", "v.douyin.com", "t.cn"):
        raise RecordError("unresolved_short_url", "先通过授权浏览器解析分享短链，再导入实际单帖URL")
    else:
        raise RecordError("unsupported_host", "不是支持的四平台单帖来源")
    return {"platform": platform, "post_id": post_id, "dedup_key": platform + ":" + post_id,
            "url": public_url, "canonical_url": public_url, "source_type": source_type}


def normalize_record(record: dict, source: str, row: int) -> tuple[dict, list[str]]:
    if not isinstance(record, dict):
        raise RecordError("invalid_record", "每条记录须为JSON对象")
    original = record.get("url") or record.get("original_url") or record.get("source_url")
    identity = identify_url(original)
    declared = record.get("platform")
    if declared and PLATFORM_ALIASES.get(str(declared), str(declared).lower()) != identity["platform"]:
        raise RecordError("platform_mismatch", "声明平台与单帖URL不一致")
    warnings = []
    title = record.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "（未记录可见标题）"
        warnings.append("missing_title")
    level = record.get("evidence_level") or record.get("reading_level") or record.get("access_level") or "unknown"
    level = LEVEL_ALIASES.get(str(level), str(level))
    if level not in LEVEL_RANK:
        warnings.append("unrecognized_evidence_level")
        declared_level, level = level, "unknown"
    else:
        declared_level = None
    if level == "unknown":
        warnings.append("unknown_evidence_level")
    # A mirror marker never claims that the source was read in full.
    reading_depth = record.get("reading_depth")
    if not reading_depth and level == "mirror":
        reading_depth = record.get("reading_level") or "unknown"
    reading_depth = LEVEL_ALIASES.get(str(reading_depth), str(reading_depth)) if reading_depth else level
    if reading_depth not in LEVEL_RANK or reading_depth == "mirror":
        reading_depth = "unknown"
    if level == "mirror" or record.get("source_type") in {"mirror", "repost", "sina_mirror"}:
        if identity["source_type"] != "sina_mirror":
            identity["source_type"] = "mirror"
    if not record.get("collected_at") and not record.get("observed_at") and not record.get("accessed_at"):
        warnings.append("missing_observation_time")
    queries = unique(as_list(record.get("queries")) + as_list(record.get("query")) + as_list(record.get("search_queries")))
    if not queries:
        warnings.append("missing_query_provenance")
    evidence = {
        "input_file": source, "input_row": row, "source_url": original.strip(),
        "public_url": identity["url"], "source_type": identity["source_type"],
        "evidence_level": level, "reading_depth": reading_depth, "title": title, "queries": queries,
    }
    for key in ("summary", "published_at", "collected_at", "observed_at", "accessed_at", "project_year", "relevance", "limitations", "reading_scope", "original_post_url", "author", "author_name", "topic_status", "access_status", "notes", "metrics", "query_id"):
        if key in record:
            evidence[key] = record[key]
    if declared_level:
        evidence["declared_evidence_level"] = declared_level
    if "post_id" in record:
        evidence["supplied_post_id"] = record["post_id"]
    supplied = as_list(record.get("evidence")) + as_list(record.get("sources"))
    if supplied:
        evidence["supplied_evidence"] = supplied
    normalized = {
        **identity, "title": title, "evidence_level": level, "evidence_levels": [level],
        "reading_depth": reading_depth, "reading_depths": [reading_depth],
        "original_urls": unique([original.strip()] + as_list(record.get("original_urls"))),
        "source_types": [identity["source_type"]], "queries": queries,
        "published_at": record.get("published_at"), "summary": record.get("summary", ""),
        "topic_status": record.get("topic_status", record.get("relevance", "unreviewed")),
        "relevance": record.get("relevance"), "limitations": record.get("limitations"),
        "accessed_at": record.get("accessed_at"), "project_year": record.get("project_year"),
        "evidence": [evidence], "review_status": review_status([evidence]),
    }
    return normalized, unique(warnings)


def merge_posts(existing: dict, incoming: dict) -> None:
    for key in ("original_urls", "source_types", "queries", "evidence_levels", "reading_depths", "evidence"):
        existing[key] = unique(existing[key] + incoming[key])
    # Preserve constraints and conflicting review decisions regardless of reading depth.
    for key in ("relevance", "limitations", "accessed_at", "project_year", "topic_status"):
        values = unique(as_list(existing.get(key)) + as_list(incoming.get(key)))
        existing[key] = values[0] if len(values) == 1 else values or None
    existing["review_status"] = review_status(existing["evidence"])
    # Keep the strongest observation at the top; preserve every observation below.
    if LEVEL_RANK[incoming["evidence_level"]] > LEVEL_RANK[existing["evidence_level"]]:
        for key in ("title", "evidence_level", "reading_depth", "published_at", "summary"):
            if incoming.get(key) is not None and incoming.get(key) != "":
                existing[key] = incoming[key]
    # A confirmed original source is preferred to a mirror, but mirror provenance remains.
    if existing["source_type"] in {"sina_mirror", "mirror"} and incoming["source_type"] == "original":
        for key in ("source_type", "url", "canonical_url"):
            existing[key] = incoming[key]


def csv_safe(value) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    stripped = text.lstrip()
    if text.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row.get(key)) for key in fields})


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def iter_input(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        for row, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                yield row, json.loads(line), None
            except json.JSONDecodeError:
                yield row, None, ("invalid_json", "JSONL行不是有效JSON")
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            yield 1, None, ("invalid_json", "文件不是有效JSON")
            return
        if isinstance(payload, dict) and isinstance(payload.get("posts"), list):
            payload = payload["posts"]
        elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
            payload = payload["records"]
        if not isinstance(payload, list):
            payload = [payload]
        for row, item in enumerate(payload, 1):
            yield row, item, None


def normalize_files(paths: list[Path], output_dir: Path) -> dict:
    posts, rejected, warnings = {}, [], []
    attempted = accepted = 0
    for path in paths:
        for row, record, parse_error in iter_input(path):
            attempted += 1
            try:
                if parse_error:
                    raise RecordError(*parse_error)
                normalized, row_warnings = normalize_record(record, path.name, row)
                accepted += 1
                for warning in row_warnings:
                    warnings.append({"input_file": path.name, "input_row": row, "code": warning})
                key = normalized["dedup_key"]
                if key in posts:
                    merge_posts(posts[key], normalized)
                else:
                    posts[key] = normalized
            except RecordError as exc:
                rejected.append({"input_file": path.name, "input_row": row, "code": exc.code, "message": str(exc)})
    items = sorted(posts.values(), key=lambda item: item["dedup_key"])
    platform_counts = {platform: sum(item["platform"] == platform for item in items) for platform in PLATFORMS}
    coverage = {
        "schema_version": 1, "generated_at": utc_now(), "network_requests_made": 0,
        "full_platform_coverage": False,
        "coverage_statement": "仅统计所提供元数据；未执行网络抓取，数量不是平台全量、完整历史或搜索穷尽证明。",
        "input_records": attempted, "accepted_observations": accepted, "unique_posts": len(items),
        "unique_candidates": len(items), "unique_posts_is_candidate_count": True,
        "accepted_observations_means": "通过元数据格式与URL检查；不表示主题纳入或可提炼经验。",
        "excluded_candidates": sum(item["review_status"] == "excluded" for item in items),
        "pending_candidates": sum(item["review_status"] == "pending" for item in items),
        "related_candidates": sum(item["review_status"] == "related" for item in items),
        "conflicted_candidates": sum(item["review_status"] == "conflict" for item in items),
        "included_experience_posts": sum(item["review_status"] == "included_experience" for item in items),
        "merged_duplicate_observations": accepted - len(items), "rejected_records": len(rejected),
        "unique_posts_by_platform": platform_counts,
        "posts_with_original_source": sum("original" in item["source_types"] for item in items),
        "mirror_only_posts": sum("original" not in item["source_types"] for item in items),
        "queries_with_accepted_results": unique([query for item in items for query in item["queries"]]),
        "query_count_is_not_attempt_count": True,
        "rejected": rejected, "warnings": warnings,
        "not_measured": ["站内搜索覆盖率", "未返回结果的已执行查询", "已删除或不可见内容", "未导入评论与视频口述", "短链接与长链接之间未证实的对应关系"],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "posts.json", items)
    write_csv(output_dir / "posts.csv", items, ["platform", "post_id", "title", "url", "published_at", "project_year", "accessed_at", "evidence_level", "reading_depth", "topic_status", "relevance", "review_status", "limitations", "summary", "queries", "source_types", "evidence_levels"])
    write_json(output_dir / "coverage.json", coverage)
    return coverage


def query_plan(platforms=None, aliases=None, topics=None, years=None) -> dict:
    rows = []
    for platform in platforms or PLATFORMS:
        for alias in aliases or ALIASES:
            for topic in topics or TOPICS:
                for year in years or [None]:
                    query = " ".join(part for part in (SCOPES[platform], alias, topic, str(year) if year else "") if part)
                    rows.append({"query_id": hashlib.sha256(query.encode("utf-8")).hexdigest()[:12],
                                 "platform": platform, "project_alias": alias, "topic": topic,
                                 "year": year, "query": query, "execution_status": "planned"})
    rows = unique(rows)
    return {"schema_version": 1, "generated_at": utc_now(), "network_requests_made": 0,
            "note": "这是待执行检索计划；由agent、授权连接器或浏览器执行并记录检索日志。site限制不保证搜索引擎严格遵守。",
            "queries": rows}


def parse_year(value: str) -> int:
    year = int(value)
    if year < 2000 or year > 2100:
        raise argparse.ArgumentTypeError("年份应为2000至2100之间的四位数")
    return year


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    queries = commands.add_parser("queries", help="输出待执行检索计划，不联网")
    queries.add_argument("--platform", choices=PLATFORMS, action="append", dest="platforms")
    queries.add_argument("--alias", action="append", dest="aliases", help="替换默认项目同义词；可重复")
    queries.add_argument("--topic", action="append", dest="topics", help="替换默认主题；可重复")
    queries.add_argument("--year", type=parse_year, action="append", dest="years")
    queries.add_argument("--output-dir", type=Path, required=True)
    normalize = commands.add_parser("normalize", help="合并授权元数据，去重并生成覆盖记录")
    normalize.add_argument("inputs", nargs="+", type=Path)
    normalize.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "queries":
            plan = query_plan(args.platforms, args.aliases, args.topics, args.years)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            write_json(args.output_dir / "query-plan.json", plan)
            write_csv(args.output_dir / "query-plan.csv", plan["queries"], ["query_id", "platform", "project_alias", "topic", "year", "query", "execution_status"])
            print(json.dumps({"planned_queries": len(plan["queries"]), "network_requests_made": 0}, ensure_ascii=False))
        else:
            coverage = normalize_files(args.inputs, args.output_dir)
            print(json.dumps({key: coverage[key] for key in ("input_records", "unique_candidates", "included_experience_posts", "excluded_candidates", "pending_candidates", "conflicted_candidates", "rejected_records", "network_requests_made")}, ensure_ascii=False))
        return 0
    except (OSError, UnicodeError, ValueError) as exc:
        print("处理失败：" + type(exc).__name__ + "；检查输入格式、路径和权限。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
