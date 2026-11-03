// Проверка помощника без сети: KV и Telegram подменены. Запуск: node --test worker/worker.test.js
import assert from "node:assert/strict";
import { test } from "node:test";
import worker, { isQuiet, nextFreeSlot, publishDue, publishFromQueue, schedulePost, visibleLen } from "./worker.js";

function makeEnv() {
  const store = new Map();
  return {
    store,
    SHARED_SECRET: "s3cret",
    GITHUB_TOKEN: "gh",
    GITHUB_REPO: "me/bot",
    BOT: {
      async get(k) { return store.has(k) ? store.get(k) : null; },
      async put(k, v) { store.set(k, v); },
      async delete(k) { store.delete(k); },
      async list({ prefix }) { return { keys: [...store.keys()].filter((k) => k.startsWith(prefix)).sort().map((name) => ({ name })) }; },
    },
  };
}

function mockFetch() {
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    const body = opts.body ? JSON.parse(opts.body) : {};
    calls.push({ url: String(url), body });
    if (String(url).includes("api.github.com")) return new Response(null, { status: 204 });
    const method = String(url).split("/").pop();
    return Response.json({ ok: true, result: method === "sendPhoto" || method === "sendMessage" ? { message_id: 555 } : true });
  };
  return calls;
}

const req = (path, body, headers = {}) => new Request(`https://helper.example.workers.dev${path}`, {
  method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body),
});
const auth = { authorization: "Bearer s3cret" };
const CFG = { token: "T", admin: "42", channel: "@novosti_vietnama", autopublish: false, quiet_from: 23, quiet_to: 7,
              min_gap_min: 45, max_per_day: 10, max_age_h: 36, tz_offset_min: 420 };
const POST = { id: 7, html: "<b>🚨 Заголовок</b>\n\nТекст &amp; ко", image: "https://img/a.jpg",
               card_html: "<b>🚨 Заголовок</b>\n\nТекст\n\n— — —\n✅ проверка", card_kind: "photo", card_msg_id: 99,
               status: "review", score: 8, published_at: Date.now() / 1000 };

test("видимая длина без тегов и сущностей", () => {
  assert.equal(visibleLen("<b>a&amp;b</b> 🚨"), 5);
});

test("бот присылает настройки и пост, помощник ставит вебхук", async () => {
  const env = makeEnv(); const calls = mockFetch();
  const res = await worker.fetch(req("/api/sync", { config: CFG, posts: [POST], want_events: true }, auth), env);
  const out = await res.json();
  assert.equal(out.webhook, "set");
  const hook = calls.find((c) => c.url.endsWith("/setWebhook"));
  assert.equal(hook.body.url, "https://helper.example.workers.dev/tg");
  assert.equal(hook.body.secret_token, "s3cret");
  assert.deepEqual(out.events, []);
  assert.equal(JSON.parse(env.store.get("post:7")).html, POST.html);
});

test("чужой запрос без секрета не проходит", async () => {
  const env = makeEnv(); mockFetch();
  assert.equal((await worker.fetch(req("/api/sync", { config: CFG }), env)).status, 403);
  assert.equal((await worker.fetch(req("/tg", {}), env)).status, 403);
});

const hdr = { "x-telegram-bot-api-secret-token": "s3cret" };
const press = (data) => ({ callback_query: { id: "cb", data, from: { id: 42 }, message: { message_id: 99, chat: { id: 42 } } } });
const VN = (h, m = 0, d = 2) => Date.UTC(2026, 10, d, h - 7, m);   // время по Вьетнаму → мс UTC

test("«Опубликовать» ставит пост на ближайший ровный час, второй — на следующий", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [POST, { ...POST, id: 8, card_msg_id: 100 }] }, auth), env);
  const r1 = await schedulePost(env, CFG, 7, VN(14, 20));
  const r2 = await schedulePost(env, CFG, 8, VN(14, 22));
  assert.equal(r1.slot * 1000, VN(15));
  assert.equal(r2.slot * 1000, VN(16));
  assert.equal(calls.filter((c) => c.url.endsWith("/sendPhoto")).length, 0);          // в канал пока ничего
  const edit = calls.find((c) => c.url.endsWith("/editMessageCaption"));
  assert.match(edit.body.caption, /Выйдет в 15:00/);
  assert.equal(edit.body.reply_markup.inline_keyboard[0][0].callback_data, "now:7");
  // повторное нажатие не двигает время
  assert.equal((await schedulePost(env, CFG, 7, VN(14, 40))).slot, r1.slot);
});

test("ночью не публикуем: после 22:00 — на 7:00 утра", async () => {
  const env = makeEnv(); mockFetch();
  assert.equal((await nextFreeSlot(env, CFG, VN(22, 10))) * 1000, VN(7, 0, 3));
});

