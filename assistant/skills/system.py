"""System commands: stop, time/date, repeat, thanks, microphone, help, exit, jokes."""
from __future__ import annotations

import datetime as dt
import re

from assistant.skills.base import Intent, Reply, Skill

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"]
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("stop", re.compile(
        r"^(стоп|хватит|замолчи|замолкни|молчи|помолчи|умолкни|тихо|тишина|отмена|отмени|отбой|отставить|ничего|не надо|"
        r"не нужно|все|всё|довольно|достаточно|стой|подожди|погоди|заткнись|ладно хватит|все хватит|стоп стоп|"
        r"можешь замолчать|перестань|прекрати|забей|проехали|неважно|ладно неважно)$")),
    ("thanks", re.compile(
        r"^(спасибо|благодарю|спс|пасиб\w*|спасибочки|сенкс|мерси|молодец|красавчик|отлично|супер|круто|класс|"
        r"ты лучший|ты молодец|хорошая работа|отличная работа|то что нужно|идеально)( (большое|огромное|тебе|тебе большое|джарвис|сэр|друг))*$|"
        r"^(понял|хорошо|ладно|отлично|окей|ок|супер) спасибо$")),
    ("insult", re.compile(
        r"\b(ты )?(тупой|тупица|дурак|идиот|дебил|кретин|бестолочь|бесполезный|глупый|болван|придурок|туп\w*ая машина)\b|"
        r"^ты (ничего )?не (умеешь|понимаешь)\b")),
    ("joke", re.compile(
        r"^(пошути|пошутишь|шутку|анекдот|рассмеши( меня)?|расскажи (шутку|анекдот|что-нибудь смешное|что нибудь смешное)|"
        r"скажи что-нибудь смешное|давай шутку|есть шутка|знаешь шутку|шутка)$")),
    ("time", re.compile(
        r"(который (сейчас )?час|сколько (сейчас )?(времени|время)|какое (сейчас )?время|сколько на часах|"
        r"^(скажи|подскажи|назови) (мне )?время|^время$|^что по времени|^часы$|^сколько время$)")),
    ("date", re.compile(
        r"(какое (сегодня |сейчас )?число|какой (сегодня |сейчас )?день( недели)?|какая (сегодня )?дата|"
        r"^сегодняшн\w+ (дата|число)|^(скажи|подскажи|назови) (мне )?(дату|число)|^дата$|какой сегодня месяц|"
        r"какое сегодня|число сегодня|день недели)")),
    ("repeat", re.compile(
        r"^(повтори|повтори что сказал|скажи еще раз|скажи снова|что ты сказал|что-что|не расслышал|не услышал|"
        r"не понял повтори|прослушал|повторить|повтори ответ|что ты ответил)$")),
    # Redo the previous command ("громче" -> "еще раз" = louder again).
    ("again", re.compile(
        r"^(еще раз|еще|снова|опять|сделай еще раз|давай еще|давай еще раз|еще разок|повтори команду|повтори действие|"
        r"и еще|и еще раз|а еще раз|можно еще раз|сделай так еще раз|то же самое|еще столько же|повтори еще раз)$")),
    ("unmute_mic", re.compile(r"(включи|верни) (микрофон|прослушку)|^слушай меня$")),
    ("mute", re.compile(
        r"(выключи|отключи|выруби) (микрофон|прослушку)|не слушай|перестань (меня )?слушать|^не подслушивай|"
        r"режим тишины|отключи уши|не мешай( мне)?$")),
    ("help", re.compile(
        r"что ты (умеешь|можешь)|что (еще )?умеешь|твои (возможности|команды|функции)|^(помощь|справка)$|"
        r"какие (есть |у тебя )?команды|список команд|чем (ты )?(можешь|можете) помочь|как тобой пользоваться")),
    ("exit", re.compile(
        r"^(выключись|отключись|заверши работу|завершить работу|выключи себя|закройся|выход|выйди|уйди|"
        r"закрой джарвиса|выключи джарвиса|выход из программы)$")),
]


class SystemSkill(Skill):
    name = "system"
    title = "Системные команды"
    examples = [
        'стоп',
        'который час',
        'какое сегодня число',
        'повтори (сказанное)',
        'еще раз (повторить прошлую команду)',
        'спасибо',
        'пошути',
        'выключи микрофон',
        'что ты умеешь',
        'выключись',
    ]

    def match(self, text: str) -> Intent | None:
        for action, pattern in _PATTERNS:
            if pattern.search(text):
                if action == "joke" and not (self.app.pack and self.app.pack.has("joke")):
                    return None  # no recorded jokes: let the model joke
                return Intent(self.name, action, text=text)
        return None

    async def handle(self, intent: Intent) -> Reply:
        now = dt.datetime.now()
        address = self.app.cfg.assistant.address
        match intent.action:
            case "stop":
                self.app.interrupt()
                return Reply(listen_after=False)
            case "thanks":
                return Reply(f"Всегда к вашим услугам, {address}.", reaction="thanks", listen_after=False)
            case "insult":
                return Reply(f"Очень тонкое замечание, {address}.", reaction="stupid", listen_after=False)
            case "joke":
                return Reply("Шутки кончились.", reaction="joke")
            case "time":
                return Reply(f"Сейчас {now.hour}:{now.minute:02d}.", listen_after=False)
            case "date":
                return Reply(f"Сегодня {_WEEKDAYS[now.weekday()]}, {now.day} {_MONTHS[now.month - 1]}.", listen_after=False)
            case "repeat":
                last = self.app.speaker.last_text
                return Reply(last or "Я пока ничего не говорил.")
            case "again":
                return await self.app.brain.redo()
            case "mute":
                self.app.set_muted(True)
                return Reply("Выключаю микрофон. Включить можно в трее или сочетанием Ctrl Alt M.", listen_after=False)
            case "unmute_mic":
                self.app.set_muted(False)
                return Reply("Слушаю.", reaction="reply", listen_after=False)
            case "help":
                return Reply("Коротко: отвечаю на вопросы с поиском в интернете, открываю и закрываю программы, "
                             "управляю окнами, звуком и музыкой, ставлю таймеры и помодоро, веду заметки и списки, "
                             "помню факты о вас, диктую текст, работаю с выделенным текстом и экраном, "
                             "говорю погоду и расписание. Полный список — в файле MODULES.")
            case "exit":
                self.app.request_exit()
                return Reply(spoken=True, listen_after=False)
        return Reply()


def create() -> Skill:
    return SystemSkill()
