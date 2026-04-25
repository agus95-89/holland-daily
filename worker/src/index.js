// Cloudflare Worker: Holland Daily subscription endpoint.
// Receives { email } POST, adds to Resend Audience, sends welcome email.
//
// Required environment variables (set in Cloudflare dashboard or wrangler secrets):
//   RESEND_API_KEY       - Resend API key (re_...)
//   RESEND_AUDIENCE_ID   - Resend Audience ID (uuid)
//   EMAIL_FROM           - sender address (e.g. onboarding@resend.dev)
//   ALLOWED_ORIGIN       - comma-separated allowed origins (e.g. https://agus95-89.github.io)

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
};

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
