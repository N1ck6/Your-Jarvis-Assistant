// Bridge between Jarvis (ws://127.0.0.1:8771, this computer only) and the browser.
//
// Safety:
// - Commands are obeyed only after mutual authentication with the pairing key that Jarvis writes into
//   pairing.json next to this file: a program that merely listens on the port gets nothing done.
// - A fixed set of commands (open, read, click, type...), never arbitrary code.
// - Password, card and one-time-code fields are never filled; banks, Gosuslugi and crypto exchanges are never opened.
// - The agent works in a window of its own (minimized by default): the user's tabs are only read, on request.
//
// Jarvis sends {id, method, params}; the answer is {id, result} or {id, error}. Events go as {event, ...}.
const PORT = 8771;
const VERSION = chrome.runtime.getManifest().version;

// Same list as assistant/web/policy.py (defence in depth: the extension refuses even if asked).
const FORBIDDEN_HOSTS = [
  "sberbank.ru", "sber.ru", "tinkoff.ru", "tbank.ru", "vtb.ru", "alfabank.ru", "gazprombank.ru", "raiffeisen.ru",
  "sovcombank.ru", "pochtabank.ru", "otpbank.ru", "rshb.ru", "mkb.ru", "open.ru", "psbank.ru", "rosbank.ru",
  "uralsib.ru", "akbars.ru", "domrf.ru", "bspb.ru", "mtsbank.ru", "ozonbank.ru", "wb-bank.ru", "yoomoney.ru",
  "qiwi.com", "paypal.com", "gosuslugi.ru", "nalog.gov.ru", "nalog.ru", "pfr.gov.ru", "sfr.gov.ru", "mos.ru",
  "binance.com", "bybit.com", "okx.com", "kucoin.com", "coinbase.com", "kraken.com", "htx.com", "huobi.com",
  "gate.io", "mexc.com", "bitget.com", "exmo.com", "exmo.me", "garantex.org", "blockchain.com", "metamask.io",
];

let ws = null;
let pingTimer = null;
let trusted = false;      // Jarvis proved it knows the pairing key
let myNonce = "";
const S = { windowId: null, tabId: null, mode: "minimized" };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function forbiddenUrl(url) {
  let host = "";
  try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return false; }
  return FORBIDDEN_HOSTS.some((h) => host === h || host.endsWith("." + h));
}

async function pairingKey() {
  try {
    const r = await fetch(chrome.runtime.getURL("pairing.json"), { cache: "no-store" });
    return (await r.json()).key || "";
  } catch (e) {
    return "";
  }
}

