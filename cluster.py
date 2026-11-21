#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cluster.py — 把「同一条新闻上了多个榜」合并成一个事件。

为什么需要
----------
21 个源抓 693 条，实际可能是 400 件事。
不合并的话：
  - 「热度」被重复计数（一条新闻在 5 个榜出现 = 5 倍热度）
  - agent 查一个词，拿到 15 条其实是 5 件事
  - 报告里同一件事排好几个名次

怎么合并
--------
两条标题满足任一条件就算同一事件：

1. **字符相似度 >= 0.55**（2-gram Jaccard）
   抓「特朗普刚返美就发重大预告」这种一字不差的转载

2. **共用 >= 2 个分词后的实词**
   抓各平台改写的情况：
     「余承东纠正发布会发言」/「余承东回应发布会言论」→ 共用 {余承东, 发布会}
     「两京十五日云包场超30万」/「两京十五日今日开播」→ 共用 {两京, 十五日}

   为什么用分词不用 n-gram：「特朗普」会产生 特朗/朗普/特朗普 三个串，
   全算「共用」就过度合并了。分词后只算一个。

传递性用并查集：A~B、B~C 则 A~B~C 是同一个事件。

「最早来源」怎么定
------------------
我们没有新闻发布时间，只有抓取时间。所以：
  - 从 history.jsonl 反查每个标题**第一次被哪一轮抓到**
  - 事件的最早来源 = 最早被抓到的那个平台
  - last_updated = 本轮抓取时间
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent

# 参与「共用词」判断时忽略的泛词。这些词几乎每条新闻都带，共现没有意义。
STOPWORDS = {
    "中国", "美国", "我们", "他们", "今天", "昨日", "最新", "回应", "表示",
    "宣布", "发布", "进行", "已经", "可以", "这个", "那个", "什么", "怎么",
    "为什么", "如何", "多少", "第一", "第二", "突发", "热议", "关注", "网友",
    "新闻", "消息", "视频", "曝光", "官方", "部门", "记者", "报道", "情况",
    "问题", "时候", "现在", "目前", "开始", "结束", "完成", "出现", "发生",
}

# 英文停用词（英文标题走空格分词，共用门槛更高）
EN_STOP = {
    "the","and","for","are","but","not","you","all","can","her","was","one",
    "our","out","day","get","has","him","his","how","its","new","now","old",
    "see","two","way","who","boy","did","she","use","may","say","too","any",
    "from","with","that","this","have","will","your","what","when","where",
    "which","their","there","about","would","could","should","after","before",
    "over","under","into","than","then","them","these","those","been","being",
    "says","said","amid","into","onto","amid","more","most","less","least",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")


def is_cjk(text: str) -> bool:
    """标题里有没有中文。中英文标题永远不合并 —— 不同语言的新闻不会是同一来源。"""
    return bool(_CJK.search(text or ""))


_JIEBA = None


def _tokenizer():
    """优先用 jieba，没有就退回字符 2-gram 兜底。"""
    global _JIEBA
    if _JIEBA is not None:
        return _JIEBA

    vendor = ROOT / "vendor"
    if vendor.is_dir() and str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))

    try:
        import jieba  # type: ignore
        jieba.setLogLevel(60)
        jieba.initialize()
        _JIEBA = jieba
    except Exception:  # noqa: BLE001
        _JIEBA = False
    return _JIEBA


def tokens(title: str) -> set[str]:
    """分词，只留长度 >= 2 的实词，去掉停用词。"""
    t = (title or "").strip()
    if not t:
        return set()

    if not is_cjk(t):
        # 英文：空格/标点分词，只留 >=3 字母的词
        words = {w for w in re.split(r"[^a-z']+", t.lower()) if len(w) >= 3}
        return {w for w in words if w not in EN_STOP and w != "s"}

    jb = _tokenizer()
    if jb:
        words = {w for w in jb.cut(t) if len(w) >= 2}
    else:
        # 兜底：连续的非空白片段按 2-4 字滑窗
        clean = re.sub(r"\s+", "", t)
        words = set()
        for L in (2, 3, 4):
            words |= {clean[i:i + L] for i in range(max(len(clean) - L + 1, 0))}

    out = set()
    for w in words:
        w = w.strip()
        if len(w) < 2 or re.fullmatch(r"[\d\W_]+", w):
            continue
        if w in STOPWORDS:
            continue
        out.add(w)
    return out


def _norm(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (title or "").lower())


def _grams(title: str, n: int = 2) -> set[str]:
    s = _norm(title)
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class _UF:
    """并查集。"""

    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _heat_of(item: dict) -> float:
    h = item.get("hot")
    if isinstance(h, (int, float)) and h > 0:
        return float(h)
    txt = str(item.get("hot_text") or "")
    m = re.search(r"([\d.]+)\s*万", txt)
    if m:
        return float(m.group(1)) * 10000
    m = re.search(r"([\d.]+)\s*亿", txt)
    if m:
        return float(m.group(1)) * 1e8
    m = re.search(r"([\d.]+)", txt)
    if m:
        return float(m.group(1))
    return 0.0


