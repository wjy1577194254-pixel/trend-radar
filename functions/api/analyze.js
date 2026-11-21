// functions/api/analyze.js — 论文抓取 + 结构化解读
// 部署在 Cloudflare Pages Functions，不需要本机常驻
// 环境变量：PATEWAY_KEY / PATEWAY_BASE / MODEL

import { auth, needAuthResponse } from "./_auth.js";

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const ANALYZE_PROMPT = `你是论文讲解员，给中文读者讲清楚一篇论文。

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
术语挑 3-6 个读者最可能不懂的。说人话，不要复述原文。

如果给了正文节选，method 和 results 要引用正文里的具体做法和数字，不要停留在摘要层面；
limits 优先用作者自己承认的局限或实验没覆盖的情形。`;

function json(obj, code = 200) {
  return new Response(JSON.stringify(obj), {
    status: code,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function stripHtml(html) {
  let s = html.replace(/<(script|style|nav|footer|header|aside)[^>]*>[\s\S]*?<\/\1>/gi, " ");
  s = s.replace(/<br\s*\/?>|<\/p>|<\/div>|<\/h[1-6]>/gi, "\n");
  s = s.replace(/<[^>]+>/g, " ");
  s = s.replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<")
       .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  s = s.replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n");
  return s.trim();
}

async function fetchArxiv(aid) {
  const r = await fetch(`http://export.arxiv.org/api/query?id_list=${encodeURIComponent(aid)}`, {
    headers: { "User-Agent": UA },
  });
  const xml = await r.text();
  const entry = xml.split("<entry>")[1] || "";
  const pick = (tag) => {
    const m = entry.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`));
    return m ? m[1].replace(/\s+/g, " ").trim() : "";
  };
  const authors = [...entry.matchAll(/<author>\s*<name>([\s\S]*?)<\/name>/g)]
    .map((m) => m[1].trim())
    .filter(Boolean);
  const cats = [...entry.matchAll(/<category[^>]*term="([^"]+)"/g)].map((m) => m[1]);
  // 再抓 arXiv 的 HTML 全文版（2024 年后大部分论文都有）；失败就只用摘要
  let fulltext = "";
  try {
    const ctrl = new AbortController();
    const tm = setTimeout(() => ctrl.abort(), 9000);
    const hr = await fetch(`https://arxiv.org/html/${aid}`, {
      headers: { "User-Agent": UA, Accept: "text/html" },
      signal: ctrl.signal,
    });
    clearTimeout(tm);
    if (hr.ok) {
      const h = await hr.text();
      fulltext = stripHtml(h).slice(0, 22000);
    }
  } catch (_) { /* 全文抓不到不影响解读 */ }

  return {
    source: "arxiv",
    arxiv_id: aid,
    title: pick("title"),
    abstract: pick("summary"),
    authors,
    categories: cats,
    fulltext,
    has_fulltext: !!fulltext,
    url: `https://arxiv.org/abs/${aid}`,
  };
}

async function fetchWeb(url) {
  const r = await fetch(url, { headers: { "User-Agent": UA, Accept: "text/html" } });
  const html = await r.text();
  const text = stripHtml(html);
  const tm = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  return {
    source: "web",
    title: tm ? tm[1].replace(/\s+/g, " ").trim().slice(0, 200) : url,
    abstract: text.slice(0, 8000),
    authors: [],
    fulltext: text.slice(0, 12000),
    url,
  };
}

async function fetchPaper(url) {
  let m = url.match(/arxiv\.org\/(?:abs|pdf)\/(\d{4}\.\d{4,5})/);
  if (!m) m = url.match(/arxiv\.org\/(?:abs|pdf)\/([a-z\-]+\/\d{7})/);
  return m ? fetchArxiv(m[1]) : fetchWeb(url);
}

async function llm(who, instructions, input, timeoutMs = 110000) {
  const base = who.base.replace(/\/$/, "");
  const key = who.key;
  if (!key) throw new Error("没有可用的 API Key");
  const model = who.model;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(`${base}/responses`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model, instructions, input }),
      signal: ctrl.signal,
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(`LLM ${r.status}: ${t.slice(0, 200)}`);
    }
    const d = await r.json();
    let text = "";
    if (d.output) {
      for (const o of d.output || []) {
        for (const c of o.content || []) {
          if (c.type === "output_text" || c.type === "text") text += c.text || "";
        }
      }
    } else {
      text = d?.choices?.[0]?.message?.content || "";
    }
    return { text, usage: d.usage || null };
  } finally {
    clearTimeout(timer);
  }
}

export async function onRequest(context) {
  const { request, env } = context;
  if (request.method !== "POST") return json({ error: "POST only" }, 405);

  const who = auth(env, request);
  if (!who.ok) return needAuthResponse();

  let body = {};
  try { body = await request.json(); } catch { /* 空 body */ }
  let url = (body.url || "").trim();
  if (!url) return json({ error: "缺少 url" }, 400);
  if (!/^https?:\/\//.test(url)) url = "https://" + url;

  const t0 = Date.now();
  try {
    const paper = await fetchPaper(url);
    if (!paper.title && !paper.abstract) throw new Error("没抓到论文内容");

    const payload =
      `标题：${paper.title}\n` +
      `作者：${(paper.authors || []).join(", ") || "未知"}\n` +
      `分类：${(paper.categories || []).join(", ") || "未知"}\n` +
      `链接：${paper.url}\n\n` +
      `摘要：\n${paper.abstract || "(无)"}\n` +
      (paper.fulltext ? `\n【正文节选（共 22000 字，下面是关键段）】\n${paper.fulltext.slice(0, 18000)}\n` : "");

    const res = await llm(who, ANALYZE_PROMPT, payload);
    const txt = res.text;
    const cleaned = txt.replace(/^```(?:json)?/gm, "").replace(/```$/gm, "").trim();
    const m = cleaned.match(/\{[\s\S]*\}/);
    if (!m) throw new Error("模型没返回 JSON");

    const analysis = JSON.parse(m[0]);
    analysis._meta = {
      source: paper.source,
      title_en: paper.title,
      url: paper.url,
      arxiv_id: paper.arxiv_id || "",
      elapsed: Math.round((Date.now() - t0) / 100) / 10,
    };
    return json({ ok: true, paper, analysis, usage: res.usage });
  } catch (e) {
    return json({ error: String(e.message || e) }, 500);
  }
}
