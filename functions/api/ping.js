// functions/api/ping.js — 轻量凭据校验，前端打开时先打这个
import { auth } from "./_auth.js";

export async function onRequest(context) {
  const { request, env } = context;
  const who = auth(env, request);
  return new Response(JSON.stringify({ ok: who.ok, byok: !!who.byok }), {
    status: who.ok ? 200 : 401,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}
