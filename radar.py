#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radar.py — 国内外社交平台「在关心什么」雷达

抓一批公开热榜 → 归一化 → 算词频 / 主题分布 / 跨平台共识 → 出 JSON + CSV + HTML 报告。

用法
----
  python3 radar.py                          # 全量抓一次
  python3 radar.py --region cn              # 只看国内
  python3 radar.py --only weibo,hn,gtrends  # 指定源
  python3 radar.py --interval 600 --rounds 12   # 每 10 分钟一轮，共 12 轮（跑 2 小时看变化）
  python3 radar.py --check weibo            # 单源调试：打印原始响应样本
  python3 radar.py --list                   # 列出所有源
  python3 radar.py --open                   # 抓完直接打开报告

零依赖，纯标准库。
"""

from __future__ import annotations

import envload  # noqa: F401  载入 .env
import argparse
import csv
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sources as S  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(ROOT, "out")


# ================================================================ 聚合：分词

# 中文虚词/高频无意义字：gram 里出现就丢掉
ZH_STOP_CHARS = set("的了是在和与及或就都也很不没有我你他她它们这那哪个些么呢吧啊呀哦嗯把被给对从到为以于之其而且但却还又再才更最太挺非常可以能够已经正在将要会能要想说做"
                    "一上下中里外前后天日年月时分秒")

ZH_STOP_GRAMS = {
    "什么", "怎么", "为什么", "如何", "已经", "可以", "没有", "我们", "他们", "自己",
    "现在", "这个", "那个", "一个", "不是", "就是", "还是", "因为", "所以", "但是",
    "然后", "之后", "之前", "时候", "问题", "事情", "东西", "地方", "网友", "官方",
    "回应", "表示", "发布", "消息", "最新", "刚刚", "今天", "昨天", "明天", "视频",
    "图片", "热搜", "话题", "讨论", "关注", "报道", "记者", "媒体", "新闻",
    "一起", "为何", "看到", "这是", "那是", "以及", "还是", "只是", "不过", "其实",
    "真的", "好像", "可能", "觉得", "知道", "发现", "出现", "引发", "导致", "成为",
    "开始", "结束", "进行", "通过", "其中", "包括", "表示", "认为", "指出", "介绍",
    "大家", "多少人", "为什么", "怎么样", "是不是", "有没有", "到底", "究竟", "原来",
    "背后", "真相", "细节", "原因", "结果", "情况", "方面", "相关", "有关", "目前",
}

EN_STOP = set("""
a an the and or but if then than that this these those of in on at to for from by with
as is are was were be been being it its it's we our you your they their he she his her
not no nor so such very more most much many some any all can could will would should
may might must do does did done have has had having i me my mine us them him hers
about into over after before between during without within across per via out up down
off again once here there when where why how what which who whom whose new now just
one two three first last next other same own each every any both few
says said say get gets got make makes made use used using like just also still even
big multiple year years video videos week day days time times people thing things way
lot good best top worse worst better new old first last long short high low more less
""".split())


_JIEBA = None


def _load_jieba():
    """可选增强：装了 jieba 就精确分词，没装就退回 n-gram。

    jieba 是唯一的外部可选依赖，`pip install -r requirements.txt` 装上即可。
    """
    global _JIEBA
    vendor = os.path.join(ROOT, "vendor")
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.insert(0, vendor)
    try:
        import jieba  # type: ignore
        jieba.setLogLevel(60)          # 关掉 "Building prefix dict" 的杂音
        jieba.initialize()
        # 新闻热榜里高频、但通用词典切错的专名。「中俄美元首」不加就会切成「美元」+「首」。
        for w in ("中俄美", "中俄美元首", "中美元首", "亚太经合", "APEC", "GTA6",
                  "DeepSeek", "ChatGPT", "新能源汽车", "比特币", "人工智能"):
            jieba.add_word(w, freq=100000)
        _JIEBA = jieba
    except Exception:                   # noqa: BLE001 - 没装就降级，不是错误
        _JIEBA = None
    return _JIEBA


def zh_words(text: str):
    """中文词：有 jieba 走精确分词，否则退回 2~4 元组。"""
    if _JIEBA is not None:
        for w in _JIEBA.cut(text):
            w = w.strip()
            if len(w) >= 2 and re.fullmatch(r"[\u4e00-\u9fff]+", w) and w not in ZH_STOP_GRAMS:
                yield w
    else:
        yield from zh_grams(text)


def zh_grams(text: str, n_min: int = 2, n_max: int = 4):
    """切出连续中文片段的所有 2~4 元组。"""
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        for n in range(n_min, min(n_max, len(chunk)) + 1):
            for i in range(len(chunk) - n + 1):
                g = chunk[i : i + n]
                if any(ch in ZH_STOP_CHARS for ch in g):
                    continue
                if g in ZH_STOP_GRAMS:
                    continue
                yield g


def en_tokens(text: str):
    """英文：单词 + 2/3 词短语。短语很关键，否则 Grand Theft Auto 会碎成三个词。"""
    toks = [
        t.lower().strip(".'-")
        for t in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-']+", text)
    ]
    toks = [t for t in toks if len(t) >= 2 and t not in EN_STOP]
    for t in toks:
        if len(t) >= 3:
            yield t
    for n in (2, 3):
        for i in range(len(toks) - n + 1):
            phrase = " ".join(toks[i : i + n])
            if sum(len(x) for x in toks[i : i + n]) >= 6:
                yield phrase


def dedupe_substrings(counter: Counter, keep_ratio: float = 0.85) -> Counter:
    """
    去冗余：如果短词 s 的某次出现几乎都被更长的词包含（count 相近），
    s 就不是独立概念，丢掉。保留"最长且独立"的词。
    """
    keep: Counter = Counter()
    # 从长到短处理，长的先占位
    for word, n in sorted(counter.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        swallowed = 0
        for longer, ln in keep.items():
            if word != longer and word in longer and ln >= n * keep_ratio:
                swallowed = 1
                break
        if not swallowed:
            keep[word] = n
    return keep


def keyword_freq(title_items: list[dict]) -> Counter:
    """对一个源的条目列表做词频。中英文都收。"""
    c: Counter = Counter()
    for it in title_items:
        title = it["title"]
        for g in zh_words(title):
            c[g] += 1
        for t in en_tokens(title):
            c[t] += 1
    return dedupe_substrings(c)


def platform_consensus(results: dict, min_platforms: int = 2, top: int = 40):
    """
    跨平台共识词：同一个词出现在 >= min_platforms 个不同源里，
    说明不是单一平台的内部梗，而是多个圈层共同在谈的东西。
    """
    where: dict[str, set] = defaultdict(set)
    for sid, r in results.items():
        if not r["ok"]:
            continue
        for it in r["items"]:
            title = it["title"]
            seen_here = set()
            for g in zh_words(title):
                seen_here.add(g)
            for t in en_tokens(title):
                seen_here.add(t)
            for w in seen_here:
                where[w].add(sid)
    rows = [(w, len(ss), sorted(ss)) for w, ss in where.items() if len(ss) >= min_platforms]
    rows.sort(key=lambda x: (-x[1], -len(x[0])))

    # 与 keyword_freq 同样的去冗余：短词若被更长的词包含、且出现平台不更少，就不算独立概念
    keep: list = []
    for word, n, plats in rows:
        cur = set(plats)
        redundant = False
        for w2, _n2, p2 in keep:
            if word != w2 and word in w2 and set(p2) >= cur:
                redundant = True
                break
        if not redundant:
            keep.append((word, n, plats))
    return keep[:top]


# ================================================================ 主题分类

TOPIC_RULES: dict[str, list[str]] = {
    "政治军事": ["主席", "总统", "元首", "国务院", "外交", "会晤", "会谈", "军演", "国防",
                 "议会", "选举", "制裁", "白宫", "特朗普", "普京", "宣言", "执政", "政权",
                 "部长", "人大", "政协", "两岸", "战争", "停火", "军队", "导弹", "阅兵",
                 "总理", "省长", "市长", "书记", "全会", "立法", "法案", "政策", "改革",
                 "election", "president", "senate", "congress", "military", "war", "nato",
                 "treaty", "minister", "summit"],
    "经济金融": ["股市", "A股", "港股", "美股", "基金", "通胀", "降息", "加息", "GDP",
                 "贸易", "关税", "房价", "楼市", "消费", "裁员", "融资", "黄金", "比特币",
                 "经济", "投资", "上市", "破产", "债务", "物价", "工资", "营收", "利润",
                 "亏损", "财报", "市值", "股价", "双十一", "电商", "直播带货", "tax",
                 "inflation", "tariff", "stock", "market", "economy", "bitcoin", "crypto",
                 "recession", "layoffs", "ipo", "revenue", "earnings"],
    "科技AI": ["AI", "人工智能", "大模型", "芯片", "机器人", "量子", "算法", "开源",
               "OpenAI", "GPT", "算力", "自动驾驶", "手机", "发布会", "鸿蒙", "安卓",
               "苹果", "华为", "小米", "英伟达", "半导体", "编程", "代码", "程序员",
               "推理", "训练", "数据集", "论文", "实验室", "硬件", "笔记本", "平板",
               "VR", "AR", "元宇宙", "区块链", "Web3", "火箭", "卫星", "航天", "登月",
               "ai", "openai", "gpt", "llm", "model", "chip", "nvidia", "apple", "google",
               "microsoft", "software", "developer", "github", "robot", "quantum",
               "anthropic", "claude", "gemini", "spacex", "starlink"],
    "娱乐明星": ["明星", "演唱会", "综艺", "粉丝", "恋情", "塌房", "官宣", "女团", "男团",
                 "演员", "歌手", "剧透", "绯闻", "出道", "经纪", "剧组", "杀青", "片场",
                 "偶像", "影帝", "影后", "流量", "选秀", "红毯", "造型", "生图", "复出",
                 "退圈", "解约", "片酬", "代言", "celebrity", "album", "tour", "netflix",
                 "actor", "singer", "taylor", "grammy", "billboard", "halloween"],
    "体育竞技": ["比赛", "联赛", "冠军", "奥运", "世界杯", "球员", "教练", "马拉松",
                 "电竞", "nba", "cba", "足球", "篮球", "乒乓", "网球", "进球", "决赛",
                 "半决赛", "积分", "转会", "伤病", "退役", "主场", "客场", "球迷",
                 "中超", "英超", "西甲", "亚洲杯", "欧洲杯", "季前赛",
                 "match", "league", "champion", "olympic", "world cup", "nba", "football",
                 "soccer", "tennis", "playoff", "super bowl"],
    "社会民生": ["外卖", "快递", "社保", "教育", "学校", "医院", "看病", "交通", "地铁",
                 "公交", "市民", "居民", "社区", "养老", "户口", "扰民", "业主", "物业",
                 "考生", "高考", "考研", "打工", "上班", "加班", "通报", "回应", "男子",
                 "女子", "老人", "孩子", "学生", "老师", "家长", "小区", "街道", "城管",
                 "纠纷", "维权", "欠薪", "司机", "乘客", "高铁", "火车", "航班", "堵车",
                 "拘留", "罚款", "整治", "查处", "曝光", "投诉", "举报", "报名", "录取",
                 "毕业", "就业", "招聘", "医保", "公积金", "彩礼", "离婚", "结婚", "生育",
                 "调查", "统计", "school", "hospital", "housing", "commute", "workers",
                 "salary", "court", "judge", "lawsuit"],
    "影视游戏": ["电影", "剧集", "票房", "上映", "导演", "番剧", "游戏", "手游", "新片",
                 "评分", "豆瓣", "动漫", "B站", "追番", "重制", "gta", "steam", "ps5",
                 "xbox", "switch", "主机", "网游", "战队", "选手", "赛季", "补丁", "DLC",
                 "发售", "公测", "抽卡", "预告", "片花", "特效", "配音", "优酷", "爱奇艺",
                 "腾讯视频", "芒果", "网飞", "动画", "漫画", "小说",
                 "anime", "film", "box office", "trailer", "gaming", "game", "movie",
                 "sequel", "steam", "playstation", "nintendo", "episode"],
    "安全事件": ["火灾", "事故", "坠", "伤亡", "警方", "犯罪", "判决", "诈骗", "偷",
                 "抢", "身亡", "遇难", "坍塌", "爆炸", "失踪", "嫌犯", "刑拘", "立案",
                 "遇袭", "袭击", "枪击", "自杀", "跳楼", "失联", "走失", "拐卖", "性侵",
                 "猥亵", "霸凌", "坠楼", "溺水", "中毒", "起火", "扑救", "救援", "搜救",
                 "crash", "fire", "police", "arrest", "scam", "fraud", "shooting",
                 "killed", "murder", "attack", "theft", "homicide"],
    "健康医疗": ["健康", "病毒", "流感", "癌症", "药", "疫苗", "减肥", "睡眠", "医生",
                 "手术", "疫情", "抑郁", "养生", "体检", "感冒", "发烧", "心脏", "血压",
                 "血糖", "糖尿病", "肿瘤", "住院", "出院", "挂号", "问诊", "药品",
                 "health", "virus", "vaccine", "cancer", "drug", "mental", "doctor",
                 "disease", "hospital", "fda"],
    "国际关系": ["美国", "日本", "韩国", "俄罗斯", "乌克兰", "以色列", "欧洲", "欧盟",
                 "联合国", "印度", "北约", "访华", "访问", "中美", "中俄", "全球",
                 "峰会", "联合声明", "协议", "条约", "谈判", "边境", "领土", "主权",
                 "大使", "领事", "签证", "移民", "难民", "南海", "台海", "中东", "加沙",
                 "china", "russia", "ukraine", "israel", "europe", "india", "global",
                 "foreign", "diplomat", "gaza", "iran", "korea", "japan"],
    "天气灾害": ["台风", "暴雨", "地震", "高温", "寒潮", "洪水", "预警", "降雨", "大风",
                 "山火", "storm", "flood", "earthquake", "heatwave", "hurricane", "climate",
                 "weather", "snow", "wildfire"],
    "生活文化": ["美食", "旅游", "穿搭", "宠物", "家居", "读书", "摄影", "咖啡", "露营",
                 "手工", "展览", "音乐节", "文创", "景区", "度假", "民宿", "菜谱",
                 "recipe", "travel", "fashion", "coffee", "photography", "art", "museum",
                 "halloween", "christmas", "thanksgiving"],
}


def classify(title: str) -> str:
    """给一条标题打一个主题标签；命中关键词最多的主题胜出，都不中归"未归类"。"""
    low = title.lower()
    best, best_score = "未归类", 0
    for topic, kws in TOPIC_RULES.items():
        score = sum(1 for kw in kws if kw.lower() in low)
        if score > best_score:
            best, best_score = topic, score
    return best


def topic_distribution(results: dict, region: str) -> Counter:
    c: Counter = Counter()
    for r in results.values():
        if not r["ok"] or r["region"] != region:
            continue
        for it in r["items"]:
            c[classify(it["title"])] += 1
    return c


# ================================================================ 输出


def ensure_out_dir() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(OUT_ROOT, stamp)
    os.makedirs(d, exist_ok=True)
    return d


def load_prev_titles(history_path: str) -> dict[str, set]:
    """读 history.jsonl 最后一行，拿上一轮的标题集合，用于标 NEW。"""
    if not os.path.exists(history_path):
        return {}
    last = ""
    with open(history_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = line
    if not last:
        return {}
    try:
        return {k: set(v) for k, v in json.loads(last).get("titles", {}).items()}
    except (json.JSONDecodeError, AttributeError):
        return {}


def append_history(history_path: str, results: dict) -> None:
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "titles": {sid: [i["title"] for i in r["items"]] for sid, r in results.items() if r["ok"]},
    }
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_csv(path: str, results: dict) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["source_id", "source_name", "region", "rank", "title", "hot", "hot_text", "url"])
        for sid, r in results.items():
            for it in r["items"]:
                w.writerow([sid, r["name"], r["region"], it["rank"], it["title"],
                            it["hot"], it["hot_text"], it["url"]])


def write_markdown(path: str, results: dict, meta: dict, max_per_source: int = 10) -> None:
    """
    给 AI 读的纯文本版：不含 CSS/JS，结构清晰，控制在 ~15KB。
    直接把路径给 agent（或整段粘进对话），比丢一个 112KB 的 HTML 有效得多。
    """
    ok = [r for r in results.values() if r["ok"]]
    lines: list[str] = []
    lines.append(f"# 趋势雷达 · {meta['stamp']}")
    lines.append("")
    lines.append(f"{len(ok)}/{len(results)} 个源在线 · "
                 f"{sum(len(r['items']) for r in ok)} 条 · 耗时 {meta['elapsed']}s")
    lines.append("")

    cn_items = [i for r in ok if r["region"] == "cn" for i in r["items"]]
    intl_items = [i for r in ok if r["region"] == "intl" for i in r["items"]]

    lines.append("## 高频词")
    lines.append("")
    for label, items in (("国内", cn_items), ("国外", intl_items)):
        top = keyword_freq(items).most_common(16)
        lines.append(f"**{label}**：" + "、".join(f"{w}({n})" for w, n in top))
        lines.append("")

    lines.append("## 跨平台共识词（≥2 个平台同时出现）")
    lines.append("")
    lines.append("| 词 | 平台数 | 出现在 |")
    lines.append("|---|---|---|")
    for word, n, plats in platform_consensus(
            {k: v for k, v in results.items() if v["category"] not in ("ai", "free")}, 2, 20):
        lines.append(f"| {word} | {n} | {', '.join(plats)} |")
    lines.append("")

    lines.append("## 主题分布")
    lines.append("")
    cn_t, intl_t = topic_distribution(results, "cn"), topic_distribution(results, "intl")
    cn_tot = sum(cn_t.values()) or 1
    intl_tot = sum(intl_t.values()) or 1
    lines.append("| 主题 | 国内 | 国外 |")
    lines.append("|---|---|---|")
    for t in sorted(set(cn_t) | set(intl_t), key=lambda x: -(cn_t[x] + intl_t[x]))[:10]:
        lines.append(f"| {t} | {cn_t[t] / cn_tot * 100:.0f}% | {intl_t[t] / intl_tot * 100:.0f}% |")
    lines.append("")

    def _ai_section(kind: str, title: str, hint: str):
        lines.append(f"## {title}")
        lines.append("")
        lines.append(hint)
        lines.append("")
        for r in results.values():
            if not r["ok"] or r["category"] != kind:
                continue
            lines.append(f"### {r['name']}（{len(r['items'])} 条）")
            for it in r["items"][:15]:
                extra = it["hot_text"] or ""
                tag = (it.get("meta") or {}).get("tag") or ""
                bits = " · ".join(x for x in (extra, tag) if x)
                zh = (it.get("zh") or "").strip()
                note = (it.get("note") or "").strip()
                head = zh if (zh and zh != it["title"]) else it["title"]
                tail = " · ".join(x for x in (note, bits) if x)
                lines.append(f"- [{head}]({it['url']})" + (f" — {tail}" if tail else ""))
            lines.append("")

    _ai_section("ai", "AI 前沿", "模型 / 论文 / 新项目 / 开源空间。标注了用途：建站 / 视频 / 3D。")
    _ai_section("free", "可以薅的羊毛", "免费 API 与免费额度，点链接直接去开通。")

    for region, title in (("cn", "国内榜单"), ("intl", "国外榜单")):
        lines.append(f"## {title}")
        lines.append("")
        for r in results.values():
            if not r["ok"] or r["region"] != region:
                continue
            if r["category"] in ("ai", "free"):
                continue
            lines.append(f"### {r['name']}（{len(r['items'])} 条）")
            for it in r["items"][:max_per_source]:
                hot = f" — {it['hot_text']}" if it["hot_text"] else ""
                zh = (it.get("zh") or "").strip()
                note = (it.get("note") or "").strip()
                if zh and zh != it["title"]:
                    line = f"{it['rank']}. {zh}"
                    if note:
                        line += f"（{note}）"
                    line += f" — 原文：{it['title']}{hot}"
                else:
                    line = f"{it['rank']}. {it['title']}{hot}"
                lines.append(line)
            if len(r["items"]) > max_per_source:
                lines.append(f"…还有 {len(r['items']) - max_per_source} 条")
            lines.append("")

    fails = [r for r in results.values() if not r["ok"]]
    if fails:
        lines.append("## 抓取失败")
        lines.append("")
        for r in fails:
            lines.append(f"- {r['name']}：{r['error']}")
        lines.append("")

    lines.append("---")
    lines.append("数据来源为各平台公开热榜，热度值单位各平台不同，仅用于平台内排序。")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def publish_to_pages() -> bool:
    """
    把 out/latest.html + latest.md 发到 Cloudflare Pages。
    走 python subprocess 而不是 shell 脚本 —— launchd 下 /bin/bash 访问 ~/Documents 会被 TCC 拒。
    """
    parent = os.path.dirname(ROOT)
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not token:
        print("  ! 没有 CLOUDFLARE_API_TOKEN，跳过发布")
        return False

    # wrangler 位置：环境变量 > PATH > 项目本地 node_modules
    wrangler = os.environ.get("WRANGLER_BIN", "").strip()
    if not wrangler:
        wrangler = shutil.which("wrangler") or ""
    if not wrangler:
        for cand in (
            os.path.join(ROOT, "node_modules", ".bin", "wrangler"),
            os.path.join(parent, "node_modules", ".bin", "wrangler"),
        ):
            if os.path.exists(cand):
                wrangler = cand
                break
    if not wrangler or not os.path.exists(wrangler):
        print("  ! 找不到 wrangler。装一个：npm i -g wrangler 或 npm i -D wrangler")
        return False

    dist = os.path.join(ROOT, "dist")
    shutil.rmtree(dist, ignore_errors=True)
    os.makedirs(dist, exist_ok=True)
    pairs = [
        (os.path.join(OUT_ROOT, "latest.html"), os.path.join(dist, "index.html")),
        (os.path.join(OUT_ROOT, "latest.md"), os.path.join(dist, "latest.md")),
    ]
    rounds = sorted(d for d in os.listdir(OUT_ROOT) if re.fullmatch(r"\d{8}-\d{6}", d))
    if rounds:
        pairs.append((os.path.join(OUT_ROOT, rounds[-1], "trends.json"),
                      os.path.join(dist, "data.json")))
    # 论文讲解器页面（/paper/）
    paper_page = os.path.join(ROOT, "paper", "index.html")
    if os.path.exists(paper_page):
        os.makedirs(os.path.join(dist, "paper"), exist_ok=True)
        pairs.append((paper_page, os.path.join(dist, "paper", "index.html")))

    promo = os.path.join(ROOT, "shots", "promo-9x16.mp4")
    if os.path.exists(promo):
        pairs.append((promo, os.path.join(dist, "promo.mp4")))

    for src, dst in pairs:
        if os.path.exists(src):
            shutil.copyfile(src, dst)
    with open(os.path.join(dist, "updated.txt"), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))

    # launchd 的 PATH 只有 /usr/bin:/bin，node 不在里面，wrangler 的 shebang 会挂
    node_dirs = [os.path.dirname(shutil.which("node") or ""), "/opt/homebrew/bin"]
    node_dirs += [os.path.join(parent, d, "bin") for d in os.listdir(parent)
                  if d.startswith(".tools/node-")]
    path_parts = [p for p in node_dirs if p and os.path.isdir(p)]
    path_parts += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    project = os.environ.get("CF_PAGES_PROJECT", "social-trend-radar")
    proxy = os.environ.get("RADAR_HTTP_PROXY", "")
    if not account:
        print("  ! 没有 CLOUDFLARE_ACCOUNT_ID，跳过发布")
        return False

    env = dict(os.environ)
    env.update({
        "PATH": ":".join(path_parts),
        "CLOUDFLARE_API_TOKEN": token,
        "CLOUDFLARE_ACCOUNT_ID": account,
    })
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
    print("  → 发布到 Cloudflare Pages …")
    try:
        r = subprocess.run(
            [wrangler, "pages", "deploy", dist, "--project-name", project,
             "--commit-dirty=true"],
            env=env, capture_output=True, text=True, timeout=300,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if "Deployment complete" in out or "Success" in out:
            print(f"  ✓ https://{project}.pages.dev")
            return True
        print(f"  ! 发布失败: {out.strip()[-300:]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 发布异常: {type(exc).__name__}: {exc}")
    return False


def refresh_latest(report_path: str, keep_rounds: int = 30) -> None:
    """把本轮报告复制成 out/latest.html，并只保留最近 N 份，避免无限增长。"""
    latest = os.path.join(OUT_ROOT, "latest.html")
    try:
        with open(report_path, "rb") as src, open(latest, "wb") as dst:
            dst.write(src.read())
    except OSError as exc:
        print(f"  ! latest.html 更新失败: {exc}")

    try:
        rounds = sorted(
            d for d in os.listdir(OUT_ROOT)
            if re.fullmatch(r"\d{8}-\d{6}", d) and os.path.isdir(os.path.join(OUT_ROOT, d))
        )
        for old in rounds[:-keep_rounds]:
            shutil.rmtree(os.path.join(OUT_ROOT, old), ignore_errors=True)
    except OSError:
        pass


def trim_history(history_path: str, keep_lines: int = 600) -> None:
    """history.jsonl 只留最近 N 轮。

    每轮约 27 KB（所有平台的标题），600 轮 ≈ 16 MB、约 300 天。
    这个文件要跟报告一起提交到 gh-pages，所以不能让它长太大。
    """
    if not os.path.exists(history_path):
        return
    try:
        with open(history_path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > keep_lines:
            with open(history_path, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep_lines:])
    except OSError:
        pass


def write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- HTML

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d10; --glass:rgba(255,255,255,.028); --glass2:rgba(255,255,255,.045);
  --line:rgba(140,150,215,.34); --line2:rgba(170,140,240,.62);
  --fg:#e8eef8; --dim:#7d90a8; --dim2:#4a5c74;
  --blue:#7b9cff; --blue2:#9b72cb; --purple:#9b72cb; --cn:#ff7a45; --intl:#7b9cff;
  --good:#4ade80; --bad:#f87171;
}
body{background:var(--bg);color:var(--fg);position:relative;
  font:15px/1.65 -apple-system,"PingFang SC","Hiragino Sans GB","Segoe UI",system-ui,sans-serif;
  padding:40px 24px 80px;-webkit-font-smoothing:antialiased}
/* 极光：三团模糊光，缓慢漂移呼吸 */
#aurora{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden}
#aurora i{position:absolute;display:block;border-radius:50%;filter:blur(90px);will-change:transform}
#aurora i:nth-child(1){width:760px;height:760px;left:-6%;top:-12%;
  background:radial-gradient(circle,rgba(70,140,255,.34),transparent 68%);
  animation:auroraA 11s ease-in-out infinite}
#aurora i:nth-child(2){width:900px;height:900px;right:-14%;top:32%;
  background:radial-gradient(circle,rgba(150,110,215,.30),transparent 68%);
  animation:auroraB 15s ease-in-out infinite;animation-delay:-5s}
#aurora i:nth-child(3){width:680px;height:680px;left:26%;bottom:-18%;
  background:radial-gradient(circle,rgba(90,150,255,.24),transparent 70%);
  animation:auroraC 19s ease-in-out infinite;animation-delay:-9s}
@keyframes auroraA{0%,100%{transform:translate3d(0,0,0) scale(1)}
  33%{transform:translate3d(90px,60px,0) scale(1.14)}
  66%{transform:translate3d(-60px,110px,0) scale(.92)}}
@keyframes auroraB{0%,100%{transform:translate3d(0,0,0) scale(1)}
  40%{transform:translate3d(-110px,-70px,0) scale(1.1)}
  70%{transform:translate3d(70px,50px,0) scale(.9)}}
@keyframes auroraC{0%,100%{transform:translate3d(0,0,0) scale(1)}
  50%{transform:translate3d(120px,-90px,0) scale(1.16)}}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.9;
  background-image:
    linear-gradient(rgba(130,140,200,.13) 1px,transparent 1px),
    linear-gradient(90deg,rgba(130,140,200,.13) 1px,transparent 1px),
    linear-gradient(rgba(130,140,200,.06) 1px,transparent 1px),
    linear-gradient(90deg,rgba(130,140,200,.06) 1px,transparent 1px);
  background-size:80px 80px,80px 80px,26px 26px,26px 26px;
  -webkit-mask-image:radial-gradient(ellipse at 50% 30%,#000 30%,rgba(0,0,0,.35) 70%,transparent 100%);
  mask-image:radial-gradient(ellipse at 50% 30%,#000 30%,rgba(0,0,0,.35) 70%,transparent 100%)}
.wrap{position:relative;z-index:1}
.wrap{max-width:1240px;margin:0 auto;position:relative;z-index:1}

50%{opacity:.60;transform:translateY(12px)}}
h1{font-size:30px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px;
  background:linear-gradient(100deg,#cfe0ff 0%,#8fb0ff 30%,#a884dd 60%,#e08a96 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
h1 em{font-style:normal;-webkit-text-fill-color:var(--purple);color:var(--purple)}
.sub{color:var(--dim);font-size:13px;margin-bottom:32px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:36px}
.stat{background:var(--glass);border:1px solid rgba(255,255,255,.10);border-radius:14px;padding:16px 18px;position:relative;
  backdrop-filter:blur(42px) saturate(1.85) brightness(.97);
  -webkit-backdrop-filter:blur(42px) saturate(1.85) brightness(.97);
  box-shadow:0 8px 32px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.055),
             0 0 48px rgba(120,120,200,.14);
  transition:border-color .25s, box-shadow .25s}
.stat:hover{border-color:var(--line2);box-shadow:0 8px 32px rgba(0,0,0,.5),
  inset 0 1px 0 rgba(255,255,255,.07), 0 0 72px rgba(150,130,220,.24)}
.stat b{display:block;font-size:26px;font-weight:700;letter-spacing:-.02em;
  background:linear-gradient(180deg,#f6f7fb,#9b9fd4);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.stat span{color:var(--dim);font-size:12px}
h2{font-size:17px;font-weight:600;margin:44px 0 16px;padding-bottom:10px;
   border-bottom:1px solid var(--line);letter-spacing:-.01em}
h2::before{content:'';display:inline-block;width:3px;height:15px;background:var(--blue);
   margin-right:10px;vertical-align:-2px;border-radius:2px;
   box-shadow:0 0 14px rgba(155,114,203,.85)}
h2 small{color:var(--dim2);font-weight:400;font-size:12px;margin-left:8px}
.verdict{background:var(--glass);border:1px solid rgba(255,255,255,.10);border-radius:16px;position:relative;
  padding:22px 24px;margin-bottom:8px;
  backdrop-filter:blur(32px) saturate(1.6) brightness(1.07);
  -webkit-backdrop-filter:blur(32px) saturate(1.6) brightness(1.07);
  box-shadow:0 10px 40px rgba(0,0,0,.38), inset 0 1px 0 rgba(255,255,255,.12),
             0 0 72px rgba(120,120,200,.16)}
.verdict p{font-size:15px;line-height:1.85;color:#c9d6e8}
.verdict b{color:var(--fg)}
.verdict .cn{color:var(--cn);font-weight:600}
.verdict .intl{color:var(--blue2);font-weight:600}

.cols{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:900px){.cols{grid-template-columns:1fr}}
.words{background:var(--glass);border:1px solid rgba(255,255,255,.10);border-radius:14px;padding:18px 20px;position:relative;
  backdrop-filter:blur(42px) saturate(1.85) brightness(.97);
  -webkit-backdrop-filter:blur(42px) saturate(1.85) brightness(.97);
  box-shadow:0 8px 32px rgba(0,0,0,.42), inset 0 1px 0 rgba(255,255,255,.05)}
.words h3{font-size:13px;color:var(--dim);font-weight:500;margin-bottom:12px;letter-spacing:.04em;text-transform:uppercase}
.wrow{display:flex;align-items:center;gap:10px;margin-bottom:7px;font-size:13px}
.wrow .t{min-width:96px;flex-shrink:0;color:var(--fg)}
.wrow .bar{flex:1;height:6px;background:rgba(19,134,255,.10);border-radius:3px;
  overflow:hidden;box-shadow:inset 0 0 0 1px rgba(19,134,255,.08)}
.wrow .bar i{display:block;height:100%;border-radius:3px;position:relative;
  background-size:200% 100%;
  animation:barFlow 4.5s linear infinite}
/* 极热：红 */
.wrow .bar i.t0{--shine:rgba(255,175,155,.95);
  background-image:linear-gradient(90deg,#ff2d1f,#ff6b5a,#ff2d1f);
  box-shadow:0 0 18px rgba(255,45,31,.65), 0 0 36px rgba(255,45,31,.28)}
/* 热：红→橙 */
.wrow .bar i.t1{--shine:rgba(255,200,150,.95);
  background-image:linear-gradient(90deg,#ff2d1f,#ff8a3c,#ff2d1f);
  box-shadow:0 0 16px rgba(255,90,40,.55), 0 0 30px rgba(255,110,60,.22)}
/* 中：橙→绿 */
.wrow .bar i.t2{--shine:rgba(225,245,160,.95);
  background-image:linear-gradient(90deg,#ff9a3c,#a8d95a,#ff9a3c);
  box-shadow:0 0 14px rgba(200,180,60,.45), 0 0 26px rgba(170,200,80,.18)}
/* 温：绿 */
.wrow .bar i.t3{--shine:rgba(170,255,200,.95);
  background-image:linear-gradient(90deg,#5fd47a,#4ade80,#5fd47a);
  box-shadow:0 0 12px rgba(74,222,128,.5), 0 0 24px rgba(74,222,128,.2)}
/* 流光：一条亮带在条内扫过 */
.wrow .bar i::after{content:'';position:absolute;inset:0;border-radius:3px;
  background:linear-gradient(90deg,transparent 34%,var(--shine) 50%,transparent 66%);
  background-size:220% 100%;
  animation:barShine 3.2s linear infinite}
@keyframes barFlow{to{background-position:-200% 0}}
@keyframes barShine{to{background-position:-220% 0}}

.wrow .n{width:34px;text-align:right;color:var(--dim2);font-variant-numeric:tabular-nums;font-size:12px}
.consensus .wrow .t{min-width:104px}
.words.consensus{display:grid;grid-template-columns:1fr 1fr;gap:0 26px;align-items:start}
@media(max-width:780px){.words.consensus{grid-template-columns:1fr}}

.topic-table{width:100%;border-collapse:collapse;font-size:13px}
.topic-table th{text-align:left;color:var(--dim);font-weight:500;font-size:12px;
  padding:6px 8px;border-bottom:1px solid var(--line)}
.topic-table td{padding:7px 8px;border-bottom:1px solid #1a1f26}
.topic-table td.name{width:100px;color:var(--fg)}
.topic-table td.pct{width:52px;text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
.topic-table .mini{height:8px;background:#1d232b;border-radius:4px;overflow:hidden;min-width:80px}
.topic-table .mini i{display:block;height:100%;border-radius:4px}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
@property --sweep{ syntax:'<angle>'; initial-value:0deg; inherits:false }

.card{background:var(--glass);border:1px solid rgba(255,255,255,.10);border-radius:16px;
  overflow:hidden;position:relative;
  backdrop-filter:blur(42px) saturate(1.85) brightness(.97);
  -webkit-backdrop-filter:blur(42px) saturate(1.85) brightness(.97);
  box-shadow:0 10px 36px rgba(0,0,0,.42), inset 0 1px 0 rgba(255,255,255,.05),
             0 0 52px rgba(120,120,200,.12);
  transition:border-color .25s, box-shadow .25s}
.card:hover{border-color:var(--line2);
  box-shadow:0 10px 36px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.09),
             0 0 96px rgba(150,130,220,.28)}
/* 边缘流动扫光：conic 渐变绕边框转，用 mask 只留 1.5px 边 */
.card::before,.stat::before,.words::before,.verdict::before{
  content:'';position:absolute;inset:0;border-radius:inherit;padding:1.5px;
  background:conic-gradient(from var(--sweep),
    transparent 0deg, rgba(123,156,255,.0) 40deg,
    rgba(123,156,255,.85) 92deg, rgba(190,150,255,.9) 130deg,
    rgba(123,156,255,.0) 178deg, transparent 360deg);
  -webkit-mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;
  mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  mask-composite:exclude;
  pointer-events:none;opacity:.9;
  animation:sweepSpin 5.5s linear infinite}
.stat:nth-child(2)::before{animation-delay:-1.1s}
.stat:nth-child(3)::before{animation-delay:-2.2s}
.stat:nth-child(4)::before{animation-delay:-3.3s}
.stat:nth-child(5)::before{animation-delay:-4.4s}
.grid .card:nth-child(2n)::before{animation-delay:-2.7s}
.grid .card:nth-child(3n)::before{animation-delay:-4.1s}
@keyframes sweepSpin{ to{ --sweep:360deg } }
.card header{display:flex;align-items:center;justify-content:space-between;
  padding:12px 16px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.01))}
.card header .nm{font-size:13.5px;font-weight:600;letter-spacing:.01em}
.card header .nm .dot{display:inline-block;width:6px;height:6px;border-radius:50%;
  margin-right:7px;vertical-align:1px}
.card header .meta{font-size:11px;color:var(--dim2);font-variant-numeric:tabular-nums}
.li{display:flex;gap:9px;padding:9px 16px;font-size:13px;line-height:1.55;
  border-bottom:1px solid rgba(19,134,255,.07);text-decoration:none;color:var(--fg);
  position:relative;transition:background .16s}
.li:last-child{border-bottom:none}
.li:hover{background:rgba(19,134,255,.07)}
.li .rk{flex-shrink:0;width:16px;color:var(--dim2);font-size:11px;
  font-variant-numeric:tabular-nums;padding-top:2px}
.li .tx{flex:1;min-width:0}
.li .tx p{overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.li .h{flex-shrink:0;color:var(--dim2);font-size:11px;padding-top:2px;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.li.new .rk{color:var(--good);font-weight:700}
.li .desc{color:var(--dim2);font-size:11px;display:block;margin-top:2px}
.li .orig{display:block;color:#5f6875;font-size:11px;margin-top:2px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:-.01em}
.li .note{display:block;color:#9aa4b2;font-size:11.5px;margin-top:2px}
.li .explain{display:inline-block;font-size:11px;color:var(--blue2);cursor:pointer;
  border:1px solid #3a2418;background:#1b120c;padding:1px 7px;border-radius:5px;
  margin-top:5px;white-space:nowrap;user-select:none}
.li .explain:hover{background:#2a1a10;border-color:var(--accent)}
.fail{background:#1a1416;border:1px solid #3b2226;border-radius:8px;
  padding:10px 14px;font-size:12.5px;color:#e8a0a0;margin-bottom:6px}
.fail b{color:#f87171}
footer{margin-top:52px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--dim2);font-size:12px;line-height:1.9}
footer code{background:#1a1f26;padding:1px 5px;border-radius:3px;color:var(--dim)}
details.more{margin:0}
details.more summary{cursor:pointer;padding:10px 16px;font-size:11.5px;color:var(--dim);
  border-top:1px solid rgba(19,134,255,.1);list-style:none;text-align:center;user-select:none;
  background:rgba(19,134,255,.03)}
details.more summary::-webkit-details-marker{display:none}
details.more summary:hover{color:var(--blue2);background:rgba(19,134,255,.09)}
details.more[open] summary{border-bottom:1px solid #191e25;color:var(--dim2)}
details.more .li{background:#101317}
.tag{display:inline-block;font-size:10px;padding:0 5px;border-radius:3px;
  vertical-align:1px;margin-left:5px}
@media(max-width:520px){
  body{padding:26px 16px 60px}
  h1{font-size:23px}
  .verdict p{font-size:14px}
  .stat b{font-size:22px}
}
"""


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _heat_tier(rank: int, total: int) -> int:
    """0=极热(红) 1=热(红橙) 2=中(橙绿) 3=温(绿)"""
    pct = rank / max(total, 1)
    if rank <= 3 or pct <= 0.10:
        return 0
    if pct <= 0.33:
        return 1
    if pct <= 0.66:
        return 2
    return 3


