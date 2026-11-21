#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sources_browser.py — 需要"登录态"的两个平台：小红书、YouTube

思路：不碰签名、不导 cookie、不存凭证。
用 ego-browser（你已经登录的那个 Chromium）打开页面，读渲染后的 DOM。
浏览器自己处理签名和登录态，我们只当"看屏幕的眼睛"。

前提：你在这个浏览器里登录过一次。登出/过期就重新登，脚本会诚实地报"未登录"。

为什么不用 cookie 直连
----------------------
小红书 web API 要 x-s/x-t/x-s-common 三个签名头，算法在混淆 JS 里，每个版本都变；
YouTube 的 trending 从 2024 起对无登录请求一律重定向回首页。
两条路都通向"维护一个每天都在坏的东西"。浏览器渲染是唯一稳的抽象层。

代价：慢（每次开页面 6-12 秒），不能高频轮询。适合每 10 分钟以上一轮。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse

EGO = os.path.expanduser("~/.local/bin/ego-browser")

# 小红书登录框出现时页面上的特征文案
XHS_LOGIN_MARK = "登录后推荐更懂你的笔记"


def _run_js(script: str, timeout: int = 150) -> dict:
    """把一段 ego-browser 脚本喂给 CLI，取回它 console.log 出来的 JSON。"""
    if not os.path.exists(EGO):
        raise RuntimeError(f"ego-browser 不在 {EGO}（先启动一次 ego lite 完成 onboarding）")
    proc = subprocess.run(
        [EGO, "nodejs"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    # 注意：ego-browser 把 console.log 写到 stderr（stdout 留空），两个流都翻。
    merged = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(f"ego-browser 退出码 {proc.returncode}: {merged.strip()[-400:]}")
    for line in reversed(merged.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"没拿到 JSON，输出尾部: {merged.strip()[-300:]}")


# ---------------------------------------------------------------- 小红书


XHS_JS = r'''
const task = await taskSpace("xhs-radar");
const page = task.page("p1");
await page.cdp("Network.setCacheDisabled", {cacheDisabled: true});
await page.goto("https://www.xiaohongshu.com/explore");
await new Promise(r => setTimeout(r, 5500));

const out = await page.evaluate(() => {
  const txt = document.body.innerText || "";
  const logged = !txt.includes("登录后推荐更懂你的笔记");
  const uniq = a => [...new Set(a.filter(Boolean))];

  // 热搜：小红书把热词放在搜索下拉里，类名带 hot / 榜
  const hotSelectors = [
    '[class*="hot-word"]', '[class*="hotword"]', '[class*="hotSearch"]',
    '[class*="hot-list"] li', '[class*="search-hots"] a', '[class*="trend-item"]'
  ];
  let hot = [];
  for (const sel of hotSelectors) {
    const got = [...document.querySelectorAll(sel)]
      .map(e => (e.innerText || "").trim().replace(/\s+/g, " "))
      .filter(t => t && t.length < 60);
    if (got.length > 3) { hot = got; break; }
  }

  // 推荐流笔记标题
  const notes = [...document.querySelectorAll('#noteContainer a[href*="/explore/"], section a[href*="/explore/"]')]
    .map(a => (a.innerText || "").trim().replace(/\s+/g, " "))
    .filter(t => t.length > 4 && t.length < 80);

  return {
    logged,
    hot: uniq(hot).slice(0, 40),
    notes: uniq(notes).slice(0, 30),
    bodyHead: txt.slice(0, 200).replace(/\n+/g, " | ")
  };
});
await task.finish({keep:[]});
console.log(JSON.stringify(out));
'''


def xiaohongshu():
    """小红书：登录态下抓热搜 + 推荐流标题。"""
    from sources import Item

    data = _run_js(XHS_JS)
    logged = bool(data.get("logged"))

    out: list[Item] = []
    for i, word in enumerate(data.get("hot") or [], 1):
        out.append(
            Item(rank=len(out) + 1, title=word,
                 url="https://www.xiaohongshu.com/search_result?keyword="
                     + urllib.parse.quote(word))
        )
    # 热搜要登录才稳；未登录就用推荐流标题 —— 那同样是"平台正在推给谁看什么"
    if not out:
        for title in data.get("notes") or []:
            out.append(
                Item(rank=len(out) + 1, title=title,
                     url="https://www.xiaohongshu.com/explore",
                     meta={"state": "登录态" if logged else "未登录·推荐流"})
            )

    if not out:
        raise RuntimeError("页面打开了，但既没抓到热搜也没抓到推荐流（DOM 结构可能改版）")
    return out


# ---------------------------------------------------------------- YouTube


YT_JS = r'''
const task = await taskSpace("yt-radar");
const page = task.page("p1");
await page.cdp("Network.setCacheDisabled", {cacheDisabled: true});
await page.goto("https://www.youtube.com/feed/trending?gl=US&hl=en");
await new Promise(r => setTimeout(r, 9000));

const out = await page.evaluate(() => {
  const txt = document.body.innerText || "";
  const titles = [...document.querySelectorAll(
      'a#video-title, ytd-video-renderer #video-title, #title-text a, a[href*="/watch?v="]')
    ]
    .map(a => (a.getAttribute('title') || a.innerText || "").trim().replace(/\s+/g, " "))
    .filter(t => t.length > 8 && t.length < 120);
  const uniq = [...new Set(titles)];
  return {
    count: uniq.length,
    titles: uniq.slice(0, 50),
    loggedOut: txt.includes("Sign in") && uniq.length < 5,
    bodyLen: txt.length
  };
});
await task.finish({keep:[]});
console.log(JSON.stringify(out));
'''


def youtube_browser():
    """YouTube 趋势：靠浏览器登录态渲染，绕开 trending 的登录墙。"""
    from sources import Item

    data = _run_js(YT_JS)
    titles = data.get("titles") or []
    if not titles:
        raise RuntimeError(
            f"页面没渲染出视频（bodyLen={data.get('bodyLen')}）"
            " —— YouTube 未登录会跳回首页，在 ego lite 里登录 Google 账号再试"
        )
    out = []
    for t in titles:
        out.append(Item(rank=len(out) + 1, title=t,
                        url="https://www.youtube.com/results?search_query=" + urllib.parse.quote(t)))
    return out


# ---------------------------------------------------------------- YouTube API


def youtube_api():
    """
    YouTube Data API v3 —— 官方路线，不需要登录，只要一把 key。
    chart=mostPopular 就是 trending，1 次请求 1 unit，免费额度 10000/天。
    key 来源：环境变量 YT_API_KEY，或 config.json 的 youtube_api_key。
    """
    from sources import Item, fetch_json

    key = os.environ.get("YT_API_KEY", "").strip()
    if not key:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    key = (json.load(f).get("youtube_api_key") or "").strip()
            except (json.JSONDecodeError, OSError):
                key = ""
    if not key:
        raise RuntimeError(
            "没有 YouTube API key：环境变量 YT_API_KEY，或 config.json 里 youtube_api_key"
        )

    region = os.environ.get("YT_REGION", "US")
    d = fetch_json(
        "https://www.googleapis.com/youtube/v3/videos"
        f"?part=snippet,statistics&chart=mostPopular&regionCode={region}"
        f"&maxResults=50&key={key}"
    )
    if "error" in d:
        raise RuntimeError(f"API 报错: {d['error'].get('message', '')[:160]}")

    out: list[Item] = []
    for v in d.get("items", []):
        sn = v.get("snippet") or {}
        st = v.get("statistics") or {}
        title = (sn.get("title") or "").strip()
        if not title:
            continue
        views = int(st.get("viewCount") or 0)
        out.append(
            Item(
                rank=len(out) + 1,
                title=title,
                hot=float(views),
                hot_text=f"{views / 1e6:.1f}M 播放" if views >= 1e6 else f"{views / 1e3:.0f}K 播放",
                url=f"https://www.youtube.com/watch?v={v.get('id', '')}",
                meta={"channel": sn.get("channelTitle") or "", "region": region},
            )
        )
    if not out:
        raise RuntimeError("API 返回空列表（region 可能没有 trending 数据）")
    return out
