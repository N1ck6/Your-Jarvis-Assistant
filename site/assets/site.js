/* Jarvis landing: boot, scroll-driven motion (HUD, hero drift, 3D console, pinned story, marquee), reveals,
   tilt and magnetic hover, the talking console, the microphone reactor, command tabs. The background is bg.js. */
(() => {
  "use strict";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fine = matchMedia("(pointer: fine)").matches;
  const root = document.documentElement;
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const voice = (name) => `assets/voice/${name}.mp3`;
  const bg = () => window.JarvisBG;

  /* ================================================================ boot */
  function ready() { root.classList.add("ready"); }
  async function boot() {
    const log = $("#boot-log"), fill = $("#boot-fill");
    const lines = ["> J.A.R.V.I.S. // ЗАПУСК", "  микрофон ........ OK", "  распознавание ... OK", "  голос ........... OK", "  реактор ......... 100%"];
    let skip = false;
    const onSkip = () => { skip = true; };
    addEventListener("pointerdown", onSkip, { once: true });
    addEventListener("keydown", onSkip, { once: true });
    for (let i = 0; i < lines.length && !skip; i++) {
      log.textContent += (i ? "\n" : "") + lines[i];
      fill.style.transform = `scaleX(${(i + 1) / lines.length})`;
      await sleep(i ? 170 : 260);
    }
    root.classList.add("boot-out");
    await sleep(250);
    ready();
    await sleep(900);
    root.classList.add("booted");
    try { sessionStorage.setItem("booted", "1"); } catch (_) { /* private mode */ }
  }
  if (reduced || root.classList.contains("booted")) { root.classList.add("booted"); requestAnimationFrame(ready); } else boot();

  /* ================================================================ navigation */
  const body = document.body;
  const menuBtn = $(".menu-btn");
  const menuLinks = $$("#menu a");
  const setMenu = (open) => {
    body.classList.toggle("menu-open", open);
    menuBtn.setAttribute("aria-expanded", String(open));
    menuBtn.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
    body.style.overflow = open ? "hidden" : "";
    menuLinks.forEach((a, i) => { a.style.transitionDelay = open ? `${Math.min(i * 60, 400)}ms` : "0ms"; });
  };
  menuBtn.addEventListener("click", () => setMenu(!body.classList.contains("menu-open")));
  menuLinks.forEach((a) => a.addEventListener("click", () => setMenu(false)));
  addEventListener("keydown", (e) => { if (e.key === "Escape" && body.classList.contains("menu-open")) { setMenu(false); menuBtn.focus(); } });

  // smooth anchor jumps (the pinned section is tall, so jump to its start)
  $$('a[href^="#"]').forEach((a) => a.addEventListener("click", (e) => {
    const id = a.getAttribute("href").slice(1);
    const target = id && document.getElementById(id);
    if (!target) return;
    e.preventDefault();
    scrollTo({ top: target.getBoundingClientRect().top + scrollY - (id === "top" ? 0 : 16), behavior: reduced ? "auto" : "smooth" });
    history.replaceState(null, "", `#${id}`);
  }));

  /* ================================================================ reveals, scrambles, counters */
  const GLYPHS = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ0123456789#%&<>/";
  function scramble(el) {
    if (reduced || el.dataset.done) return;
    el.dataset.done = "1";
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push([walker.currentNode, walker.currentNode.textContent]);
    const total = nodes.reduce((n, [, s]) => n + s.length, 0);
    const start = performance.now();
    const dur = Math.min(1100, 300 + total * 14);
    (function step(now) {
      const k = clamp((now - start) / dur);
      let shown = Math.floor(k * total);
      for (const [node, text] of nodes) {
        let out = "";
        for (let i = 0; i < text.length; i++) {
          const ch = text[i];
          if (shown > 0 || ch === " " || ch === "\n") out += ch;
          else out += Math.random() < 0.5 ? GLYPHS[(Math.random() * GLYPHS.length) | 0] : ch;
          shown--;
        }
        node.textContent = out;
      }
      if (k < 1) requestAnimationFrame(step);
      else nodes.forEach(([node, text]) => { node.textContent = text; });
    })(start);
  }

  function count(el) {
    const to = parseFloat(el.dataset.to), dec = +(el.dataset.dec || 0), suffix = el.dataset.suffix || "";
    const fmt = (v) => v.toFixed(dec).replace(".", ",") + (suffix ? `<small>${suffix === "%" ? "%" : " " + suffix}</small>` : "");
    if (reduced) { el.innerHTML = fmt(to); return; }
    const start = performance.now();
    (function step(now) {
      const k = clamp((now - start) / 1600);
      const e = 1 - Math.pow(2, -10 * k);
      el.innerHTML = fmt(k < 1 ? to * e + (Math.random() - 0.5) * to * 0.04 * (1 - k) : to);
      if (k < 1) requestAnimationFrame(step);
    })(start);
  }

  const revealer = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      const el = en.target;
      el.classList.add("in");
      $$("[data-scramble]", el).forEach(scramble);
      $$(".num", el).forEach(count);
      if (el.classList.contains("compare")) $$("tbody tr", el).forEach((tr, i) => { tr.style.transitionDelay = `${200 + i * 70}ms`; });
      revealer.unobserve(el);
    });
  }, { rootMargin: "0px 0px -12% 0px" });
  $$(".reveal").forEach((el) => {
    const sibs = el.parentElement ? $$(":scope > .reveal", el.parentElement) : [];
    const idx = sibs.indexOf(el);
    if (idx > 0) el.style.transitionDelay = `${Math.min(idx * 110, 440)}ms`;
    revealer.observe(el);
  });
  // the hero eyebrow decodes on load
  setTimeout(() => $$(".hero [data-scramble]").forEach(scramble), root.classList.contains("booted") ? 100 : 1400);

  /* ================================================================ pointer: cursor, magnetic, tilt */
  const pointer = { x: innerWidth / 2, y: innerHeight / 2 };
  const cursor = $(".cursor");
  const cur = { x: pointer.x, y: pointer.y };
  addEventListener("pointermove", (e) => { pointer.x = e.clientX; pointer.y = e.clientY; }, { passive: true });
  if (fine && !reduced) {
    const HOVER = "a, button, summary, [role=tab], .tilt";
    addEventListener("pointerover", (e) => cursor.classList.toggle("hover", !!e.target.closest(HOVER)));
    addEventListener("pointerdown", () => cursor.classList.add("down"));
    addEventListener("pointerup", () => cursor.classList.remove("down"));

    $$(".magnetic").forEach((btn) => {
      btn.addEventListener("pointermove", (e) => {
        const r = btn.getBoundingClientRect();
        btn.style.transform = `translate(${(e.clientX - r.left - r.width / 2) * 0.3}px, ${(e.clientY - r.top - r.height / 2) * 0.45}px)`;
      });
      btn.addEventListener("pointerleave", () => { btn.style.transform = ""; });
    });

    document.addEventListener("pointermove", (e) => {
      const card = e.target.closest && e.target.closest(".tilt");
      if (!card) return;
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height;
      card.style.setProperty("--ry", `${(px - 0.5) * 10}deg`);
      card.style.setProperty("--rx", `${(0.5 - py) * 10}deg`);
      card.style.setProperty("--mx", `${px * 100}%`);
      card.style.setProperty("--my", `${py * 100}%`);
    });
    document.addEventListener("pointerout", (e) => {
      const card = e.target.closest && e.target.closest(".tilt");
      if (card && !card.contains(e.relatedTarget)) { card.style.setProperty("--rx", "0deg"); card.style.setProperty("--ry", "0deg"); }
    });
  }

  /* ================================================================ audio */
  let audioCtx = null, analyser = null, current = null;
  const level = new Float32Array(1024);
  function ensureAudio() {
    if (audioCtx) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    audioCtx = new Ctx();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.connect(audioCtx.destination);
  }
  function stopVoice() { if (current) { const a = current; current = null; a.pause(); a.dispatchEvent(new Event("ended")); } }
  function playVoice(name) {
    stopVoice();
    ensureAudio();
    const audio = new Audio(voice(name));
    try {
      if (audioCtx) { audioCtx.createMediaElementSource(audio).connect(analyser); if (audioCtx.state === "suspended") audioCtx.resume(); }
    } catch (_) { /* opened as a file: plays without the meter */ }
    current = audio;
    return new Promise((resolve) => {
      const end = () => { if (current === audio) current = null; resolve(); };
      audio.addEventListener("ended", end, { once: true });
      audio.addEventListener("error", end, { once: true });
      audio.play().catch(end);
    });
  }
  function voiceLevel() {
    if (!analyser || !current) return 0;
    analyser.getFloatTimeDomainData(level);
    let s = 0;
    for (let i = 0; i < level.length; i++) s += level[i] * level[i];
    return clamp(Math.sqrt(s / level.length) * 4.5);
  }

  /* ================================================================ reactors */
  const COLORS = { idle: ["#2AA8E0", "#8FDCFF"], listen: ["#2AA8E0", "#8FDCFF"], think: ["#F5C542", "#FFE29A"], speak: ["#2AA8E0", "#8FDCFF"], hear: ["#FF7A6B", "#FFC2B8"] };
  function makeOrb(canvas, getLevel) {
    const o = { canvas, g: canvas.getContext("2d"), state: "idle", amp: 0.05, spin: 0, typing: 0, visible: false, level: getLevel };
    const fit = () => {
      const dpr = Math.min(devicePixelRatio || 1, 2), s = canvas.clientWidth || 280;
      canvas.width = s * dpr; canvas.height = s * dpr; o.g.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    fit(); addEventListener("resize", fit);
    new IntersectionObserver(([en]) => { o.visible = en.isIntersecting; }).observe(canvas);
    return o;
  }
  function drawOrb(o, time) {
    const g = o.g, size = o.canvas.clientWidth || 280, k = size / 280;
    const [main, light] = COLORS[o.state];
    let target = 0.05 + 0.03 * Math.sin(time * 1.6);
    if (o.state === "listen") target = 0.16 + o.typing * (0.25 + 0.2 * Math.abs(Math.sin(time * 11)));
    if (o.state === "think") target = 0.18;
    if (o.state === "speak") { const v = voiceLevel(); target = v > 0 ? v : 0.3 + 0.35 * Math.abs(Math.sin(time * 12.7) * Math.sin(time * 3.3)); }
    if (o.state === "hear") target = o.level ? o.level() : 0.1;
    o.amp += (target - o.amp) * 0.2;
    if (!reduced) o.spin += o.state === "think" ? 0.035 : 0.004 + o.amp * 0.012;

    g.clearRect(0, 0, size, size);
    g.save(); g.translate(size / 2, size / 2); g.scale(k, k);
    for (let i = 0; i < 120; i++) {
      const a = (i / 120) * Math.PI * 2;
      const n = 0.5 + 0.5 * Math.sin(i * 1.7 + time * 5) * Math.sin(i * 0.43 - time * 2.3);
      const len = 3 + o.amp * 28 * n;
      g.strokeStyle = light; g.globalAlpha = 0.18 + o.amp * 0.6 * n; g.lineWidth = 2;
      g.beginPath(); g.moveTo(Math.cos(a) * 118, Math.sin(a) * 118); g.lineTo(Math.cos(a) * (118 + len), Math.sin(a) * (118 + len)); g.stroke();
    }
    g.globalAlpha = 0.55; g.strokeStyle = main; g.lineWidth = 2;
    g.beginPath(); g.arc(0, 0, 110, 0, Math.PI * 2); g.stroke();
    g.save(); g.rotate(o.spin); g.globalAlpha = 0.9; g.lineWidth = 16; g.setLineDash([17, 6.5]);
    g.beginPath(); g.arc(0, 0, 90, 0.2, Math.PI * 2 - 0.2); g.stroke(); g.setLineDash([]); g.restore();
    g.save(); g.rotate(-o.spin * 1.7); g.globalAlpha = 0.6; g.strokeStyle = light; g.lineWidth = 2;
    g.beginPath(); g.arc(0, 0, 102, 0, 0.9); g.stroke(); g.beginPath(); g.arc(0, 0, 102, Math.PI, Math.PI + 0.9); g.stroke(); g.restore();
    g.globalAlpha = 1; g.strokeStyle = light; g.lineWidth = 2.5;
    g.beginPath(); g.arc(0, 0, 74, 0, Math.PI * 2); g.stroke();
    g.shadowColor = light; g.shadowBlur = 14 + o.amp * 44; g.fillStyle = main; g.globalAlpha = 0.35 + o.amp * 0.5;
    g.beginPath(); g.arc(0, 0, 68, 0, Math.PI * 2); g.fill(); g.shadowBlur = 0;
    g.globalAlpha = 1; g.lineJoin = "round"; g.lineCap = "round"; g.fillStyle = "#04101C"; g.strokeStyle = light; g.lineWidth = 3;
    const s = 1.85;
    g.beginPath(); g.moveTo(-34 * s, -19 * s); g.lineTo(34 * s, -19 * s); g.lineTo(0, 36 * s); g.closePath(); g.fill(); g.stroke();
    g.beginPath(); g.moveTo(-20 * s, -11 * s); g.lineTo(20 * s, -11 * s); g.lineTo(0, 22 * s); g.closePath(); g.stroke();
    g.beginPath(); g.moveTo(-20 * s, -11 * s); g.lineTo(0, s); g.lineTo(20 * s, -11 * s); g.moveTo(0, s); g.lineTo(0, 22 * s); g.stroke();
    g.restore();
  }

  /* the pinned story: six stages on a ring, lit by the scroll */
  const pipeCanvas = $("#pipe-orb");
  const pipe = { g: pipeCanvas.getContext("2d"), p: 0, target: 0, spin: 0, visible: false, stage: -1 };
  const PIPE_LABELS = ["Микрофон", "Имя", "Текст", "Понимание", "Действие", "Ответ"];
  const fitPipe = () => { const dpr = Math.min(devicePixelRatio || 1, 2), s = pipeCanvas.clientWidth || 400; pipeCanvas.width = s * dpr; pipeCanvas.height = s * dpr; pipe.g.setTransform(dpr, 0, 0, dpr, 0, 0); };
  fitPipe(); addEventListener("resize", fitPipe);
  new IntersectionObserver(([en]) => { pipe.visible = en.isIntersecting; }).observe(pipeCanvas);
  function drawPipe(time) {
    const g = pipe.g, size = pipeCanvas.clientWidth || 400, c = size / 2, R = size * 0.36;
    pipe.p += (pipe.target - pipe.p) * 0.12;
    const p = pipe.p, amber = pipe.stage === 3;
    const main = amber ? "#F5C542" : "#2AA8E0", light = amber ? "#FFE29A" : "#8FDCFF";
    pipe.spin += reduced ? 0 : 0.003;
    g.clearRect(0, 0, size, size);
    g.save(); g.translate(c, c);
    // the track and its lit part
    g.lineWidth = 2; g.strokeStyle = "rgba(143,220,255,0.15)";
    g.beginPath(); g.arc(0, 0, R, 0, Math.PI * 2); g.stroke();
    const a0 = -Math.PI / 2, a1 = a0 + Math.PI * 2 * Math.min(p, 5 / 6);
    g.strokeStyle = light; g.lineWidth = 3; g.shadowColor = light; g.shadowBlur = 16;
    g.beginPath(); g.arc(0, 0, R, a0, a1); g.stroke();
    g.fillStyle = "#FFFFFF"; g.beginPath(); g.arc(Math.cos(a1) * R, Math.sin(a1) * R, 4, 0, Math.PI * 2); g.fill();
    g.shadowBlur = 0;
    // reactor in the middle, turned by the scroll
    g.save(); g.rotate(pipe.spin + p * Math.PI * 4);
    g.strokeStyle = main; g.lineWidth = size * 0.035; g.globalAlpha = 0.85; g.setLineDash([size * 0.03, size * 0.012]);
    g.beginPath(); g.arc(0, 0, R * 0.62, 0, Math.PI * 2); g.stroke(); g.setLineDash([]);
    g.restore();
    g.save(); g.rotate(-pipe.spin * 2 - p * Math.PI * 2); g.strokeStyle = light; g.lineWidth = 1.5; g.globalAlpha = 0.7;
    for (let i = 0; i < 3; i++) { g.beginPath(); g.arc(0, 0, R * 0.78, i * 2.09, i * 2.09 + 1.1); g.stroke(); }
    g.restore();
    g.globalAlpha = 1; g.shadowColor = light; g.shadowBlur = 30; g.fillStyle = main; g.globalAlpha = 0.28 + 0.1 * Math.sin(time * 3);
    g.beginPath(); g.arc(0, 0, R * 0.42, 0, Math.PI * 2); g.fill(); g.shadowBlur = 0; g.globalAlpha = 1;
    g.strokeStyle = light; g.lineWidth = 2; g.beginPath(); g.arc(0, 0, R * 0.46, 0, Math.PI * 2); g.stroke();
    // nodes and labels
    g.font = `600 ${Math.max(11, size * 0.026)}px Manrope, sans-serif`;
    g.textAlign = "center"; g.textBaseline = "middle";
    for (let i = 0; i < 6; i++) {
      const a = a0 + (Math.PI * 2 * i) / 6, x = Math.cos(a) * R, y = Math.sin(a) * R;
      const on = i <= pipe.stage, now = i === pipe.stage;
      g.beginPath(); g.arc(x, y, now ? 9 + Math.sin(time * 6) * 2 : 7, 0, Math.PI * 2);
      g.fillStyle = on ? light : "#02060B"; g.strokeStyle = on ? light : "rgba(143,220,255,0.4)"; g.lineWidth = 2;
      if (now) { g.shadowColor = light; g.shadowBlur = 24; }
      g.fill(); g.stroke(); g.shadowBlur = 0;
      if (now) { g.globalAlpha = 0.5; g.beginPath(); g.arc(x, y, 18 + Math.sin(time * 6) * 3, 0, Math.PI * 2); g.stroke(); g.globalAlpha = 1; }
      const lx = Math.cos(a) * (R + size * 0.075), ly = Math.sin(a) * (R + size * 0.075);
      g.fillStyle = on ? "#EEF6FA" : "rgba(166,180,190,0.6)";
      g.fillText(PIPE_LABELS[i], lx, ly);
    }
    g.restore();
  }

  /* ================================================================ the scroll engine */
  const hudSections = $$("[data-hud]");
  const hudNum = $("#hud-num"), hudName = $("#hud-name"), hudFill = $("#hud-fill"), hudPct = $("#hud-pct"), progress = $("#progress");
  $("#hud-total").textContent = String(hudSections.length).padStart(2, "0");
  const heroCopy = $(".hero-copy"), masks = $$(".hero h1 .mask"), consoleEl = $("#console"), console3d = $("#console-3d");
  const ghosts = $$("[data-speed]");
  const inside = $("#inside"), stagesEl = $$("#stages .st"), dots = $$("#pin-dots i"), pinNum = $("#pin-num");
  const navLinks = $$(".nav-links a");
  const mqs = $$(".mq").map((mq) => {
    const track = $(".mq-track", mq);
    track.innerHTML += track.innerHTML; // twice as long, so it can wrap around
    return { track, dir: +mq.dataset.dir, x: 0 };
  });
  let lastY = scrollY, vel = 0, hudIdx = -1;

  function onFrame(time) {
    const vh = innerHeight, y = scrollY, max = Math.max(1, root.scrollHeight - vh);
    vel += ((y - lastY) - vel) * 0.12; lastY = y;
    if (bg()) bg().setVelocity(vel);

    const page = y / max;
    progress.style.transform = `scaleX(${page})`;
    hudFill.style.transform = `scaleY(${page})`;
    hudPct.textContent = `${Math.round(page * 100)}%`;

    // which section is in the middle of the screen
    let idx = 0;
    hudSections.forEach((s, i) => { if (s.getBoundingClientRect().top < vh * 0.5) idx = i; });
    if (idx !== hudIdx) {
      hudIdx = idx;
      hudNum.textContent = String(idx + 1).padStart(2, "0");
      hudName.textContent = hudSections[idx].dataset.hud;
      const id = hudSections[idx].id;
      navLinks.forEach((a) => a.setAttribute("aria-current", String(a.getAttribute("href") === `#${id}`)));
    }

    if (!reduced) {
      // hero: the two lines part, the copy sinks and fades
      const hp = clamp(y / (vh * 0.9));
      masks[0].style.transform = `translateX(${-hp * 22}vw)`;
      masks[1].style.transform = `translateX(${hp * 22}vw)`;
      heroCopy.style.transform = `translateY(${hp * 120}px)`;
      heroCopy.style.opacity = String(1 - hp * 1.1);

      // console stands up as it enters
      const r = console3d.getBoundingClientRect();
      const cp = clamp((vh - r.top) / (vh * 0.85));
      consoleEl.style.setProperty("--tilt", `${28 * (1 - cp)}deg`);
      consoleEl.style.setProperty("--lift", `${80 * (1 - cp)}px`);
      consoleEl.style.setProperty("--sc", String(0.9 + 0.1 * cp));

      ghosts.forEach((el) => {
        const sr = el.parentElement.getBoundingClientRect();
        el.style.transform = `translateY(${(sr.top + sr.height / 2 - vh / 2) * +el.dataset.speed}px)`;
      });

      // marquee: runs on its own, faster and skewed with the scroll speed
      mqs.forEach((m) => {
        const half = m.track.scrollWidth / 2;
        m.x -= m.dir * (0.6 + Math.abs(vel) * 0.35) * (vel < -0.5 ? -1 : 1);
        if (m.x <= -half) m.x += half;
        if (m.x > 0) m.x -= half;
        m.track.style.transform = `translateX(${m.x}px) skewX(${clamp(-vel * 0.4, -14, 14)}deg)`;
      });
    }

    // pinned story
    const ir = inside.getBoundingClientRect();
    const ip = clamp(-ir.top / (ir.height - vh));
    pipe.target = ip;
    const stage = Math.min(5, Math.floor(ip * 6));
    if (stage !== pipe.stage) {
      pipe.stage = stage;
      stagesEl.forEach((s, i) => { s.classList.toggle("on", i === stage); s.classList.toggle("past", i < stage); });
      dots.forEach((d, i) => d.classList.toggle("on", i <= stage));
      pinNum.textContent = String(stage + 1).padStart(2, "0");
      pinNum.classList.toggle("amber", stage === 3);
    }
    const inStory = ir.top < vh * 0.5 && ir.bottom > vh * 0.5;
    if (bg()) bg().setHue(inStory ? (stage === 3 ? 1 : 0) : +(hudSections[hudIdx].dataset.hue || 0));

    // cursor ring
    if (fine && !reduced) {
      cur.x += (pointer.x - cur.x) * 0.2; cur.y += (pointer.y - cur.y) * 0.2;
      cursor.style.transform = `translate(${cur.x}px, ${cur.y}px)`;
    }

    const t = time / 1000;
    if (heroOrb.visible) drawOrb(heroOrb, t);
    if (micOrb.visible) drawOrb(micOrb, t);
    if (pipe.visible) drawPipe(t);
    requestAnimationFrame(onFrame);
  }

  /* ================================================================ HUD readouts */
  const hudXY = $("#hud-xy"), clock = $("#hud-clock");
  addEventListener("pointermove", (e) => { hudXY.textContent = `X ${String(Math.round(e.clientX)).padStart(4, "0")} · Y ${String(Math.round(e.clientY + scrollY)).padStart(4, "0")}`; }, { passive: true });
  const tick = () => { clock.textContent = new Date().toLocaleTimeString("ru-RU"); };
  tick(); setInterval(tick, 1000);

  /* ================================================================ the console */
  const heroOrb = makeOrb($("#orb"));
  const SCENES = [
    { chip: "Открой телеграм", said: "Джарвис, открой телеграм", reply: "Открываю Телеграм.", done: "Telegram на переднем плане", voice: "telegram" },
    { chip: "Напомни про плиту", said: "Джарвис, напомни через 15 минут выключить плиту", reply: "Хорошо. Через пятнадцать минут напомню выключить плиту.", done: "Таймер 15:00 · выключить плиту", voice: "timer" },
    { chip: "Спокойная музыка", said: "Джарвис, включи спокойную музыку", reply: "Включаю спокойную подборку. Приятного вечера, сэр.", done: "Играет плейлист «Спокойное»", voice: "music" },
    { chip: "Будет ли дождь", said: "Джарвис, когда будет следующий дождь?", reply: "Дождь начнётся завтра около четырёх часов дня. Зонт лучше взять.", done: "Прогноз по часам на карточке", voice: "rain" },
    { chip: "Реши задачу", said: "Джарвис, реши задачу с экрана", reply: "Готово. Решение и ответ на экране.", done: "Окна «Решение» и «Ответ» закреплены", voice: "solve" },
    { chip: "Ты здесь?", said: "Джарвис, ты здесь?", reply: "Добрый вечер, сэр. Все системы в норме. Чем могу помочь?", done: "Готов к работе", voice: "hello" },
  ];
  const stateEl = $(".state"), stateText = $("#state-text"), said = $("#said"), reply = $("#reply");
  const steps = $$("#steps li"), done = $("#done"), soundBtn = $("#sound"), picker = $(".picker");
  let soundOn = false, run = 0, sceneIdx = 0, consoleVisible = false;
  const chips = SCENES.map((sc, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip"; b.textContent = sc.chip; b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => play(i, true));
    picker.appendChild(b);
    return b;
  });
  const STATES = { idle: "Жду «Джарвис»", listen: "Слушаю", think: "Думаю", speak: "Отвечаю" };
  function setState(s) { heroOrb.state = s; stateEl.dataset.s = s; stateText.textContent = STATES[s]; }
  async function type(el, text, perChar, id) {
    el.innerHTML = '<span class="t"></span><span class="caret"></span>';
    const t = el.firstChild;
    for (let i = 1; i <= text.length; i++) { if (id !== run) return false; t.textContent = text.slice(0, i); await sleep(perChar); }
    el.lastChild.remove();
    return true;
  }
  async function play(i, byUser = false) {
    const id = ++run;
    stopVoice();
    sceneIdx = i;
    const sc = SCENES[i];
    chips.forEach((c, j) => c.setAttribute("aria-pressed", String(j === i)));
    steps.forEach((s) => s.classList.remove("on"));
    done.classList.remove("on");
    said.textContent = ""; reply.textContent = "";
    setState("listen"); steps[0].classList.add("on"); heroOrb.typing = 1;
    if (!(await type(said, sc.said, 38, id))) return;
    heroOrb.typing = 0; steps[1].classList.add("on"); setState("think");
    await sleep(520);
    if (id !== run) return;
    steps[2].classList.add("on"); setState("speak");
    const speaking = soundOn ? playVoice(sc.voice) : sleep(Math.max(1400, sc.reply.length * 52));
    await Promise.all([speaking, type(reply, sc.reply, 26, id)]);
    if (id !== run) return;
    steps[3].classList.add("on");
    done.lastElementChild.textContent = sc.done; done.classList.add("on");
    setState("idle");
    await sleep(byUser ? 4200 : 3000);
    if (id !== run) return;
    if (!consoleVisible || document.hidden) { run++; return; }
    play((sceneIdx + 1) % SCENES.length);
  }
  new IntersectionObserver(([en]) => {
    const was = consoleVisible;
    consoleVisible = en.isIntersecting;
    if (consoleVisible && !was) play(sceneIdx);
    if (!consoleVisible) { run++; if (heroOrb.state !== "idle") stopVoice(); setState("idle"); }
  }, { threshold: 0.35 }).observe(consoleEl);
  document.addEventListener("visibilitychange", () => { if (document.hidden) { run++; stopVoice(); } else if (consoleVisible) play(sceneIdx); });
  soundBtn.addEventListener("click", () => {
    soundOn = !soundOn;
    soundBtn.setAttribute("aria-pressed", String(soundOn));
    soundBtn.innerHTML = soundOn ? '<i class="ph ph-speaker-high"></i><span>Голос включён</span>' : '<i class="ph ph-speaker-slash"></i><span>Включить голос</span>';
    if (soundOn) { ensureAudio(); play(SCENES.length - 1, true); } else stopVoice();
  });

  /* ================================================================ the microphone reactor */
  let micLevel = 0;
  const micOrb = makeOrb($("#mic-orb"), () => micLevel);
  const micBtn = $("#mic"), micNote = $("#mic-note");
  let micStream = null, micTimer = 0;
  async function micOn() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      micNote.textContent = "Этот браузер не даёт странице доступ к микрофону."; micNote.classList.add("err"); return;
    }
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (err) {
      micNote.textContent = err && err.name === "NotFoundError" ? "Микрофон не найден. Подключите его и попробуйте ещё раз." : "Нет доступа к микрофону. Разрешите его в настройках сайта в браузере.";
      micNote.classList.add("err"); return;
    }
    ensureAudio();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const an = audioCtx.createAnalyser(); an.fftSize = 1024;
    audioCtx.createMediaStreamSource(micStream).connect(an);
    const buf = new Float32Array(1024);
    let speaking = false, since = 0, last = 0, busy = false;
    micBtn.setAttribute("aria-pressed", "true");
    micBtn.lastElementChild.textContent = "Слушаю. Нажмите, чтобы выключить";
    micNote.classList.remove("err");
    micNote.textContent = "Скажите «Джарвис» и замолчите на секунду.";
    micOrb.state = "hear";
    micTimer = setInterval(async () => {
      an.getFloatTimeDomainData(buf);
      let s = 0; for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
      micLevel = clamp(Math.sqrt(s / buf.length) * 7);
      const now = performance.now();
      if (busy) return;
      if (micLevel > 0.12) { if (!speaking) { speaking = true; since = now; } last = now; }
      else if (speaking && now - last > 650) {
        speaking = false;
        if (last - since > 200) {
          busy = true; micOrb.state = "speak"; micNote.textContent = "«Да, сэр?»";
          await playVoice("yes");
          await sleep(400);
          if (micStream) { micOrb.state = "hear"; micNote.textContent = "Ещё раз? Реактор слушает."; }
          busy = false;
        }
      }
    }, 50);
  }
  function micOff() {
    clearInterval(micTimer);
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    micStream = null; micLevel = 0; micOrb.state = "idle";
    micBtn.setAttribute("aria-pressed", "false");
    micBtn.lastElementChild.textContent = "Скажите «Джарвис» в микрофон";
    micNote.textContent = "Реактор отзовётся на ваш голос. Звук не покидает страницу.";
  }
  micBtn.addEventListener("click", () => (micStream ? micOff() : micOn()));

  requestAnimationFrame(onFrame);

  /* ================================================================ tagline: words light up one by one */
  const tagline = $("#tagline");
  $$(".l", tagline).forEach((line) => {
    const accent = line.hasAttribute("data-accent");
    line.innerHTML = line.textContent.split(" ").map((w) => `<span class="w${accent ? " accent" : ""}">${w}</span>`).join(" ");
  });
  const words = $$(".w", tagline);
  let lit = 0, ticking = false;
  function lightWords() {
    ticking = false;
    const line = innerHeight * 0.66;
    while (lit < words.length && words[lit].getBoundingClientRect().top < line) {
      const w = words[lit++];
      w.style.transitionDelay = `${(lit % 4) * 40}ms`;
      w.classList.add("on");
    }
    if (lit >= words.length) removeEventListener("scroll", onScroll);
  }
  function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(lightWords); } }
  addEventListener("scroll", onScroll, { passive: true });
  lightWords();

  /* ================================================================ card demos, only while visible */
  function whenVisible(el, start, stop) { new IntersectionObserver(([en]) => (en.isIntersecting ? start() : stop && stop()), { threshold: 0.3 }).observe(el); }
  const solve = $("#solve-viz");
  whenVisible(solve, () => solve.classList.add("play"), () => solve.classList.remove("play"));

  const dictText = $("#dict-text");
  const PHRASES = ["Добрый день, Анна. ", "Отчёт за октябрь отправлю сегодня до шести. ", "Если нужны правки, напишите, пожалуйста."];
  let dictRun = 0;
  async function dictate() {
    const id = ++dictRun;
    while (id === dictRun) {
      dictText.innerHTML = '<span class="t"></span><span class="caret"></span>';
      const t = dictText.firstChild;
      for (const p of PHRASES) {
        await sleep(1100);
        for (let i = 0; i < p.length; i++) { if (id !== dictRun) return; t.textContent += p[i]; await sleep(14); }
      }
      await sleep(3200);
    }
  }
  whenVisible($("#dict-viz"), dictate, () => dictRun++);

  const agentItems = $$("#agent-viz li"), confirmEl = $("#agent-viz .confirm");
  let agentRun = 0;
  async function agent() {
    const id = ++agentRun;
    while (id === agentRun) {
      agentItems.forEach((li) => li.classList.remove("on")); confirmEl.classList.remove("on");
      for (const li of agentItems) { await sleep(800); if (id !== agentRun) return; li.classList.add("on"); }
      await sleep(600); if (id !== agentRun) return;
      confirmEl.classList.add("on");
      await sleep(3600);
    }
  }
  whenVisible($("#agent-viz"), agent, () => agentRun++);

  /* ================================================================ commands */
  const AREAS = [
    { id: "pc", title: "Компьютер", icon: "ph-desktop", items: [
      ["Открой телеграм", "Telegram выходит на передний план", "telegram"],
      ["Включи спокойную музыку", "Плейлист из папки «Музыка», пауза и треки с клавиатуры", "music"],
      ["Сделай дискорд тише", "Тише только Discord, остальной звук как был"],
      ["Сверни всё", "Все окна свёрнуты, рабочий стол перед вами"],
      ["Выключи компьютер через час", "Выключение через 60 минут, отмена одной фразой"],
      ["Что грузит компьютер", "Три процесса, которые сильнее всего нагружают систему"],
    ]},
    { id: "time", title: "Время и дела", icon: "ph-clock", items: [
      ["Напомни через 15 минут выключить плиту", "Таймер с подписью, напомнит голосом", "timer"],
      ["Будильник по будням в 7:30", "Повторяется с понедельника по пятницу"],
      ["Добавь в список покупок молоко и хлеб", "Список хранится у вас, вычеркнуть тоже голосом"],
      ["Запусти помодоро на 50 минут", "Работа и перерывы по таймеру"],
      ["Доброе утро", "Дата, погода, календарь и дела на сегодня"],
      ["Сколько будет 15% от 2300", "345, ответ сразу, без интернета"],
    ]},
    { id: "text", title: "Текст и экран", icon: "ph-text-aa", items: [
      ["Реши задачу с экрана", "Выделите область, рядом встанут окна «Решение» и «Ответ»", "solve"],
      ["Переведи выделенное", "Перевод на карточке, можно вставить вместо оригинала"],
      ["Исправь ошибки", "Исправленный текст встаёт на место выделенного"],
      ["Что это за ошибка", "Выделите сообщение на экране, Джарвис объяснит, что делать"],
      ["Напечатай буду через 10 минут", "Текст появляется там, где стоит курсор"],
      ["Скопируй текст с экрана", "Текст с картинки сразу в буфере обмена"],
    ]},
    { id: "know", title: "Знания", icon: "ph-lightbulb", items: [
      ["Когда будет следующий дождь?", "Ответ голосом, прогноз по часам на карточке", "rain"],
      ["Объясни подробно, что такое DNS", "Объяснение по шагам на карточках с картинками"],
      ["Покажи график y = x² − 3x", "График на карточке, можно уточнить голосом"],
      ["Новости технологий", "Пять главных новостей, любую можно открыть голосом"],
      ["Курс доллара", "Свежий курс из интернета"],
      ["А завтра?", "Уточнение к прошлому вопросу, без слова «Джарвис»"],
    ]},
    { id: "web", title: "Браузер", icon: "ph-browser", items: [
      ["Найди на Озоне наушники до 3000 с хорошими отзывами", "Три варианта со ссылками на карточке"],
      ["Включи на ютубе обзор RTX 5070", "Найдёт, откроет и включит видео"],
      ["Перескажи эту статью", "Краткий пересказ открытой страницы"],
      ["Каждый день в 9 проверяй цену на AirPods", "Скажет, когда подешевеют"],
      ["Сравни цены на пылесос на Маркете", "Таблица цен и рейтингов на карточке"],
      ["Покажи браузер", "Вкладка агента выходит вперёд, видно каждый шаг"],
    ]},
    { id: "you", title: "Джарвис и вы", icon: "ph-user-sound", items: [
      ["Джарвис", "«Да, сэр?» и ждёт команду", "yes"],
      ["Стоп", "Замолкает на полуслове и слушает новую команду"],
      ["Не то", "Отменяет действие и забывает, как понял фразу"],
      ["Запомни, что я не ем мясо", "Учтёт это в следующих ответах"],
      ["Запомни мой голос", "Дальше откликается только на вас"],
      ["Говори медленнее", "Скорость и громкость голоса меняются сразу"],
    ]},
  ];
  const tabs = $("#tabs"), panels = $("#panels");
  const tabEls = AREAS.map((area, i) => {
    const tab = document.createElement("button");
    tab.type = "button"; tab.className = "tab"; tab.id = `tab-${area.id}`;
    tab.setAttribute("role", "tab"); tab.setAttribute("aria-controls", `panel-${area.id}`);
    tab.innerHTML = `<i class="ph ${area.icon}"></i>${area.title}`;
    tabs.appendChild(tab);
    const panel = document.createElement("div");
    panel.className = "panel"; panel.id = `panel-${area.id}`;
    panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", tab.id);
    panel.hidden = true;
    area.items.forEach(([phrase, result, sample], j) => {
      const el = document.createElement(sample ? "button" : "div");
      if (sample) { el.type = "button"; el.setAttribute("aria-label", `${phrase}. Послушать ответ`); }
      el.className = "cmd glass tilt in";
      el.style.animationDelay = `${Math.min(j * 60, 400)}ms`;
      el.innerHTML = `<q>${phrase}</q><span><i class="ph ph-arrow-bend-down-right"></i>${result}</span>` +
        (sample ? '<b class="listen"><i class="ph-fill ph-speaker-high"></i>Послушать ответ</b>' : "");
      if (sample) el.addEventListener("click", async () => {
        $$(".cmd.playing").forEach((c) => c.classList.remove("playing"));
        el.classList.add("playing");
        run++; setState("idle");
        await playVoice(sample);
        el.classList.remove("playing");
      });
      panel.appendChild(el);
    });
    panels.appendChild(panel);
    tab.addEventListener("click", () => select(i, false));
    return [tab, panel];
  });
  function select(i, focus) {
    tabEls.forEach(([tab, panel], j) => {
      const on = i === j;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      panel.hidden = !on;
      if (on) $$(".cmd", panel).forEach((c) => { c.style.animation = "none"; void c.offsetWidth; c.style.animation = ""; });
      if (on && focus) tab.focus();
    });
  }
  tabs.addEventListener("keydown", (e) => {
    const i = tabEls.findIndex(([t]) => t.getAttribute("aria-selected") === "true");
    if (e.key === "ArrowRight") { e.preventDefault(); select((i + 1) % AREAS.length, true); }
    if (e.key === "ArrowLeft") { e.preventDefault(); select((i - 1 + AREAS.length) % AREAS.length, true); }
  });
  select(0, false);

  /* ================================================================ FAQ schema from the page itself */
  $("#faq-schema").textContent = JSON.stringify({
    "@context": "https://schema.org", "@type": "FAQPage",
    mainEntity: $$("#faq-list details").map((d) => ({
      "@type": "Question", name: $("summary", d).textContent.trim(),
      acceptedAnswer: { "@type": "Answer", text: $(".a", d).textContent.trim() },
    })),
  });
})();
