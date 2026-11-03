// Помощник бота на Cloudflare Workers.
//
// Зачем он нужен: сам бот работает на GitHub и запускается по расписанию, поэтому на кнопки
// отвечал с задержкой. Помощник принимает нажатия мгновенно и сам публикует пост в канал,
// по точному таймеру запускает бота на GitHub и в режиме автопилота публикует посты из очереди.
//
// Хранилище KV (привязка BOT):
//   cfg            — настройки и токен Telegram, их присылает сам бот при каждом запуске
//   post:<id>      — готовый пост и карточка для вас
//   q:<id>         — пост ждёт публикации в очереди автопилота
//   slot:<время>   — пост, отложенный кнопкой «Опубликовать» на ровный час (время — секунды UTC)
//   ev:<время>-<id>-<действие> — что произошло (нажатия, публикации); бот забирает и удаляет
//   pub            — когда и сколько постов вышло сегодня
// Секреты: SHARED_SECRET (общий с ботом), GITHUB_TOKEN (запускать бота). Переменные: GITHUB_REPO, WORKFLOW.

const SEP = "\n\n— — —\n";
const CAPTION_LIMIT = 1024;
const MARKS = {
  posted: "✅ <b>В КАНАЛЕ</b>",
  skipped: "🚫 <b>Пропущено</b>",
  queued: "🕒 <b>В очереди на публикацию</b>",
  removed: "🗑 <b>Убрано из канала</b>",
};

// ---------- мелочи ----------
export function visibleLen(html) {
  const text = html.replace(/<[^>]+>/g, "")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
    .replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, "&");
  return [...text].length;
}

export function hhmm(cfg, sec) {
  return new Date(sec * 1000 + (cfg.tz_offset_min || 420) * 60000).toISOString().slice(11, 16);
}

export function marked(card, status, cfg = {}, slot = 0) {
  if (status === "scheduled") return `🕒 <b>Выйдет в ${hhmm(cfg, slot)}</b> (время Вьетнама)\n\n${card}`;
  return MARKS[status] ? `${MARKS[status]}\n\n${card}` : card;
}

export function postButtons(id, status) {
  if (status === "posted") {
    return [[{ text: "📄 Полный перевод", callback_data: `full:${id}` }, { text: "🗑 Убрать из канала", callback_data: `del:${id}` }]];
  }
  if (status === "confirm_del") {
    return [[{ text: "✅ Да, убрать из канала", callback_data: `delok:${id}` }],
            [{ text: "Нет, оставить", callback_data: `keep:${id}` }]];
  }
  if (status === "scheduled") {
    return [
      [{ text: "⚡ Опубликовать сейчас", callback_data: `now:${id}` }],
      [{ text: "📄 Полный перевод", callback_data: `full:${id}` }, { text: "↩️ Отменить", callback_data: `unsched:${id}` }],
    ];
  }
  if (status === "skipped") return [[{ text: "↩️ Вернуть", callback_data: `back:${id}` }]];
  return [
    [{ text: "🚀 Опубликовать", callback_data: `go:${id}` }],
    [{ text: "📄 Полный перевод", callback_data: `full:${id}` }, { text: "🚫 Мимо", callback_data: `skip:${id}` }],
  ];
}

async function getJSON(env, key, fallback = null) {
  const v = await env.BOT.get(key);
  if (v === null) return fallback;
  try { return JSON.parse(v); } catch { return fallback; }
}

const putJSON = (env, key, value) => env.BOT.put(key, JSON.stringify(value));

let evSeq = 0;   // события в одну миллисекунду не должны перепутаться местами
async function addEvent(env, action, id, extra = {}) {
  evSeq = (evSeq + 1) % 1000000;
  const key = `ev:${Date.now()}-${String(evSeq).padStart(6, "0")}-${id}-${action}`;
  await putJSON(env, key, { key, action, id: Number(id), at: Date.now() / 1000, ...extra });
}

