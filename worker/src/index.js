// Cloudflare Worker — three responsibilities:
//   1. POST /             — HARRO LIFE subscription endpoint (Resend Audience).
//   2. GET/POST /unsubscribe — RFC 8058 one-click unsubscribe + confirmation page.
//   3. scheduled()        — reliable cron trigger that POSTs to GitHub API to
//                            workflow_dispatch the daily news + weekly column workflows.
//                            GitHub Actions' built-in `schedule:` is best-effort and silently
//                            skipped on low-traffic repos (we lost a day of content on 4/30
//                            because of this); Cloudflare Workers cron is ±1 minute reliable.
//
// Required environment variables (set in Cloudflare dashboard or wrangler secrets):
//   RESEND_API_KEY         - Resend API key (re_...)
//   RESEND_AUDIENCE_ID     - HARRO LIFE news Audience (uuid) — every subscriber lands here
//   MARKETING_AUDIENCE_ID  - HARRO Marketing Audience (uuid) — opt-in only; if unset, the
//                            checkbox value is silently ignored (graceful degradation)
//   EMAIL_FROM             - sender address (e.g. onboarding@resend.dev)
//   ALLOWED_ORIGIN         - comma-separated allowed origins (e.g. https://agus95-89.github.io)
//   GH_DISPATCH_TOKEN      - GitHub PAT with `workflow` scope; used to dispatch
//                            daily-news.yml + weekly-column.yml on holland-daily repo
//   GH_DISPATCH_OWNER      - GitHub owner (default "agus95-89")
//   GH_DISPATCH_REPO       - GitHub repo (default "holland-daily")

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/unsubscribe") {
      return handleUnsubscribe(request, env);
    }

    return handleSubscribe(request, env);
  },

  /**
   * Cron trigger handler. Dispatches the appropriate GitHub Actions
   * workflow based on which cron schedule fired.
   *
   * Schedules (UTC, see wrangler.toml [triggers] crons):
   *   0 6/7 * * *   → daily-news.yml — fires twice daily, Python's
   *                   already_ran_today() guard ensures only one actually
   *                   produces an episode (one for DST, one for CET)
   *   0 6/7 * * 4   → weekly-column.yml on Thursdays additionally
   */
  async scheduled(event, env, ctx) {
    const cron = event.cron;
    console.log(`Cron fired: ${cron}`);
    const owner = env.GH_DISPATCH_OWNER || "agus95-89";
    const repo = env.GH_DISPATCH_REPO || "holland-daily";

    // Thursday-only crons (with `* * 4` day-of-week field) fire the column
    // workflow in addition to the daily one.
    const isThursdayCron = cron.endsWith("* * 4");
    const workflow = isThursdayCron ? "weekly-column.yml" : "daily-news.yml";

    ctx.waitUntil(dispatchWorkflow(env, owner, repo, workflow));
  },
};

async function handleSubscribe(request, env) {
  const origin = request.headers.get("Origin") || "";
  const corsHeaders = {
    "Access-Control-Allow-Origin": isAllowedOrigin(origin, env) ? origin : "null",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Vary": "Origin",
  };

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }

  if (request.method !== "POST") {
    return jsonResp({ error: "Method not allowed" }, 405, corsHeaders);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResp({ error: "Invalid JSON" }, 400, corsHeaders);
  }

  const email = (body.email || "").trim().toLowerCase();
  if (!email || !EMAIL_RE.test(email) || email.length > 320) {
    return jsonResp({ error: "Invalid email" }, 400, corsHeaders);
  }

  // Marketing opt-in is a separate Audience the user can opt INTO at signup
  // for HARRO product / promo updates (separate from daily news). Defaults to
  // false on the form per GDPR (no pre-ticked consent).
  const marketingOptIn = body.marketing_optin === true;

  try {
    const newsResult = await addToAudience(env, env.RESEND_AUDIENCE_ID, email);
    if (newsResult === "error") {
      return jsonResp({ error: "Subscription failed" }, 500, corsHeaders);
    }

    // If they opted into marketing, also add to the marketing Audience.
    // This is best-effort: a failure here does not break the news
    // subscription (which already succeeded above).
    if (marketingOptIn && env.MARKETING_AUDIENCE_ID) {
      const mktResult = await addToAudience(env, env.MARKETING_AUDIENCE_ID, email);
      if (mktResult === "error") {
        console.warn(
          "Marketing add failed for",
          email,
          "— news subscription succeeded, marketing did not",
        );
      }
    } else if (marketingOptIn && !env.MARKETING_AUDIENCE_ID) {
      console.warn(
        "MARKETING_AUDIENCE_ID not set — opt-in flag ignored for",
        email,
      );
    }

    if (newsResult === "exists") {
      return jsonResp({ message: "Already subscribed" }, 200, corsHeaders);
    }

    await sendWelcome(env, email).catch((err) =>
      console.error("Welcome email failed (non-fatal):", err),
    );

    return jsonResp({ message: "Subscribed" }, 200, corsHeaders);
  } catch (err) {
    console.error("Unexpected error:", err);
    return jsonResp({ error: "Server error" }, 500, corsHeaders);
  }
}

