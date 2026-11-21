#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sources.py — 14 个公开热榜/趋势数据源的采集器（纯标准库，零依赖）

每个采集器签名：  def xxx() -> list[Item]
失败直接抛异常，由 radar.py 的调度器捕获并记录，不影响其它源。

维护须知
--------
* 所有站点都会改版。改版后先跑 `python3 radar.py --check weibo` 单源调试，
  打印原始响应，再回来改解析。不要把解析逻辑散到 main 里。
* 只打公开接口，不带登录态、不模拟点击、不破解签名。
  需要签名或登录的源（X/Twitter、TikTok、小红书、Reddit）职责明确地留空，
  在 SOURCES 里标 available=False，让报告诚实显示"这条路要 API key"。
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------- 数据结构


@dataclass
class Item:
    """榜单里的一条。"""

    rank: int
    title: str
    hot: float = 0.0          # 归一化用的数值（浏览量/热度/投票数），拿不到就是 0
    hot_text: str = ""        # 给人看的原始热度（"112.3万" "1,353 stars"）
    url: str = ""
    meta: dict = field(default_factory=dict)


def human_num(n) -> str:
    """1123279 -> 112.3万"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 1e8:
        return f"{n / 1e8:.1f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return f"{int(n)}"


# ---------------------------------------------------------------- 网络


def fetch(
    url: str,
    *,
    referer: str | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 2,
    data: bytes | None = None,
) -> bytes:
    """GET 一个 URL，自动解 gzip，失败重试。"""
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
    }
    if referer:
        hdrs["Referer"] = referer
    if headers:
        hdrs.update(headers)

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
            last = exc
            if attempt < retries:
                time.sleep(0.7 * (attempt + 1))
    raise last  # type: ignore[misc]


def fetch_json(url: str, **kw) -> dict:
    return json.loads(fetch(url, **kw).decode("utf-8", "replace"))


def _rss_items(xml_bytes: bytes):
    """通用 RSS 解析：返回 [(title, link, extra_dict), ...]"""
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iter("item"):
        def _text(tag_part: str) -> str:
            for child in item:
                if tag_part in child.tag:
                    return (child.text or "").strip()
            return ""

        title = _text("title")
        if not title:
            continue
        out.append((title, _text("link"), {"traffic": _text("approx_traffic")}))
    return out


# ================================================================ 国内


def weibo() -> list[Item]:
    """微博实时热搜。"""
    d = fetch_json(
        "https://weibo.com/ajax/side/hotSearch", referer="https://weibo.com/"
    )
    out: list[Item] = []
    for it in d.get("data", {}).get("realtime", []):
        if it.get("is_ad"):
            continue
        word = (it.get("word") or it.get("note") or "").strip()
        if not word:
            continue
        num = it.get("num") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=word,
                hot=float(num),
                hot_text=human_num(num),
                url="https://s.weibo.com/weibo?q=" + urllib.parse.quote(f"#{word}#"),
                meta={"label": it.get("label_name") or ""},
            )
        )
    return out


def baidu() -> list[Item]:
    """百度热搜榜（realtime 卡）。"""
    d = fetch_json(
        "https://top.baidu.com/api/board?platform=wise&tab=realtime",
        referer="https://top.baidu.com/",
    )
    out: list[Item] = []
    cards = d.get("data", {}).get("cards", [])
    for card in cards:
        # 两种结构：content 直接是条目数组，或者外层再包一层 content
        for block in card.get("content", []):
            rows = block.get("content") if isinstance(block, dict) and "content" in block else [block]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                word = (row.get("word") or row.get("query") or "").strip()
                if not word:
                    continue
                hot = row.get("hotScore") or 0
                out.append(
                    Item(
                        rank=len(out) + 1,
                        title=word,
                        hot=float(hot),
                        hot_text=human_num(hot),
                        url=row.get("url") or ("https://www.baidu.com/s?wd=" + urllib.parse.quote(word)),
                        meta={"is_top": bool(row.get("isTop"))},
                    )
                )
    return out


_BILI_COOKIE: str = ""


def _bili_cookie() -> str:
    """B 站 API 要 buvid3 才不返回 -352；访客 cookie 从首页拿一次就够。"""
    global _BILI_COOKIE
    if _BILI_COOKIE:
        return _BILI_COOKIE
    try:
        req = urllib.request.Request(
            "https://www.bilibili.com/",
            headers={"User-Agent": UA, "Accept": "text/html"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            pairs = []
            for raw in resp.headers.get_all("Set-Cookie") or []:
                head = raw.split(";")[0].strip()
                if head.startswith(("buvid3=", "buvid4=", "b_nut=", "buvid_fp=")):
                    pairs.append(head)
            _BILI_COOKIE = "; ".join(pairs)
    except Exception:  # noqa: BLE001 - 拿不到就空着，下面还有一次重试
        _BILI_COOKIE = ""
    return _BILI_COOKIE


def bilibili() -> list[Item]:
    """B 站全站排行榜。"""
    cookie = _bili_cookie()
    hdrs = {"Origin": "https://www.bilibili.com"}
    if cookie:
        hdrs["Cookie"] = cookie
    d = fetch_json(
        "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
        referer="https://www.bilibili.com/",
        headers=hdrs,
    )
    if d.get("code") == -352 and cookie:
        _BILI_COOKIE = ""          # cookie 失效，下次重新取
        raise RuntimeError("bili -352 风控：访客 cookie 被拒（稍后重试或降低频率）")
    if d.get("code") not in (0, None):
        raise RuntimeError(f"bili code={d.get('code')} {d.get('message', '')}")
    out: list[Item] = []
    for v in d.get("data", {}).get("list", []):
        title = (v.get("title") or "").strip()
        if not title:
            continue
        stat = v.get("stat") or {}
        view = stat.get("view") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                hot=float(view),
                hot_text=f"{human_num(view)}播放",
                url=f"https://www.bilibili.com/video/{v.get('bvid', '')}",
                meta={
                    "tname": v.get("tname") or "",
                    "up": (v.get("owner") or {}).get("name") or "",
                    "like": stat.get("like") or 0,
                },
            )
        )
    return out


def douyin() -> list[Item]:
    """抖音热榜。"""
    d = fetch_json(
        "https://www.douyin.com/aweme/v1/web/hot/search/list/",
        referer="https://www.douyin.com/",
    )
    words = (d.get("data") or {}).get("word_list") or []
    words = sorted(words, key=lambda x: x.get("position") or 999)
    out: list[Item] = []
    for w in words:
        title = (w.get("word") or "").strip()
        if not title:
            continue
        hot = w.get("hot_value") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                hot=float(hot),
                hot_text=human_num(hot),
                url=f"https://www.douyin.com/hot/{w.get('sentence_id', '')}",
                meta={"video_count": w.get("video_count") or 0},
            )
        )
    return out


def toutiao() -> list[Item]:
    """今日头条热榜。"""
    d = fetch_json(
        "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        referer="https://www.toutiao.com/",
    )
    out: list[Item] = []
    for it in d.get("data", []):
        title = (it.get("Title") or "").strip()
        if not title:
            continue
        hot = it.get("HotValue") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                hot=float(hot),
                hot_text=human_num(hot),
                url=it.get("Url") or "",
                meta={"label": it.get("Label") or ""},
            )
        )
    return out


def tieba() -> list[Item]:
    """百度贴吧热议话题。"""
    d = fetch_json(
        "https://tieba.baidu.com/hottopic/browse/topicList",
        referer="https://tieba.baidu.com/",
    )
    topics = (
        (d.get("data") or {}).get("bang_topic", {}).get("topic_list") or []
    )
    out: list[Item] = []
    for t in topics:
        title = (t.get("topic_name") or "").strip()
        if not title:
            continue
        hot = t.get("topic_hot") or t.get("discuss_num") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                hot=float(hot),
                hot_text=human_num(hot),
                url=f"https://tieba.baidu.com/hottopic/browse/hottopic?topic_id={t.get('topic_id', '')}",
                meta={"desc": (t.get("topic_desc") or "")[:120]},
            )
        )
    return out


def zhihu() -> list[Item]:
    """知乎搜索热词（公开 top_search 接口，只有 ~10 条，但无需签名）。"""
    d = fetch_json(
        "https://www.zhihu.com/api/v4/search/top_search",
        referer="https://www.zhihu.com/",
    )
    words = (d.get("top_search") or {}).get("words") or []
    out: list[Item] = []
    for w in words:
        title = (w.get("display_query") or w.get("query") or "").strip()
        if not title:
            continue
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                url="https://www.zhihu.com/search?q=" + urllib.parse.quote(title),
            )
        )
    return out


def douban() -> list[Item]:
    """豆瓣电影「实时热门」。看大家在追什么片。"""
    d = fetch_json(
        "https://m.douban.com/rexxar/api/v2/subject_collection/"
        "movie_real_time_hotest/items?start=0&count=20",
        referer="https://m.douban.com/",
    )
    out: list[Item] = []
    for it in d.get("subject_collection_items", []):
        title = (it.get("title") or "").strip()
        if not title:
            continue
        rating = (it.get("rating") or {}).get("value") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                hot=float(rating) * (it.get("rating", {}).get("count") or 1),
                hot_text=f"{rating}分" if rating else "",
                url=f"https://movie.douban.com/subject/{it.get('id', '')}/",
                meta={"subtitle": (it.get("card_subtitle") or "")[:100]},
            )
        )
    return out


# ================================================================ 国外


def hackernews() -> list[Item]:
    """Hacker News 首页（Algolia 镜像，官方 API 的只读封装）。"""
    d = fetch_json(
        "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50"
    )
    out: list[Item] = []
    for h in d.get("hits", []):
        title = (h.get("title") or h.get("story_title") or "").strip()
        if not title:
            continue
        pts = h.get("points") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                hot=float(pts),
                hot_text=f"{pts} pts",
                url=h.get("url")
                or f"https://news.ycombinator.com/item?id={h.get('objectID', '')}",
                meta={
                    "comments": h.get("num_comments") or 0,
                    "hn": f"https://news.ycombinator.com/item?id={h.get('objectID', '')}",
                },
            )
        )
    return out


def google_trends() -> list[Item]:
    """Google Trends 每日热搜（RSS，免 cookie）。geo 可改 US/GB/JP…"""
    xml = fetch("https://trends.google.com/trending/rss?geo=US", retries=1)
    out: list[Item] = []
    for title, link, extra in _rss_items(xml):
        traffic = extra.get("traffic") or ""
        num = 0.0
        m = re.match(r"([\d,.]+)([KMB]?)", traffic.replace("+", ""))
        if m:
            num = float(m.group(1).replace(",", ""))
            num *= {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                hot=num,
                hot_text=traffic,
                url=link
                or ("https://trends.google.com/trends/explore?q=" + urllib.parse.quote(title)),
            )
        )
    return out


def google_news() -> list[Item]:
    """Google News 头条（英文区）。代表西方媒体的议程。"""
    xml = fetch("https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en")
    out: list[Item] = []
    for title, link, _ in _rss_items(xml)[:40]:
        title = title.split(" - ")[0].strip()
        out.append(Item(rank=len(out) + 1, title=title, url=link))
    return out


def nytimes() -> list[Item]:
    """纽约时报首页 RSS。"""
    xml = fetch("https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml")
    out: list[Item] = []
    for title, link, _ in _rss_items(xml)[:40]:
        out.append(Item(rank=len(out) + 1, title=title.strip(), url=link))
    return out


def github_trending() -> list[Item]:
    """GitHub 每日 Trending（技术圈注意力）。"""
    html = fetch("https://github.com/trending?since=daily").decode("utf-8", "replace")
    blocks = re.findall(r'<article class="Box-row".*?</article>', html, re.S)
    out: list[Item] = []
    for b in blocks:
        m = re.search(r'<a[^>]+href="/([\w.\-]+/[\w.\-]+)"', b)
        if not m:
            continue
        repo = m.group(1)
        if repo.split("/")[0] in {"sponsors", "apps", "trending", "features", "topics"}:
            continue
        desc = re.search(r'<p class="col-9[^"]*"[^>]*>\s*(.*?)\s*</p>', b, re.S)
        stars = re.search(r"([\d,]+)\s*stars?\s*today", b)
        n = int(stars.group(1).replace(",", "")) if stars else 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=repo,
                hot=float(n),
                hot_text=f"+{stars.group(1)} stars/day" if stars else "",
                url=f"https://github.com/{repo}",
                meta={
                    "desc": re.sub(r"<[^>]+>", " ", desc.group(1)).strip()[:140] if desc else "",
                },
            )
        )
    return out


def mastodon() -> list[Item]:
    """Mastodon（mastodon.social）当下热门标签。联邦宇宙的风向。"""
    d = fetch_json("https://mastodon.social/api/v1/trends/tags?limit=20")
    out: list[Item] = []
    for t in d:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        history = t.get("history") or []
        uses = int(history[0].get("uses") or 0) if history else 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=name,
                hot=float(uses),
                hot_text=f"{uses} 次",
                url=t.get("url") or "",
            )
        )
    return out


# ================================================================ 源登记表


@dataclass
class SourceDef:
    id: str
    name: str
    region: str          # cn | intl
    category: str        # social|video|news|tech|movie
    fn: object = None
    available: bool = True
    heavy: bool = False  # 重源：要开浏览器（10s 级），默认不参与全量
    note: str = ""       # 不可用时的原因，直接显示在报告里


SOURCES: dict[str, SourceDef] = {
    "weibo":      SourceDef("weibo", "微博热搜", "cn", "social", weibo),
    "baidu":      SourceDef("baidu", "百度热搜", "cn", "social", baidu),
    "douyin":     SourceDef("douyin", "抖音热榜", "cn", "video", douyin),
    "toutiao":    SourceDef("toutiao", "头条热榜", "cn", "news", toutiao),
    "bilibili":   SourceDef("bilibili", "B站排行", "cn", "video", bilibili),
    "tieba":      SourceDef("tieba", "贴吧热议", "cn", "social", tieba),
    "zhihu":      SourceDef("zhihu", "知乎热词", "cn", "social", zhihu),
    "douban":     SourceDef("douban", "豆瓣热门电影", "cn", "movie", douban),
    "hn":         SourceDef("hn", "Hacker News", "intl", "tech", hackernews),
    "gtrends":    SourceDef("gtrends", "Google Trends", "intl", "social", google_trends),
    "gnews":      SourceDef("gnews", "Google News", "intl", "news", google_news),
    "nytimes":    SourceDef("nytimes", "NY Times", "intl", "news", nytimes),
    "github":     SourceDef("github", "GitHub Trending", "intl", "tech", github_trending),
    "mastodon":   SourceDef("mastodon", "Mastodon 热门", "intl", "social", mastodon),
    # ---- 明确不可用，保留位置让报告诚实 ----
    "x": SourceDef("x", "X / Twitter", "intl", "social", None,
                   available=False, note="官方 API 要付费 Bearer Token；网页端要登录+反爬"),
    "reddit": SourceDef("reddit", "Reddit", "intl", "social", None,
                        available=False, note="本机 IP 被 Reddit 403 封；换住宅 IP 或 OAuth 可解"),
    "tiktok": SourceDef("tiktok", "TikTok", "intl", "video", None,
                        available=False, note="Creative Center 接口要 cookie + 签名，当前无权限"),
    # 需要登录态 / 需要 API key 的两个，走 sources_browser
    "xiaohongshu": SourceDef("xiaohongshu", "小红书", "cn", "social", None,
                             available=True, heavy=True,
                             note="登录态下是【个性化推荐流】，反映算法对这个账号的画像，不是大众榜"),
    "youtube": SourceDef("youtube", "YouTube 趋势", "intl", "video", None,
                         available=bool(os.environ.get("YT_API_KEY")),
                         heavy=True,
                         note="需要 YT_API_KEY 才能启用（export YT_API_KEY=... 后自动出现）"),
}


def run_one(sid: str) -> dict:
    """跑单个源，永不抛异常，返回统一结构。"""
    sd = SOURCES[sid]
    started = time.time()
    base = {
        "id": sd.id,
        "name": sd.name,
        "region": sd.region,
        "category": sd.category,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "caveat": sd.note if sd.available else "",
    }
    if not sd.available or sd.fn is None:
        return {**base, "ok": False, "error": sd.note, "elapsed": 0.0, "items": []}
    try:
        items = sd.fn()  # type: ignore[operator]
        if not items:
            raise RuntimeError("返回空列表（可能被风控、限流或接口改版）")
        return {
            **base,
            "ok": True,
            "error": "",
            "elapsed": round(time.time() - started, 2),
            "items": [it.__dict__ for it in items],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed": round(time.time() - started, 2),
            "items": [],
        }


# ---------------------------------------------------------------- AI 前沿 / 羊毛


def _ai_entry(name: str):
    def _fn():
        import importlib
        mod = importlib.import_module("sources_ai")
        return getattr(mod, name)()

    return _fn


SOURCES.update({
    "hf_models": SourceDef("hf_models", "HF 趋势模型", "intl", "ai", _ai_entry("hf_models")),
    "hf_spaces": SourceDef("hf_spaces", "HF Spaces", "intl", "ai", _ai_entry("hf_spaces")),
    "arxiv":     SourceDef("arxiv", "arXiv 前沿论文", "intl", "ai", _ai_entry("arxiv_ai")),
    "gh_ai":     SourceDef("gh_ai", "GitHub 新 AI 项目", "intl", "ai", _ai_entry("github_ai")),
    "free_llm":  SourceDef("free_llm", "免费 LLM API", "intl", "free", _ai_entry("free_llm")),
    "free_dev":  SourceDef("free_dev", "AI 免费额度", "intl", "free", _ai_entry("free_dev")),
})


# ---------------------------------------------------------------- 重源入口
# 放在文件末尾、用延迟导入，避免与 sources_browser 循环 import


def _browser_entry(name: str):
    def _fn():
        import importlib
        mod = importlib.import_module("sources_browser")
        return getattr(mod, name)()

    return _fn


def youtube_entry():
    """有 API key 走 API（1 秒），没有就退回浏览器渲染（10 秒，需登录）。"""
    import sources_browser as sb

    try:
        return sb.youtube_api()
    except Exception as api_err:  # noqa: BLE001
        try:
            return sb.youtube_browser()
        except Exception as web_err:  # noqa: BLE001
            raise RuntimeError(f"API 路线: {api_err}；浏览器路线: {web_err}") from None


SOURCES["xiaohongshu"].fn = _browser_entry("xiaohongshu")
SOURCES["youtube"].fn = youtube_entry
