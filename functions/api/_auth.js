// functions/api/_auth.js — 访问控制
//
// 两种通过方式：
//   1) x-user-key：调用者自己的 API Key（走他自己的额度，站点不花钱）
//   2) x-access-code：站点所有者分配的访问码，匹配 ACCESS_CODE 才放行（走站点额度）
//
// 两者都没有 → 401 + needAuth，前端弹输入框。

export function auth(env, request) {
  const userKey = (request.headers.get("x-user-key") || "").trim();
  if (userKey) {
    return {
      ok: true,
      byok: true,
      key: userKey,
      base: (request.headers.get("x-user-base") || env.PATEWAY_BASE || "https://api.pateway.ai/v1").trim(),
      model: (request.headers.get("x-user-model") || env.MODEL || "gpt-6.2-sol").trim(),
    };
  }

  const code = (request.headers.get("x-access-code") || "").trim();
  if (code && env.ACCESS_CODE && code === env.ACCESS_CODE) {
    return {
      ok: true,
      byok: false,
      key: env.PATEWAY_KEY,
      base: env.PATEWAY_BASE || "https://api.pateway.ai/v1",
      model: env.MODEL || "gpt-6.2-sol",
    };
  }

  return { ok: false };
}

export function needAuthResponse() {
  return new Response(
    JSON.stringify({
      error: "需要访问码，或者填写你自己的 API Key",
      needAuth: true,
    }),
    {
      status: 401,
      headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
    }
  );
}