def bar_rows(rows, color_cls: str = "", unit: str = "") -> str:
    if not rows:
        return '<p style="color:#5f6875;font-size:12px">（这组没有足够的数据）</p>'
    mx = max(n for _, n in rows) or 1
    total = len(rows)
    out = []
    for i, (word, n) in enumerate(rows, 1):
        w = max(2, round(n / mx * 100))
        tier = _heat_tier(i, total)
        out.append(
            f'<div class="wrow"><span class="t">{esc(word)}</span>'
            f'<span class="bar"><i class="t{tier}" style="width:{w}%"></i></span>'
            f'<span class="n">{n}</span></div>'
        )
    return "".join(out)


def topic_table(cn: Counter, intl: Counter) -> str:
    topics = sorted(set(cn) | set(intl), key=lambda t: -(cn[t] + intl[t]))
    if not topics:
        return ""
    tcn = sum(cn.values()) or 1
    tintl = sum(intl.values()) or 1
    rows = []
    for t in topics[:12]:
        a = cn[t] / tcn * 100
        b = intl[t] / tintl * 100
        rows.append(
            f"<tr><td class='name'>{esc(t)}</td>"
            f"<td><div class='mini'><i style='width:{a:.0f}%;background:var(--cn)'></i></div></td>"
            f"<td class='pct'>{a:.0f}%</td>"
            f"<td><div class='mini'><i style='width:{b:.0f}%;background:var(--intl)'></i></div></td>"
            f"<td class='pct'>{b:.0f}%</td></tr>"
        )
    return (
        "<table class='topic-table'><tr><th>主题</th><th colspan=2>国内占比</th>"
        "<th colspan=2>国外占比</th></tr>" + "".join(rows) + "</table>"
    )