async function tg(cfg, method, params) {
  const r = await fetch(`https://api.telegram.org/bot${cfg.token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(params),
  });
  const data = await r.json().catch(() => ({ ok: false, description: `HTTP ${r.status}` }));
  if (!data.ok) throw new Error(`Telegram ${method}: ${data.description || JSON.stringify(data)}`);
  return data.result;
}

function preview(image) {
  return image ? { url: image, prefer_large_media: true, show_above_text: true } : { is_disabled: true };
}

// Пост в канал: короткий — подписью к фото, длинный — текстом с картинкой сверху
export async function sendPost(cfg, post) {
  if (post.image && visibleLen(post.html) <= CAPTION_LIMIT) {
    try {
      const res = await tg(cfg, "sendPhoto", { chat_id: cfg.channel, photo: post.image, caption: post.html, parse_mode: "HTML" });
      return res.message_id;
    } catch (e) {
      console.log("Фото не отправилось, шлю текстом:", e.message);
    }
  }
  try {
    const res = await tg(cfg, "sendMessage", { chat_id: cfg.channel, text: post.html, parse_mode: "HTML",
                                              link_preview_options: preview(post.image) });
    return res.message_id;
  } catch (e) {
    if (!post.image) throw e;
    const res = await tg(cfg, "sendMessage", { chat_id: cfg.channel, text: post.html, parse_mode: "HTML",
                                              link_preview_options: preview("") });
    return res.message_id;
  }
}

async function redrawCard(cfg, post, status) {
  if (!post.card_msg_id || !post.card_html) return;
  let text = marked(post.card_html, status, cfg, post.slot);
  const markup = { inline_keyboard: postButtons(post.id, status) };
  try {
    if (post.card_kind === "photo") {
      if (visibleLen(text) > CAPTION_LIMIT && text.includes(SEP)) text = text.split(SEP)[0];
      await tg(cfg, "editMessageCaption", { chat_id: cfg.admin, message_id: post.card_msg_id, caption: text,
                                            parse_mode: "HTML", reply_markup: markup });
    } else {
      await tg(cfg, "editMessageText", { chat_id: cfg.admin, message_id: post.card_msg_id, text,
                                         parse_mode: "HTML", link_preview_options: preview(post.image),
                                         reply_markup: markup });
    }
  } catch (e) {
    console.log("Не смог обновить карточку:", e.message);   // пост уже в канале — это главное
  }
}

// Публикация с защитой от повтора: сначала отмечаем, потом отправляем
async function publish(env, cfg, id, how, now = Date.now()) {
  const post = await getJSON(env, `post:${id}`);
  if (!post) return { ok: false, why: "missing" };
  if (post.status === "posted") return { ok: false, why: "already" };
  const msgId = await sendPost(cfg, post);
  post.status = "posted";
  post.posted_at = now / 1000;
  post.channel_msg_id = msgId;
  await putJSON(env, `post:${id}`, post);
  await env.BOT.delete(`q:${id}`);
  if (post.slot) await env.BOT.delete(`slot:${post.slot}`);
  const pub = await getJSON(env, "pub", { last: 0, day: "", count: 0 });
  const day = localDay(cfg, now);
  await putJSON(env, "pub", { last: post.posted_at, day, count: (pub.day === day ? pub.count : 0) + 1 });
  await addEvent(env, "posted", id, { channel_msg_id: msgId, how });
  await redrawCard(cfg, post, "posted");
  return { ok: true, post };
}

// Убрать пост из канала. Telegram разрешает ботам удалять сообщения только первые 48 часов.
export async function unpublish(env, cfg, id) {
  const post = await getJSON(env, `post:${id}`);
  if (!post) return { ok: false, why: "missing" };
  if (post.status !== "posted") return { ok: false, why: "not_posted" };
  if (!post.channel_msg_id) return { ok: false, why: "no_msg" };
  try {
    await tg(cfg, "deleteMessage", { chat_id: cfg.channel, message_id: post.channel_msg_id });
  } catch (e) {
    if (!/not found|MESSAGE_ID_INVALID/i.test(e.message)) return { ok: false, why: "telegram", error: e.message };
    // сообщения в канале уже нет (удалили руками) — просто снимаем отметку
  }
  post.status = "removed";
  post.channel_msg_id = 0;
  post.slot = 0;
  await putJSON(env, `post:${id}`, post);
  const pub = await getJSON(env, "pub", { last: 0, day: "", count: 0 });
  if (pub.day === localDay(cfg) && pub.count > 0) await putJSON(env, "pub", { ...pub, count: pub.count - 1 });
  await addEvent(env, "unposted", id);
  await redrawCard(cfg, post, "removed");
  return { ok: true, post };
}

function localHour(cfg, now = Date.now()) {
  return new Date(now + (cfg.tz_offset_min || 420) * 60000).getUTCHours();
}

function localDay(cfg, now = Date.now()) {
  return new Date(now + (cfg.tz_offset_min || 420) * 60000).toISOString().slice(0, 10);
}

export function isQuiet(cfg, now = Date.now()) {
  const h = localHour(cfg, now), from = cfg.quiet_from ?? 23, to = cfg.quiet_to ?? 7;
  if (from === to) return false;
  return from > to ? (h >= from || h < to) : (h >= from && h < to);
}

// ---------- отложенная публикация: ближайший свободный ровный час ----------
export async function nextFreeSlot(env, cfg, now = Date.now()) {
  let slot = Math.floor(now / 3600000) * 3600 + 3600;            // следующий ровный час
  for (let i = 0; i < 24 * 7; i++, slot += 3600) {
    if (isQuiet(cfg, slot * 1000)) continue;                      // ночью не публикуем
    if ((await env.BOT.get(`slot:${slot}`)) === null) return slot;
  }
  throw new Error("нет свободного часа на неделю вперёд");
}

export async function schedulePost(env, cfg, id, now = Date.now()) {
  const post = await getJSON(env, `post:${id}`);
  if (!post) return { ok: false, why: "missing" };
  if (post.status === "posted") return { ok: false, why: "already" };
  if (post.status === "scheduled" && post.slot) return { ok: true, slot: post.slot, post };
  const slot = await nextFreeSlot(env, cfg, now);
  await env.BOT.put(`slot:${slot}`, String(id));
  post.status = "scheduled";
  post.slot = slot;
  await putJSON(env, `post:${id}`, post);
  await env.BOT.delete(`q:${id}`);
  await addEvent(env, "scheduled", id, { slot });
  await redrawCard(cfg, post, "scheduled");
  return { ok: true, slot, post };
}

async function unschedule(env, cfg, post) {
  if (post.slot) await env.BOT.delete(`slot:${post.slot}`);
  post.status = "review";
  post.slot = 0;
  await putJSON(env, `post:${post.id}`, post);
  await addEvent(env, "back", post.id);
  await redrawCard(cfg, post, "review");
}

// Пора публиковать отложенное. Если помощник пропустил несколько часов, выходит один пост,
// а остальные переносятся на следующие свободные часы — чтобы не было пачки подряд.
export async function publishDue(env, cfg, now = Date.now()) {
  if (!cfg) return null;
  const list = await env.BOT.list({ prefix: "slot:" });
  const due = list.keys.map((k) => Number(k.name.slice(5))).filter((t) => t * 1000 <= now + 60000).sort((a, b) => a - b);
  let done = null;
  for (const slot of due) {
    const id = await env.BOT.get(`slot:${slot}`);
    const post = id && (await getJSON(env, `post:${id}`));
    if (!post || post.status !== "scheduled" || post.slot !== slot) { await env.BOT.delete(`slot:${slot}`); continue; }
    if (done === null) {
      const res = await publish(env, cfg, post.id, "slot", now);
      if (res.ok) done = post.id;
      continue;
    }
    await env.BOT.delete(`slot:${slot}`);
    post.status = "review";
    post.slot = 0;
    await putJSON(env, `post:${post.id}`, post);
    await schedulePost(env, cfg, post.id, now);
  }
  return done;
}

// ---------- кнопки ----------
async function handleCallback(env, cb) {
  const cfg = await getJSON(env, "cfg");
  if (!cfg) return;
  const answer = (text) => tg(cfg, "answerCallbackQuery", { callback_query_id: cb.id, text }).catch(() => {});
  if (String(cb.from?.id) !== String(cfg.admin) && String(cb.message?.chat?.id) !== String(cfg.admin)) return;
  const [action, rawId] = String(cb.data || "").split(":");
  const id = Number(rawId);
  if (!id) return answer("Эта кнопка от старой версии бота");

  if (action === "go") {
    try {
      const res = await schedulePost(env, cfg, id);
      if (res.ok) return answer(`Выйдет в ${hhmm(cfg, res.slot)}`);
      if (res.why === "already") return answer("Уже в канале");
      // поста нет у помощника (например, карточка старая) — опубликует сам бот при следующем запуске
      await addEvent(env, "go", id);
      await runBot(env);
      return answer("Опубликую через пару минут");
    } catch (e) {
      console.log("Публикация не удалась:", e.message);
      await tg(cfg, "sendMessage", { chat_id: cfg.admin, text: `⚠️ Пост не опубликован: ${e.message.slice(0, 300)}` }).catch(() => {});
      return answer("Не получилось — подробности в чате");
    }
  }

  if (action === "now") {
    try {
      const res = await publish(env, cfg, id, "now");
      return answer(res.ok ? "Опубликовано в канале" : res.why === "already" ? "Уже в канале" : "Пост не найден");
    } catch (e) {
      await tg(cfg, "sendMessage", { chat_id: cfg.admin, text: `⚠️ Пост не опубликован: ${e.message.slice(0, 300)}` }).catch(() => {});
      return answer("Не получилось — подробности в чате");
    }
  }

  if (action === "del" || action === "keep") {
    // «Убрать из канала» — сначала переспрашиваем, чтобы случайное нажатие ничего не удалило
    const p = await getJSON(env, `post:${id}`);
    if (!p || p.status !== "posted") return answer(p ? "Этого поста нет в канале" : "Пост не найден");
    await tg(cfg, "editMessageReplyMarkup", { chat_id: cfg.admin, message_id: cb.message?.message_id,
                                              reply_markup: { inline_keyboard: postButtons(id, action === "del" ? "confirm_del" : "posted") } })
      .catch((e) => console.log("Кнопки не обновились:", e.message));
    return answer(action === "del" ? "Точно убрать пост из канала?" : "Оставил");
  }
  if (action === "delok") {
    const res = await unpublish(env, cfg, id);
    if (res.ok) return answer("Пост убран из канала");
    if (res.why === "telegram") {
      await tg(cfg, "sendMessage", { chat_id: cfg.admin,
        text: `⚠️ Telegram не дал удалить пост: ${res.error.slice(0, 200)}\nБот может удалять посты только первые 48 часов — дальше удалите его в канале вручную.` }).catch(() => {});
      return answer("Не получилось — подробности в чате");
    }
    if (res.why === "no_msg") return answer("Не знаю номер сообщения в канале — удалите пост вручную");
    return answer(res.why === "not_posted" ? "Этого поста уже нет в канале" : "Пост не найден");
  }

  const post = await getJSON(env, `post:${id}`);
  if (post && action === "unsched") {
    if (post.status === "posted") return answer("Уже в канале");
    await unschedule(env, cfg, post);
    return answer("Публикация отменена");
  }
  if (post && (action === "skip" || action === "back")) {
    if (post.status === "removed" && action === "back") return answer("Пост уже не в канале");
    if (post.status === "posted") return answer("Уже в канале");
    post.status = action === "skip" ? "skipped" : "review";
    await putJSON(env, `post:${id}`, post);
    if (action === "skip") await env.BOT.delete(`q:${id}`);
    await addEvent(env, action === "skip" ? "skipped" : "back", id);
    await redrawCard(cfg, post, action === "skip" ? "skipped" : "review");
    return answer(action === "skip" ? "Пропущено" : "Вернул");
  }

  // перевод и кнопки старых карточек-выжимок делает сам бот — запускаем его сразу
  await addEvent(env, action, id);
  if (action === "full") {
    await runBot(env);
    return answer("Перевод пришлю через пару минут");
  }
  return answer("Отмечу при следующем запуске бота");
}

// ---------- запуск бота на GitHub ----------
async function runBot(env) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return false;
  const r = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW || "bot.yml"}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "user-agent": "vn-news-bot-helper",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  if (r.status !== 204) console.log("GitHub не запустил бота:", r.status, await r.text());
  return r.status === 204;
}

// ---------- автопилот ----------
export async function publishFromQueue(env, cfg, now = Date.now()) {
  if (!cfg?.autopublish || isQuiet(cfg, now)) return null;
  const hour = Math.floor(now / 3600000) * 3600;
  if (now - hour * 1000 > 5 * 60000) return null;                    // автопилот тоже выходит только в ровный час
  if ((await env.BOT.get(`slot:${hour}`)) !== null) return null;     // этот час уже занят вашим постом
  const pub = await getJSON(env, "pub", { last: 0, day: "", count: 0 });
  if (now / 1000 - (pub.last || 0) < (cfg.min_gap_min ?? 45) * 60) return null;
  if (pub.day === localDay(cfg, now) && pub.count >= (cfg.max_per_day ?? 10)) return null;
  const list = await env.BOT.list({ prefix: "q:" });
  let best = null;
  for (const k of list.keys) {
    const post = await getJSON(env, `post:${k.name.slice(2)}`);
    if (!post || post.status !== "queued") { await env.BOT.delete(k.name); continue; }
    if (post.published_at && now / 1000 - post.published_at > (cfg.max_age_h ?? 36) * 3600) {
      post.status = "expired";
      await putJSON(env, `post:${post.id}`, post);
      await env.BOT.delete(k.name);
      await addEvent(env, "expired", post.id);
      continue;
    }
    if (!best || post.score > best.score || (post.score === best.score && post.published_at > best.published_at)) best = post;
  }
  if (!best) return null;
  const res = await publish(env, cfg, best.id, "auto", now);
  return res.ok ? best.id : null;
}

// Один раз после обновления: у постов последних 48 часов появляется кнопка «Убрать из канала»
export async function addRemoveButtons(env, cfg, now = Date.now()) {
  if ((await env.BOT.get("buttons_v2")) !== null) return 0;
  await env.BOT.put("buttons_v2", "1");
  let n = 0;
  const list = await env.BOT.list({ prefix: "post:" });
  for (const k of list.keys) {
    const post = await getJSON(env, k.name);
    if (post?.status === "posted" && post.channel_msg_id && now / 1000 - (post.posted_at || 0) < 48 * 3600) {
      await redrawCard(cfg, post, "posted");
      n++;
    }
  }
  return n;
}

// ---------- связь с ботом ----------
async function sync(env, request, body) {
  const out = { ok: true };
  if (body.config) {
    const old = await getJSON(env, "cfg", {});
    const cfg = { ...old, ...body.config };
    const hook = `${new URL(request.url).origin}/tg`;
    if (cfg.webhook !== hook || old.token !== cfg.token) {
      try {
        await tg(cfg, "setWebhook", { url: hook, secret_token: env.SHARED_SECRET, allowed_updates: ["callback_query"],
                                       drop_pending_updates: false });
        cfg.webhook = hook;
        out.webhook = "set";
      } catch (e) {
        // новый адрес *.workers.dev Telegram видит не сразу — попробуем при следующем запуске бота
        cfg.webhook = "";
        out.webhook_error = e.message;
      }
    }
    await putJSON(env, "cfg", cfg);
  }
  for (const p of body.posts || []) {
    const prev = await getJSON(env, `post:${p.id}`);
    if (prev && (prev.status === "posted" || prev.status === "scheduled")) continue;   // уже в канале или в расписании
    if (p.status === "schedule") {                        // бот передаёт нажатие «Опубликовать», которое не дошло сюда
      await putJSON(env, `post:${p.id}`, { ...p, status: "review" });
      const cfg = await getJSON(env, "cfg");
      if (cfg) await schedulePost(env, cfg, p.id);
      continue;
    }
    await putJSON(env, `post:${p.id}`, p);
    if (p.status === "queued") await env.BOT.put(`q:${p.id}`, String(p.score || 0));
  }
  for (const key of body.ack || []) {
    if (String(key).startsWith("ev:")) await env.BOT.delete(key);
  }
  if (body.want_events) {
    const list = await env.BOT.list({ prefix: "ev:" });
    out.events = [];
    for (const k of list.keys) {
      const ev = await getJSON(env, k.name);
      if (ev) out.events.push(ev);
    }
  }
  return out;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/tg") {
      if (request.headers.get("x-telegram-bot-api-secret-token") !== env.SHARED_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const update = await request.json().catch(() => ({}));
      if (update.callback_query) {
        try { await handleCallback(env, update.callback_query); } catch (e) { console.log("Кнопка:", e.message); }
      }
      return new Response("ok");
    }
    if (request.method === "POST" && url.pathname === "/api/sync") {
      if (request.headers.get("authorization") !== `Bearer ${env.SHARED_SECRET}`) {
        return new Response("forbidden", { status: 403 });
      }
      try {
        const body = await request.json();
        return Response.json(await sync(env, request, body));
      } catch (e) {
        return Response.json({ ok: false, error: e.message }, { status: 500 });
      }
    }
    if (url.pathname === "/") return new Response("Помощник бота «Вьетнам Новости» работает");
    return new Response("not found", { status: 404 });
  },

  // каждые 5 минут: автопилот публикует из очереди, раз в RUN_EVERY_MIN минут — запуск бота на GitHub
  async scheduled(event, env, ctx) {
    const cfg = await getJSON(env, "cfg");
    const now = event.scheduledTime || Date.now();
    if (cfg) { try { await addRemoveButtons(env, cfg, now); } catch (e) { console.log("Кнопки:", e.message); } }
    let published = null;
    try { published = await publishDue(env, cfg, now); } catch (e) { console.log("Отложенные:", e.message); }
    if (published === null) {
      try { await publishFromQueue(env, cfg, now); } catch (e) { console.log("Автопилот:", e.message); }
    }
    const every = Number(env.RUN_EVERY_MIN || 20);
    const minute = Math.floor(event.scheduledTime / 60000);
    if (minute % every < 5) await runBot(env);
  },
};
