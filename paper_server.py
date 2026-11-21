#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_server.py — 论文翻译与讲解器

粘贴一个论文链接（arXiv / 任意网页），返回：
  · 结构化解读（问题 / 方法 / 结果 / 局限）
  · 术语表
  · 摘要中文翻译
并支持围绕这篇论文的连续提问。

用法：
  python3 paper_server.py          # http://127.0.0.1:8833
  python3 paper_server.py 9000     # 换端口

复用 ~/.codex/config.toml 里的 OpenAI 兼容端点，不新增密钥。
"""

from __future__ import annotations

import envload  # noqa: F401  载入 .env
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from translate import _extract_text, _http_json, _load_endpoint  # noqa: E402

ACCESS_CODE = os.environ.get("PAPER_ACCESS_CODE", "").strip()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
def _proxy_map() -> dict:
    """代理从 RADAR_HTTP_PROXY / HTTPS_PROXY 读，没配就走直连。"""
    px = (os.environ.get("RADAR_HTTP_PROXY")
          or os.environ.get("HTTPS_PROXY")
          or os.environ.get("HTTP_PROXY") or "").strip()
    return {"https": px, "http": px} if px else {}


PROXY = _proxy_map()

ANALYZE_PROMPT = """你是论文讲解员，给中文读者讲清楚一篇论文。

输出严格 JSON（不要 markdown 代码围栏，不要多余文字）：
{
 "title_zh": "标题中文",
 "authors": ["作者"],
 "field": "领域，一句话",
 "tldr": "一句话说清这篇在干什么，40 字内",
 "problem": "它想解决什么问题，2-3 句",
 "method": "用了什么方法，3-5 句，说人话，避免堆砌公式",
 "results": "结果如何，2-3 句，有数字就写数字",
 "limits": "局限或没解决的部分，1-2 句",
 "terms": [{"en":"英文术语","zh":"中文","explain":"一句解释"}],
 "abstract_zh": "摘要的中文翻译"
}
术语挑 3-6 个读者最可能不懂的。说人话，不要复述原文。"""

ASK_PROMPT = """你是这篇论文的讲解员。用中文、说人话回答，必要时打比方或举例。
不确定就说不确定，不要编造论文没写的内容。回答控制在 300 字内。

【论文】
{paper}

【之前的问答】
{history}