def _render_items(items: list[dict], mx: float, prev_titles: set) -> str:
    """渲染一批榜单条目。折叠区复用它，保证样式一致。"""
    out = []
    for it in items:
        is_new = bool(prev_titles) and it["title"] not in prev_titles
        hot = f'<span class="h">{esc(it["hot_text"])}</span>' if it["hot_text"] else ""
        meta = it.get("meta") or {}
        desc = meta.get("desc_zh") or meta.get("desc") or meta.get("subtitle") or ""
        if desc:
            desc = desc if len(desc) <= 62 else desc[:60].rstrip() + "…"
        desc_html = f'<span class="desc">{esc(desc)}</span>' if desc else ""
        zh = (it.get("zh") or "").strip()
        note = (it.get("note") or "").strip()
        raw_title = it["title"]
        short = raw_title if len(raw_title) <= 68 else raw_title[:66].rstrip() + "…"
        shown = esc(zh) if zh and zh != raw_title else esc(short)
        orig = (f'<span class="orig">{esc(short)}</span>' if zh and zh != raw_title else "")
        note_html = f'<span class="note">· {esc(note)}</span>' if note else ""
        # arXiv 条目挂一个「讲解」直达
        # 注意：外层已经是 <a>，这里必须用 span —— a 里不能再套 a，浏览器会拆 DOM
        explain = ""
        if "arxiv.org/abs/" in (it.get("url") or ""):
            q = urllib.parse.quote(it["url"], safe="")
            explain = (f'<span class="explain" data-go="/paper/?url={q}">讲解 ↗</span>')
        out.append(
            f'<a class="li {"new" if is_new else ""}" href="{esc(it["url"])}" target="_blank" rel="noopener">'
            f'<span class="rk">{it["rank"]}</span>'
            f'<span class="tx"><p>{shown}</p>{orig}{note_html}{desc_html}{explain}</span>'
            f'{hot}</a>'
        )
    return "".join(out)


