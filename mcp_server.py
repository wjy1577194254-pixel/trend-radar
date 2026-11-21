#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trend-radar MCP server — 让 agent 直接查这个雷达。

传输：stdio（JSON-RPC 2.0，换行分隔），零依赖，只用标准库。

启动：
    python3 mcp_server.py

在 Claude Desktop / Cursor / 任意 MCP 客户端里配：
    {
      "mcpServers": {
        "trend-radar": {
          "command": "python3",
          "args": ["/绝对路径/mcp_server.py"]
        }
      }
    }

数据来自 out/ 下最近一轮的 trends.json。没有数据就先跑一次：
    python3 radar.py
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
OUT_ROOT = ROOT / "out"

# ─────────────────────────────────────────────────────────
# 数据读取
# ─────────────────────────────────────────────────────────


def latest_round() -> pathlib.Path | None:
    """最近一轮的目录（按时间戳倒序）。"""
    if not OUT_ROOT.is_dir():
        return None
    rounds = sorted(
        (d for d in OUT_ROOT.iterdir()
         if d.is_dir() and len(d.name) == 15 and d.name[8] == "-"),
        reverse=True,
    )
    return rounds[0] if rounds else None


def load_data() -> dict[str, Any] | None:
    d = latest_round()
    if not d:
        return None
    f = d / "trends.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


HISTORY = OUT_ROOT / "history.jsonl"


def load_history(limit: int = 200) -> list[dict]:
    """读 history.jsonl 最近 N 轮。每行 {"ts": "...", "titles": {sid: [title, ...]}}。"""
    if not HISTORY.exists():
        return []
    lines = HISTORY.read_text(encoding="utf-8").splitlines()
    rows = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _ok_count(data: dict) -> int:
    return sum(1 for s in data["sources"].values() if s.get("ok"))


def _all_items(data: dict):
    """遍历所有条目，附带来源信息。"""
    for sid, src in data["sources"].items():
        if not src.get("ok"):
            continue
        for it in src.get("items", []):
            yield sid, src, it


# ─────────────────────────────────────────────────────────
# 工具实现
# ─────────────────────────────────────────────────────────


def tool_overview(_args: dict) -> dict:
    data = load_data()
    if not data:
        return {"error": "还没有数据。先跑一次 python3 radar.py"}

    a = data["analysis"]
    total = sum(len(s.get("items", [])) for s in data["sources"].values() if s.get("ok"))
    cn = sum(len(s.get("items", [])) for s in data["sources"].values()
             if s.get("ok") and s.get("region") == "cn")
    return {
        "generated_at": data["generated_at"],
        "elapsed_seconds": data["elapsed"],
        "sources_online": f"{_ok_count(data)}/{len(data['sources'])}",
        "items_total": total,
        "items_cn": cn,
        "items_intl": total - cn,
        "top_cn": [{"term": w, "n": n} for w, n in a["cn_keywords"][:12]],
        "top_intl": [{"term": w, "n": n} for w, n in a["intl_keywords"][:12]],
        "consensus_count": len(a["consensus"]),
        "topics_cn": a["topics"]["cn"],
        "topics_intl": a["topics"]["intl"],
    }


def tool_search(args: dict) -> dict:
    data = load_data()
    if not data:
        return {"error": "还没有数据。先跑一次 python3 radar.py"}

    q = (args.get("term") or "").strip().lower()
    if not q:
        return {"error": "term 不能为空"}

    hits: list[dict] = []
    for sid, src, it in _all_items(data):
        blob = (it.get("title") or "") + " " + (it.get("zh") or "") + " " + \
               str((it.get("meta") or {}).get("desc") or "")
        if q in blob.lower():
            hits.append({
                "source": src["name"],
                "source_id": sid,
                "region": src.get("region"),
                "rank": it.get("rank"),
                "title": it.get("title"),
                "zh": it.get("zh") or "",
                "hot": it.get("hot_text") or it.get("hot"),
                "url": it.get("url"),
            })

    hits.sort(key=lambda h: (h["rank"] or 999))
    platforms = sorted({h["source_id"] for h in hits})
    return {
        "term": args.get("term"),
        "hit_count": len(hits),
        "platform_count": len(platforms),
        "platforms": platforms,
        "is_consensus": len(platforms) >= 2,
        "items": hits[: args.get("limit") or 20],
    }