【这次的问题】
{question}"""


# ---------------------------------------------------------------- 抓取


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(PROXY))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return raw


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</h[1-6]>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_paper(url: str) -> dict:
    """返回 {source, title, authors, abstract, fulltext, url}"""
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", url)
    if not m:
        m = re.search(r"arxiv\.org/(?:abs|pdf)/([a-z\-]+/\d{7})", url)
    if m:
        aid = m.group(1)
        xml = _http_get(f"http://export.arxiv.org/api/query?id_list={aid}", timeout=35)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml)
        entry = root.find("a:entry", ns)
        if entry is None:
            raise RuntimeError("arXiv 没返回这条论文")
        title = re.sub(r"\s+", " ", entry.findtext("a:title", "", ns) or "").strip()
        abstract = re.sub(r"\s+", " ", entry.findtext("a:summary", "", ns) or "").strip()
        authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
        cats = [c.get("term") for c in entry.findall("a:category", ns)]

        # 再抓 arXiv 的 HTML 全文版；失败就只用摘要
        fulltext = ""
        try:
            h = _http_get(f"https://arxiv.org/html/{aid}", timeout=12)
            fulltext = _strip_html(h.decode("utf-8", "replace"))[:22000]
        except Exception:  # noqa: BLE001
            fulltext = ""

        return {
            "source": "arxiv",
            "arxiv_id": aid,
            "title": title,
            "authors": [a for a in authors if a],
            "abstract": abstract,
            "categories": cats,
            "fulltext": fulltext,
            "has_fulltext": bool(fulltext),
            "url": f"https://arxiv.org/abs/{aid}",
        }

    raw = _http_get(url, timeout=35)
    html = raw.decode("utf-8", "replace")
    text = _strip_html(html)
    title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else url
    return {
        "source": "web",
        "title": title[:200],
        "authors": [],
        "abstract": text[:8000],
        "fulltext": text[:12000],
        "url": url,
    }


# ---------------------------------------------------------------- LLM


def analyze(paper: dict) -> dict:
    ep = _load_endpoint()
    if not ep:
        raise RuntimeError("找不到可用的 LLM 端点（~/.codex/config.toml）")
    base, key, model = ep

    payload = (
        f"标题：{paper['title']}\n"
        f"作者：{', '.join(paper.get('authors') or []) or '未知'}\n"
        f"分类：{', '.join(paper.get('categories') or []) or '未知'}\n"
        f"链接：{paper['url']}\n\n"
        f"摘要：\n{paper.get('abstract') or '(无)'}\n"
    )
    if paper.get("fulltext"):
        payload += f"\n【正文节选】\n{paper['fulltext'][:18000]}\n"

    resp = _http_json(f"{base}/responses",
                      {"model": model, "instructions": ANALYZE_PROMPT, "input": payload},
                      key, timeout=120)
    txt = _extract_text(resp)
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise RuntimeError(f"模型没返回 JSON：{txt[:200]}")
    data = json.loads(m.group(0))
    data["_meta"] = {
        "source": paper["source"],
        "title_en": paper["title"],
        "url": paper["url"],
        "arxiv_id": paper.get("arxiv_id", ""),
    }
    return data


def ask(paper: dict, question: str, history: list[dict], analysis: dict | None) -> str:
    ep = _load_endpoint()
    if not ep:
        raise RuntimeError("找不到可用的 LLM 端点")
    base, key, model = ep

    ctx = (
        f"标题：{paper['title']}\n"
        f"作者：{', '.join(paper.get('authors') or [])}\n"
        f"链接：{paper['url']}\n"
        f"摘要：{paper.get('abstract', '')[:3000]}\n"
    )
    if paper.get("fulltext"):
        ctx += f"正文节选：{paper['fulltext'][:4000]}\n"
    if analysis:
        ctx += ("\n已有的解读：\n"
                f"- 一句话：{analysis.get('tldr','')}\n"
                f"- 问题：{analysis.get('problem','')}\n"
                f"- 方法：{analysis.get('method','')}\n"
                f"- 结果：{analysis.get('results','')}\n")

    hist = "\n".join(f"Q: {h.get('q','')}\nA: {h.get('a','')}" for h in (history or [])[-4:])
    prompt = ASK_PROMPT.format(paper=ctx, history=hist or "(无)", question=question)
    resp = _http_json(f"{base}/responses",
                      {"model": model, "input": prompt}, key, timeout=90)
    return _extract_text(resp).strip()


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *a):  # 静音
        pass

    def _authorized(self) -> bool:
        return (self.headers.get("x-access-code") or "").strip() == ACCESS_CODE

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/ping":
            if self._authorized():
                return self._json(200, {"ok": True})
            return self._json(401, {"ok": False, "needAuth": True})
        if path in ("/", "/index.html"):
            f = os.path.join(HERE, "paper", "index.html")
            if not os.path.exists(f):
                return self._send(404, b"index.html missing", "text/plain")
            with open(f, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})

        if path.startswith("/api/") and not self._authorized():
            return self._json(401, {"error": "需要访问码", "needAuth": True})

        if path == "/api/analyze":
            url = (body.get("url") or "").strip()
            if not url:
                return self._json(400, {"error": "缺少 url"})
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            t0 = time.time()
            try:
                paper = fetch_paper(url)
                data = analyze(paper)
                data["_meta"]["elapsed"] = round(time.time() - t0, 1)
                return self._json(200, {"ok": True, "paper": paper, "analysis": data})
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        if path == "/api/ask":
            url = (body.get("url") or "").strip()
            q = (body.get("question") or "").strip()
            if not q:
                return self._json(400, {"error": "缺少 question"})
            try:
                paper = fetch_paper(url) if url else {"url": "", "title": "", "abstract": ""}
                ans = ask(paper, q, body.get("history") or [], body.get("analysis"))
                return self._json(200, {"ok": True, "answer": ans})
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        return self._json(404, {"error": "unknown endpoint"})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8833
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"论文讲解器  →  http://127.0.0.1:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")


if __name__ == "__main__":
    main()