test("в ровный час выходит один пост, следующий — через час", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [POST, { ...POST, id: 8 }] }, auth), env);
  await schedulePost(env, CFG, 7, VN(14, 20));
  await schedulePost(env, CFG, 8, VN(14, 21));
  assert.equal(await publishDue(env, CFG, VN(14, 55)), null);                         // ещё рано
  assert.equal(await publishDue(env, CFG, VN(15, 0)), 7);
  assert.equal(await publishDue(env, CFG, VN(15, 5)), null);
  assert.equal(await publishDue(env, CFG, VN(16, 0)), 8);
  assert.equal(calls.filter((c) => c.url.endsWith("/sendPhoto")).length, 2);
});

test("если помощник пропустил часы — один пост, остальные переносятся, а не выходят пачкой", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [POST, { ...POST, id: 8 }] }, auth), env);
  await schedulePost(env, CFG, 7, VN(14, 20));
  await schedulePost(env, CFG, 8, VN(14, 21));
  assert.equal(await publishDue(env, CFG, VN(17, 0)), 7);
  assert.equal(calls.filter((c) => c.url.endsWith("/sendPhoto")).length, 1);
  assert.equal(JSON.parse(env.store.get("post:8")).slot * 1000, VN(18));
});

test("кнопки: «Сейчас» публикует сразу, «Отменить» снимает с расписания, второй раз не дублирует", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [POST, { ...POST, id: 8 }] }, auth), env);
  await worker.fetch(req("/tg", press("go:7"), hdr), env);
  assert.match(calls.filter((c) => c.url.endsWith("/answerCallbackQuery")).at(-1).body.text, /^Выйдет в \d\d:00$/);
  await worker.fetch(req("/tg", press("now:7"), hdr), env);
  const photo = calls.find((c) => c.url.endsWith("/sendPhoto"));
  assert.equal(photo.body.chat_id, "@novosti_vietnama");
  assert.equal(photo.body.caption, POST.html);                          // в канал — пост без служебного хвоста
  assert.equal([...env.store.keys()].filter((k) => k.startsWith("slot:")).length, 0);   // час освободился
  await worker.fetch(req("/tg", press("go:7"), hdr), env);
  assert.equal(calls.filter((c) => c.url.endsWith("/answerCallbackQuery")).at(-1).body.text, "Уже в канале");
  assert.equal(calls.filter((c) => c.url.endsWith("/sendPhoto")).length, 1);

  await worker.fetch(req("/tg", press("go:8"), hdr), env);
  await worker.fetch(req("/tg", press("unsched:8"), hdr), env);
  assert.equal(JSON.parse(env.store.get("post:8")).status, "review");
  assert.equal([...env.store.keys()].filter((k) => k.startsWith("slot:")).length, 0);

  const out = await (await worker.fetch(req("/api/sync", { want_events: true }, auth), env)).json();
  assert.deepEqual(out.events.map((e) => `${e.action}:${e.id}`), ["scheduled:7", "posted:7", "scheduled:8", "back:8"]);
  await worker.fetch(req("/api/sync", { ack: out.events.map((e) => e.key) }, auth), env);
  const after = await (await worker.fetch(req("/api/sync", { want_events: true }, auth), env)).json();
  assert.deepEqual(after.events, []);
});

test("бот передаёт нажатие, которое не дошло до помощника, — пост встаёт в расписание", async () => {
  const env = makeEnv(); mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [{ ...POST, status: "schedule" }] }, auth), env);
  const post = JSON.parse(env.store.get("post:7"));
  assert.equal(post.status, "scheduled");
  assert.ok(env.store.has(`slot:${post.slot}`));
});

test("чужие нажатия игнорируются", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [POST] }, auth), env);
  const press = { callback_query: { id: "cb", data: "go:7", from: { id: 1 }, message: { message_id: 99, chat: { id: 1 } } } };
  await worker.fetch(req("/tg", press, { "x-telegram-bot-api-secret-token": "s3cret" }), env);
  assert.equal(calls.filter((c) => c.url.endsWith("/sendPhoto")).length, 0);
});

test("перевод — событие для бота и немедленный запуск на GitHub", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [POST] }, auth), env);
  const press = { callback_query: { id: "cb", data: "full:7", from: { id: 42 }, message: { message_id: 99, chat: { id: 42 } } } };
  await worker.fetch(req("/tg", press, { "x-telegram-bot-api-secret-token": "s3cret" }), env);
  assert.ok(calls.some((c) => c.url === "https://api.github.com/repos/me/bot/actions/workflows/bot.yml/dispatches"));
  const out = await (await worker.fetch(req("/api/sync", { want_events: true }, auth), env)).json();
  assert.equal(out.events[0].action, "full");
});

