// Runs inside a page (isolated world): reads what is on it and acts on its elements for Jarvis.
// Injected again before every call; the second injection is a no-op.
(() => {
  if (window.__jarvis && window.__jarvis.v === 2) return;

  const ATTR = "data-jarvis-id";
  const INTERACTIVE = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]", "[role=tab]", "[role=menuitem]",
    "[role=option]", "[role=switch]", "[role=combobox]", "[role=searchbox]", "[role=textbox]",
    "[contenteditable=''], [contenteditable=true]", "[onclick]",
  ].join(",");
  const CAPTCHA_SEL = [
    "iframe[src*='captcha' i]", "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", ".g-recaptcha", ".h-captcha",
    "#captcha", ".CheckboxCaptcha", ".AdvancedCaptcha", "form[action*='checkcaptcha']", "[class*='smart-captcha' i]",
    "#challenge-form", "#cf-challenge-running",
  ].join(",");
  const CAPTCHA_TEXT = /я не робот|подтвердите, что (вы не робот|запросы отправляли вы)|i'?m not a robot|verify you are human|проверка (браузера|безопасности)|checking your browser|доступ (временно )?ограничен|are you a robot/i;
  const SEARCH_HINT = /search|query|^q$|^text$|поиск|искать|найти|запрос/i;
  // Same idea as SECRET_FIELD in assistant/web/policy.py.
  const SECRET_FIELD = /password|passwd|парол|cc-|card|cvc|cvv|csc|карт[аыу]|срок действия|one-time-code|otp|sms|смс|код из|код подтверждения|passport|паспорт|снилс|\bpin\b|пин-?код/i;

  let seq = 0;
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const cut = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function shown(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none" && parseFloat(st.opacity || "1") > 0.05;
  }

  function isField(el) {
    return el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT";
  }

  function labelOf(el) {
    let t = el.getAttribute("aria-label") || "";
    const by = el.getAttribute("aria-labelledby");
    if (!t && by) t = by.split(/\s+/).map((id) => (document.getElementById(id) || {}).innerText || "").join(" ");
    if (!t && el.labels && el.labels.length) t = el.labels[0].innerText;
    if (!t && !isField(el)) t = el.innerText;
    if (!t) t = el.getAttribute("placeholder") || el.title || "";
    if (!t) { const img = el.querySelector && el.querySelector("img[alt]"); t = img ? img.alt : ""; }
    if (!t && el.tagName === "INPUT" && /^(submit|button)$/i.test(el.type)) t = el.value;
    return cut(clean(t), 120);
  }

  function isSearchField(el) {
    if (el.type === "search" || el.getAttribute("role") === "searchbox") return true;
    const hints = [el.name, el.id, el.getAttribute("placeholder"), el.getAttribute("aria-label")].filter(Boolean).join(" ");
    if (SEARCH_HINT.test(hints)) return true;
    const form = el.form || el.closest("form");
    return !!form && (form.getAttribute("role") === "search" || /search|поиск/i.test(form.action || ""));
  }

  function idOf(el) {
    let id = el.getAttribute(ATTR);
    if (!id) { id = String(++seq); el.setAttribute(ATTR, id); }
    return Number(id);
  }

  function info(el) {
    const tag = el.tagName.toLowerCase();
    const d = { id: idOf(el), tag, text: labelOf(el) };
    const role = el.getAttribute("role");
    if (role) d.role = role;
    if (tag === "input") d.type = (el.type || "text").toLowerCase();
    if (tag === "a") d.href = el.href;
    if (isField(el) && d.type !== "password" && !/^(submit|button|checkbox|radio)$/.test(d.type || "") && el.value)
      d.value = cut(String(el.value), 80);
    if (el.checked) d.checked = true;
    if (el.disabled) d.disabled = true;
    const ac = el.getAttribute("autocomplete");
    if (ac) d.ac = ac;
    const name = el.getAttribute("name") || el.id || "";
    if (name) d.name = cut(name, 40);
    if (el.placeholder) d.placeholder = cut(el.placeholder, 60);
    if (tag === "select") d.options = [...el.options].slice(0, 30).map((o) => cut(clean(o.text), 40));
    if (el.isContentEditable) d.editable = true;
    if (el.form || el.closest("form")) d.form = true;
    if ((isField(el) || el.isContentEditable) && isSearchField(el)) d.search = true;
    const r = el.getBoundingClientRect();
    d.inView = r.bottom > 0 && r.top < innerHeight;
    return d;
  }

  function pageText(limit) {
    const text = (document.body ? document.body.innerText : "").replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n");
    return { text: text.slice(0, limit), total: text.length };
  }

  function snapshot({ max = 120, textLimit = 3000 } = {}) {
    const all = [...document.querySelectorAll(INTERACTIVE)].filter(shown);
    const picked = new Set();
    const result = [];
    const consider = (el) => {
      if (picked.has(el) || result.length >= max) return;
      const parent = el.parentElement && el.parentElement.closest(INTERACTIVE);
      if (parent && picked.has(parent)) {
        const own = labelOf(el);
        if (!own || own === labelOf(parent)) return;   // <a><div role=button>same text</div></a>
      }
      picked.add(el);
      result.push(el);
    };
    for (const el of all) { const r = el.getBoundingClientRect(); if (r.bottom > 0 && r.top < innerHeight) consider(el); }
    for (const el of all) consider(el);
    result.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    const body = pageText(textLimit);
    const dialogs = [...document.querySelectorAll("[role=dialog], [aria-modal=true], dialog[open]")].filter(shown);
    const pw = [...document.querySelectorAll("input[type=password]")].some(shown);
    const head = (document.body ? document.body.innerText : "").slice(0, 3000);
    return {
      url: location.href,
      title: document.title,
      elements: result.map(info),
      text: body.text,
      textTotal: body.total,
      scroll: { y: Math.round(scrollY), max: Math.max(0, Math.round(document.documentElement.scrollHeight - innerHeight)) },
      flags: {
        captcha: !!document.querySelector(CAPTCHA_SEL) || CAPTCHA_TEXT.test(head) && head.length < 1500,
        password: pw,
        dialog: dialogs.length ? cut(clean(dialogs[0].innerText), 200) : "",
        video: !!document.querySelector("video"),
        canvas: [...document.querySelectorAll("canvas")].some((c) => c.width * c.height > 200000),
      },
    };
  }

  function links({ max = 80 } = {}) {
    const seen = new Set();
    const out = [];
    for (const a of document.querySelectorAll("a[href]")) {
      if (out.length >= max) break;
      const href = a.href;
      if (!/^https?:/.test(href) || seen.has(href)) continue;
      const text = cut(clean(a.innerText || a.getAttribute("aria-label") || a.title || ""), 160);
      if (text.length < 6) continue;
      const r = a.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      seen.add(href);
      out.push({ text, href });
    }
    return out;
  }

  function readable({ limit = 30000 } = {}) {
    const cands = document.querySelectorAll(
      "article, main, [role=main], #content, .content, .post, .article, .entry-content, .post-content, .article__body, .tm-article-body");
    let best = null;
    let bestLen = 0;
    for (const c of cands) {
      if (!shown(c)) continue;
      const n = (c.innerText || "").length;
      if (n > bestLen) { best = c; bestLen = n; }
    }
    const bodyLen = (document.body ? document.body.innerText : "").length;
    const root = best && bestLen > 500 && bestLen > 0.25 * bodyLen ? best : document.body;
    const text = (root ? root.innerText : "").replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n\n");
    const meta = document.querySelector("meta[name=description], meta[property='og:description']");
    return {
      url: location.href, title: document.title, description: meta ? meta.content : "",
      text: text.slice(0, limit), total: text.length, lang: document.documentElement.lang || "",
    };
  }

  function byId(id) {
    return document.querySelector(`[${ATTR}="${Number(id)}"]`);
  }

  function fire(el, type, Ctor, extra) {
    el.dispatchEvent(new Ctor(type, { bubbles: true, cancelable: true, composed: true, view: window, ...extra }));
  }

  // The element the agent is about to use gets a glowing frame for a moment: watching its tab, the user sees it.
  function highlight(el) {
    const old = [el.style.outline, el.style.outlineOffset, el.style.transition];
    el.style.transition = "outline-color .2s";
    el.style.outline = "3px solid #1FD1A5";
    el.style.outlineOffset = "2px";
    setTimeout(() => { [el.style.outline, el.style.outlineOffset, el.style.transition] = old; }, 1500);
  }

  // "Джарвис: ищу наушники…" in the corner of the agent's tab. A closed shadow root keeps it out of the page text
  // the agent reads, and it never catches clicks.
  let bannerHost = null;
  let bannerText = null;
  function banner({ text = "" } = {}) {
    if (!text) {
      if (bannerHost) bannerHost.remove();
      bannerHost = null;
      return { ok: true };
    }
    if (!bannerHost || !bannerHost.isConnected) {
      bannerHost = document.createElement("div");
      bannerHost.style.cssText = "position:fixed;top:12px;right:12px;z-index:2147483647;pointer-events:none";
      const root = bannerHost.attachShadow({ mode: "closed" });
      const box = document.createElement("div");
      box.style.cssText = "font:600 13px 'Segoe UI',sans-serif;color:#E8FFF8;background:rgba(4,16,28,.88);" +
        "border:1px solid #1FD1A5;border-radius:10px;padding:8px 12px;box-shadow:0 0 14px rgba(31,209,165,.45);" +
        "max-width:360px";
      bannerText = document.createElement("span");
      box.append("Джарвис: ", bannerText);
      root.append(box);
      document.documentElement.append(bannerHost);
    }
    bannerText.textContent = text;
    return { ok: true };
  }

  function gone(id) {
    return { ok: false, error: `элемента ${id} уже нет: страница изменилась, нужен свежий снимок` };
  }

  function click({ id }) {
    const el = byId(id);
    if (!el) return gone(id);
    el.scrollIntoView({ block: "center", inline: "center" });
    highlight(el);
    const a = el.closest("a[href]");
    if (a && a.target === "_blank" && /^https?:/.test(a.href)) return { ok: true, navigate: a.href };
    const r = el.getBoundingClientRect();
    const at = { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 };
    fire(el, "pointerover", PointerEvent, at);
    fire(el, "pointerdown", PointerEvent, at);
    fire(el, "mousedown", MouseEvent, at);
    if (el.focus) el.focus({ preventScroll: true });
    fire(el, "pointerup", PointerEvent, at);
    fire(el, "mouseup", MouseEvent, at);
    el.click();
    return { ok: true };
  }

  function setValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function type({ id, text = "", submit = false }) {
    const el = byId(id);
    if (!el) return gone(id);
    const hints = [el.type, el.name, el.id, el.getAttribute("autocomplete"), el.getAttribute("placeholder"),
                   el.getAttribute("aria-label")].filter(Boolean).join(" ");
    if (el.type === "password" || SECRET_FIELD.test(hints))
      return { ok: false, error: "пароли, данные карт, коды из СМС и документы вводит только пользователь" };
    el.scrollIntoView({ block: "center" });
    highlight(el);
    el.focus();
    if (el.isContentEditable) {
      document.execCommand("selectAll");
      document.execCommand("insertText", false, text);
    } else {
      setValue(el, text);
    }
    if (submit) {
      const before = location.href;
      for (const t of ["keydown", "keypress", "keyup"])
        el.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
      await sleep(600);
      if (location.href === before && el.form) {
        try { el.form.requestSubmit(); } catch (e) { el.form.submit(); }
      }
    }
    return { ok: true };
  }

  function select({ id, value = "" }) {
    const el = byId(id);
    if (!el) return gone(id);
    if (el.tagName !== "SELECT") return click({ id });
    const want = String(value).toLowerCase();
    const i = [...el.options].findIndex((o) => o.value.toLowerCase() === want || clean(o.text).toLowerCase().includes(want));
    if (i < 0) return { ok: false, error: "нет такого варианта" };
    el.selectedIndex = i;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true };
  }

  function scroll({ to = "", dy = 0 } = {}) {
    if (to === "top") window.scrollTo(0, 0);
    else if (to === "bottom") window.scrollTo(0, document.documentElement.scrollHeight);
    else window.scrollBy(0, dy || Math.round(innerHeight * 0.85));
    // A hidden window runs no rendering steps, so scroll events would never come: lazy lists wait for them.
    window.dispatchEvent(new Event("scroll"));
    document.dispatchEvent(new Event("scroll"));
    return { ok: true, y: Math.round(scrollY) };
  }

  function key({ key = "Escape" } = {}) {
    const el = document.activeElement || document.body;
    for (const t of ["keydown", "keyup"]) el.dispatchEvent(new KeyboardEvent(t, { key, code: key, bubbles: true, cancelable: true }));
    return { ok: true };
  }

  async function video({ cmd = "state" } = {}) {
    const v = document.querySelector("video");
    if (!v) return { ok: false, error: "на странице нет видео" };
    try {
      if (cmd === "play") await v.play();
      if (cmd === "pause") v.pause();
    } catch (e) {
      return { ok: false, error: String(e && e.name), gesture: true };
    }
    const r = v.getBoundingClientRect();
    return { ok: true, paused: v.paused, time: Math.round(v.currentTime), duration: Math.round(v.duration || 0),
             x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  function center({ id }) {
    const el = byId(id);
    if (!el) return gone(id);
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    return { ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  window.__jarvis = { v: 2, snapshot, links, readable, click, type, select, scroll, key, video, center, banner };
})();