def source_card(sid: str, r: dict, prev_titles: set) -> str:
    color = "var(--cn)" if r["region"] == "cn" else "var(--intl)"
    if not r["ok"]:
        return (f'<div class="fail"><b>{esc(r["name"])}</b> 抓取失败 · {esc(r["error"])}</div>')

    items = r["items"]
    FIRST = 10          # 默认显示前 10 条，其余折叠
    body = _render_items(items[:FIRST], 0, prev_titles)

    rest = items[FIRST:]
    more = ""
    if rest:
        more = (f'<details class="more"><summary>展开其余 {len(rest)} 条 ↓</summary>'
                f'{_render_items(rest, 0, prev_titles)}</details>')

    caveat = r.get("caveat") or ""
    caveat_html = ""
    if caveat and "个性化" in caveat:
        caveat_html = ('<div style="padding:6px 14px;font-size:11px;color:#c99;'
                       'background:#1b1614;border-bottom:1px solid #2a201c">'
                       '⚠ 登录态·个性化推荐 —— 这是算法推给该账号的内容，不代表大众</div>')
    return (
        f'<div class="card"><header><span class="nm"><span class="dot" style="background:{color}"></span>'
        f'{esc(r["name"])}</span><span class="meta">{len(items)} 条 · {r["elapsed"]}s</span></header>'
        + caveat_html + body + more + "</div>"
    )