/**
 * Add an email to a Resend Audience. Returns:
 *   "added"  — newly inserted
 *   "exists" — already present (treated as success at the call site)
 *   "error"  — unexpected failure
 */
async function addToAudience(env, audienceId, email) {
  const resp = await fetch(
    `https://api.resend.com/audiences/${audienceId}/contacts`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.RESEND_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ email, unsubscribed: false }),
    },
  );
  if (resp.ok) return "added";
  const text = await resp.text();
  const alreadyExists =
    resp.status === 409 ||
    /already.*exist/i.test(text) ||
    /duplicate/i.test(text);
  if (alreadyExists) return "exists";
  console.error(
    `Resend add contact failed (audience=${audienceId})`,
    resp.status,
    text,
  );
  return "error";
}

async function handleUnsubscribe(request, env) {
  const url = new URL(request.url);
  const email = (url.searchParams.get("email") || "").trim().toLowerCase();

  if (!email || !EMAIL_RE.test(email)) {
    return htmlResp(unsubErrorHtml("メールアドレスが指定されていません。"), 400);
  }

  if (request.method === "GET") {
    // GET = render confirmation page. We deliberately do NOT unsubscribe on GET so
    // that email scanners (Gmail link prefetch etc.) cannot accidentally trigger it.
    return htmlResp(unsubConfirmHtml(email), 200);
  }

  if (request.method === "POST") {
    const ok = await markResendContactUnsubscribed(env, email);
    if (!ok) {
      return htmlResp(
        unsubErrorHtml("配信停止の処理に失敗しました。お手数ですが suga@harrojp.com までご連絡ください。"),
        500,
      );
    }
    return htmlResp(unsubSuccessHtml(email), 200);
  }

  return new Response("Method not allowed", { status: 405 });
}

async function markResendContactUnsubscribed(env, email) {
  // Resend's API: PATCH /audiences/{audience_id}/contacts/{email_or_id}
  // The path parameter accepts either the contact ID or the email address.
  const audience = env.RESEND_AUDIENCE_ID;
  const url = `https://api.resend.com/audiences/${audience}/contacts/${encodeURIComponent(email)}`;
  const resp = await fetch(url, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ unsubscribed: true }),
  });
  if (resp.ok) return true;
  const text = await resp.text();
  console.error("Resend unsubscribe failed", resp.status, text);
  // 404 means the email was never subscribed — treat as success from the user's POV.
  return resp.status === 404;
}

async function dispatchWorkflow(env, owner, repo, workflow) {
  if (!env.GH_DISPATCH_TOKEN) {
    console.error("GH_DISPATCH_TOKEN not set; cannot dispatch", workflow);
    return;
  }
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  const inputs =
    workflow === "daily-news.yml" ? { force_run: false } : {};
  // force_run: false lets the Python window guard + idempotency check apply
  // (so a second fire same day silently skips). Manual workflow_dispatch from
  // the GitHub UI defaults to true, which is the right escape hatch for
  // humans but wrong for an unattended cron.
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "harro-life-cron/1.0",
    },
    body: JSON.stringify({ ref: "main", inputs }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    console.error(`workflow_dispatch ${workflow} failed`, resp.status, text);
    return;
  }
  console.log(`workflow_dispatch ${workflow} OK`);
}

function isAllowedOrigin(origin, env) {
  if (!env.ALLOWED_ORIGIN) return true;
  const allowed = env.ALLOWED_ORIGIN.split(",").map((s) => s.trim());
  return allowed.includes(origin);
}

function jsonResp(data, status, headers) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function htmlResp(html, status) {
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

async function sendWelcome(env, to) {
  const from = env.EMAIL_FROM || "onboarding@resend.dev";
  const html = welcomeHtml();
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from,
      to: [to],
      subject: "HARRO LIFE へようこそ",
      html,
    }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`welcome send failed ${resp.status}: ${text}`);
  }
}

const HARRO_LIFE_LOGO_URL =
  "https://harro-life-site.pages.dev/images/brand/harro-life-on-dark.png";

