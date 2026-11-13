"""Observable invariants for the metadata collector; entirely offline."""

import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

SPEC = importlib.util.spec_from_file_location("collect", Path(__file__).resolve().parents[1] / "scripts" / "collect.py")
collect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collect)


class CollectorTests(unittest.TestCase):
    def test_wechat_identity_survives_tracking_cleanup(self):
        url = "https://mp.weixin.qq.com/s?__biz=MzA%3D%3D&mid=2247481234&idx=2&sn=content-signature&scene=1&sharer_shareinfo=tracking&custom=keep#wechat_redirect"
        row = collect.identify_url(url)
        query = parse_qs(urlsplit(row["url"]).query)
        self.assertEqual(query, {"__biz": ["MzA=="], "mid": ["2247481234"], "idx": ["2"], "sn": ["content-signature"], "custom": ["keep"]})
        self.assertEqual(row["post_id"], "long:MzA==:2247481234:2")
        other = collect.identify_url(url.replace("scene=1", "scene=99").replace("content-signature", "changed-signature"))
        self.assertEqual(row["dedup_key"], other["dedup_key"])

    def test_xhs_keeps_original_but_strips_public_signature(self):
        url = "https://www.xiaohongshu.com/discovery/item/64a123456789abcdef012345?xsec_token=example-only&xsec_source=pc_search&utm_source=example"
        row, _ = collect.normalize_record({"url": url, "title": "合成测试", "evidence_level": "search_snippet"}, "fixture.json", 1)
        self.assertEqual(row["original_urls"], [url])
        self.assertEqual(row["url"], "https://www.xiaohongshu.com/explore/64a123456789abcdef012345")
        self.assertEqual(row["dedup_key"], "xiaohongshu:64a123456789abcdef012345")

    def test_merge_preserves_queries_evidence_and_mirror_identity(self):
        first, _ = collect.normalize_record({"url": "https://www.sina.cn/news/detail/4975729319871568.html", "title": "镜像", "query": "第一检索式", "evidence_level": "search_snippet", "evidence": [{"note": "摘要线索"}]}, "one.json", 1)
        second, _ = collect.normalize_record({"url": "https://m.weibo.cn/detail/4975729319871568?from=search", "title": "原帖", "queries": ["第二检索式", "第一检索式"], "evidence_level": "post_fulltext", "summary": "合成摘要"}, "two.json", 2)
        self.assertEqual(first["dedup_key"], second["dedup_key"])
        collect.merge_posts(first, second)
        self.assertEqual(first["queries"], ["第一检索式", "第二检索式"])
        self.assertEqual(len(first["evidence"]), 2)
        self.assertEqual(first["evidence"][0]["supplied_evidence"], [{"note": "摘要线索"}])
        self.assertEqual(first["evidence_levels"], ["search_snippet", "post_fulltext"])
        self.assertEqual(first["source_types"], ["sina_mirror", "original"])
        self.assertEqual(first["title"], "原帖")
        self.assertTrue(first["url"].startswith("https://m.weibo.cn/detail/"))

    def test_weibo_base62_matches_decimal_mid(self):
        def encode(number):
            chars = []
            while number:
                number, remainder = divmod(number, 62)
                chars.append(collect.BASE62[remainder])
            return "".join(reversed(chars)) or "0"
        mid = "4975729319871568"
        chunks = []
        value = mid
        while value:
            part, value = value[-7:], value[:-7]
            chunks.append(encode(int(part)))
        chunks.reverse()
        status = chunks[0] + "".join(chunk.zfill(4) for chunk in chunks[1:])
        self.assertEqual(collect.weibo_mid(status), mid)
        parsed = collect.identify_url("https://weibo.com/1234567890/" + status)
        self.assertEqual(parsed["post_id"], mid)

    def test_aggregate_pages_and_spoof_hosts_rejected(self):
        for url in ("https://www.xiaohongshu.com/search_result?keyword=博士", "https://www.douyin.com/search/国自然", "https://weibo.com/u/1234567890", "https://s.weibo.com/weibo?q=博士", "https://mp.weixin.qq.com/mp/profile_ext?action=home", "https://www.sina.cn/media/5628523640", "https://www.douyin.com.evil.example/video/123"):
            with self.subTest(url=url), self.assertRaises(collect.RecordError):
                collect.identify_url(url)
        with self.assertRaises(collect.RecordError) as ctx:
            collect.identify_url("https://xhslink.com/a/example")
        self.assertEqual(ctx.exception.code, "unresolved_short_url")

    def test_missing_fields_are_visible_not_invented(self):
        with self.assertRaises(collect.RecordError) as ctx:
            collect.normalize_record({"title": "缺URL"}, "fixture.json", 1)
        self.assertEqual(ctx.exception.code, "missing_url")
        row, warnings = collect.normalize_record({"url": "https://www.douyin.com/video/12345"}, "fixture.json", 2)
        self.assertEqual(row["evidence_level"], "unknown")
        self.assertIsNone(row["published_at"])
        self.assertIn("missing_title", warnings)
        self.assertIn("missing_observation_time", warnings)
        self.assertIn("unknown_evidence_level", warnings)

    def test_csv_dangerous_prefixes_are_neutralized(self):
        for value in ("=1+1", "+formula", "-formula", "@formula", "  =formula", "\t=1", "\rplain", "\nplain"):
            self.assertEqual(collect.csv_safe(value), "'" + value)
        self.assertEqual(collect.csv_safe("正常标题"), "正常标题")

    def test_end_to_end_json_and_jsonl_count_real_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = root / "one.json"
            one.write_text(json.dumps({"posts": [{"url": "https://www.douyin.com/video/12345", "title": "=1+1", "query": "查询A"}, {"url": "https://www.douyin.com/search/项目", "title": "非帖子"}]}), encoding="utf-8")
            two = root / "two.jsonl"
            two.write_text(json.dumps({"url": "https://www.douyin.com/video/12345?source=search", "title": "=1+1", "query": "查询B", "evidence_level": "post_fulltext"}) + "\ninvalid-json\n", encoding="utf-8")
            report = collect.normalize_files([one, two], root / "result")
            self.assertEqual((report["input_records"], report["accepted_observations"], report["unique_posts"], report["rejected_records"]), (4, 2, 1, 2))
            self.assertEqual(report["merged_duplicate_observations"], 1)
            self.assertFalse(report["full_platform_coverage"])
            data = json.loads((root / "result/posts.json").read_text(encoding="utf-8"))
            self.assertEqual(data[0]["queries"], ["查询A", "查询B"])
            self.assertEqual(len(data[0]["evidence"]), 2)
            with (root / "result/posts.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["title"], "'=1+1")

    def test_queries_cover_four_platforms_and_optional_years(self):
        plan = collect.query_plan(aliases=["合成项目"], topics=["经验"], years=[2025, 2026])
        self.assertEqual(len(plan["queries"]), 8)
        self.assertEqual({row["platform"] for row in plan["queries"]}, set(collect.PLATFORMS))
        self.assertEqual({row["year"] for row in plan["queries"]}, {2025, 2026})
        self.assertTrue(all(row["execution_status"] == "planned" for row in plan["queries"]))
        self.assertEqual(plan["network_requests_made"], 0)

    def test_research_record_preserves_exclusion_and_access_date(self):
        record = {
            "platform": "douyin", "url": "https://www.douyin.com/video/12345",
            "title": "合成的博士就业话题", "published_at": None, "project_year": 2025,
            "accessed_at": "2026-11-13", "evidence_level": "search_snippet",
            "relevance": "excluded_comment_only", "query": "国自然博士生项目",
            "limitations": "仅评论命中；未观看；不作为博士生项目申请经验。",
        }
        item, warnings = collect.normalize_record(record, "social-posts.json", 1)
        for field in ("project_year", "accessed_at", "relevance", "limitations"):
            self.assertEqual(item[field], record[field])
            self.assertEqual(item["evidence"][0][field], record[field])
        self.assertEqual(item["topic_status"], "excluded_comment_only")
        self.assertEqual(item["review_status"], "excluded")
        self.assertNotIn("missing_observation_time", warnings)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.json"
            source.write_text(json.dumps([record]), encoding="utf-8")
            report = collect.normalize_files([source], root / "result")
            self.assertEqual(report["unique_candidates"], 1)
            self.assertEqual(report["excluded_candidates"], 1)
            self.assertEqual(report["included_experience_posts"], 0)
            with (root / "result/posts.csv").open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["relevance"], "excluded_comment_only")
            self.assertEqual(row["limitations"], record["limitations"])

    def test_stronger_observation_does_not_erase_conflicting_exclusion(self):
        base = {"url": "https://www.douyin.com/video/12345", "title": "合成例", "query": "查询"}
        first, _ = collect.normalize_record({**base, "accessed_at": "2026-01-01", "project_year": 2025,
                                             "evidence_level": "search_snippet", "relevance": "excluded_comment_only",
                                             "limitations": "只在评论中提及"}, "one.json", 1)
        second, _ = collect.normalize_record({**base, "accessed_at": "2026-01-02", "project_year": 2026,
                                              "evidence_level": "watched", "relevance": "included_experience",
                                              "limitations": "只观看前30秒"}, "two.json", 1)
        collect.merge_posts(first, second)
        self.assertEqual(first["review_status"], "conflict")
        self.assertEqual(first["limitations"], ["只在评论中提及", "只观看前30秒"])
        self.assertEqual(first["relevance"], ["excluded_comment_only", "included_experience"])
        self.assertEqual(first["project_year"], [2025, 2026])
        self.assertEqual(first["accessed_at"], ["2026-01-01", "2026-01-02"])
        self.assertEqual(first["evidence"][0]["limitations"], "只在评论中提及")
        self.assertEqual(first["evidence"][1]["limitations"], "只观看前30秒")

    def test_image_watched_and_mirror_levels_do_not_invent_full_reading(self):
        for level in ("image_text", "watched", "mirror"):
            item, warnings = collect.normalize_record({"url": "https://www.douyin.com/video/12345",
                                                       "title": "合成媒体例", "evidence_level": level,
                                                       "limitations": "仅读取部分内容"}, "input.json", 1)
            self.assertEqual(item["evidence_level"], level)
            self.assertNotIn("unrecognized_evidence_level", warnings)
            self.assertNotEqual(item["reading_depth"], "post_fulltext")
            if level == "mirror":
                self.assertEqual(item["source_type"], "mirror")
                self.assertEqual(item["reading_depth"], "unknown")
            else:
                self.assertEqual(item["reading_depth"], level)
        item, _ = collect.normalize_record({"url": "https://www.sina.cn/news/detail/4975729319871568.html",
                                            "title": "合成镜像", "evidence_level": "mirror",
                                            "reading_level": "page_text"}, "input.json", 1)
        self.assertEqual(item["source_type"], "sina_mirror")
        self.assertEqual(item["evidence_level"], "mirror")
        self.assertEqual(item["reading_depth"], "page_text")


if __name__ == "__main__":
    unittest.main()
