"""What the settings page shows and edits. Values live in config/default.toml + data/settings.json."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class F:
    key: str                 # dotted path, e.g. "music.dir" or "llm.daily_limits.groq"
    label: str
    type: str                # text | number | range | bool | select | folder | folders | list | time | device_in | device_out | ollama | chain | hotkey
    hint: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[list[str]] = field(default_factory=list)   # [[value, label], ...]
    restart: bool = False    # takes effect after restarting Jarvis


SECTIONS: list[tuple[str, list[F]]] = [
    ("Основное", [
        F("assistant.default_city", "Город по умолчанию", "text", "для погоды и сводки"),
        F("assistant.address", "Как Джарвис обращается к вам", "text", "например: сэр"),
        F("assistant.hot_window_sec", "Горячее окно после ответа, с", "number", "можно продолжать без «Джарвис»", 0, 15, 0.5),
        F("tts.volume", "Громкость голоса", "range", "", 0.2, 1.5, 0.05),
        F("audio.earcons", "Короткие звуковые сигналы", "bool"),
        F("audio.earcon_volume", "Громкость сигналов", "range", "", 0.0, 1.0, 0.05),
        F("ui.card_threshold_chars", "Показывать ответ в окне, если длиннее (символов)", "number", "", 100, 2000, 20),
        F("ui.autostart", "Запускать вместе с Windows", "bool", "ярлык в папке «Автозагрузка»"),
        F("ui.hud", "Индикатор Джарвиса на экране", "bool", "анимированный круг: слушаю / думаю / говорю", restart=True),
        F("ui.check_updates", "Проверять обновления", "bool", "раз в день, только уведомление"),
        F("privacy.log_phrases", "Записывать фразы в журнал", "select", "журнал — data/logs; фразы нужны только для отладки",
          options=[["auto", "Только при запуске с консолью"], ["on", "Всегда"], ["off", "Никогда"]]),
    ]),
    ("Папки", [
        F("music.dir", "Папка с музыкой", "folder", "«включи <трек>», «сколько треков»"),
        F("notes.dir", "Папка заметок и списков", "folder", "можно указать хранилище Obsidian"),
        F("files.allowed_dirs", "Папки, которые Джарвис может читать", "folders",
          "считать, искать, показывать файлы. Изменять и удалять он не может нигде"),
        F("files.allow_cmd", "Разрешить модели команды cmd только для чтения", "bool",
          "dir, tree, type, findstr, where, ipconfig, systeminfo, tasklist, netstat, ping"),
    ]),
    ("Микрофон и звук", [
        F("wake.phrases", "Кодовое слово (и его варианты)", "list",
          "русские слова из словаря Vosk; первое — основное", restart=True),
        F("wake.verify_ratio", "Строгость проверки кодового слова", "range", "выше — меньше ложных срабатываний",
          55, 95, 1),
        F("wake.owner_only", "Реагировать только на мой голос", "bool", "сначала скажите «Джарвис, запомни мой голос»"),
        F("wake.owner_threshold", "Строгость узнавания голоса", "range", "выше — чужие голоса отсекаются надёжнее",
          0.3, 0.8, 0.05),
        F("audio.input_device", "Микрофон", "device_in", restart=True),
        F("audio.output_device", "Динамики", "device_out", restart=True),
        F("audio.vad_threshold", "Порог распознавания речи", "range", "выше — меньше реагирует на шум", 0.2, 0.9, 0.05, restart=True),
        F("audio.end_silence_ms", "Пауза, которая завершает фразу, мс", "number", "меньше — быстрее ответ, больше — не обрывает на паузах", 500, 2500, 50, restart=True),
        F("audio.wake_grace_ms", "Сколько ждать начала вопроса после «Джарвис», мс", "number", "", 800, 4000, 100, restart=True),
        F("audio.await_command_sec", "Сколько ждать команду после «Джарвис», с", "number", "", 2, 15, 0.5),
        F("audio.echo_cancellation", "Эхоподавление", "bool",
          "микрофон не слышит самого Джарвиса: его можно перебить «Джарвис, …» посреди ответа", restart=True),
        F("audio.talk_over_volume", "Громкость Джарвиса, когда его перебивают", "range",
          "пока вы говорите поверх ответа (1 — не убавлять)", 0.0, 1.0, 0.05),
        F("music.volume", "Громкость музыки", "range", "", 0.05, 1.0, 0.05),
        F("music.duck_volume", "Громкость музыки, пока Джарвис слушает или говорит", "range", "доля от обычной", 0.0, 1.0, 0.05),
    ]),
    ("Голос", [
        F("tts.voice", "Голос ответов", "select", "клоны Джарвиса требуют видеокарту", options=[]),
        F("tts.rate", "Скорость речи", "range", "", 0.7, 1.4, 0.05),
        F("voice.pack", "Записанные реплики", "select", options=[
            ["jarvis-remaster", "Jarvis Remaster"], ["jarvis-howdy", "Jarvis Howdy"], ["jarvis-og", "Jarvis OG"], ["none", "Без реплик"]]),
        F("voice.greet_on_start", "Приветствие при запуске", "bool"),
        F("voice.reply_on_bare_wake", "«Слушаю, сэр» на одно слово «Джарвис»", "bool"),
        F("voice.ok_for_actions", "«Слушаюсь, сэр» вместо описания простых действий", "bool"),
    ]),
    ("ИИ", [
        F("llm.info_chain", "Кто отвечает на вопросы о свежих фактах (по порядку)", "chain",
          "первый доступный отвечает; остальные — запасные", options=[["groq", "Groq (поиск)"], ["gemini", "Gemini"], ["local", "Локальная"]]),
        F("llm.local.model", "Локальная модель", "ollama"),
        F("llm.local.vision_model", "Модель для экрана (со зрением)", "ollama"),
        F("router.enabled", "Понимать свободные формулировки", "bool", "модель превращает непредусмотренную фразу в команду"),
        F("router.model", "Отдельная маленькая модель для разбора фраз", "ollama",
          "быстрее основной (~0,4 с вместо ~1 с); пусто — основная модель"),
        F("router.provider", "Кто разбирает свободные фразы", "select", "локальная не тратит токены",
          options=[["auto", "Локальная, если запущена, иначе облако"], ["local", "Только локальная"], ["cloud", "Только облако"]]),
        F("llm.daily_limits.groq", "Лимит запросов к Groq в день", "number", "бесплатно ~1000", 0, 14400, 10),
        F("llm.daily_limits.gemini", "Лимит запросов к Gemini в день", "number", "", 0, 1500, 10),
        F("router.cache_fresh_min", "Помнить ответы о свежих данных, мин", "number", "повторный вопрос не тратит токены", 0, 240, 5),
        F("router.cache_stable_days", "Помнить ответы о неизменных фактах, дней", "number", "", 0, 90, 1),
        F("llm.cloud_history_turns", "Сколько прошлых реплик отправлять в облако", "number", "каждая стоит токенов", 0, 6, 1),
    ]),
    ("Модули", [
        F("search.default", "Поисковик для «найди в браузере»", "select", options=[["yandex", "Яндекс"], ["google", "Google"]]),
        F("calendar.ics_urls", "Ссылки на календари (iCal)", "list", "Google Календарь → Настройки → Закрытый адрес в формате iCal"),
        F("briefing.auto_time", "Утренняя сводка автоматически в", "time", "пусто — только по просьбе", restart=True),
        F("briefing.news", "Новости города в сводке", "bool"),
        F("news.spoken", "Сколько заголовков читать вслух", "number", "остальные — на карточке", 1, 8, 1),
        F("focus.work_min", "Помодоро: работа, мин", "number", "", 5, 120, 5),
        F("focus.break_min", "Помодоро: перерыв, мин", "number", "", 1, 30, 1),
        F("focus.rounds", "Помодоро: раундов", "number", "", 1, 8, 1),
        F("explain.provider", "Кто готовит карточки объяснений", "select", "облако быстрее и точнее, но тратит ~2,5 тыс. токенов",
          options=[["local", "Локальная модель"], ["cloud", "Облако (Groq / Gemini), запасная — локальная"]]),
        F("explain.max_cards", "Карточек в объяснении, до", "number", "", 4, 8, 1),
        F("explain.linger_sec", "Окно объяснения закрывается через, с", "number", "", 5, 120, 5),
    ]),
    ("Браузер", [
        F("web.enabled", "Браузерный агент", "bool",
          "«найди на озоне…», «перескажи эту статью»; нужно расширение — «Джарвис, подключи браузер»", restart=True),
        F("web.window", "Окно агента", "select", "где агент открывает сайты",
          options=[["minimized", "Свёрнутое — не мешает"], ["background", "Обычное, позади ваших окон"]]),
        F("web.max_steps", "Шагов на задачу, до", "number", "", 5, 40, 1),
        F("web.timeout_sec", "Время на задачу, с", "number", "", 60, 600, 30),
        F("web.handover_wait_sec", "Ждать вас на капче или входе, с", "number", "", 30, 600, 30),
    ]),
    ("Клавиши", [
        F("ui.hotkey_listen", "Слушать без «Джарвис»", "hotkey", restart=True),
        F("ui.hotkey_mute", "Микрофон вкл/выкл", "hotkey", restart=True),
        F("ui.hotkey_dictation", "Диктовка (удерживать)", "hotkey", restart=True),
        F("ui.hotkey_screen", "Помощь с экраном", "hotkey", restart=True),
    ]),
]

RESTART_KEYS = {f.key for _, fields in SECTIONS for f in fields if f.restart}


def schema(voice_options: list[list[str]]) -> list[dict[str, Any]]:
    out = []
    for title, fields in SECTIONS:
        items = []
        for f in fields:
            d = asdict(f)
            if f.key == "tts.voice":
                d["options"] = voice_options
            items.append(d)
        out.append({"title": title, "fields": items})
    return out


def get_path(data: dict, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node