function welcomeHtml() {
  return `<!doctype html>
<html lang="ja"><body style="margin:0;padding:0;background:#faf7f2;font-family:-apple-system,'Helvetica Neue','Hiragino Sans','Yu Gothic',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#faf7f2;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;margin:40px 20px;border-radius:8px;overflow:hidden;">
<tr><td style="background:#09202e;padding:22px 32px;">
<img src="${HARRO_LIFE_LOGO_URL}" alt="HARRO LIFE" style="height:36px;width:auto;display:block;border:0;" />
</td></tr>
<tr><td style="padding:40px 32px;">
<div style="color:#666;font-size:15px;margin-bottom:32px;">オランダのニュースを、日本語で。</div>

<p style="font-size:15px;line-height:1.8;color:#333;">ご購読ありがとうございます。</p>
<p style="font-size:15px;line-height:1.8;color:#333;">明日の朝9時（オランダ時間）から、毎日 HARRO LIFE をメールでお届けします。</p>
<p style="font-size:15px;line-height:1.8;color:#333;">オランダの主要メディアから選んだ10本のニュースを日本語で要約、ポッドキャスト版も併せてお楽しみください。</p>

<div style="margin-top:40px;padding:28px 0;border-top:1px solid #eee;text-align:center;font-size:13px;color:#555;">
<a href="https://shop.harrojp.com/ja-nl" style="color:#1a1a1a;text-decoration:none;border-bottom:1px solid #ddd;padding-bottom:1px;">HARRO Online Shop</a>
<span style="color:#ccc;margin:0 12px;">·</span>
<a href="https://www.instagram.com/harro_app/" style="color:#1a1a1a;text-decoration:none;border-bottom:1px solid #ddd;padding-bottom:1px;">Instagram</a>
<div style="margin-top:14px;font-size:11px;color:#bbb;letter-spacing:0.1em;">Presented by HARRO</div>
</div>

</td></tr></table>
</td></tr></table>
</body></html>`;
}

function unsubBaseHtml(title, bodyHtml) {
  return `<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="robots" content="noindex" />
<title>${escapeHtml(title)} — HARRO LIFE</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; padding:0; background:#faf7f2; font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif; color:#1a1a1a; }
  .wrap { max-width:520px; margin:0 auto; padding:40px 24px; }
  .card { background:#ffffff; border-radius:12px; padding:36px 28px; box-shadow:0 1px 2px rgba(0,0,0,0.04); }
  .header-band { background:#09202e; padding:22px 28px; border-radius:12px 12px 0 0; margin:-36px -28px 28px; }
  .header-band img { height:32px; width:auto; display:block; }
  h1 { font-size:22px; margin:0 0 12px; letter-spacing:-0.01em; }
  p { font-size:15px; line-height:1.75; color:#333; margin:0 0 14px; }
  .email { font-weight:700; word-break:break-all; }
  form { margin-top:24px; }
  button, .btn-link { display:inline-block; background:#9E3E24; color:#ffffff; border:0; padding:14px 28px; border-radius:999px; font-size:15px; font-weight:700; cursor:pointer; text-decoration:none; }
  button:hover, .btn-link:hover { background:#7c2d1b; }
  .secondary { display:inline-block; margin-left:8px; color:#666; font-size:14px; text-decoration:underline; }
  .muted { color:#888; font-size:13px; margin-top:18px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="header-band">
      <img src="${HARRO_LIFE_LOGO_URL}" alt="HARRO LIFE" />
    </div>
    ${bodyHtml}
  </div>
</div>
</body></html>`;
}

function unsubConfirmHtml(email) {
  return unsubBaseHtml(
    "配信停止の確認",
    `
    <h1>HARRO LIFE の配信を停止しますか？</h1>
    <p>このメールアドレスへの配信を停止します:</p>
    <p class="email">${escapeHtml(email)}</p>
    <form method="POST" action="/unsubscribe?email=${encodeURIComponent(email)}">
      <button type="submit">配信を停止する</button>
      <a href="https://harro-life-site.pages.dev/" class="secondary">やめる</a>
    </form>
    <p class="muted">いつでも再購読できます。</p>
    `,
  );
}

function unsubSuccessHtml(email) {
  return unsubBaseHtml(
    "配信停止しました",
    `
    <h1>配信を停止しました</h1>
    <p>このメールアドレスへの HARRO LIFE 配信は停止されました:</p>
    <p class="email">${escapeHtml(email)}</p>
    <p>ご購読ありがとうございました。気が向いたら、いつでもまた戻ってきてください。</p>
    <p style="margin-top:24px;">
      <a href="https://harro-life-site.pages.dev/" class="btn-link">HARRO LIFE に戻る</a>
    </p>
    `,
  );
}

function unsubErrorHtml(message) {
  return unsubBaseHtml(
    "エラー",
    `
    <h1>処理できませんでした</h1>
    <p>${escapeHtml(message)}</p>
    <p style="margin-top:24px;">
      <a href="https://harro-life-site.pages.dev/" class="btn-link">HARRO LIFE に戻る</a>
    </p>
    `,
  );
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
