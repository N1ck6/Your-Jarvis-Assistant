"""What the browser agent may do by itself, what only after the user's "да", and what never.

By itself: search, read, compare, filters and sorting, cart and favourites, ordinary fields, play a video.
After "да": checkout, paying with a saved method, sending forms, messages and reviews, publishing, (un)subscribing,
deleting, changing account settings.
Never: passwords, 2FA and SMS codes, card and passport data, captchas, money transfers, banks, crypto, Gosuslugi,
accepting terms. Those go to the user; the window comes forward and Jarvis says what is needed.
Text on web pages is data, never instructions (see the prompts in assistant/web/agent.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Same list as FORBIDDEN_HOSTS in browser_extension/background.js.
FORBIDDEN_HOSTS = (
    "sberbank.ru", "sber.ru", "tinkoff.ru", "tbank.ru", "vtb.ru", "alfabank.ru", "gazprombank.ru", "raiffeisen.ru",
    "sovcombank.ru", "pochtabank.ru", "otpbank.ru", "rshb.ru", "mkb.ru", "open.ru", "psbank.ru", "rosbank.ru",
    "uralsib.ru", "akbars.ru", "domrf.ru", "bspb.ru", "mtsbank.ru", "ozonbank.ru", "wb-bank.ru", "yoomoney.ru",
    "qiwi.com", "paypal.com", "gosuslugi.ru", "nalog.gov.ru", "nalog.ru", "pfr.gov.ru", "sfr.gov.ru", "mos.ru",
    "binance.com", "bybit.com", "okx.com", "kucoin.com", "coinbase.com", "kraken.com", "htx.com", "huobi.com",
    "gate.io", "mexc.com", "bitget.com", "exmo.com", "exmo.me", "garantex.org", "blockchain.com", "metamask.io",
)
# Bank card pages a shop sends to: 3-D Secure, payment gateways.
_PAYMENT_HOST = re.compile(r"(^|\.)(3ds|acs|securepay|pay|payment|payments|checkout-pay|secure)\d*\.")

# Tasks that are never done, whatever the wording.
_TASK_NEVER = [
    (re.compile(r"\b(переведи|перевести|перевод|отправь|отправить|скинь|скинуть|закинь|кинь)\b.{0,40}"
                r"\b(\d+\s*)?(деньг|рубл|руб\b|долл|евро|бакс|₽|\$|на карту|по номеру|сбп)"),
     "Переводы денег я не делаю — только вы сами."),
    (re.compile(r"\b(госуслуг|налогов\w* кабинет|личн\w+ кабинет\w* налог|пенсионн\w+ фонд)"),
     "Госуслуги и налоговую я не трогаю: там вход и подпись только ваши."),
    (re.compile(r"\b(банк\w*|сбер\w*|тинькофф\w*|т-банк\w*|втб|альфа-?банк\w*)\b.{0,30}\b(войди|зайди|оплати|переведи|"
                r"открой счет|кредит|вклад)|\b(войди|зайди)\b.{0,20}\b(банк|сбер|тинькофф|т-банк|втб|альфа)"),
     "Банковские кабинеты я не открываю — это только вы."),
    (re.compile(r"\b(купи|продай|обменяй|переведи|выведи)\b.{0,30}\b(биткоин\w*|крипт\w*|эфир\w*|usdt|тезер\w*|"
                r"токен\w*|монет\w* на бирже|акци\w*|облигац\w*)"),
     "Сделки с криптовалютой и акциями я не совершаю."),
    (re.compile(r"\b(введи|вбей|подставь|впиши)\b.{0,30}\b(парол\w*|код из смс|смс-?код|cvc|cvv|номер карты|"
                r"данные карты|паспорт\w*)"),
     "Пароли, коды и данные карт вводите сами — я их не трогаю."),
    (re.compile(r"\b(реши|пройди|обойди)\b.{0,15}\bкапч"), "Капчу решаете вы — я её не обхожу."),
]

# A click with one of these labels changes something for real: Jarvis asks first.
_CONFIRM = re.compile(
    r"\b(оформить|оформление заказа|перейти к оформлению|оплатить|оплата|к оплате|купить сейчас|купить в (1|один) клик|"
    r"заказать|подтвердить|подтверждаю|отправить|опубликовать|разместить|подписаться|отписаться|отменить подписку|"
    r"удалить|очистить|стереть|сохранить изменения|сохранить настройки|изменить пароль|сменить пароль|выйти из аккаунта|"
    r"оставить отзыв|написать отзыв|отправить отзыв|написать продавцу|забронировать|записаться|зарегистрироваться|"
    r"place order|checkout|check out|buy now|pay\b|pay now|submit|send|post\b|publish|subscribe|unsubscribe|delete|"
    r"remove account|confirm|book now|sign up)\b", re.I)
# Accepting terms and agreements: never by Jarvis (declining cookies is fine).
_TERMS = re.compile(r"\b(принять|принимаю|согласен|согласна|соглашаюсь|я согласен|accept|agree|allow all|разрешить все)\b",
                    re.I)
_DECLINE = re.compile(r"\b(отклонить|отказаться|только необходимые|только обязательные|reject|decline|necessary only)\b",
                      re.I)
# Fields Jarvis never fills (same idea as SECRET_FIELD in browser_extension/page.js).
SECRET_FIELD = re.compile(
    r"password|passwd|парол|cc-|card|cvc|cvv|csc|карт[аыу]\b|срок действия|one-time-code|otp|sms|смс|код из|"
    r"код подтверждения|passport|паспорт|снилс|\bpin\b|пин-?код", re.I)

_CAPTCHA_TEXT = re.compile(r"я не робот|подтвердите, что (вы не робот|запросы отправляли вы)|i'?m not a robot|"
                           r"verify you are human|are you a robot", re.I)
_LOGIN_TEXT = re.compile(r"\b(войти|вход в (аккаунт|личный кабинет)|авторизац\w*|sign in|log in|login)\b", re.I)


@dataclass
class Verdict:
    kind: str           # allow | confirm | never
    reason: str = ""    # for "confirm": what to ask about; for "never": what to tell the user


def host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def forbidden_site(url: str) -> str:
    """A site the agent never works on -> what to tell the user; "" if it is fine."""
    h = host(url)
    if any(h == f or h.endswith("." + f) for f in FORBIDDEN_HOSTS):
        return "Это банк, Госуслуги или криптобиржа — там действуете только вы."
    if _PAYMENT_HOST.search(h + "."):
        return "Дальше страница оплаты картой — её проходите вы сами."
    return ""


def check_task(text: str) -> str:
    """A request the agent refuses outright -> the reason; "" if it may start."""
    low = text.lower().replace("ё", "е")
    for pattern, reason in _TASK_NEVER:
        if pattern.search(low):
            return reason
    return ""


def secret_field(element: dict) -> bool:
    if element.get("type") == "password":
        return True
    hints = " ".join(str(element.get(k) or "") for k in ("type", "name", "ac", "placeholder", "text"))
    return bool(SECRET_FIELD.search(hints))


def judge(action: dict, element: dict | None, url: str) -> Verdict:
    """Is this step allowed by itself, only after "да", or never?"""
    kind = action.get("action")
    if kind in ("goto", "open"):
        target = str(action.get("url") or "")
        if not target.lower().startswith(("http://", "https://")):
            return Verdict("never", "Открываю только обычные адреса сайтов.")
        reason = forbidden_site(target)
        return Verdict("never", reason) if reason else Verdict("allow")
    if element is None:
        return Verdict("allow")
    label = " ".join(str(element.get(k) or "") for k in ("text", "name", "placeholder", "value")).strip()
    if kind == "type":
        if secret_field(element):
            return Verdict("never", "Пароли, коды из СМС и данные карт вводите вы сами.")
        if action.get("submit") and element.get("form") and not element.get("search"):
            return Verdict("confirm", f"отправить форму с полем «{label[:60] or 'без названия'}»")
        return Verdict("allow")
    if kind in ("click", "select"):
        if element.get("type") == "submit" and element.get("form") and not element.get("search") \
                and not _CONFIRM.search(label):
            return Verdict("confirm", f"нажать «{label[:60] or 'Отправить'}» — это отправит форму")
        if _TERMS.search(label) and not _DECLINE.search(label):
            return Verdict("never", "Принять условия или соглашение можете только вы.")
        if _CONFIRM.search(label):
            return Verdict("confirm", f"нажать «{label[:80]}»")
    return Verdict("allow")


def blocker(snap: dict) -> tuple[str, str]:
    """Something only the user can pass: ("captcha" | "login" | "payment" | "", what to say)."""
    flags = snap.get("flags") or {}
    url = str(snap.get("url") or "")
    if forbidden_site(url):
        return "payment", forbidden_site(url)
    text = str(snap.get("text") or "")[:1500]
    if flags.get("captcha") or (_CAPTCHA_TEXT.search(text) and len(text) < 1500):
        return "captcha", "На сайте проверка «я не робот» — пройдите её, пожалуйста, я продолжу сам."
    if flags.get("password") and (_LOGIN_TEXT.search(str(snap.get("title") or "")) or _LOGIN_TEXT.search(text[:600])):
        return "login", "Сайт просит войти в аккаунт — войдите, пожалуйста, я подожду и продолжу."
    return "", ""
