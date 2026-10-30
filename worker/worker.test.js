// Проверка помощника без сети: KV и Telegram подменены. Запуск: node --test worker/worker.test.js
import assert from "node:assert/strict";
import { test } from "node:test";
import worker, { isQuiet, publishFromQueue, visibleLen } from "./worker.js";

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

test("кнопка «Опубликовать» сразу шлёт пост в канал и не дублирует его", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.fetch(req("/api/sync", { config: CFG, posts: [POST] }, auth), env);
  const press = { callback_query: { id: "cb", data: "go:7", from: { id: 42 }, message: { message_id: 99, chat: { id: 42 } } } };
  const hdr = { "x-telegram-bot-api-secret-token": "s3cret" };
  await worker.fetch(req("/tg", press, hdr), env);
  const photo = calls.find((c) => c.url.endsWith("/sendPhoto"));
  assert.equal(photo.body.chat_id, "@novosti_vietnama");
  assert.equal(photo.body.caption, POST.html);                         // в канал — пост без служебного хвоста
  const edit = calls.find((c) => c.url.endsWith("/editMessageCaption"));
  assert.match(edit.body.caption, /В КАНАЛЕ/);
  assert.equal(calls.find((c) => c.url.endsWith("/answerCallbackQuery")).body.text, "Опубликовано в канале");

  await worker.fetch(req("/tg", press, hdr), env);
  assert.equal(calls.filter((c) => c.url.endsWith("/sendPhoto")).length, 1);
  assert.equal(calls.filter((c) => c.url.endsWith("/answerCallbackQuery")).at(-1).body.text, "Уже в канале");

  const out = await (await worker.fetch(req("/api/sync", { want_events: true }, auth), env)).json();
  assert.equal(out.events.length, 1);
  assert.equal(out.events[0].action, "posted");
  assert.equal(out.events[0].channel_msg_id, 555);
  await worker.fetch(req("/api/sync", { ack: [out.events[0].key] }, auth), env);
  const after = await (await worker.fetch(req("/api/sync", { want_events: true }, auth), env)).json();
  assert.deepEqual(after.events, []);
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
  const noonVN = Date.UTC(2026, 9, 31, 5, 0);          // 12:00 по Вьетнаму
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
  assert.equal(await publishFromQueue(env, cfg, noonVN + 50 * 60000), 1);
  assert.equal(JSON.parse(env.store.get("post:3")).status, "expired");
  assert.equal(calls.filter((c) => c.url.endsWith("/sendPhoto")).length, 2);
});

test("таймер запускает бота раз в 20 минут", async () => {
  const env = makeEnv(); const calls = mockFetch();
  await worker.scheduled({ scheduledTime: Date.UTC(2026, 9, 31, 5, 20) }, env, {});
  await worker.scheduled({ scheduledTime: Date.UTC(2026, 9, 31, 5, 25) }, env, {});
  assert.equal(calls.filter((c) => c.url.includes("dispatches")).length, 1);
});
