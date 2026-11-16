#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 日报：抓取公开信息源 -> 打分筛选 -> 排成报纸 -> 发到邮箱。

用法：
    python main.py --dry-run     # 只生成 output/日期.html，不发信（可本地预览）
    python main.py               # 生成并发送

环境变量（两种发信通道，优先 SMTP）：
    SMTP_USER / SMTP_PASS   发件邮箱 + SMTP 授权码（QQ/Gmail/163，可发任意收件人）
    SMTP_HOST / SMTP_PORT   可选，留空则按发件邮箱域名自动判断
    RESEND_API_KEY          Resend Key（免费版只能发给注册账号自己的邮箱）
    MAIL_FROM               Resend 模式下的发件人
    MAIL_TO                 收件人，多个用英文逗号分隔
    GITHUB_TOKEN            可选，提升 GitHub API 限额（Actions 里自动有）
    TRANSLATE=off           可选，关闭英文汉化
"""
import argparse
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "output")
DOCS_DIR = os.path.join(ROOT, "docs")  # 详情页，交给 GitHub Pages 托管

# 4 : 3 : 2 : 1 的内容配比
SECTIONS = [
    ("models", "模型与大厂动态", "MODELS & LABS", 8),
    ("github", "GitHub 火爆项目", "OPEN SOURCE", 6),
    ("industry", "行业投融资与产品", "MARKET", 4),
    ("tech", "技术教程与工程实践", "ENGINEERING", 2),
]

FEEDS = [
    ("models", "OpenAI", "https://openai.com/news/rss.xml", 1.7),
    ("models", "Hugging Face", "https://huggingface.co/blog/feed.xml", 1.5),
    ("models", "Google DeepMind", "https://deepmind.google/blog/rss.xml", 1.5),
    ("models", "MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", 1.3),
    ("models", "Google News AI",
     "https://news.google.com/rss/search?q=AI+OR+%22large+language+model%22+OR+GPT+OR+Gemini+OR+Claude+when:1d&hl=en-US&gl=US&ceid=US:en", 1.2),
    ("models", "Google News 中文",
     "https://news.google.com/rss/search?q=%E5%A4%A7%E6%A8%A1%E5%9E%8B+OR+%E6%99%BA%E8%83%BD%E4%BD%93+OR+%E7%94%9F%E6%88%90%E5%BC%8FAI+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans", 1.2),
    ("industry", "TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", 1.3),
    ("industry", "The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 1.1),
    ("industry", "VentureBeat AI", "https://venturebeat.com/category/ai/feed/", 1.1),
    ("tech", "Simon Willison", "https://simonwillison.net/atom/everything/", 1.2),
    ("tech", "GitHub Blog AI", "https://github.blog/ai-and-ml/feed/", 1.1),
    ("tech", "AWS ML Blog", "https://aws.amazon.com/blogs/machine-learning/feed/", 1.0),
]

AI_WORDS = (
    "ai", "llm", "llms", "gpt", "chatgpt", "claude", "gemini", "openai", "anthropic",
    "deepmind", "deepseek", "qwen", "mistral", "llama", "sora", "agent", "agents",
    "agentic", "rag", "mcp", "diffusion", "transformer", "multimodal", "embeddings",
    "copilot", "midjourney", "inference", "fine-tuning", "neural",
)
AI_CJK = ("大模型", "模型", "智能体", "多模态", "推理", "训练", "算力", "芯片", "人工智能", "生成式")
AI_RE = re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % "|".join(re.escape(w) for w in AI_WORDS), re.I)
MAX_AGE_HOURS = 96
NL = "\n"

PRODUCT_HINTS = ("introducing", "introduces", "launch", "launches", "release", "releases",
                 "announc", "unveil", "ships", "now available", "open-source",
                 "发布", "推出", "开源", "上线", "更新")


def is_ai(text):
    text = (text or "").lower()
    return bool(AI_RE.search(text)) or any(k in text for k in AI_CJK)



# ---------------------------------------------------------------- 抓取工具

def fetch(url, headers=None, timeout=25, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    req.add_header("Accept-Encoding", "gzip")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":  # 部分站点无视请求头强制 gzip
            raw = gzip.decompress(raw)
        return raw


def local_name(tag):
    return tag.split("}")[-1]


def strip_html(raw):
    if not raw:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\[\s*…+\s*\]|\[\.{3}\]", "", text)  # 各大站点的「内容折叠」占位
    text = re.sub(r"\s+", " ", text).strip()
    for junk in ("Continue reading", "Read more", "The post ", " appeared first on ",
                 "阅读全文", "点击查看", "查看原文"):
        text = text.replace(junk, " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        return parsedate_to_datetime(value)
    except Exception:
        pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def child(entry, *names):
    for name in names:
        for c in entry:
            if local_name(c.tag) == name:
                return c
    return None


def entry_link(entry):
    fallback = ""
    for c in entry:
        if local_name(c.tag) != "link":
            continue
        href = c.get("href")
        if href:
            if c.get("rel") in (None, "alternate"):
                return href
            fallback = fallback or href
        elif c.text and c.text.strip():
            return c.text.strip()
    return fallback


def parse_feed(xml_bytes, source):
    if xml_bytes[:1] != b"<":  # 源已改成 HTML 页面等非 XML 内容
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    items = []
    for entry in root.iter():
        if local_name(entry.tag) not in ("item", "entry"):
            continue
        title = strip_html((child(entry, "title").text if child(entry, "title") is not None else "") or "")
        link = entry_link(entry)
        desc_el = child(entry, "description", "summary", "content", "encoded")
        desc = strip_html(desc_el.text if desc_el is not None else "")
        date_el = child(entry, "pubDate", "published", "updated", "date")
        published = parse_date(date_el.text if date_el is not None else "") if date_el is not None else None
        if not title or not link:
            continue
        if source.startswith("Google News") and " - " in title:  # 去掉「 - 观点网」这类来源尾巴
            title = title.rsplit(" - ", 1)[0]
        items.append({"title": title, "link": link, "summary": desc,
                      "published": published, "source": source})
    return items


# ---------------------------------------------------------------- 各来源

def fetch_feeds():
    collected = []
    for section, source, url, weight in FEEDS:
        try:
            items = parse_feed(fetch(url), source)
        except Exception as exc:  # 单个源失败不影响整期
            print("  ! %-18s %s" % (source, exc), file=sys.stderr)
            continue
        for it in items:
            it["section"] = section
            it["weight"] = weight
        collected.extend(items)
        print("  · %-18s %d 条" % (source, len(items)), file=sys.stderr)
    return collected


def fetch_github(now):
    since = (now - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    query = urllib.parse.quote("created:>=%s stars:>=150" % since)
    url = ("https://api.github.com/search/repositories?q=%s&sort=stars&order=desc&per_page=40" % query)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        payload = json.loads(fetch(url, headers=headers, timeout=30).decode("utf-8"))
    except Exception as exc:
        print("  ! GitHub API %s" % exc, file=sys.stderr)
        return []

    items = []
    for repo in payload.get("items", []):
        full_name = repo.get("full_name", "")
        stars = repo.get("stargazers_count", 0)
        desc = (repo.get("description") or "").strip()
        topics = ", ".join(repo.get("topics") or [])
        created = parse_date(repo.get("created_at", ""))
        created = created.astimezone(dt.timezone.utc) if created else now
        age_days = max((now - created).total_seconds() / 86400.0, 1.0)
        stars_per_day = stars / age_days
        blob = (full_name + " " + desc + " " + topics).lower()
        if stars < 150 or not is_ai(blob):  # AI 报纸只收 AI 相关仓库
            continue
        summary = desc or "暂无简介"
        summary += "（%s · 共 %s star，日均新增 %.0f）" % (
            repo.get("language") or "N/A", "{:,}".format(stars), stars_per_day)
        if topics:
            summary += " 标签：" + topics
        items.append({
            "title": full_name,
            "link": repo.get("html_url", ""),
            "summary": summary,
            "desc": desc,
            "suffix": summary[len(desc):],
            "published": created,
            "source": "GitHub",
            "section": "github",
            "weight": 1.0,
            "stars": stars,
            "stars_today": stars_per_day,
            "keywords": blob,
        })
    print("  · %-18s %d 条" % ("GitHub Trending", len(items)), file=sys.stderr)
    return items


def fetch_hn(now):
    url = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=40"
    try:
        payload = json.loads(fetch(url, timeout=25).decode("utf-8"))
    except Exception as exc:
        print("  ! Hacker News %s" % exc, file=sys.stderr)
        return []
    items = []
    for hit in payload.get("hits", []):
        title = hit.get("title") or ""
        link = hit.get("url") or ("https://news.ycombinator.com/item?id=%s" % hit.get("objectID"))
        blob = (title + " " + (hit.get("url") or "")).lower()
        if not is_ai(blob):
            continue
        published = hit.get("created_at_i")
        published = dt.datetime.fromtimestamp(published, dt.timezone.utc) if published else now
        items.append({
            "title": title,
            "link": link,
            "summary": "Hacker News 首页 %s 分 / %s 条讨论" % (hit.get("points", 0), hit.get("num_comments", 0)),
            "published": published,
            "source": "Hacker News",
            "section": "tech",
            "weight": 1.15,
        })
    print("  · %-18s %d 条" % ("Hacker News", len(items)), file=sys.stderr)
    return items


# ---------------------------------------------------------------- 排序筛选

def score(item, now):
    published = item.get("published") or now
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.timezone.utc)
    age_h = max((now - published).total_seconds() / 3600.0, 0.2)
    recency = max(0.0, (MAX_AGE_HOURS - age_h) / MAX_AGE_HOURS)
    blob = item["keywords"] if "keywords" in item else (item["title"] + " " + item["summary"]).lower()
    relevant = 1.0 if is_ai(blob) else 0.0
    product = 1.0 if any(h in item["title"].lower() for h in PRODUCT_HINTS) else 0.0  # 发布/上线优先
    title_rel = 1.0 if is_ai(item["title"]) else 0.0  # 标题就是 AI 话题的优先
    if item.get("section") == "github":
        heat = max(0.0, min(6.0, (item.get("stars_today") or 0) / 60.0))
        return heat + recency * 1.5 + relevant * 2.5
    return item.get("weight", 1.0) * 1.5 + recency * 3.0 + relevant * 2.0 + product * 1.5 + title_rel * 1.2


def dedupe_key(title):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.lower())[:48]


def fresh_enough(item, now):
    if item.get("section") == "github":  # 仓库按创建时间筛选，不套用资讯时效
        return True
    published = item.get("published") or now
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.timezone.utc)
    return (now - published).total_seconds() <= MAX_AGE_HOURS * 3600


def pick(all_items, now):
    need_map = dict((s[0], s[3]) for s in SECTIONS)
    ranked = sorted(all_items, key=lambda x: score(x, now), reverse=True)
    ranked = [it for it in ranked if fresh_enough(it, now)]
    seen, buckets, per_source = set(), {}, {}

    def take(cap):
        for item in ranked:
            section = item["section"]
            bucket = buckets.setdefault(section, [])
            if len(bucket) >= need_map[section]:
                continue
            key = dedupe_key(item["title"])
            if not key or key in seen:
                continue
            slot = (section, item["source"])
            if cap and section != "github" and per_source.get(slot, 0) >= cap:
                continue
            seen.add(key)
            per_source[slot] = per_source.get(slot, 0) + 1
            bucket.append(item)

    take(3)  # 每来源最多 3 条，保证版面来源多样
    take(0)  # 单一来源不够时放开限制补满
    return buckets


# ---------------------------------------------------------------- 汉化与 AI 总结

_TRANS_CACHE = {}
_TRANS_OK = {}

LLM_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE = (os.environ.get("LLM_BASE") or "https://discovery-api.intern-ai.org.cn/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL") or "deepseek-v4.1-flash-1017"
LLM_WORKERS = 2
# 哪些栏目走大模型（其余走机器翻译）。可用 LLM_SECTIONS 环境变量覆盖，如 models,github,industry,tech
LLM_SECTIONS = tuple(s.strip() for s in (os.environ.get("LLM_SECTIONS") or "github,tech").split(",") if s.strip())

BRIEF_PROMPT = """你是中文科技媒体的值班编辑，把英文 AI 资讯改写成可直接阅读的中文简报。
来源：{source}（栏目：{section}）
标题：{title}
导语：{desc}

