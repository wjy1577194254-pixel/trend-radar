#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sources_ai.py — AI 前沿与免费额度采集

覆盖用户实际在用的三条线：AI 建站、AI 视频、AI 建模，外加通用的模型/论文动向。

六个源：
  hf_models   HuggingFace 趋势模型（谁在开源什么）
  hf_spaces   HF Spaces 热门（什么 AI 应用在火，能直接点开用）
  arxiv       arXiv cs.AI 最新论文（前沿）
  gh_ai       GitHub 本月新增 AI 仓库（新工具都从这里冒出来）
  free_llm    OpenRouter 免费模型（真羊毛：0 成本 API）
  free_dev    free-for.dev 里的 AI 免费额度（云服务/推理/存储）

每个都只读公开接口，不需要任何 key。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from sources import Item, fetch, fetch_json, human_num

# 用来判定"跟我们三条线有关"的关键词
SITE_KW = ("website", "site", "web app", "landing", "frontend", "ui", "html", "css",
           "v0", "lovable", "bolt", "cursor", "replit", "webflow", "framer", "建站", "网页")
VIDEO_KW = ("video", "animation", "motion", "film", "clip", "sora", "veo", "kling",
            "seedance", "runway", "pika", "heygen", "luma", "wan", "视频", "动画")
MODEL3D_KW = ("3d", "mesh", "model", "gaussian", "splat", "tripo", "meshy", "rodin",
              "hunyuan3d", "sloyd", "csm", "text-to-3d", "重建", "建模", "渲染")


def _tag(text: str) -> str:
    """给一条 AI 内容打用途标签。"""
    t = (text or "").lower()
    hits = []
    if any(k in t for k in SITE_KW):
        hits.append("建站")
    if any(k in t for k in VIDEO_KW):
        hits.append("视频")
    if any(k in t for k in MODEL3D_KW):
        hits.append("3D")
    return "/".join(hits) or "通用"


# ---------------------------------------------------------------- HuggingFace


def hf_models() -> list[Item]:
    """HF 趋势模型：开源侧正在被下载什么。"""
    d = fetch_json(
        "https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=40"
    )
    out: list[Item] = []
    for m in d:
        mid = m.get("id") or ""
        if not mid:
            continue
        pipe = m.get("pipelineTag") or m.get("pipeline_tag") or ""
        likes = m.get("likes") or 0
        dl = m.get("downloads") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=mid,
                hot=float(m.get("trendingScore") or likes or 0),
                hot_text=f"♥{human_num(likes)} · ↓{human_num(dl)}",
                url=f"https://huggingface.co/{mid}",
                meta={"task": pipe, "tag": _tag(mid + " " + pipe)},
            )
        )
    return out


def hf_spaces() -> list[Item]:
    """HF Spaces 热门：能直接开箱用的 AI 应用。"""
    d = fetch_json(
        "https://huggingface.co/api/spaces?sort=likes&direction=-1&limit=25"
    )
    out: list[Item] = []
    for s in d:
        sid = s.get("id") or ""
        if not sid:
            continue
        likes = s.get("likes") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=sid,
                hot=float(likes),
                hot_text=f"♥{human_num(likes)}",
                url=f"https://huggingface.co/spaces/{sid}",
                meta={"sdk": s.get("sdk") or "", "tag": _tag(sid)},
            )
        )
    return out


# ---------------------------------------------------------------- arXiv


def arxiv_ai() -> list[Item]:
    """arXiv cs.AI 最新投稿。"""
    xml = fetch(
        "https://export.arxiv.org/api/query?"
        "search_query=cat:cs.AI+OR+cat:cs.CV+OR+cat:cs.GR"
        "&sortBy=submittedDate&sortOrder=descending&max_results=30",
        retries=2,
        timeout=35,
    )
    out: list[Item] = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml)
    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", "", ns) or "").strip().replace("\n", " ")
        title = re.sub(r"\s+", " ", title)
        if not title:
            continue
        link = entry.findtext("a:id", "", ns)
        published = (entry.findtext("a:published", "", ns) or "")[:10]
        out.append(
            Item(
                rank=len(out) + 1,
                title=title[:160],
                hot=0,
                hot_text=published,
                url=link,
                meta={"tag": _tag(title)},
            )
        )
    return out