async function hmac(key, text) {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(key), { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function nonce() {
  return [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function loadState() {
  const saved = (await chrome.storage.session.get("agent")).agent;
  if (saved) Object.assign(S, saved);
}

async function saveState() {
  await chrome.storage.session.set({ agent: { ...S } });
}

function setBadge(state) {
  const text = { on: "", off: "off", pair: "key" }[state];
  const title = {
    on: "Джарвис: нажмите, чтобы позвать (как «Джарвис» голосом)",
    off: "Джарвис: нет связи (запущен ли Джарвис?)",
    pair: "Джарвис: нет ключа связи — запустите Джарвиса и перезагрузите расширение",
  }[state];
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color: "#777" });
  chrome.action.setTitle({ title });
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const key = await pairingKey();
  if (!key) {
    setBadge("pair");
    return;
  }
  try {
    ws = new WebSocket(`ws://127.0.0.1:${PORT}/`);
  } catch (e) {
    ws = null;
    return;
  }
  trusted = false;
  ws.onopen = () => {
    myNonce = nonce();
    send({ event: "hello", version: VERSION, nonce: myNonce });
    clearInterval(pingTimer);
    pingTimer = setInterval(() => send({ event: "ping" }), 20000);  // keeps the service worker alive
  };
  ws.onclose = () => {
    setBadge("off");
    clearInterval(pingTimer);
    ws = null;
    trusted = false;
    setTimeout(connect, 5000);
  };
  ws.onerror = () => {};
  ws.onmessage = async (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch (err) { return; }
    if (!msg) return;
    if (msg.event === "welcome") {
      // Jarvis answers our nonce with the key; we answer its nonce. Only then commands are obeyed.
      if (msg.proof !== (await hmac(key, "jarvis:" + myNonce))) {
        ws.close();
        return;
      }
      trusted = true;
      setBadge("on");
      send({ event: "auth", proof: await hmac(key, "ext:" + msg.nonce) });
      return;
    }
    if (msg.id == null || !trusted) return;
    const handler = HANDLERS[msg.method];
    try {
      if (!handler) throw new Error(`неизвестная команда ${msg.method}`);
      await loadState();
      send({ id: msg.id, result: (await handler(msg.params || {})) ?? null });
    } catch (err) {
      send({ id: msg.id, error: String((err && err.message) || err) });
    }
  };
}

// ------------------------------------------------------------------ agent window
async function windowAlive() {
  if (S.windowId == null) return false;
  try { await chrome.windows.get(S.windowId); return true; } catch (e) { return false; }
}

async function agentTab() {
  if (!(await windowAlive())) throw new Error("окно агента закрыто");
  try {
    const t = await chrome.tabs.get(S.tabId);
    if (t.windowId === S.windowId) return t.id;
  } catch (e) { /* closed: take the active one */ }
  const [t] = await chrome.tabs.query({ windowId: S.windowId, active: true });
  if (!t) throw new Error("в окне агента нет вкладок");
  S.tabId = t.id;
  await saveState();
  return t.id;
}

async function waitLoad(tabId, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    try {
      if ((await chrome.tabs.get(tabId)).status === "complete") return true;
    } catch (e) { return false; }
    await sleep(250);
  }
  return false;
}

async function tabInfo(tabId) {
  const t = await chrome.tabs.get(tabId);
  return { tabId: t.id, windowId: t.windowId, url: t.url || t.pendingUrl || "", title: t.title || "" };
}

async function open({ url, show = false, mode = "" }) {
  if (!/^https?:\/\//i.test(url || "")) throw new Error("открываю только адреса http(s)");
  if (forbiddenUrl(url)) throw new Error("банки, Госуслуги и криптобиржи агент не открывает");
  if (mode) S.mode = mode;
  let tabId;
  if (await windowAlive()) {
    tabId = await agentTab();
    await chrome.tabs.update(tabId, { url, active: true });
  } else {
    const opts = { url };
    if (show) Object.assign(opts, { focused: true, state: "maximized" });
    else if (S.mode === "background") Object.assign(opts, { focused: false, state: "normal" });
    else opts.state = "minimized";
    const w = await chrome.windows.create(opts);
    S.windowId = w.id;
    tabId = w.tabs[0].id;
  }
  S.tabId = tabId;
  await saveState();
  if (show) await showWindow();
  await sleep(300);
  await waitLoad(tabId);
  return tabInfo(tabId);
}

async function showWindow() {
  if (!(await windowAlive())) return { ok: false, error: "окна агента нет" };
  await chrome.windows.update(S.windowId, { state: "maximized", focused: true, drawAttention: true });
  return { ok: true };
}

async function hideWindow() {
  if (!(await windowAlive())) return { ok: false };
  await chrome.windows.update(S.windowId, { state: "minimized" });
  return { ok: true };
}

async function closeWindow() {
  if (await windowAlive()) await chrome.windows.remove(S.windowId);
  S.windowId = null;
  S.tabId = null;
  await saveState();
  return { ok: true };
}

// The tab the user is looking at ("перескажи эту статью"): the last focused normal window. Read only.
async function activeTab() {
  let win = null;
  try { win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }); } catch (e) { /* none */ }
  if (win && win.id === S.windowId && win.state === "minimized") win = null;
  if (!win) {
    const all = await chrome.windows.getAll({ windowTypes: ["normal"] });
    win = all.find((w) => w.id !== S.windowId && w.state !== "minimized") || all.find((w) => w.id !== S.windowId);
  }
  if (!win) return null;
  const [t] = await chrome.tabs.query({ windowId: win.id, active: true });
  return t ? { tabId: t.id, windowId: t.windowId, url: t.url || "", title: t.title || "", agent: win.id === S.windowId } : null;
}

// ------------------------------------------------------------------ page calls
async function inPage(tabId, method, args) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["page.js"] });
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (m, a) => window.__jarvis[m](a),
    args: [method, args || {}],
  });
  return res ? res.result : null;
}