只输出 JSON，不要代码块、不要解释：
{{"title": "中文标题，20 字以内，不用感叹号和营销词",
 "summary": "中文摘要，120-200 字，2-4 句：第一句说清发生了什么，第二句补充关键数字/技术细节，第三句说这意味着什么。不要复述标题，不要用「本文」「该文章」这类词",
 "points": ["要点一（25 字内，具体事实）", "要点二", "要点三"]}}
导语信息不足时只写导语里有的信息，绝不编造数字或评论。"""


def parse_json_loose(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def llm_brief(item):
    """让大模型写中文标题 / 摘要 / 要点；任何异常都返回 None 走机器翻译兜底。"""
    if not LLM_KEY:
        return None
    prompt = BRIEF_PROMPT.format(
        source=item["source"], section=item["section"], title=item["title"],
        desc=(item.get("desc") or item.get("summary") or "")[:800])
    payload = {"model": LLM_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.3, "max_tokens": 4000}
    for attempt in range(2):
        try:
            raw = fetch(LLM_BASE + "/chat/completions",
                        headers={"Authorization": "Bearer " + LLM_KEY,
                                 "Content-Type": "application/json"},
                        data=json.dumps(payload).encode("utf-8"), method="POST", timeout=90)
            content = (json.loads(raw.decode("utf-8")).get("choices") or [{}])[0].get("message", {}).get("content") or ""
            return parse_json_loose(content)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 1:  # 限流：等一会儿再试
                time.sleep(5)
                continue
            print("  ! LLM HTTP %s" % exc.code, file=sys.stderr)
            return None
        except Exception as exc:
            print("  ! LLM %s" % str(exc)[:120], file=sys.stderr)
            return None
    return None


def apply_brief(item, brief):
    summary = (brief.get("summary") or "").strip()
    if not summary:
        return False
    if item["section"] == "github":  # 仓库名保持英文，只换简介
        item["summary"] = summary + item.get("suffix", "")
    else:
        title = (brief.get("title") or "").strip()
        if title:
            item.setdefault("title_en", item["title"])
            item["title"] = title
        item["summary"] = summary
    points = [str(p).strip() for p in (brief.get("points") or []) if str(p).strip()][:3]
    if points:
        item["points"] = points
    return True


def has_cjk(text):
    if not text:
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk >= 2 and cjk >= len(text) * 0.15


def _google_extract(data):
    return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()


def _mymemory_extract(data):
    text = (data.get("responseData") or {}).get("translatedText") or ""
    if not text or "MYMEMORY WARNING" in text.upper() or "QUERY LENGTH LIMIT" in text.upper():
        return ""
    return text.strip()


def translate(text):
    """英文 -> 中文，失败时原样返回，绝不让翻译问题影响出刊。"""
    text = (text or "").strip()
    if not text or has_cjk(text):
        return text
    if os.environ.get("TRANSLATE", "on").lower() in ("0", "off", "false"):
        return text
    if text in _TRANS_CACHE:
        return _TRANS_CACHE[text]
    payload = text[:600]
    providers = (
        ("google",
         "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q="
         + urllib.parse.quote(payload), _google_extract),
        ("mymemory",
         "https://api.mymemory.translated.net/get?q=%s&langpair=en%%7Czh-CN"
         % urllib.parse.quote(payload[:480]), _mymemory_extract),
    )
    for name, url, extract in providers:
        if not _TRANS_OK.get(name, True):  # 该通道本次运行已失败过，直接跳过
            continue
        try:
            out = extract(json.loads(fetch(url, timeout=12).decode("utf-8")))
        except Exception:
            _TRANS_OK[name] = False
            continue
        if out:
            _TRANS_CACHE[text] = out
            return out
    _TRANS_CACHE[text] = text
    return text


def localize(buckets):
    items = [it for its in buckets.values() for it in its]
    targets = [it for it in items if it["section"] in LLM_SECTIONS]
    done = set()
    if LLM_KEY and targets:
        print("  AI 总结 %d/%d 条（%s，栏目 %s）..."
              % (len(targets), len(items), LLM_MODEL, ",".join(LLM_SECTIONS)), file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
            for item, brief in zip(targets, pool.map(llm_brief, targets)):
                if brief and apply_brief(item, brief):
                    done.add(id(item))
        print("  AI 总结成功 %d/%d；其余走机器翻译" % (len(done), len(targets)), file=sys.stderr)
    elif not LLM_KEY:
        print("  ! 未配置 LLM_API_KEY，全部走机器翻译", file=sys.stderr)
    for item in items:
        if id(item) in done:
            continue
        if item["section"] == "github":  # 兜底：仓库名保留原文，只译简介
            desc_zh = translate(item.get("desc") or "")
            item["summary"] = (desc_zh or "暂无简介") + item.get("suffix", "")
        else:
            title_zh = translate(item["title"])
            if title_zh and title_zh != item["title"]:
                item["title_en"] = item["title"]
                item["title"] = title_zh
            item["summary"] = translate(item.get("summary") or "")
    return buckets


# ---------------------------------------------------------------- 排版

def human_time(published, now):
    if not published:
        return ""
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.timezone.utc)
    minutes = int((now - published).total_seconds() / 60)
    if minutes < 60:
        return "%d 分钟前" % max(minutes, 1)
    if minutes < 24 * 60:
        return "%d 小时前" % (minutes // 60)
    return "%d 天前" % (minutes // 1440)


def esc(text):
    return html.escape(text or "", quote=True)


def clip(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "！", "？", ". ", "；", "，"):
        idx = cut.rfind(sep)
        if idx > limit * 0.55:
            cut = cut[: idx + len(sep)].strip()
            break
    else:
        cut = cut.rstrip() + "…"
    if cut.count("（") > cut.count("）"):  # 别把「（TypeScript · 共 1,6…」这种括号截断
        cut = cut[: cut.rfind("（")].rstrip(" ，,、") + "…"
    return cut


SERIF = "Georgia,'Times New Roman','Songti SC','Noto Serif CJK SC',serif"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"
INK = "#14110f"
RED = "#b5322a"
PAPER = "#fbf9f4"


def render_item(item, now):
    link = esc(item.get("detail_url") or item["link"])
    meta = "%s · %s" % (esc(item["source"]), human_time(item.get("published"), now))
    summary = clip(item.get("summary", ""), 260)
    summary_html = ('<div style="font:14px/1.7 %s;color:#3c372d;padding:7px 0 0;">%s</div>'
                    % (SANS, esc(summary))) if summary else ""
    points_html = ""
    if item.get("points"):
        lis = "".join('<li style="margin:0 0 4px;">%s</li>' % esc(p) for p in item["points"])
        points_html = ('<ul style="margin:8px 0 0;padding-left:18px;font:13px/1.65 %s;color:#6b6250;">%s</ul>'
                       % (SANS, lis))
    origin_html = ""
    if item.get("title_en"):
        origin_html = ('<div style="font:11px/1.5 %s;color:#a89d88;padding:6px 0 0;">原文：%s</div>'
                       % (SANS, esc(clip(item["title_en"], 90))))
    thumb = ""
    if item.get("image"):
        thumb = ('<td width="98" valign="top" style="padding-right:14px;">'
                 '<a href="%s"><img src="%s" width="98" alt="" '
                 'style="width:98px;max-width:98px;height:74px;display:block;'
                 'border:1px solid #e0d9c9;background:#efeade;"></a></td>'
                 % (link, esc(item["image"])))
    return """
            <tr><td style="padding:18px 30px 18px;border-bottom:1px solid #e8e2d4;">
              <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"><tr>
                %s
                <td valign="top">
                  <div style="font:11px/1.4 %s;letter-spacing:.6px;color:#9c9280;text-transform:uppercase;">
                    <span style="color:%s;">■</span>&nbsp;&nbsp;%s</div>
                  <a href="%s" style="text-decoration:none;color:%s;"><div style="font:700 17px/1.5 %s;padding:5px 0 0;">%s</div></a>
                  %s%s%s
                </td>
              </tr></table>
            </td></tr>""" % (thumb, SANS, RED, meta, link, INK, SERIF, esc(item["title"]),
                            summary_html, points_html, origin_html)


def render_headline(item, now):
    link = esc(item.get("detail_url") or item["link"])
    summary = clip(item.get("summary", ""), 300)
    origin_html = ""
    if item.get("title_en"):
        origin_html = ('<div style="font:11px/1.5 %s;color:#a89d88;padding:6px 0 0;">原文：%s</div>'
                       % (SANS, esc(clip(item["title_en"], 110))))
    image_html = ""
    if item.get("image"):
        image_html = ('<div style="padding:14px 0 0;"><a href="%s"><img src="%s" alt="" '
                      'style="width:100%%;max-width:580px;display:block;border:1px solid #ddd5c5;"></a></div>'
                      % (link, esc(item["image"])))
    points_html = ""
    if item.get("points"):
        lis = "".join('<li style="margin:0 0 5px;">%s</li>' % esc(p) for p in item["points"])
        points_html = ('<ul style="margin:12px 0 0;padding-left:18px;font:13px/1.7 %s;color:#5a5347;">%s</ul>'
                       % (SANS, lis))
    return """
            <tr><td style="padding:22px 30px 22px;background:#f3efe3;border-top:3px double %s;border-bottom:1px solid #e0d9c9;">
              <div style="font:11px/1.4 %s;letter-spacing:2px;color:%s;font-weight:700;">头条 · HEADLINE</div>
              <a href="%s" style="text-decoration:none;color:%s;"><div style="font:700 27px/1.35 %s;padding:8px 0 0;">%s</div></a>
              <div style="font:14px/1.8 %s;color:#3c372d;padding:8px 0 0;">%s</div>
              %s%s%s
              <div style="font:11px/1.4 %s;color:#9c9280;padding:8px 0 0;">%s · %s</div>
            </td></tr>""" % (INK, SANS, RED, link, INK, SERIF, esc(item["title"]),
                            SANS, esc(summary), points_html, origin_html, image_html, SANS,
                            esc(item["source"]), human_time(item.get("published"), now))


def render_section(index, title, en, items, now):
    body = "".join(render_item(it, now) for it in items)
    return """
            <tr><td style="padding:30px 30px 0;">
              <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0"><tr>
                <td style="font:700 12px/1 %s;letter-spacing:2.5px;color:%s;">0%d &nbsp;%s</td>
                <td align="right" style="font:11px/1 %s;letter-spacing:2px;color:#b3a992;">%s</td>
              </tr></table>
              <div style="height:1px;background:%s;margin:10px 0 0;"></div>
            </td></tr>%s""" % (SANS, RED, index, esc(title), SANS, en, INK, body)


def render_html(buckets, now, issue_no):
    models = buckets.get("models", [])
    headline = models[0] if models else None
    rest_models = models[1:] if headline else []

    sections_html = ""
    for i, (key, title, en, _need) in enumerate(SECTIONS, start=1):
        items = rest_models if key == "models" else buckets.get(key, [])
        if items:
            sections_html += render_section(i, title, en, items, now)

    total = sum(len(v) for v in buckets.values())
    date_cn = "%d 年 %d 月 %d 日 · 星期%s" % (
        now.year, now.month, now.day, "一二三四五六日"[now.weekday()])
    headline_html = render_headline(headline, now) if headline else ""
    sources = "、".join(s[1] for s in FEEDS) + "、GitHub Trending、Hacker News"

    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 日报 · %(short)s</title></head>
<body style="margin:0;padding:0;background:#ece7dc;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">今日 AI 要闻 %(total)d 条：%(preview)s</div>
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" style="background:#ece7dc;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
         style="width:100%%;max-width:640px;background:%(paper)s;border:1px solid #ddd5c5;">

    <tr><td style="padding:30px 30px 0;text-align:center;">
      <div style="font:11px/1.6 %(sans)s;letter-spacing:4px;color:#a89d88;">DAILY AI INTELLIGENCE · NO.%(issue)s</div>
      <div style="font:700 46px/1.15 %(serif)s;color:%(ink)s;letter-spacing:3px;padding:8px 0 0;">AI 日报</div>
      <div style="font:12px/1.6 %(sans)s;color:#7d7360;letter-spacing:1px;padding:8px 0 0;">%(date)s &nbsp;|&nbsp; 今日精选 %(total)d 条</div>
    </td></tr>

    <tr><td style="padding:16px 30px 0;">
      <div style="height:3px;background:%(ink)s;"></div>
      <div style="height:1px;background:%(ink)s;margin-top:2px;"></div>
    </td></tr>

    %(headline)s
    %(sections)s

    <tr><td style="padding:28px 30px 34px;">
      <div style="height:1px;background:#d8d0be;margin-bottom:14px;"></div>
      <div style="font:11px/1.9 %(sans)s;color:#9c9280;">
        信息源：%(sources)s<br>
        本期由 GitHub Actions 于 %(stamp)s 自动生成，英文标题与摘要经机器自动翻译，术语以原文为准。<br>
        内容配比 模型动态 4 : 开源项目 3 : 行业市场 2 : 工程实践 1
      </div>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""

    preview = "；".join(clip(it["title"], 24) for it in (headline, rest_models[0] if rest_models else None) if it)
    return page % {
        "short": now.strftime("%Y-%m-%d"),
        "total": total,
        "preview": esc(preview),
        "paper": PAPER,
        "sans": SANS,
        "serif": SERIF,
        "ink": INK,
        "date": date_cn,
        "issue": issue_no,
        "headline": headline_html,
        "sections": sections_html,
        "sources": esc(sources),
        "stamp": now.strftime("%Y-%m-%d %H:%M"),
    }


# ---------------------------------------------------------------- 配图与详情页

_OG_PATTERNS = (
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
)


def og_image(url):
    """从原文页抓 og:image 作为缩略图，失败就留空（不显示破图）。"""
    if not url:
        return ""
    try:
        raw = fetch(url, timeout=15)[:400000].decode("utf-8", "replace")
    except Exception:
        return ""
    for pattern in _OG_PATTERNS:
        match = re.search(pattern, raw, re.I)
        if match:
            src = html.unescape(match.group(1).strip())
            if src.startswith("//"):
                src = "https:" + src
            return urllib.parse.urljoin(url, src)
    return ""


def attach_images(buckets):
    plain = [it for key, items in buckets.items() if key != "github" for it in items]
    for items in buckets.values():
        for item in items:
            if item["section"] == "github":  # GitHub 自带社交预览图，无需再抓
                item["image"] = "https://opengraph.githubassets.com/1/" + item["title"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, image in zip(plain, pool.map(lambda it: og_image(it["link"]), plain)):
            item["image"] = image
    got = sum(1 for items in buckets.values() for it in items if it.get("image"))
    print("配图 %d/%d 条" % (got, sum(len(v) for v in buckets.values())), file=sys.stderr)
    return buckets


def pages_base():
    explicit = os.environ.get("PAGES_BASE", "").strip().rstrip("/")
    if explicit:
        return explicit
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()  # Actions 里形如 owner/name
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return "https://%s.github.io/%s" % (owner, name)
    return ""


def slugify(item, index):
    base = re.sub(r"[^a-z0-9]+", "-", (item["title"] or "").lower()).strip("-")[:40]
    if not base:  # 纯中文标题
        base = hashlib.md5(item["link"].encode("utf-8")).hexdigest()[:10]
    return "%02d-%s" % (index, base)


DETAIL_TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · AI 日报</title></head>
<body style="margin:0;padding:0;background:#ece7dc;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ece7dc;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
         style="width:100%;max-width:640px;background:#fbf9f4;border:1px solid #ddd5c5;">
    <tr><td style="padding:18px 30px;border-bottom:2px solid #14110f;">
      <a href="{index_url}" style="text-decoration:none;color:#14110f;font:700 16px/1 Georgia,'Songti SC',serif;letter-spacing:2px;">AI 日报</a>
      <span style="font:11px/1 -apple-system,sans-serif;color:#9c9280;">&nbsp;&nbsp;NO.{issue} · {date}</span>
    </td></tr>
    <tr><td style="padding:26px 30px 0;">
      <div style="font:700 11px/1 -apple-system,sans-serif;letter-spacing:2px;color:#b5322a;">{section_en} · {section_cn}</div>
      <h1 style="margin:12px 0 0;font:700 30px/1.4 Georgia,'Songti SC',serif;color:#14110f;">{title}</h1>
      <div style="padding-top:12px;font:12px/1.6 -apple-system,sans-serif;color:#9c9280;">{source} · {when}</div>
    </td></tr>
    {image}
    {points}
    <tr><td style="padding:24px 30px 0;">
      {original}
      <div style="font:15px/1.95 -apple-system,sans-serif;color:#3c372d;">{summary}</div>
    </td></tr>
    <tr><td style="padding:26px 30px 34px;">
      <div style="height:1px;background:#d8d0be;margin-bottom:20px;"></div>
      <a href="{orig}" style="display:inline-block;background:#14110f;color:#fbf9f4;text-decoration:none;font:600 13px/1 -apple-system,sans-serif;padding:12px 22px;">阅读原文 →</a>
      <a href="{index_url}" style="display:inline-block;margin-left:14px;color:#6b6250;text-decoration:none;font:13px/1 -apple-system,sans-serif;padding:12px 0;">← 返回本期日报</a>
      <div style="padding-top:18px;font:11px/1.8 -apple-system,sans-serif;color:#a89d88;">原文链接：{orig}<br>摘要由机器翻译整理，准确表述请以原文为准。</div>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def render_detail(item, section_cn, section_en, now, issue_no, index_url):
    image_html = ""
    if item.get("image"):
        image_html = ('<tr><td style="padding:20px 30px 0;">'
                      '<img src="%s" alt="" style="width:100%%;max-width:580px;display:block;'
                      'border:1px solid #e0d9c9;"></td></tr>' % esc(item["image"]))
    points_html = ""
    if item.get("points"):
        lis = "".join('<li style="margin:0 0 9px;">%s</li>' % esc(p) for p in item["points"])
        points_html = ('<tr><td style="padding:22px 30px 0;">'
                       '<div style="font:700 11px/1 -apple-system,sans-serif;letter-spacing:2px;color:#b5322a;">要点</div>'
                       '<ul style="margin:10px 0 0;padding-left:20px;font:14px/1.8 -apple-system,sans-serif;'
                       'color:#3c372d;">%s</ul></td></tr>' % lis)
    original_html = ""
    if item.get("title_en"):
        original_html = ('<div style="font:12px/1.7 -apple-system,sans-serif;color:#9c9280;'
                         'padding-bottom:12px;">原文标题：%s</div>' % esc(item["title_en"]))
    summary = esc(item.get("summary") or "该条暂无摘要，可点击下方按钮阅读原文。")
    return DETAIL_TPL.format(
        title=esc(item["title"]),
        index_url=esc(index_url),
        issue=issue_no,
        date=now.strftime("%Y-%m-%d"),
        section_en=esc(section_en),
        section_cn=esc(section_cn),
        source=esc(item["source"]),
        when=human_time(item.get("published"), now),
        image=image_html,
        points=points_html,
        original=original_html,
        summary=summary,
        orig=esc(item["link"]),
    )


def build_detail_pages(buckets, now, issue_no):
    day = now.strftime("%Y-%m-%d")
    out_dir = os.path.join(DOCS_DIR, day)
    os.makedirs(out_dir, exist_ok=True)
    for name in os.listdir(out_dir):  # 先清掉当天旧页面，避免历次运行留下重复文件
        if name.endswith(".html"):
            os.remove(os.path.join(out_dir, name))
    base = pages_base()
    index_url = ("%s/%s/index.html" % (base, day)) if base else "index.html"
    index = 0
    for key, title_cn, en, _need in SECTIONS:
        for item in buckets.get(key, []):
            index += 1
            name = slugify(item, index) + ".html"
            item["detail_url"] = ("%s/%s/%s" % (base, day, name)) if base else name
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
                fh.write(render_detail(item, title_cn, en, now, issue_no, index_url))
    print("详情页 %d 个 -> %s" % (index, out_dir), file=sys.stderr)
    return out_dir


def write_archive_index(now):
    """站点首页：列出所有往期，方便随时回看。"""
    days = sorted((d for d in os.listdir(DOCS_DIR) if re.match(r"^\d{4}-\d{2}-\d{2}$", d)), reverse=True)
    rows = []
    for day in days:
        page = os.path.join(DOCS_DIR, day, "index.html")
        if not os.path.exists(page):
            continue
        count = len(re.findall(r"font:700 17px", open(page, encoding="utf-8").read())) + 1
        rows.append('<li style="margin:0 0 12px;font:15px/1.6 %s;">'
                    '<a href="%s/index.html" style="color:#14110f;text-decoration:none;">'
                    '%s 第 %d 条</a></li>' % (SANS, day, day, count))
    body = "".join(rows) or '<li style="font:15px/1.6 %s;color:#9c9280;">还没有出刊</li>' % SANS
    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 日报 · 往期</title></head>
<body style="margin:0;padding:0;background:#ece7dc;">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" style="background:#ece7dc;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
         style="width:100%%;max-width:640px;background:#fbf9f4;border:1px solid #ddd5c5;">
    <tr><td style="padding:30px 30px 18px;text-align:center;border-bottom:2px solid #14110f;">
      <div style="font:11px/1.6 %s;letter-spacing:4px;color:#a89d88;">ARCHIVE</div>
      <div style="font:700 38px/1.2 %s;color:#14110f;letter-spacing:3px;padding-top:6px;">AI 日报 · 往期</div>
      <div style="font:12px/1.6 %s;color:#7d7360;padding-top:8px;">每天北京时间 08:00 自动出刊</div>
    </td></tr>
    <tr><td style="padding:24px 30px 34px;">
      <ul style="margin:0;padding-left:20px;color:#3c372d;">%s</ul>
    </td></tr>
  </table>
</td></tr></table>
</body></html>""" % (SANS, SERIF, SANS, body)
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    print("归档首页 -> docs/index.html（%d 期）" % len(days), file=sys.stderr)


