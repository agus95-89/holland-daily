// Cloudflare Worker — two responsibilities:
//   1. POST /  — Holland Daily subscription endpoint (Resend Audience).
//   2. scheduled() — reliable cron trigger that POSTs to GitHub API to
//      workflow_dispatch the daily news + weekly column workflows.
//      GitHub Actions' built-in `schedule:` is best-effort and silently
//      skipped on low-traffic repos (we lost a day of content on 4/30
//      because of this); Cloudflare Workers cron is ±1 minute reliable.
//
// Required environment variables (set in Cloudflare dashboard or wrangler secrets):
//   RESEND_API_KEY       - Resend API key (re_...)
//   RESEND_AUDIENCE_ID   - Resend Audience ID (uuid)
//   EMAIL_FROM           - sender address (e.g. onboarding@resend.dev)
//   ALLOWED_ORIGIN       - comma-separated allowed origins (e.g. https://agus95-89.github.io)
//   GH_DISPATCH_TOKEN    - GitHub PAT with `workflow` scope; used to dispatch
//                          daily-news.yml + weekly-column.yml on holland-daily repo
//   GH_DISPATCH_OWNER    - GitHub owner (default "agus95-89")
//   GH_DISPATCH_REPO     - GitHub repo (default "holland-daily")

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default {
  async fetch(request, env) {
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

    try {
      const addResp = await fetch(
        `https://api.resend.com/audiences/${env.RESEND_AUDIENCE_ID}/contacts`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${env.RESEND_API_KEY}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ email, unsubscribed: false }),
        },
      );

      const addBody = await addResp.text();
      if (!addResp.ok) {
        const alreadyExists =
          addResp.status === 409 ||
          /already.*exist/i.test(addBody) ||
          /duplicate/i.test(addBody);
        if (alreadyExists) {
          return jsonResp({ message: "Already subscribed" }, 200, corsHeaders);
        }
        console.error("Resend add contact failed", addResp.status, addBody);
        return jsonResp({ error: "Subscription failed" }, 500, corsHeaders);
      }

      await sendWelcome(env, email).catch((err) =>
        console.error("Welcome email failed (non-fatal):", err),
      );

      return jsonResp({ message: "Subscribed" }, 200, corsHeaders);
    } catch (err) {
      console.error("Unexpected error:", err);
      return jsonResp({ error: "Server error" }, 500, corsHeaders);
    }
  },

  /**
   * Cron trigger handler. Dispatches the appropriate GitHub Actions
   * workflow based on which cron schedule fired.
   *
   * Schedules (UTC, see wrangler.toml [triggers] crons):
   *   0 8 * * *   → daily-news.yml every day
   *                 (= 10:00 NL DST or 9:00 NL CET, both inside the
   *                  Python-side 6h window starting at 09:00 NL)
   *   0 8 * * 4   → weekly-column.yml on Thursdays only (additionally;
   *                 daily-news.yml still fires from the * cron)
   */
  async scheduled(event, env, ctx) {
    const cron = event.cron;
    console.log(`Cron fired: ${cron}`);
    const owner = env.GH_DISPATCH_OWNER || "agus95-89";
    const repo = env.GH_DISPATCH_REPO || "holland-daily";

    // Map cron expression to workflow file. Both crons may fire on Thursday;
    // each dispatches its own workflow.
    let workflow;
    if (cron === "0 8 * * 4") {
      workflow = "weekly-column.yml";
    } else {
      workflow = "daily-news.yml";
    }

    ctx.waitUntil(dispatchWorkflow(env, owner, repo, workflow));
  },
};

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
      subject: "Holland Daily へようこそ",
      html,
    }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`welcome send failed ${resp.status}: ${text}`);
  }
}

function welcomeHtml() {
  return `<!doctype html>
<html lang="ja"><body style="margin:0;padding:0;background:#faf7f2;font-family:-apple-system,'Helvetica Neue','Hiragino Sans','Yu Gothic',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#faf7f2;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;margin:40px 20px;">
<tr><td style="padding:44px 32px;">
<div style="font-size:11px;color:#ff6b35;letter-spacing:0.2em;text-transform:uppercase;font-weight:700;margin-bottom:24px;">Presented by HARRO</div>
<h1 style="font-size:36px;font-weight:200;letter-spacing:-0.04em;margin:0 0 6px;color:#1a1a1a;">Holland Daily<span style="color:#ff6b35;">.</span></h1>
<div style="color:#666;font-size:15px;margin-bottom:32px;">オランダのニュースを、日本語で。</div>

<p style="font-size:15px;line-height:1.8;color:#333;">ご購読ありがとうございます。</p>
<p style="font-size:15px;line-height:1.8;color:#333;">明日の朝9時（オランダ時間）から、毎日 Holland Daily をメールでお届けします。</p>
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