def tool_consensus(args: dict) -> dict:
    data = load_data()
    if not data:
        return {"error": "还没有数据。先跑一次 python3 radar.py"}

    c = data["analysis"]["consensus"]
    min_plat = args.get("min_platforms") or 2
    rows = [
        {"term": x["word"], "platform_count": x["n"], "platforms": x["platforms"]}
        for x in c if x["n"] >= min_plat
    ]
    rows.sort(key=lambda r: -r["platform_count"])
    return {
        "definition": "同时出现在 >=2 个平台的词。只在一个平台出现的大多是平台内部梗。",
        "count": len(rows),
        "items": rows[: args.get("limit") or 25],
    }


def tool_sources(_args: dict) -> dict:
    data = load_data()
    if not data:
        return {"error": "还没有数据。先跑一次 python3 radar.py"}

    rows = []
    for sid, s in data["sources"].items():
        rows.append({
            "id": sid,
            "name": s.get("name"),
            "region": s.get("region"),
            "category": s.get("category"),
            "ok": bool(s.get("ok")),
            "items": len(s.get("items", [])),
            "elapsed": s.get("elapsed"),
            "error": s.get("error") or "",
        })
    return {"count": len(rows), "sources": rows}


def tool_source_items(args: dict) -> dict:
    data = load_data()
    if not data:
        return {"error": "还没有数据。先跑一次 python3 radar.py"}

    sid = (args.get("source") or "").strip()
    s = data["sources"].get(sid)
    if not s:
        return {"error": f"没有这个源：{sid}", "available": list(data["sources"].keys())}

    limit = args.get("limit") or 30
    items = [
        {
            "rank": it.get("rank"),
            "title": it.get("title"),
            "zh": it.get("zh") or "",
            "note": it.get("note") or "",
            "hot": it.get("hot_text") or it.get("hot"),
            "url": it.get("url"),
        }
        for it in s.get("items", [])[:limit]
    ]
    return {
        "source": s.get("name"),
        "region": s.get("region"),
        "total": len(s.get("items", [])),
        "items": items,
    }


def tool_topics(_args: dict) -> dict:
    data = load_data()
    if not data:
        return {"error": "还没有数据。先跑一次 python3 radar.py"}

    t = data["analysis"]["topics"]
    cn_total = sum(t["cn"].values()) or 1
    intl_total = sum(t["intl"].values()) or 1
    keys = sorted(set(t["cn"]) | set(t["intl"]))
    rows = []
    for k in keys:
        c = t["cn"].get(k, 0)
        i = t["intl"].get(k, 0)
        rows.append({
            "topic": k,
            "cn": c, "cn_pct": round(c / cn_total * 100, 1),
            "intl": i, "intl_pct": round(i / intl_total * 100, 1),
        })
    rows.sort(key=lambda r: -(r["cn"] + r["intl"]))
    return {"topics": rows}


