/* Jarvis landing: navigation, reveals, the talking console, the tagline, command tabs and the small card demos. */
(() => {
  "use strict";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const voice = (name) => `assets/voice/${name}.mp3`;

  /* ---------------------------------------------------------------- navigation */
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

  // the section in view is marked in the nav
  const navLinks = $$(".nav-links a");
  const spy = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      navLinks.forEach((a) => a.setAttribute("aria-current", String(a.getAttribute("href") === `#${en.target.id}`)));
    });
  }, { rootMargin: "-45% 0px -50% 0px" });
  ["top", "features", "commands", "install", "compare", "faq"].forEach((id) => { const el = document.getElementById(id); if (el) spy.observe(el); });

  /* ---------------------------------------------------------------- scroll reveals (once) */
  const revealer = new IntersectionObserver((entries) => {
    entries.forEach((en) => { if (en.isIntersecting) { en.target.classList.add("in"); revealer.unobserve(en.target); } });
  }, { rootMargin: "0px 0px -10% 0px" });
  $$(".reveal").forEach((el, i) => {
    const siblings = el.parentElement ? $$(":scope > .reveal", el.parentElement) : [];
    const idx = siblings.indexOf(el);
    if (idx > 0) el.style.transitionDelay = `${Math.min(idx * 80, 320)}ms`;
    revealer.observe(el);
  });

  /* ---------------------------------------------------------------- audio */
  let audioCtx = null;
  let analyser = null;
  const level = new Float32Array(1024);
  const sources = new WeakSet();
  let current = null; // the <audio> speaking now

  function ensureAudio() {
    if (audioCtx) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    audioCtx = new Ctx();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.connect(audioCtx.destination);
  }
  function stopVoice() {
    if (current) { current.pause(); current.dispatchEvent(new Event("ended")); current = null; }
  }
  // plays a reply; resolves when it ends (or right away if it cannot play)
  function playVoice(name) {
    stopVoice();
    ensureAudio();
    const audio = new Audio(voice(name));
    try {
      if (audioCtx && !sources.has(audio)) { audioCtx.createMediaElementSource(audio).connect(analyser); sources.add(audio); }
      if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    } catch (_) { /* the page is opened as a file: plays without the meter */ }
    current = audio;
    return new Promise((resolve) => {
      audio.addEventListener("ended", () => { if (current === audio) current = null; resolve(); }, { once: true });
      audio.addEventListener("error", () => { if (current === audio) current = null; resolve(); }, { once: true });
      audio.play().catch(() => { if (current === audio) current = null; resolve(); });
    });
  }
  function voiceLevel() {
    if (!analyser || !current) return 0;
    analyser.getFloatTimeDomainData(level);
    let sum = 0;
    for (let i = 0; i < level.length; i++) sum += level[i] * level[i];
    return Math.min(1, Math.sqrt(sum / level.length) * 4.5);
  }

  /* ---------------------------------------------------------------- the orb */
  const COLORS = {
    idle: ["#2AA8E0", "#8FDCFF"],
    listen: ["#2AA8E0", "#8FDCFF"],
    think: ["#F5C542", "#FFE29A"],
    speak: ["#2AA8E0", "#8FDCFF"],
  };
  const orb = { state: "idle", amp: 0.04, spin: 0, typing: 0 };
  const canvas = $("#orb");
  const g = canvas.getContext("2d");
  function fitCanvas() {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const size = canvas.clientWidth || 280;
    canvas.width = size * dpr; canvas.height = size * dpr;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  fitCanvas();
  addEventListener("resize", fitCanvas);

  let orbVisible = true;
  new IntersectionObserver(([en]) => { orbVisible = en.isIntersecting; }).observe(canvas);

  function drawOrb(t) {
    requestAnimationFrame(drawOrb);
    if (!orbVisible) return;
    const size = canvas.clientWidth || 280;
    const c = size / 2;
    const k = size / 280;
    const [main, light] = COLORS[orb.state];
    const time = t / 1000;

    // target loudness by state
    let target = 0.05 + 0.03 * Math.sin(time * 1.6);
    if (orb.state === "listen") target = 0.16 + orb.typing * (0.25 + 0.2 * Math.abs(Math.sin(time * 11)));
    if (orb.state === "think") target = 0.18;
    if (orb.state === "speak") {
      const v = voiceLevel();
      target = v > 0 ? v : 0.3 + 0.35 * Math.abs(Math.sin(time * 12.7) * Math.sin(time * 3.3));
    }
    orb.amp += (target - orb.amp) * 0.18;
    if (!reduced) orb.spin += orb.state === "think" ? 0.035 : 0.004 + orb.amp * 0.01;

    g.clearRect(0, 0, size, size);
    g.save();
    g.translate(c, c);
    g.scale(k, k);

    // radial ticks: the voice meter
    const ticks = 120;
    for (let i = 0; i < ticks; i++) {
      const a = (i / ticks) * Math.PI * 2;
      const n = 0.5 + 0.5 * Math.sin(i * 1.7 + time * 5) * Math.sin(i * 0.43 - time * 2.3);
      const len = 3 + orb.amp * 26 * n;
      g.strokeStyle = light;
      g.globalAlpha = 0.18 + orb.amp * 0.6 * n;
      g.lineWidth = 2;
      g.beginPath();
      g.moveTo(Math.cos(a) * 118, Math.sin(a) * 118);
      g.lineTo(Math.cos(a) * (118 + len), Math.sin(a) * (118 + len));
      g.stroke();
    }

    g.globalAlpha = 0.55;
    g.strokeStyle = main;
    g.lineWidth = 2;
    g.beginPath(); g.arc(0, 0, 110, 0, Math.PI * 2); g.stroke();

    // the segmented ring of the reactor
    g.save();
    g.rotate(orb.spin);
    g.globalAlpha = 0.9;
    g.lineWidth = 16;
    g.setLineDash([17, 6.5]);
    g.beginPath(); g.arc(0, 0, 90, 0.2, Math.PI * 2 - 0.2); g.stroke();
    g.setLineDash([]);
    g.restore();

    g.globalAlpha = 1;
    g.strokeStyle = light;
    g.lineWidth = 2.5;
    g.beginPath(); g.arc(0, 0, 74, 0, Math.PI * 2); g.stroke();

    // the core glows with the voice
    g.shadowColor = light;
    g.shadowBlur = 12 + orb.amp * 40;
    g.fillStyle = main;
    g.globalAlpha = 0.35 + orb.amp * 0.5;
    g.beginPath(); g.arc(0, 0, 68, 0, Math.PI * 2); g.fill();
    g.shadowBlur = 0;

    // the triangle from the tray icon
    g.globalAlpha = 1;
    g.lineJoin = "round"; g.lineCap = "round";
    g.fillStyle = "#04101C";
    g.strokeStyle = light;
    g.lineWidth = 3;
    const s = 1.85;
    g.beginPath(); g.moveTo(-34 * s, -19 * s); g.lineTo(34 * s, -19 * s); g.lineTo(0, 36 * s); g.closePath(); g.fill(); g.stroke();
    g.beginPath(); g.moveTo(-20 * s, -11 * s); g.lineTo(20 * s, -11 * s); g.lineTo(0, 22 * s); g.closePath(); g.stroke();
    g.beginPath(); g.moveTo(-20 * s, -11 * s); g.lineTo(0, 1 * s); g.lineTo(20 * s, -11 * s); g.moveTo(0, 1 * s); g.lineTo(0, 22 * s); g.stroke();
    g.restore();
  }
  requestAnimationFrame(drawOrb);

  /* ---------------------------------------------------------------- the console */
  const SCENES = [
    { chip: "Открой телеграм", said: "Джарвис, открой телеграм", reply: "Открываю Телеграм.", done: "Telegram на переднем плане", voice: "telegram" },
    { chip: "Напомни про плиту", said: "Джарвис, напомни через 15 минут выключить плиту", reply: "Хорошо. Через пятнадцать минут напомню выключить плиту.", done: "Таймер 15:00 · выключить плиту", voice: "timer" },
    { chip: "Спокойная музыка", said: "Джарвис, включи спокойную музыку", reply: "Включаю спокойную подборку. Приятного вечера, сэр.", done: "Играет плейлист «Спокойное»", voice: "music" },
    { chip: "Будет ли дождь", said: "Джарвис, когда будет следующий дождь?", reply: "Дождь начнётся завтра около четырёх часов дня. Зонт лучше взять.", done: "Прогноз по часам на карточке", voice: "rain" },
    { chip: "Реши задачу", said: "Джарвис, реши задачу с экрана", reply: "Готово. Решение и ответ на экране.", done: "Окна «Решение» и «Ответ» закреплены", voice: "solve" },
    { chip: "Ты здесь?", said: "Джарвис, ты здесь?", reply: "Добрый вечер, сэр. Все системы в норме. Чем могу помочь?", done: "Готов к работе", voice: "hello" },
  ];
  const stateEl = $(".state");
  const stateText = $("#state-text");
  const said = $("#said");
  const reply = $("#reply");
  const steps = $$("#steps li");
  const done = $("#done");
  const soundBtn = $("#sound");
  const picker = $(".picker");
  let soundOn = false;
  let run = 0;          // a new scene cancels the one in progress
  let sceneIdx = 0;
  let consoleVisible = false;

  const chips = SCENES.map((sc, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip"; b.textContent = sc.chip;
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => play(i, true));
    picker.appendChild(b);
    return b;
  });

  const STATES = { idle: "Жду «Джарвис»", listen: "Слушаю", think: "Думаю", speak: "Отвечаю" };
  function setState(s) { orb.state = s; stateEl.dataset.s = s; stateText.textContent = STATES[s]; }

  async function type(el, text, perChar, id) {
    el.innerHTML = '<span class="t"></span><span class="caret"></span>';
    const t = el.firstChild;
    for (let i = 1; i <= text.length; i++) {
      if (id !== run) return false;
      t.textContent = text.slice(0, i);
      await sleep(perChar);
    }
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

    setState("listen");
    steps[0].classList.add("on");
    orb.typing = 1;
    if (!(await type(said, sc.said, 38, id))) return;
    orb.typing = 0;
    steps[1].classList.add("on");
    setState("think");
    await sleep(520);
    if (id !== run) return;
    steps[2].classList.add("on");
    setState("speak");
    const speaking = soundOn ? playVoice(sc.voice) : sleep(Math.max(1400, sc.reply.length * 52));
    const typed = type(reply, sc.reply, 26, id);
    await Promise.all([speaking, typed]);
    if (id !== run) return;
    steps[3].classList.add("on");
    done.lastElementChild.textContent = sc.done;
    done.classList.add("on");
    setState("idle");
    await sleep(byUser ? 4200 : 3000);
    if (id !== run) return;
    loop();
  }
  function loop() {
    if (!consoleVisible || document.hidden) { run++; return; }
    play((sceneIdx + 1) % SCENES.length);
  }

  new IntersectionObserver(([en]) => {
    const was = consoleVisible;
    consoleVisible = en.isIntersecting;
    if (consoleVisible && !was) play(sceneIdx);
    if (!consoleVisible) { run++; stopVoice(); setState("idle"); }
  }, { threshold: 0.35 }).observe($("#console"));
  document.addEventListener("visibilitychange", () => { if (document.hidden) { run++; stopVoice(); } else if (consoleVisible) play(sceneIdx); });

  soundBtn.addEventListener("click", () => {
    soundOn = !soundOn;
    soundBtn.setAttribute("aria-pressed", String(soundOn));
    soundBtn.innerHTML = soundOn ? '<i class="ph ph-speaker-high"></i><span>Голос включён</span>' : '<i class="ph ph-speaker-slash"></i><span>Включить голос</span>';
    if (soundOn) { ensureAudio(); play(SCENES.length - 1, true); } else stopVoice();
  });

  /* ---------------------------------------------------------------- tagline: words light up one by one */
  const tagline = $("#tagline");
  $$(".l", tagline).forEach((line) => {
    const accent = line.hasAttribute("data-accent");
    line.innerHTML = line.textContent.split(" ").map((w) => `<span class="w${accent ? " accent" : ""}">${w}</span>`).join(" ");
  });
  const words = $$(".w", tagline);
  let lit = 0;
  let ticking = false;
  function lightWords() {
    ticking = false;
    const line = innerHeight * 0.62;
    while (lit < words.length && words[lit].getBoundingClientRect().top < line) words[lit++].classList.add("on");
    if (lit >= words.length) removeEventListener("scroll", onScroll);
  }
  function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(lightWords); } }
  addEventListener("scroll", onScroll, { passive: true });
  lightWords();

  /* ---------------------------------------------------------------- card demos, only while visible */
  function whenVisible(el, start, stop) {
    new IntersectionObserver(([en]) => (en.isIntersecting ? start() : stop && stop()), { threshold: 0.3 }).observe(el);
  }

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
        await sleep(1100); // the phrase is spoken, then typed at once
        for (let i = 1; i <= p.length; i++) { if (id !== dictRun) return; t.textContent += p[i - 1]; await sleep(14); }
      }
      await sleep(3200);
    }
  }
  whenVisible($("#dict-viz"), dictate, () => dictRun++);

  const agentItems = $$("#agent-viz li");
  const confirmEl = $("#agent-viz .confirm");
  let agentRun = 0;
  async function agent() {
    const id = ++agentRun;
    while (id === agentRun) {
      agentItems.forEach((li) => li.classList.remove("on"));
      confirmEl.classList.remove("on");
      for (const li of agentItems) { await sleep(800); if (id !== agentRun) return; li.classList.add("on"); }
      await sleep(600); if (id !== agentRun) return;
      confirmEl.classList.add("on");
      await sleep(3600);
    }
  }
  whenVisible($("#agent-viz"), agent, () => agentRun++);

  /* ---------------------------------------------------------------- commands */
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
  const tabs = $("#tabs");
  const panels = $("#panels");
  const tabEls = AREAS.map((area, i) => {
    const tab = document.createElement("button");
    tab.type = "button"; tab.className = "tab"; tab.id = `tab-${area.id}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", `panel-${area.id}`);
    tab.innerHTML = `<i class="ph ${area.icon}"></i>${area.title}`;
    tabs.appendChild(tab);

    const panel = document.createElement("div");
    panel.className = "panel"; panel.id = `panel-${area.id}`;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    panel.hidden = true;
    area.items.forEach(([phrase, result, sample], j) => {
      const el = document.createElement(sample ? "button" : "div");
      if (sample) { el.type = "button"; el.setAttribute("aria-label", `${phrase}. Послушать ответ`); }
      el.className = "cmd";
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
      if (on) {
        // replay the entrance of the cards
        panel.hidden = false;
        $$(".cmd", panel).forEach((c) => { c.style.animation = "none"; void c.offsetWidth; c.style.animation = ""; });
      } else panel.hidden = true;
      if (on && focus) tab.focus();
    });
  }
  tabs.addEventListener("keydown", (e) => {
    const i = tabEls.findIndex(([t]) => t.getAttribute("aria-selected") === "true");
    if (e.key === "ArrowRight") { e.preventDefault(); select((i + 1) % AREAS.length, true); }
    if (e.key === "ArrowLeft") { e.preventDefault(); select((i - 1 + AREAS.length) % AREAS.length, true); }
  });
  select(0, false);

  /* ---------------------------------------------------------------- FAQ schema from the page itself */
  const faqLd = {
    "@context": "https://schema.org", "@type": "FAQPage",
    mainEntity: $$("#faq-list details").map((d) => ({
      "@type": "Question", name: $("summary", d).textContent.trim(),
      acceptedAnswer: { "@type": "Answer", text: $(".a", d).textContent.trim() },
    })),
  };
  $("#faq-schema").textContent = JSON.stringify(faqLd);
})();