def build_events(
    data: dict[str, Any],
    first_seen: dict[str, dict[str, str]] | None = None,
    jaccard_threshold: float = 0.55,
    min_shared_tokens: int = 2,
) -> list[dict[str, Any]]:
    """把一轮的所有条目聚成事件。

    first_seen: {标题: {"ts": "...", "source": "weibo"}} —— 从 history 反查出来的首见信息
    """
    first_seen = first_seen or {}

    items: list[dict[str, Any]] = []
    for sid, src in data["sources"].items():
        if not src.get("ok"):
            continue
        for it in src.get("items", []):
            items.append({
                "sid": sid,
                "source_name": src.get("name") or sid,
                "region": src.get("region") or "",
                "rank": it.get("rank"),
                "title": it.get("title") or "",
                "zh": it.get("zh") or "",
                "hot": _heat_of(it),
                "hot_text": it.get("hot_text") or "",
                "url": it.get("url") or "",
            })

    if not items:
        return []

    # 预算分词与 2-gram
    for it in items:
        it["_tok"] = tokens(it["title"])
        it["_gram"] = _grams(it["title"])

    uf = _UF(len(items))
    n = len(items)

    # O(n²) 两两比较：693 条约 24 万次，纯 python 几秒内跑完
    for i in range(n):
        ti, gi = items[i]["_tok"], items[i]["_gram"]
        ci = is_cjk(items[i]["title"])
        for j in range(i + 1, n):
            # 中英文标题永不合并
            if ci != is_cjk(items[j]["title"]):
                continue
            if not ti or not items[j]["_tok"]:
                continue

            # 判据 1：字符相似（英文要求更高，因为英文改写幅度大）
            th = jaccard_threshold if ci else 0.70
            if _jaccard(gi, items[j]["_gram"]) >= th:
                uf.union(i, j)
                continue

            # 判据 2：共用实词
            shared = ti & items[j]["_tok"]
            need = min_shared_tokens if ci else min_shared_tokens + 1
            if len(shared) >= need:
                # 中文额外要求：共用词里至少一个长度 >= 3，避免「深圳」「回应」这类泛词凑数
                if ci and not any(len(w) >= 3 for w in shared):
                    continue
                uf.union(i, j)

    # 归组
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    now_ts = data.get("generated_at") or ""

    events: list[dict[str, Any]] = []
    for members in groups.values():
        rows = [items[i] for i in members]

        # 代表标题：热度最高那条的原文（没有中文翻译时用原标题）
        lead = max(rows, key=lambda r: (r["hot"], -len(r["title"])))

        # 每个平台只留最好的一条
        by_platform: dict[str, dict[str, Any]] = {}
        for r in rows:
            cur = by_platform.get(r["sid"])
            if cur is None or (r["rank"] or 999) < (cur["rank"] or 999):
                by_platform[r["sid"]] = r

        sources = []
        for sid, r in by_platform.items():
            fs = first_seen.get(r["title"]) or {}
            sources.append({
                "id": sid,
                "name": r["source_name"],
                "region": r["region"],
                "rank": r["rank"],
                "hot": r["hot_text"] or r["hot"],
                "url": r["url"],
                "first_seen": fs.get("ts") or now_ts,
            })

        # 最早出现的那个平台 = 首发
        sources.sort(key=lambda s: (s["first_seen"], s["rank"] or 999))
        origin = sources[0] if sources else None

        regions = {s["region"] for s in sources if s["region"]}
        events.append({
            "title": lead["zh"] or lead["title"],
            "title_raw": lead["title"],
            "titles": sorted({r["title"] for r in rows if r["title"]}),
            "first_seen": origin["first_seen"] if origin else now_ts,
            "last_updated": now_ts,
            "origin_source": origin["id"] if origin else "",
            "origin_source_name": origin["name"] if origin else "",
            "source_count": len(sources),
            "platforms": [s["id"] for s in sources],
            "region": "both" if len(regions) > 1 else (next(iter(regions)) if regions else ""),
            "aggregate_heat": sum(r["hot"] for r in rows),
            "item_count": len(rows),
            "sources": sources,
        })

    # 热度高的在前
    events.sort(key=lambda e: (-e["source_count"], -e["aggregate_heat"]))
    for k, e in enumerate(events, 1):
        e["event_id"] = f"e{k:03d}"
    return events


def load_first_seen(history_path: pathlib.Path) -> dict[str, dict[str, str]]:
    """从 history.jsonl 反查每个标题第一次出现在哪一轮、哪个平台。"""
    seen: dict[str, dict[str, str]] = {}
    if not history_path.exists():
        return seen
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = row.get("ts") or ""
        for sid, titles in (row.get("titles") or {}).items():
            for t in titles:
                if t and t not in seen:
                    seen[t] = {"ts": ts, "source": sid}
    return seen


if __name__ == "__main__":
    # 自测：拿最近一轮跑一遍，打印合并效果
    out_root = ROOT / "out"
    rounds = sorted((d for d in out_root.iterdir() if d.is_dir()), reverse=True) if out_root.is_dir() else []
    if not rounds:
        print("没有数据，先跑 python3 radar.py")
        raise SystemExit(1)

    data = json.loads((rounds[0] / "trends.json").read_text(encoding="utf-8"))
    fs = load_first_seen(out_root / "history.jsonl")
    ev = build_events(data, fs)

    total_items = sum(len(s.get("items", [])) for s in data["sources"].values() if s.get("ok"))
    print(f"条目 {total_items} → 事件 {len(ev)}")
    print(f"合并率 {100 * (1 - len(ev) / max(total_items, 1)):.1f}%\n")

    multi = [e for e in ev if e["source_count"] >= 2]
    print(f"多平台事件 {len(multi)} 个，前 8：")
    for e in multi[:8]:
        print(f"  [{e['source_count']} 平台] {e['title'][:36]}")
        print(f"            首发 {e['origin_source_name']} @ {e['first_seen'][:16]}")
