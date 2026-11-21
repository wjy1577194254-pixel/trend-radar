#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translate.py — 给英文条目加中文翻译 + 一句话批注

端点来自环境变量 LLM_BASE / LLM_KEY / LLM_MODEL，兼容 OpenAI 协议。
没配就跳过翻译，报告只保留原文。所有结果落盘缓存，同一条标题永远只翻一次。

设计取舍
--------
* 只翻"确实是英文"的条目（中文字符占比 < 20%），中文标题原样跳过。
* 一次请求 15 条，避免 token 过量；失败自动重试一次。
* 端点不可用 → 返回空，条目保持原文，不影响主流程。
"""

from __future__ import annotations

import envload  # noqa: F401  载入 .env
import hashlib
import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(ROOT, "cache", "translate.json")

SYSTEM_PROMPT = """你是中文科技编辑，为中文读者翻译英文标题。

对每条标题，输出：
- zh：准确的中文译法。模型名/仓库名/人名/论文专有术语保留英文原名，中文说明放在破折号后面，
      例：`Lightricks/LTX-2.5 —— 轻量级视频生成模型`
- note：一句不超过 28 字的批注，说清楚它是什么、用在哪。用途优先从这几个里挑：
        建站 / 视频 / 3D / 模型 / 论文 / 工具 / 新闻 / 开源
- 不要客套话，不要"这是一篇关于"这种开头

