# Навыки-папки

Навык — папка с манифестом, фразами, промптами и кодом. Встроенные лежат в `assistant/skills/<id>/`,
свои и установленные — в `data/skills/<id>/` (обновление Джарвиса их не трогает). Джарвис находит папку по
`skill.toml` при запуске. Старые модули одним файлом (`assistant/skills/<имя>.py`) работают как раньше и
переезжают в папки постепенно. Уже в папках: `solve`, `focus`, `weather`.

```
data/skills/hello/
  skill.toml          манифест: кто, что умеет, разрешения, настройки
  commands.toml       фразы → действия, примеры для маршрутизатора, готовые ответы
  main.py             код (необязателен)
  prompts/hello.md    промпты для моделей (необязательны)
  tests/phrases.toml  какие фразы что должны запускать — проверяется тестами
```

## skill.toml

```toml
[skill]
id = "hello"                 # = имя папки: латиница, цифры, _
name = "Привет"
version = "1.0.0"
author = "Вы"
description = "Здоровается и прощается."
api = 1                      # версия API Джарвиса, под которую написан навык
entry = "main:create"        # модуль:функция; "" — навык без кода, только ответы из commands.toml
before = ["calc"]            # проверять фразы раньше этих навыков (или after = [...]); иначе — в конце
requires = []                # другие навыки, с которыми он работает (предупреждение, если их нет)
permissions = []             # screen, clipboard, cloud_llm, local_llm, browser, files, power, keyboard, network, microphone

[config]                     # настройки по умолчанию; значения пользователя — data/settings.json → "hello"
greeting = "Здравствуйте"

[[settings]]                 # поле на странице настроек (вкладка «Модули»)
key = "greeting"             # имя из [config]
label = "Приветствие"
type = "text"                # text | number | bool | select | list | time …; для number — min, max, step
hint = ""
```

## commands.toml

```toml
examples = ["поздоровайся"]  # канонические команды: в них маршрутизатор превращает свободные фразы

[[command]]                  # проверяются по порядку, побеждает первая совпавшая
action = "hi"
patterns = ['^поздоровайся']  # регулярки по нормализованной фразе: строчные, без пунктуации, «ё» → «е», без «Джарвис»

[[command]]
action = "bye"
patterns = ['^попрощайся']
reply = "До свидания."       # готовый ответ: такой команде код не нужен
```

Если фразы сложно описать регулярками (город и день в погоде, источник задачи в `solve`), навык
переопределяет `match()` в коде, а в `commands.toml` оставляет только `examples`.

## main.py

```python
from assistant.skills.base import Reply, Skill


class Hello(Skill):
    async def handle(self, intent):
        if intent.action == "hi":
            return Reply(self.ctx.config.greeting + ". " + self.ctx.prompt("hello"))
        return await super().handle(intent)   # команды с готовым reply


def create():
    return Hello()
```

## API навыка: `self.ctx` (версия 1)

Навык обращается к Джарвису только через `self.ctx`. Внутренности Джарвиса меняются, а `ctx` остаётся
совместимым в пределах версии `api`.

| Что | Зачем |
|---|---|
| `ctx.config.<имя>` | свои настройки; страница настроек меняет их сразу |
| `ctx.settings` | общие настройки только для чтения: `ctx.settings.assistant.default_city` |
| `ctx.prompt("имя")` | текст `prompts/имя.md` |
| `ctx.data_dir` | своя папка для файлов: `data/skill_data/<id>/` |
| `ctx.card(title, text, pinned=True)` | окно с текстом; закреплённые стоят рядом, пока не закрыть ✕ |
| `ctx.notify(title, text)` | уведомление в трее |
| `await ctx.select_region()` | пользователь выделяет область экрана → PNG или None |
| `await ctx.say(text)` | сказать самому (таймеры, фоновые задачи) |
| `await ctx.ask(messages, chain=[...])` | ответ модели целиком; `chain` — `gemini`, `groq`, `local` |
| `ctx.stream(messages, chain=[...])` | ответ по мере генерации → `Reply(stream=...)` |
| `ctx.last_provider` | кто ответил: gemini, groq, local |
| `await ctx.selection()` | выделенный текст в активном окне |
| `await ctx.set_clipboard(text)` | положить текст в буфер обмена |
| `ctx.share(key, value)` / `ctx.recent(key, сек)` | передать значение другим навыкам: `screen.region` — последняя выделенная область |
| `ctx.other("id")` | другой навык (укажите его в `requires`) |

`handle()` возвращает `Reply`: `speech` (сказать), `stream` (говорить по мере ответа), `listen_after`,
`confirm` (опасное действие после «да»), `undo` («верни как было») — см. `assistant/skills/base.py`.

## tests/phrases.toml

```toml
phrase = [
    { text = "поздоровайся", action = "hi" },
    { text = "реши 2 плюс 2", skill = "calc", action = "calc" },   # фраза должна достаться другому навыку
    { text = "реши капчу", skill = "none", action = "none" },      # никому
]
```

`tests/test_skillpack.py` прогоняет фразы всех навыков со всеми навыками в настоящем порядке: если новый навык
перехватит чужие команды, тест это покажет.

## Включение и выключение

`[skills] disabled = ["hello"]` в настройках — папка найдена, но не загружается. Навык с ошибкой в манифесте
пропускается с записью в журнале, остальные работают. Свой навык не может подменить встроенный с тем же `id`.

## Безопасность

Код навыка на Python может всё, что может сам Джарвис: разрешения в манифесте пока только описывают, что навык
делает. Ставьте навыки с кодом только из источников, которым доверяете. Навыки без кода (только
`commands.toml` с готовыми ответами) безопасны.