# ---------------------------------------------------------------- GitHub


def github_ai() -> list[Item]:
    """GitHub 本月新建的 AI 仓库，按 star 排。新工具通常从这里冒头。"""
    from time import strftime, gmtime, time as _time

    since = strftime("%Y-%m-%d", gmtime(_time() - 30 * 86400))
    d = fetch_json(
        "https://api.github.com/search/repositories"
        f"?q=topic:ai+created:>{since}&sort=stars&order=desc&per_page=30",
        headers={"Accept": "application/vnd.github+json"},
    )
    out: list[Item] = []
    for r in d.get("items", []):
        name = r.get("full_name") or ""
        if not name:
            continue
        stars = r.get("stargazers_count") or 0
        desc = (r.get("description") or "").strip()
        out.append(
            Item(
                rank=len(out) + 1,
                title=name,
                hot=float(stars),
                hot_text=f"★{human_num(stars)}",
                url=r.get("html_url") or "",
                meta={"desc": desc[:120], "tag": _tag(name + " " + desc)},
            )
        )
    return out


# ---------------------------------------------------------------- 羊毛


def free_llm() -> list[Item]:
    """
    OpenRouter 里 prompt 和 completion 都为 0 的模型 —— 真·免费 API。
    这是最实在的羊毛：不用注册一堆平台，换个 model id 就能白嫖。
    """
    d = fetch_json("https://openrouter.ai/api/v1/models")
    out: list[Item] = []
    for m in d.get("data", []):
        pr = m.get("pricing") or {}
        try:
            if float(pr.get("prompt", "1")) != 0 or float(pr.get("completion", "1")) != 0:
                continue
        except (TypeError, ValueError):
            continue
        mid = m.get("id") or ""
        if not mid:
            continue
        ctx = m.get("context_length") or 0
        out.append(
            Item(
                rank=len(out) + 1,
                title=mid,
                hot=float(ctx),
                hot_text=f"{human_num(ctx)} ctx · FREE",
                url=f"https://openrouter.ai/models/{mid}",
                meta={"kind": "free-llm"},
            )
        )
    out.sort(key=lambda i: -i.hot)
    for i, it in enumerate(out, 1):
        it.rank = i
    return out[:40]


FREE_HINTS = ("ai", "llm", "gpt", "machine learning", "stable diffusion", "text-to-image",
              "text-to-speech", "transcription", "computer vision", "embedding", "vector",
              "gpu", "inference", "model", "ocr")


def free_dev() -> list[Item]:
    """
    free-for.dev 里的 AI 相关免费额度。
    这个 README 每个月都在更新，是"能白嫖什么"最全的一张表。
    """
    md = fetch(
        "https://raw.githubusercontent.com/ripienaar/free-for-dev/master/README.md",
        retries=1,
    ).decode("utf-8", "replace")

    out: list[Item] = []
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith(("*", "-")):
            continue
        m = re.match(r"[*-]\s*\[([^\]]+)\]\(([^)]+)\)\s*[-–—:]\s*(.+)", line)
        if not m:
            continue
        name, url, desc = m.group(1), m.group(2), m.group(3).strip()
        blob = (name + " " + desc).lower()
        if not any(h in blob for h in FREE_HINTS):
            continue
        # 去掉 markdown 的粗体标记
        desc = re.sub(r"[*`]", "", desc)[:110]
        out.append(
            Item(
                rank=len(out) + 1,
                title=name,
                hot=0,
                hot_text="免费额度",
                url=url,
                meta={"desc": desc, "kind": "free-service"},
            )
        )
    return out[:40]