# ---------------------------------------------------------------- 发信

def send_email(subject, html_body, to_list):
    """优先用 QQ/163 等 SMTP（可发任意收件人），否则回退 Resend。"""
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    if smtp_user and smtp_pass:
        return send_via_smtp(subject, html_body, to_list, smtp_user, smtp_pass)

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("MAIL_FROM", "AI 日报 <onboarding@resend.dev>").strip()
    if not api_key:
        raise SystemExit("缺少 SMTP_USER/SMTP_PASS 或 RESEND_API_KEY，无法发信。可先用 --dry-run 生成 HTML。")
    payload = json.dumps({"from": sender, "to": to_list, "subject": subject, "html": html_body}).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=payload,
                                headers={"Authorization": "Bearer " + api_key,
                                         "Content-Type": "application/json",
                                         "Accept": "application/json",
                                         "User-Agent": UA},  # 不带浏览器 UA 会被 Cloudflare 以 1010 拦截
                                method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            print("已发送 -> %s" % resp.read().decode("utf-8", "replace")[:200])
    except urllib.error.HTTPError as exc:
        raise SystemExit("Resend 返回 %s：%s" % (exc.code, exc.read().decode("utf-8", "replace")[:400]))


def send_via_smtp(subject, html_body, to_list, user, password):
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr

    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:  # 按发件邮箱自动选服务器，无需额外配置
        domain = user.split("@")[-1].lower()
        host = {"gmail.com": "smtp.gmail.com", "googlemail.com": "smtp.gmail.com",
                "qq.com": "smtp.qq.com", "foxmail.com": "smtp.qq.com",
                "163.com": "smtp.163.com", "126.com": "smtp.126.com",
                "outlook.com": "smtp-mail.outlook.com"}.get(domain, "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT") or 465)
    sender_name = os.environ.get("MAIL_FROM_NAME", "AI 日报").strip()

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(sender_name, "utf-8")), user))
    msg["To"] = ", ".join(to_list)

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=45)
    else:
        server = smtplib.SMTP(host, port, timeout=45)
        server.starttls()
    try:
        server.login(user, password)
        server.sendmail(user, to_list, msg.as_string())
    finally:
        server.quit()
    print("已通过 %s 发送给 %s" % (host, ", ".join(to_list)))


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只生成 HTML，不发信")
    parser.add_argument("--to", default="", help="临时覆盖收件人")
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(dt.timezone(dt.timedelta(hours=8)))
    print("抓取中 %s ..." % local.strftime("%Y-%m-%d %H:%M"), file=sys.stderr)
    print("配置自检：LLM_API_KEY=%s（模型 %s，栏目 %s）| SMTP_USER=%s | MAIL_TO=%s | PAGES_BASE=%s"
          % ("已设置" if LLM_KEY else "未设置", LLM_MODEL, ",".join(LLM_SECTIONS) or "无",
             "已设置" if os.environ.get("SMTP_USER") else "未设置",
             "已设置" if os.environ.get("MAIL_TO") else "未设置",
             pages_base() or "未设置"), file=sys.stderr)

    items = fetch_feeds() + fetch_github(now) + fetch_hn(now)
    print("共抓取 %d 条，去重排序中 ..." % len(items), file=sys.stderr)

    buckets = pick(items, now)
    for key, title, _en, need in SECTIONS:
        got = len(buckets.get(key, []))
        print("  %s: %d/%d" % (title, got, need), file=sys.stderr)
    if not any(buckets.values()):
        raise SystemExit("所有来源均无有效内容，今日不发送（可能是网络或源变更）。")

    print("汉化中 ...", file=sys.stderr)
    localize(buckets)

    print("抓取缩略图 ...", file=sys.stderr)
    attach_images(buckets)

    issue_no = local.strftime("%Y%m%d")
    day_dir = build_detail_pages(buckets, local, issue_no)

    page = render_html(buckets, local, issue_no)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, local.strftime("%Y-%m-%d") + ".html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    with open(os.path.join(day_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)  # 供 GitHub Pages 访问的列表页
    write_archive_index(local)
    print("HTML 已生成：%s" % path, file=sys.stderr)
    print("详情页目录：%s" % day_dir, file=sys.stderr)

    subject = "AI 日报 · %s · %d 条精选" % (local.strftime("%m/%d"), sum(len(v) for v in buckets.values()))
    if args.dry_run:
        print("dry-run 完成，未发信。", file=sys.stderr)
        return

    to_list = [x.strip() for x in (args.to or os.environ.get("MAIL_TO", "")).replace(";", ",").split(",") if x.strip()]
    if not to_list:
        raise SystemExit("缺少 MAIL_TO 收件人。")
    send_email(subject, page, to_list)


if __name__ == "__main__":
    main()