def tool_term_history(args: dict) -> dict:
    """一个词在各轮抓取中的出现轨迹 —— 用来区分真趋势和一次性上榜。"""
    term = (args.get("term") or "").strip()
    if not term:
        return {"error": "term 不能为空"}

    rows = load_history(args.get("rounds") or 60)
    if not rows:
        return {"error": "还没有历史数据。history.jsonl 需要至少两轮抓取才会出现。"}

    q = term.lower()
    series = []
    for row in rows:
        titles = row.get("titles") or {}
        per_platform = {}
        for sid, lst in titles.items():
            hits = [(i + 1, t) for i, t in enumerate(lst) if q in (t or "").lower()]
            if hits:
                per_platform[sid] = {
                    "count": len(hits),
                    "best_rank": min(r for r, _ in hits),
                    "top_title": hits[0][1],
                }
        series.append({
            "ts": row.get("ts", ""),
            "platforms": len(per_platform),
            "mentions": sum(v["count"] for v in per_platform.values()),
            "best_rank": min((v["best_rank"] for v in per_platform.values()), default=None),
            "detail": per_platform,
        })

    appeared = [s for s in series if s["platforms"] > 0]
    if not appeared:
        return {
            "term": term,
            "rounds_analyzed": len(series),
            "appearances": 0,
            "appearance_rate": 0.0,
            "avg_platforms": 0.0,
            "verdict": "never_seen",
            "verdict_meaning": "从未出现",
            "series": [],
        }

    rate = len(appeared) / len(series)
    half = max(len(series) // 2, 1)
    early = sum(1 for s in series[:half] if s["platforms"] > 0)
    late = sum(1 for s in series[half:] if s["platforms"] > 0)
    early_rate = early / half
    late_rate = late / max(len(series) - half, 1)

    if rate >= 0.7:
        verdict = "persistent"
    elif late_rate > early_rate * 1.5:
        verdict = "rising"
    elif early_rate > late_rate * 1.5:
        verdict = "fading"
    else:
        verdict = "sporadic"

    return {
        "term": term,
        "rounds_analyzed": len(series),
        "appearances": len(appeared),
        "appearance_rate": round(rate, 2),
        "avg_platforms": round(sum(s["platforms"] for s in appeared) / len(appeared), 1),
        "verdict": verdict,
        "verdict_meaning": {
            "persistent": "长期在榜，已经形成稳定关注",
            "rising": "近期明显升温",
            "fading": "在退潮",
            "sporadic": "零星出现，可能是偶发话题",
            "never_seen": "从未出现",
        }.get(verdict, ""),
        "series": [s for s in series if s["platforms"] > 0][-20:],
    }


TOOLS = [
    {
        "name": "trending_overview",
        "description": "当前这一轮的全局视图：什么时候抓的、多少个源在线、总条目数、"
                       "国内/国外的高频词 top12、主题分布、跨平台共识词数量。"
                       "想知道「现在大家在关心什么」，从这里开始。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_term",
        "description": "查一个词在各平台的表现：哪些平台提到了、在第几名、热度多少。"
                       "返回 platform_count —— 如果 >=2，说明这是跨平台发酵的话题，不是单个平台的内部梗。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "要查的词，中英文都行，大小写不敏感"},
                "limit": {"type": "integer", "description": "最多返回几条，默认 20"},
            },
            "required": ["term"],
        },
    },
    {
        "name": "consensus_terms",
        "description": "跨平台共识词排行。这些是真正跨圈层发酵的话题 —— "
                       "同时出现在 2 个以上平台的词。比单个平台的热榜更可信。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_platforms": {"type": "integer", "description": "最少几个平台，默认 2"},
                "limit": {"type": "integer", "description": "返回条数，默认 25"},
            },
        },
    },
    {
        "name": "list_sources",
        "description": "列出所有数据源及其状态：哪些在线、各抓到多少条、耗时多少、有没有报错。"
                       "排查「为什么某个平台没数据」时用它。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "source_items",
        "description": "拿某一个源的完整榜单。source 用 list_sources 里的 id（如 weibo、hn、arxiv）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源的 id，如 weibo / douyin / hn / arxiv"},
                "limit": {"type": "integer", "description": "返回条数，默认 30"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "term_history",
        "description": "一个词在历史各轮抓取中的出现轨迹。用来区分「真趋势」和「今天刚好上了榜」——"
                       "返回每轮出现在几个平台、最低排名、以及 rising/persistent/fading/sporadic 的判断。"
                       "问「这东西火多久了」「是不是在退潮」时用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "要查的词，中英文都行"},
                "rounds": {"type": "integer", "description": "回看多少轮，默认 60"},
            },
            "required": ["term"],
        },
    },
    {
        "name": "topic_breakdown",
        "description": "主题分布对照：国内 vs 国外在各主题（政治军事/科技AI/娱乐明星…）的占比。"
                       "用来判断「两边关注的东西差在哪」。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "trending_overview": tool_overview,
    "search_term": tool_search,
    "consensus_terms": tool_consensus,
    "list_sources": tool_sources,
    "source_items": tool_source_items,
    "topic_breakdown": tool_topics,
    "term_history": tool_term_history,
}


# ─────────────────────────────────────────────────────────
# JSON-RPC over stdio
# ─────────────────────────────────────────────────────────


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "trend-radar", "version": "1.0.0"},
            },
        }

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        try:
            payload = fn(args)
        except Exception as exc:  # noqa: BLE001
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "content": [{"type": "text",
                             "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "isError": "error" in payload,
            },
        }

    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"unknown method: {method}"}}
    return None


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()
