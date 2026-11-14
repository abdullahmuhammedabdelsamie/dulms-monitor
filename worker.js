// Relay: Telegram -> GitHub. Runs on Cloudflare Workers (free).
// When YOU message the bot, it answers "⏳" at once and wakes the GitHub workflow with your text.
export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");

    // only Telegram (with our secret) may call this
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TG_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    // only YOU (the owner) may trigger anything
    const msg = update.message;
    if (!msg || !msg.text || String(msg.chat?.id) !== String(env.OWNER_CHAT_ID)) {
      return new Response("ignored");
    }

    const say = (text) =>
      fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: msg.chat.id, text }),
      });

    await say("⏳ استلمت، ثواني وأرد عليك...");

    const gh = await fetch(
      `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/monitor.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "dulms-relay",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: env.GH_BRANCH || "main", inputs: { text: msg.text.slice(0, 200) } }),
      }
    );
    if (!gh.ok) await say(`⚠️ ما قدرتش أشغّل التنفيذ (GitHub رد بـ ${gh.status}).`);

    return new Response("ok"); // always 200, so Telegram never re-sends
  },
};