严格输出 JSON 数组，形如 [{"id":1,"zh":"...","note":"..."}]，不要额外文字。"""


DEFAULT_MODEL = "gpt-4o-mini"


def _load_endpoint() -> tuple[str, str, str] | None:
    """端点完全由环境变量决定（LLM_BASE / LLM_KEY / LLM_MODEL）。

    没配 LLM_BASE 就返回 None —— 翻译整个跳过，报告里只保留原文。
    这样既不猜本地服务，也不偷用任何人的额度。
    """
    env_base = (os.environ.get("LLM_BASE") or "").strip()
    if env_base:
        return (env_base.rstrip("/"),
                os.environ.get("LLM_KEY", ""),
                os.environ.get("LLM_MODEL", DEFAULT_MODEL))

    return None


def _http_json(url: str, payload: dict, key: str, timeout: int = 90) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    if "127.0.0.1" in url or "localhost" in url:
        # 本地端点绕开代理
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        # 交给 urllib 自动读 HTTPS_PROXY / HTTP_PROXY（.env 里的 RADAR_HTTP_PROXY 会同步过去）
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _extract_text(resp: dict) -> str:
    """兼容 Responses API 和 Chat Completions 两种返回。"""
    if "output" in resp:  # responses
        out = []
        for o in resp.get("output", []):
            for c in o.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    out.append(c.get("text", ""))
        return "".join(out)
    # chat completions
    try:
        return resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def looks_english(text: str) -> bool:
    """中文字符占比低于 20% 且有足够英文字母，就算英文条目。"""
    if not text:
        return False
    zh = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[A-Za-z]", text))
    if en < 8 or zh / max(len(text), 1) >= 0.2:
        return False
    # 没有空格的短单词基本是社交媒体标签（#hochwasser / #lhp3），翻不出意义
    if " " not in text.strip() and len(text) < 28:
        return False
    return True


class Translator:
    def __init__(self, batch_size: int = 15):
        self.batch_size = batch_size
        self.cache: dict[str, dict] = {}
        self.ep = _load_endpoint()
        self._dirty = False
        self._load_cache()
        self.calls = 0
        self.hits = 0
        self.failed = 0
        self.fail_reason = ""

    # ---------------- 缓存

    def _load_cache(self) -> None:
        if not os.path.exists(CACHE_PATH):
            return
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            self.cache = {}

    def save(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False)

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    # ---------------- 翻译

    def available(self) -> bool:
        return self.ep is not None

    def translate(self, titles: list[str]) -> dict[str, dict]:
        """
        入参是待翻译的英文标题列表，返回 {原标题: {"zh":…, "note":…}}。
        能命中缓存的不发请求；端点不可用就返回已缓存的部分。
        """
        result: dict[str, dict] = {}
        todo: list[str] = []
        for t in titles:
            if not looks_english(t):
                continue
            ck = self._key(t)
            if ck in self.cache:
                result[t] = self.cache[ck]
                self.hits += 1
            else:
                todo.append(t)

        if not todo or not self.available():
            return result

        base, key, model = self.ep  # type: ignore[misc]

        def do_batch(batch: list[str]) -> dict[str, dict]:
            got: dict[str, dict] = {}
            payload_in = [{"id": j + 1, "t": t} for j, t in enumerate(batch)]
            prompt = "翻译下面这批标题：\n" + json.dumps(payload_in, ensure_ascii=False)
            body = {"model": model, "instructions": SYSTEM_PROMPT, "input": prompt}
            txt = ""
            for attempt in range(2):
                try:
                    resp = _http_json(f"{base}/responses", body, key)
                    txt = _extract_text(resp)
                    if txt.strip():
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        # 限流：不重试，直接放弃这一批，避免雪上加霜
                        self.fail_reason = "端点限流 429（额度或速率已满）"
                        return got
                    if exc.code in (401, 403):
                        self.fail_reason = f"凭据被拒 {exc.code}（检查 LLM_KEY）"
                        return got
                    if attempt == 0:
                        continue
                    return got
                except Exception as exc:  # noqa: BLE001
                    self.fail_reason = f"{type(exc).__name__}: {exc}"[:80]
                    if attempt == 0:
                        continue
                    return got
            if not txt.strip():
                return got
            txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
            m = re.search(r"\[.*\]", txt, re.S)
            if not m:
                return got
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                return got
            by_id = {int(x.get("id", 0)): x for x in parsed if isinstance(x, dict)}
            for j, t in enumerate(batch, 1):
                item = by_id.get(j)
                if not item:
                    continue
                zh = (item.get("zh") or "").strip()[:200]
                if zh:
                    got[t] = {"zh": zh, "note": (item.get("note") or "").strip()[:80]}
            return got

        chunks = [todo[i : i + self.batch_size] for i in range(0, len(todo), self.batch_size)]
        with ThreadPoolExecutor(max_workers=4) as ex:
            for part in ex.map(do_batch, chunks):
                self.calls += 1
                if not part:
                    self.failed += 1
                for t, entry in part.items():
                    self.cache[self._key(t)] = entry
                    result[t] = entry
                    self._dirty = True
        return result

    def _unused_serial(self, todo, batch_size=0):
        for i in range(0, 0, 1):
            batch = todo[i : i + batch_size]
            payload_in = [{"id": j + 1, "t": t} for j, t in enumerate(batch)]
            prompt = (
                "翻译下面这批标题：\n"
                + json.dumps(payload_in, ensure_ascii=False)
            )
            body = {
                "model": model,
                "instructions": SYSTEM_PROMPT,
                "input": prompt,
            }
            txt = ""
            for attempt in range(2):
                try:
                    resp = _http_json(f"{base}/responses", body, key)
                    txt = _extract_text(resp)
                    if txt.strip():
                        break
                except Exception:  # noqa: BLE001
                    if attempt == 0:
                        continue
                    self.failed += 1
                    break

            if not txt.strip():
                continue

            # 模型有时会包 ```json
            txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
            m = re.search(r"\[.*\]", txt, re.S)
            if not m:
                self.failed += 1
                continue
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                self.failed += 1
                continue

            self.calls += 1
            by_id = {int(x.get("id", 0)): x for x in parsed if isinstance(x, dict)}
            for j, t in enumerate(batch, 1):
                item = by_id.get(j)
                if not item:
                    continue
                entry = {
                    "zh": (item.get("zh") or "").strip()[:200],
                    "note": (item.get("note") or "").strip()[:80],
                }
                if entry["zh"]:
                    self.cache[self._key(t)] = entry
                    result[t] = entry
                    self._dirty = True

        return result

    def stats(self) -> str:
        line = f"翻译 {self.hits} 命中缓存 · {self.calls} 次请求 · {self.failed} 批失败"
        if self.failed and self.fail_reason:
            line += f"（{self.fail_reason}）"
        return line