// Reading works on any tab (the user's own one too); acting only in the agent's window.
async function read(method, p) {
  const tabId = p.tabId ?? (await agentTab());
  await waitLoad(tabId, 8000);
  return inPage(tabId, method, p);
}

async function act(method, p) {
  const tabId = await agentTab();
  const before = (await chrome.tabs.get(tabId)).url;
  let res;
  try {
    res = await inPage(tabId, method, p);
  } catch (e) {
    const text = String((e && e.message) || e);
    // The click navigated away while the script was running: that is success.
    if (!/frame|navigat|removed|context/i.test(text)) return { ok: false, error: text };
    res = { ok: true };
  }
  if (res && res.navigate) {
    if (forbiddenUrl(res.navigate)) return { ok: false, error: "банки, Госуслуги и криптобиржи агент не открывает" };
    await chrome.tabs.update(tabId, { url: res.navigate });
  }
  await sleep(800);
  const now = await agentTab();  // the site may have opened a new tab
  await waitLoad(now, 12000);
  await sleep(300);
  const info = await tabInfo(now);
  if (forbiddenUrl(info.url)) {
    await chrome.tabs.goBack(now).catch(() => {});
    return { ok: false, error: "сайт перевёл на банк или платёжную страницу — дальше только сами", url: info.url };
  }
  return { ...(res || { ok: true }), url: info.url, changed: info.url !== before || now !== tabId };
}

// The capture API sees only a window that is on screen: a minimized agent window is restored without focus for a
// moment; if it is still hidden behind other windows, it is brought forward only when Jarvis allows (p.focus).
async function screenshot(p) {
  const tabId = p.tabId ?? (await agentTab());
  const tab = await chrome.tabs.get(tabId);
  const win = await chrome.windows.get(tab.windowId);
  const restore = win.state === "minimized";
  const capture = async () =>
    (await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 85 })).split(",")[1];
  try {
    if (restore) {
      await chrome.windows.update(win.id, { state: "normal", focused: false });
      await sleep(700);
    }
    try {
      return { image: await capture() };
    } catch (e) {
      if (!p.focus) throw e;
      await chrome.windows.update(win.id, { focused: true });
      await sleep(500);
      return { image: await capture() };
    }
  } finally {
    if (restore) await chrome.windows.update(win.id, { state: "minimized" }).catch(() => {});
  }
}

async function back() {
  const tabId = await agentTab();
  await chrome.tabs.goBack(tabId).catch(() => {});
  await sleep(600);
  await waitLoad(tabId);
  return tabInfo(tabId);
}

async function video(p) {
  const tabId = p.tabId ?? (await agentTab());
  return inPage(tabId, "video", { cmd: p.cmd || "state" });
}

const HANDLERS = {
  open,
  show: showWindow,
  hide: hideWindow,
  close: closeWindow,
  back,
  active_tab: activeTab,
  agent_tab: async () => ((await windowAlive()) ? tabInfo(await agentTab()) : null),
  snapshot: (p) => read("snapshot", p),
  links: (p) => read("links", p),
  readable: (p) => read("readable", p),
  click: (p) => act("click", p),
  type: (p) => act("type", p),
  select: (p) => act("select", p),
  scroll: (p) => act("scroll", p),
  key: (p) => act("key", p),
  video,
  screenshot,
};

// ------------------------------------------------------------------ lifecycle
chrome.tabs.onCreated.addListener(async (tab) => {
  await loadState();
  if (S.windowId != null && tab.windowId === S.windowId) {
    S.tabId = tab.id;  // the site opened a new tab: follow it
    await saveState();
  }
});

chrome.windows.onRemoved.addListener(async (windowId) => {
  await loadState();
  if (windowId === S.windowId) {
    S.windowId = null;
    S.tabId = null;
    await saveState();
    if (trusted) send({ event: "window_closed" });
  }
});

// The button in the toolbar: the same as saying "Джарвис" — he answers and listens for the command.
chrome.action.onClicked.addListener(async () => {
  if (trusted) {
    send({ event: "wake" });
    return;
  }
  await connect();
  chrome.action.setTitle({ title: "Джарвис не запущен или ещё подключается — попробуйте через пару секунд" });
});

chrome.alarms.create("jarvis-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());
setBadge("off");
connect();