test("автопилот: тишина ночью, интервал, лучший пост первым", async () => {
  const env = makeEnv(); const calls = mockFetch();
  const cfg = { ...CFG, autopublish: true };
  const noonVN = Date.UTC(2026, 9, 31, 5, 0);          // 12:00 по Вьетнаму, ровный час
  const nightVN = Date.UTC(2026, 9, 31, 17, 0);        // 00:00 по Вьетнаму
  assert.equal(isQuiet(cfg, nightVN), true);
  assert.equal(isQuiet(cfg, noonVN), false);
  const fresh = noonVN / 1000;
  await worker.fetch(req("/api/sync", { config: cfg, posts: [
    { ...POST, id: 1, status: "queued", score: 6, published_at: fresh },
    { ...POST, id: 2, status: "queued", score: 9, published_at: fresh },
    { ...POST, id: 3, status: "queued", score: 10, published_at: fresh - 40 * 3600 },   // протухла
  ] }, auth), env);
  assert.equal(await publishFromQueue(env, cfg, nightVN), null);
  assert.equal(await publishFromQueue(env, cfg, noonVN), 2);
  assert.equal(await publishFromQueue(env, cfg, noonVN + 10 * 60000), null);          // рано, нужно 45 минут
  assert.equal(await publishFromQueue(env, cfg, noonVN + 50 * 60000), null);         // автопилот — только в ровный час
  assert.equal(await publishFromQueue(env, cfg, noonVN + 60 * 60000), 1);
  assert.equal(JSON.parse(env.store.get("post:3")).status, "expired");
  assert.equal(calls.filter((c) => c.url.endsWith("/sendPhoto")).length, 2);
});

test("таймер запускает бота раз в 20 минут", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.scheduled({ scheduledTime: Date.UTC(2026, 9, 31, 5, 20) }, env, {});
  await worker.scheduled({ scheduledTime: Date.UTC(2026, 9, 31, 5, 25) }, env, {});
  assert.equal(calls.filter((c) => c.url.includes("dispatches")).length, 1);
});

test("«Убрать из канала»: сначала переспрашивает, потом удаляет пост и возвращает карточку", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [POST] }, auth), env);
  await worker.fetch(req("/tg", press("now:7"), hdr), env);
  assert.equal(JSON.parse(env.store.get("post:7")).channel_msg_id, 555);

  await worker.fetch(req("/tg", press("del:7"), hdr), env);
  assert.equal(calls.filter((c) => c.url.endsWith("/deleteMessage")).length, 0);       // одно нажатие ничего не удаляет
  const ask = calls.filter((c) => c.url.endsWith("/editMessageReplyMarkup")).at(-1);
  assert.equal(ask.body.reply_markup.inline_keyboard[0][0].callback_data, "delok:7");

  await worker.fetch(req("/tg", press("keep:7"), hdr), env);
  assert.equal(JSON.parse(env.store.get("post:7")).status, "posted");

  await worker.fetch(req("/tg", press("delok:7"), hdr), env);
  const del = calls.find((c) => c.url.endsWith("/deleteMessage"));
  assert.deepEqual(del.body, { chat_id: "@novosti_vietnama", message_id: 555 });
  const post = JSON.parse(env.store.get("post:7"));
  assert.equal(post.status, "removed");
  const card = calls.filter((c) => c.url.endsWith("/editMessageCaption")).at(-1);
  assert.match(card.body.caption, /Убрано из канала/);
  assert.equal(card.body.reply_markup.inline_keyboard[0][0].callback_data, "go:7");     // можно опубликовать заново

  const out = await (await worker.fetch(req("/api/sync", { want_events: true }, auth), env)).json();
  assert.deepEqual(out.events.map((e) => e.action), ["posted", "unposted"]);
});

test("после обновления у свежих постов в канале один раз появляется кнопка «Убрать»", async () => {
  const { addRemoveButtons } = await import("./worker.js");
  const env = makeEnv(); const calls = mockFetch();
  const now = Date.now();
  await env.BOT.put("post:7", JSON.stringify({ ...POST, status: "posted", channel_msg_id: 555, posted_at: now / 1000 - 3600 }));
  await env.BOT.put("post:8", JSON.stringify({ ...POST, id: 8, status: "posted", channel_msg_id: 556, posted_at: now / 1000 - 72 * 3600 }));
  assert.equal(await addRemoveButtons(env, CFG, now), 1);
  assert.equal(await addRemoveButtons(env, CFG, now), 0);
  assert.equal(calls.filter((c) => c.url.endsWith("/editMessageCaption")).at(-1).body.reply_markup.inline_keyboard[0][1].callback_data, "del:7");
});
