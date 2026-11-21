// functions/api/ask.js — 围绕一篇论文的追问
// 环境变量：PATEWAY_KEY / PATEWAY_BASE / MODEL

import { auth, needAuthResponse } from "./_auth.js";

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const ASK_PROMPT = `你是这篇论文的讲解员。用中文、说人话回答，必要时打比方或举例。
不确定就说不确定，不要编造论文没写的内容。回答控制在 300 字内。

【论文】
{paper}

【之前的问答】
{history}

【这次的问题】
{question}`;

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
  s = s.replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">");
  return s.replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
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
  const authors = [...entry.matchAll(/<author>\s*<name>([\s\S]*?)<\/name>/g)].map((m) => m[1].trim());
  return {
    title: pick("title"),
    abstract: pick("summary"),
    authors,
    fulltext: "",
    url: `https://arxiv.org/abs/${aid}`,
  };
}

async function fetchWeb(url) {
  const r = await fetch(url, { headers: { "User-Agent": UA, Accept: "text/html" } });
  const html = await r.text();
  const text = stripHtml(html);
  const tm = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  return {
    title: tm ? tm[1].replace(/\s+/g, " ").trim().slice(0, 200) : url,
    abstract: text.slice(0, 5000),
    authors: [],
    fulltext: text.slice(0, 6000),
    url,
  };
}

async function fetchPaper(url) {
  let m = url.match(/arxiv\.org\/(?:abs|pdf)\/(\d{4}\.\d{4,5})/);
  if (!m) m = url.match(/arxiv\.org\/(?:abs|pdf)\/([a-z\-]+\/\d{7})/);
  return m ? fetchArxiv(m[1]) : fetchWeb(url);
}

async function llm(who, prompt, timeoutMs = 90000) {
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
      body: JSON.stringify({ model, input: prompt }),
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error(`LLM ${r.status}: ${(await r.text()).slice(0, 200)}`);
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
  try { body = await request.json(); } catch { /* noop */ }
  const q = (body.question || "").trim();
  let url = (body.url || "").trim();
  if (!q) return json({ error: "缺少 question" }, 400);
  if (url && !/^https?:\/\//.test(url)) url = "https://" + url;

  try {
    // 前端已经带了论文数据就直接用，省一次抓取（也省时间）
    let paper = body.paper;
    if (!paper || !paper.title) {
      paper = url ? await fetchPaper(url) : { title: "", abstract: "", authors: [], fulltext: "", url: "" };
    }
    const a = body.analysis || null;

    let ctx =
      `标题：${paper.title}\n作者：${(paper.authors || []).join(", ") || "未知"}\n` +
      `链接：${paper.url}\n摘要：${(paper.abstract || "").slice(0, 3000)}\n`;
    if (paper.fulltext) ctx += `正文节选：${paper.fulltext.slice(0, 4000)}\n`;
    if (a) {
      ctx += `\n已有的解读：\n- 一句话：${a.tldr || ""}\n- 问题：${a.problem || ""}\n` +
             `- 方法：${a.method || ""}\n- 结果：${a.results || ""}\n`;
    }

    const hist = (body.history || []).slice(-4)
      .map((h) => `Q: ${h.q || ""}\nA: ${h.a || ""}`).join("\n") || "(无)";

    const res = await llm(who, ASK_PROMPT.replace("{paper}", ctx)
      .replace("{history}", hist).replace("{question}", q));
    return json({ ok: true, answer: res.text.trim(), usage: res.usage });
  } catch (e) {
    return json({ error: String(e.message || e) }, 500);
  }
}