EXPLAIN_JS = """<script>
document.addEventListener('click', function (e) {
  var el = e.target.closest('.explain');
  if (!el) return;
  e.preventDefault();
  e.stopPropagation();
  window.open(el.getAttribute('data-go'), '_blank', 'noopener');
}, true);
</script>"""


def build_report(results: dict, meta: dict, prev_titles: dict) -> str:
    ok = [r for r in results.values() if r["ok"]]
    total_items = sum(len(r["items"]) for r in ok)

    # 词频：分区域
    def _main(region):
        return [i for r in results.values()
                if r["ok"] and r["region"] == region
                and r["category"] not in ("ai", "free")
                for i in r["items"]]

    cn_items, intl_items = _main("cn"), _main("intl")
    cn_words = keyword_freq(cn_items).most_common(28)
    intl_words = keyword_freq(intl_items).most_common(28)
    main_results = {k: v for k, v in results.items() if v["category"] not in ("ai", "free")}
    consensus = platform_consensus(main_results, min_platforms=2, top=24)

    cn_topics = topic_distribution(results, "cn")
    intl_topics = topic_distribution(results, "intl")

    cn_text = "、".join(w for w, _ in cn_words[:6]) or "—"
    intl_text = "、".join(w for w, _ in intl_words[:6]) or "—"
    cn_top = cn_topics.most_common(1)[0] if cn_topics else ("—", 0)
    intl_top = intl_topics.most_common(1)[0] if intl_topics else ("—", 0)
    cn_tot = sum(cn_topics.values()) or 1
    intl_tot = sum(intl_topics.values()) or 1

    share_rows = []
    max_plat = max((len(p) for _w, _n, p in consensus), default=1) or 1
    total_c = len(consensus)
    for idx, (word, _n, plats) in enumerate(consensus, 1):
        w = max(3, round(len(plats) / max_plat * 100))
        tier = _heat_tier(idx, total_c)
        share_rows.append(
            f'<div class="wrow"><span class="t">{esc(word)}</span>'
            f'<span class="bar"><i class="t{tier}" style="width:{w}%"></i></span>'
            f'<span class="n">{len(plats)}</span></div>'
        )

    def sort_key(r):
        return (0 if r["region"] == "cn" else 1, r["name"])

    main_sources = [(sid, r) for sid, r in results.items()
                    if r["category"] not in ("ai", "free")]
    ai_sources = [(sid, r) for sid, r in results.items() if r["category"] == "ai"]
    free_sources = [(sid, r) for sid, r in results.items() if r["category"] == "free"]

    cards = [source_card(sid, r, prev_titles.get(sid, set()))
             for sid, r in sorted(main_sources, key=lambda kv: sort_key(kv[1]))]
    ai_cards = [source_card(sid, r, prev_titles.get(sid, set())) for sid, r in ai_sources]
    free_cards = [source_card(sid, r, prev_titles.get(sid, set())) for sid, r in free_sources]

    fails = [r for r in results.values() if not r["ok"]]
    fail_html = "".join(
        f'<div class="fail"><b>{esc(r["name"])}</b> · {esc(r["error"])}</div>' for r in fails
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>趋势雷达 · {esc(meta["stamp"])}</title><style>{CSS}</style></head>
<body><div id="aurora" aria-hidden="true"><i></i><i></i><i></i></div><div class="wrap">

<h1>趋势雷达 <em>/</em> 大家在关心什么</h1>
<div class="sub">{esc(meta["stamp"])} · 抓取 {len(results)} 个源 · 成功 {len(ok)} · 失败 {len(fails)} · 耗时 {meta["elapsed"]}s</div>

<div class="stats">
  <div class="stat"><b>{len(ok)}/{len(results)}</b><span>数据源在线</span></div>
  <div class="stat"><b>{total_items}</b><span>抓取条目</span></div>
  <div class="stat"><b>{len(cn_items)}</b><span>国内条目</span></div>
  <div class="stat"><b>{len(intl_items)}</b><span>国外条目</span></div>
  <div class="stat"><b>{len(consensus)}</b><span>跨平台共识词</span></div>
</div>

<div class="verdict">
  <p>此刻<span class="cn">国内</span>谈得最多的是 <b>{esc(cn_text)}</b>，
  主题重心落在「<b>{esc(cn_top[0])}</b>」（占 {cn_top[1] / cn_tot * 100:.0f}%）。<br>
  <span class="intl">国外</span>侧高频是 <b>{esc(intl_text)}</b>，
  主题重心是「<b>{esc(intl_top[0])}</b>」（占 {intl_top[1] / intl_tot * 100:.0f}%）。<br>
  两边同时出现的词有 <b>{len(consensus)}</b> 个 —— 那才是真正跨语言发酵的话题。</p>
</div>

<h2>跨平台共识词 <small>出现在 ≥2 个平台 → 不是单一平台的内部梗</small></h2>
<div class="words consensus">{''.join(share_rows) or '<p style="color:#5f6875;font-size:12px">本轮没有跨平台共现词</p>'}</div>

<h2>主题分布对照 <small>国内 vs 国外，按条目数占比</small></h2>
{topic_table(cn_topics, intl_topics)}

<h2>高频词 <small>2–4 字词 / 英文词，已去停用词与冗余子串</small></h2>
<div class="cols">
  <div class="words"><h3>国内</h3>{bar_rows(cn_words, '')}</div>
  <div class="words"><h3>国外</h3>{bar_rows(intl_words, 'intl')}</div>
</div>

<h2>AI 前沿 <small>模型 · 论文 · 新项目 · 开源空间 —— 不参与上面的通用词频统计</small></h2>
<div class="grid">{''.join(ai_cards)}</div>

<h2>可以薅的羊毛 <small>免费 API 与免费额度，点进去就能用</small></h2>
<div class="grid">{''.join(free_cards)}</div>

<h2>各平台榜单 <small>点击标题跳原站；绿色序号 = 相比上一轮新上榜</small></h2>
<div class="grid">{''.join(cards)}</div>

{('<h2>抓不到的源 <small>诚实记录，不用假数据填</small></h2>' + fail_html) if fail_html else ''}

<footer>
  生成于 {esc(meta["stamp"])} · 全量数据 <code>trends.json</code> · 表格 <code>words.csv</code><br>
  只读公开热榜接口，不含登录态，不做翻页轰炸。热度值单位各平台不同，仅用于本平台内排序。<br>
  主题分布是按关键词表规则打分的结果，不是语义模型 —— 用来快速看重心，不用来当精确统计。
</footer>
</div>
{EXPLAIN_JS}
</body></html>"""


# ================================================================ 主流程


def collect(sids: list[str], workers: int = 8) -> tuple[dict, float]:
    started = time.time()
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(S.run_one, sid): sid for sid in sids}
        for fut in as_completed(futs):
            sid = futs[fut]
            results[sid] = fut.result()
            r = results[sid]
            flag = "OK " if r["ok"] else "ERR"
            print(f"  [{flag}] {r['name']:<16} {len(r['items']):>3} 条  {r['elapsed']:>5.2f}s  {r['error'][:60]}")
    # 保持登记表顺序，报告更稳定
    ordered = {sid: results[sid] for sid in sids if sid in results}
    return ordered, round(time.time() - started, 1)


def run_once(sids: list[str], args, prev_titles: dict) -> dict:
    print(f"\n▶ 抓取 {len(sids)} 个源…")
    results, elapsed = collect(sids)

    # 英文条目 → 中文翻译 + 批注（有缓存，第二次几乎零成本）
    try:
        from translate import Translator

        tr = Translator()
        if tr.available():
            want: list[str] = []
            for r in results.values():
                if r["ok"]:
                    for it in r["items"][:30]:
                        want.append(it["title"])
                        d = (it.get("meta") or {}).get("desc")
                        if d:
                            want.append(d)
            got = tr.translate(want)
            for r in results.values():
                for it in r["items"]:
                    e = got.get(it["title"])
                    if e:
                        it["zh"] = e["zh"]
                        it["note"] = e["note"]
                    d = (it.get("meta") or {}).get("desc")
                    if d:
                        de = got.get(d)
                        if de:
                            it["meta"]["desc_zh"] = de["zh"]
            tr.save()
            print(f"  {tr.stats()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 翻译跳过: {type(exc).__name__}: {exc}")

    out_dir = ensure_out_dir()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "generated_at": stamp,
        "elapsed": elapsed,
        "sources": results,
        "analysis": {
            "cn_keywords": keyword_freq(
                [i for r in results.values() if r["ok"] and r["region"] == "cn" for i in r["items"]]
            ).most_common(60),
            "intl_keywords": keyword_freq(
                [i for r in results.values() if r["ok"] and r["region"] == "intl" for i in r["items"]]
            ).most_common(60),
            "consensus": [
                {"word": w, "platforms": p, "n": len(p)}
                for w, _n, p in platform_consensus(
                    {k: v for k, v in results.items() if v["category"] not in ("ai", "free")},
                    min_platforms=2, top=60)
            ],
            "topics": {
                "cn": dict(topic_distribution(results, "cn")),
                "intl": dict(topic_distribution(results, "intl")),
            },
        },
    }

    write_json(os.path.join(out_dir, "trends.json"), payload)
    write_csv(os.path.join(out_dir, "words.csv"), results)
    report = build_report(results, {"stamp": stamp, "elapsed": elapsed}, prev_titles)
    report_path = os.path.join(out_dir, "report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    history_path = os.path.join(OUT_ROOT, "history.jsonl")
    append_history(history_path, results)
    trim_history(history_path)
    refresh_latest(report_path)
    md_path = os.path.join(out_dir, "report.md")
    write_markdown(md_path, results, {"stamp": stamp, "elapsed": elapsed})
    try:
        with open(md_path, "rb") as src, open(os.path.join(OUT_ROOT, "latest.md"), "wb") as dst:
            dst.write(src.read())
    except OSError:
        pass

    ok = [r for r in results.values() if r["ok"]]
    total = sum(len(r["items"]) for r in ok)
    print(f"\n✓ {total} 条 / {len(ok)} 源 → {out_dir}")
    print(f"  报告 {os.path.basename(report_path)}  ·  全量 trends.json  ·  表格 words.csv")

    # 终端里也给一眼结论
    cons = payload["analysis"]["consensus"][:8]
    if cons:
        print("  跨平台共识: " + "、".join(f"{c['word']}({c['n']})" for c in cons))

    if getattr(args, "publish", False):
        publish_to_pages()

    if args.open:
        webbrowser.open("file://" + report_path)
    return payload


def cmd_check(sid: str) -> None:
    if sid not in S.SOURCES:
        print(f"没有这个源：{sid}\n可用：{', '.join(S.SOURCES)}")
        return
    sd = S.SOURCES[sid]
    print(f"== {sd.name} ({sid}) region={sd.region} available={sd.available}")
    if not sd.available:
        print(f"   不可用原因：{sd.note}")
        return
    t0 = time.time()
    try:
        items = sd.fn()  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001
        print(f"   失败 {type(exc).__name__}: {exc}")
        return
    print(f"   OK {len(items)} 条 / {time.time() - t0:.2f}s")
    for it in items[:8]:
        print(f"   {it.rank:>2}. {it.title}   [{it.hot_text}] {it.url}")


def cmd_list() -> None:
    print(f"{'id':<13}{'名称':<18}{'区域':<6}{'类别':<9}状态")
    print("-" * 62)
    for sid, sd in S.SOURCES.items():
        state = "可用" if sd.available else "需授权/被封"
        print(f"{sid:<13}{sd.name:<18}{sd.region:<6}{sd.category:<9}{state}  {sd.note}")


def main() -> None:
    ap = argparse.ArgumentParser(description="国内外社交/资讯平台热榜雷达")
    ap.add_argument("--only", help="只抓这些源，逗号分隔，如 weibo,hn")
    ap.add_argument("--region", choices=["cn", "intl", "all"], default="all")
    ap.add_argument("--interval", type=int, default=0, help="循环间隔秒数（0=只跑一次）")
    ap.add_argument("--rounds", type=int, default=1, help="循环轮数")
    ap.add_argument("--list", action="store_true", help="列出所有源")
    ap.add_argument("--check", metavar="SOURCE", help="单源调试")
    ap.add_argument("--open", action="store_true", help="抓完打开报告")
    ap.add_argument("--workers", type=int, default=8, help="并发线程数")
    ap.add_argument("--publish", action="store_true",
                    help="抓完发到 Cloudflare Pages")
    ap.add_argument("--with-browser", action="store_true",
                    help="带上小红书/YouTube 这类要开浏览器的重源（慢，约 20s）")
    args = ap.parse_args()

    if args.list:
        cmd_list()
        return
    if args.check:
        cmd_check(args.check)
        return

    if args.only:
        sids = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in sids if s not in S.SOURCES]
        if unknown:
            print(f"未知源：{', '.join(unknown)}\n可用：{', '.join(S.SOURCES)}")
            sys.exit(1)
    else:
        sids = [
            sid for sid, sd in S.SOURCES.items()
            if sd.available
            and (args.with_browser or not sd.heavy)
            and (args.region == "all" or sd.region == args.region)
        ]

    if _load_jieba() is not None:
        print("· 中文分词：jieba（vendor/）")
    else:
        print("· 中文分词：n-gram 兜底（想更准：pip install --target ./vendor jieba）")

    history_path = os.path.join(OUT_ROOT, "history.jsonl")
    round_no = 0
    while True:
        round_no += 1
        prev = load_prev_titles(history_path)
        run_once(sids, args, prev)
        if not args.interval or round_no >= args.rounds:
            break
        nxt = args.rounds - round_no
        print(f"\n… {args.interval}s 后开始第 {round_no + 1} 轮（还剩 {nxt} 轮），Ctrl-C 停止")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n已停止。")
            break


if __name__ == "__main__":
    main()
