import asyncio
import html
import io
import json
import logging
import math
import os
import random
import re
import signal
import sys
import tempfile
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан токен бота. Установите переменную окружения BOT_TOKEN "
        "(в настройках Render: Environment -> Add Environment Variable)."
    )

SCRAPINGANT_API_KEY = os.environ.get("SCRAPINGANT_API_KEY", "2e2075e51d5e4236a474c52c2434d15a")

PORT = int(os.environ.get("PORT", "10000"))

BASE_URL = "https://supremevalues.com"

CATEGORIES: list[tuple[str, str, str]] = [
    ("godlies", "Godly", "🌸"),
    ("chromas", "Chroma", "🌈"),
    ("legendaries", "Legendary", "🔴"),
    ("ancients", "Ancient", "🟣"),
    ("vintages", "Vintage", "🟡"),
    ("rares", "Rare", "🟢"),
    ("uncommons", "Uncommon", "🔵"),
    ("commons", "Common", "⚪"),
]
CATEGORY_SLUGS = [c[0] for c in CATEGORIES]
RARITY_EMOJI = {slug: emoji for slug, _, emoji in CATEGORIES}
RARITY_LABEL_TO_SLUG = {label.lower(): slug for slug, label, _ in CATEGORIES}

REQUEST_TIMEOUT = 60
MAX_RETRIES = 5
RETRY_BASE_DELAY = 5

ADMIN_ID = 1420898868

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mm2bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

try:
    from deep_translator import GoogleTranslator

    TRANSLATOR_AVAILABLE = True
except ImportError:
    TRANSLATOR_AVAILABLE = False
    logger.warning("deep-translator не установлен.")

# --------------------------------------------------------------------------- #
# Обновлённые ID премиум-эмодзи
# --------------------------------------------------------------------------- #

PREMIUM = {
    "div_dark": 5255779912798710813,
    "div_light": 5256249601832268668,
    "name_tag": 5319194297470304045,
    "candlestick": 5451882707875276247,
    "chart_up": 5244837092042750681,
    "chart_down": 5246762912428603768,
    "value": 5319065555825605163,
    "money_falling": 5316926009277168831,
    "fire": 5355092132346482871,
    "info": 5262831879731555779,
    "refresh": 5375338737028841420,
    "top": 5415655814079723871,
    "new": 5382357040008021292,
    "soon": 5440621591387980068,
    "plus": 5397916757333654639,
    "diamond": 5316798019251749107,
    "trash": 5263003781502608081,
    "heart": 5316865463123197927,
    "lock": 5296369303661067030,
    "rainbow": 5409109841538994759,
    "settings": 5262933846550130144,
    "clip": 5305265301917549162,
    "loading": 5318943711898384234,
    "bulb": 5422439311196834318,
    "free": 5406756500108501710,
    "pencil": 5395444784611480792,
    "red_flag": 5460755126761312667,
    "home": 5416041192905265756,
    "party": 5461151367559141950,
    "star": 5354799331541011105,
    "alarm": 5316960914476384229,
    "wave": 5262586680048625238,
    "stats": 5262498959636573342,
    "yes": 5316773048311887967,
    "verify": 5354844694985590087,
    "like": 5262501394883028496,
    "dislike": 5262904116786507271,
    "lang": 5447410659077661506,
    "filters": 5264985741405989559,
    "list": 5265079444707486638,
    "gift": 5192879906295397710,
    "left": 5316757423220867321,
    "right": 5316926258385271354,
}

FALLBACK_EMOJI = {
    "wave": "👋", "stats": "📊", "yes": "✅", "no": "❌", "verify": "✅",
    "like": "👍", "dislike": "👎", "lang": "🌐", "filters": "🎚",
    "list": "📜", "value": "💵", "name_tag": "📌", "chart_up": "📈",
    "chart_down": "📉", "gift": "🎁", "left": "⬅️", "right": "➡️",
    "star": "✨", "fire": "🔥", "info": "ℹ️", "refresh": "🔄",
    "home": "🏠", "div_dark": "⬛", "div_light": "⬜", "candlestick": "🕯",
    "plus": "➕", "diamond": "💎", "trash": "🗑", "heart": "💖",
    "rainbow": "🌈", "settings": "⚙️", "loading": "⌛", "bulb": "💡",
    "pencil": "✏️", "red_flag": "🚩", "party": "🎉", "alarm": "🚨",
    "top": "🔝", "new": "🆕", "soon": "🔜", "free": "🆓", "lock": "🔒",
    "clip": "📎", "money_falling": "💸"
}

PLATE_EMOJI_IDS: dict[str, tuple[int, int, int]] = {
    "godlies": (5424896822764152247, 5424710739011084749, 5424601088496020066),
    "ancients": (5424809669287782271, 5424947511968182015, 5422360356813053692),
    "rares": (5424637106091763719, 5424968492883423047, 5424736341311139342),
    "uncommons": (5424827648020880967, 5425146089781109016, 5424852906723549420),
    "commons": (5425061148212897338, 5422488024715927278, 5424705030999548016),
    "chromas": (5424594066224496687, 5424594268087953477, 5424693267084126572),
    "legendaries": (5424716103425237337, 5424782550864274577, 5425120646394851617),
    "vintages": (5424932062970817364, 5424601324719219065, 5424800460877896182),
}


def use_premium() -> bool:
    return state_store.get_use_premium_emoji()


def emoji(name: str) -> str:
    if use_premium():
        eid = PREMIUM.get(name)
        if eid:
            return f'<tg-emoji emoji-id="{eid}">⬜</tg-emoji>'
    return FALLBACK_EMOJI.get(name, f"[{name}]")


def icon_id(name: str) -> Optional[str]:
    if use_premium():
        eid = PREMIUM.get(name)
        if eid:
            return str(eid)
    return None


def divider() -> str:
    if use_premium():
        return (emoji("div_dark") + emoji("div_light")) * 3
    return "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def rarity_plate_html(slug: str) -> str:
    if use_premium():
        ids = PLATE_EMOJI_IDS.get(slug)
        if ids:
            return "".join(f'<tg-emoji emoji-id="{eid}">⬜</tg-emoji>' for eid in ids)
    return RARITY_EMOJI.get(slug, "❓")


# --------------------------------------------------------------------------- #
# Модель Item
# --------------------------------------------------------------------------- #

@dataclass
class Item:
    name: str
    category_slug: str
    rarity: str
    value: Optional[int]
    value_display: str
    ranged_value: Optional[str]
    stability: str
    image_url: str
    image_url_candidates: list[str] = field(default_factory=list)
    origin: str = ""

    @property
    def search_key(self) -> str:
        return normalize_text(self.name)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category_slug": self.category_slug,
            "rarity": self.rarity,
            "value": self.value,
            "value_display": self.value_display,
            "ranged_value": self.ranged_value,
            "stability": self.stability,
            "image_url": self.image_url,
            "image_url_candidates": self.image_url_candidates,
            "origin": self.origin,
        }

    @staticmethod
    def from_dict(d: dict) -> "Item":
        return Item(
            name=d["name"],
            category_slug=d["category_slug"],
            rarity=d["rarity"],
            value=d.get("value"),
            value_display=d.get("value_display", "N/A"),
            ranged_value=d.get("ranged_value"),
            stability=d.get("stability", "Unknown"),
            image_url=d.get("image_url", ""),
            image_url_candidates=d.get("image_url_candidates", []),
            origin=d.get("origin", ""),
        )


# --------------------------------------------------------------------------- #
# Нормализация
# --------------------------------------------------------------------------- #

CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

RU_LAYOUT_TO_EN = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбю.ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?",
)
EN_LAYOUT_TO_RU = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?",
    "йцукенгшщзхъфывапролджэячсмитьбю.ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,",
)

ROMAN_NUMERALS = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20,
}


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_text(text: str) -> str:
    text = strip_accents(text.lower())
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return " ".join(text.split())


def token_sorted_text(text: str) -> str:
    words = sorted(re.findall(r"[a-zа-я0-9]+", strip_accents(text.lower())))
    return " ".join(words)


def transliterate_ru_to_lat(text: str) -> str:
    text = text.lower()
    return "".join(CYR_TO_LAT.get(ch, ch) for ch in text)


def generate_query_variants(raw_query: str) -> list[str]:
    raw = raw_query.strip()
    variants = set()
    base = normalize_text(raw)
    if base:
        variants.add(base)
    swapped_to_en = raw.translate(RU_LAYOUT_TO_EN)
    v = normalize_text(swapped_to_en)
    if v:
        variants.add(v)
    swapped_to_ru = raw.translate(EN_LAYOUT_TO_RU)
    v = normalize_text(swapped_to_ru)
    if v:
        variants.add(v)
    translit = transliterate_ru_to_lat(raw)
    v = normalize_text(translit)
    if v:
        variants.add(v)
    
    # Обработка слитных цифр (EternalII -> Eternal II, Eternal2 -> Eternal 2)
    spaced = re.sub(r'(?i)([a-z])(\d+|iii|ii|iv|v|vi|vii|viii|ix|x)\b', r'\1 \2', raw)
    if spaced != raw:
        variants.add(normalize_text(spaced))
        base = normalize_text(spaced)

    words = base.split()
    for i, w in enumerate(words):
        # Арабские -> Римские и наоборот
        if w in ROMAN_NUMERALS:
            new_words = words.copy()
            new_words[i] = str(ROMAN_NUMERALS[w])
            variants.add(" ".join(new_words))
        for roman, arabic in ROMAN_NUMERALS.items():
            if w == str(arabic):
                new_words = words.copy()
                new_words[i] = roman
                variants.add(" ".join(new_words))
                
    return list(variants)


# --------------------------------------------------------------------------- #
# Перевод
# --------------------------------------------------------------------------- #

ROOT_TRANSLATIONS: dict[str, str] = {
    "gun": "Пистолет", "revolver": "Револьвер", "blaster": "Бластер",
    "beam": "Луч", "cannon": "Пушка", "shot": "Выстрел", "raygun": "Лучемёт",
    "blade": "Лезвие", "knife": "Нож", "sword": "Меч", "dagger": "Кинжал",
    "axe": "Топор", "battleaxe": "Боевой топор", "scythe": "Коса",
    "edge": "Грань", "shard": "Осколок", "saw": "Пила", "handsaw": "Ножовка",
    "cane": "Трость", "wand": "Жезл", "luger": "Люгер", "sabre": "Сабля",
    "saber": "Сабля", "spear": "Копьё", "claw": "Коготь", "fang": "Клык",
    "chopper": "Тесак", "cleaver": "Тесак", "crusher": "Дробитель",
    "breaker": "Ледокол", "piercer": "Пронзатель", "slasher": "Потрошитель",
    "phaser": "Фазер", "laser": "Лазер", "harvester": "Жнец",
    "wing": "Крыло",
    "ice": "Лёд", "icewing": "Ледокрыло",
    "fire": "Огонь", "flame": "Пламя", "frost": "Мороз", "snow": "Снег",
    "winter": "Зима", "chill": "Холод", "midnight": "Полночь",
    "shadow": "Тень", "void": "Пустота", "corrupt": "Порча",
    "light": "Светлый", "dark": "Тёмный", "bright": "Яркий",
    "star": "Звезда", "galaxy": "Галактика", "comet": "Комета",
    "nebula": "Туманность", "cosmic": "Космический",
    "crystal": "Кристалл", "pearl": "Жемчуг",
    "rainbow": "Радуга", "pixel": "Пиксель", "virtual": "Виртуальный",
    "plasma": "Плазма", "bio": "Био", "electric": "Электрический",
    "ghost": "Призрак", "phantom": "Фантом", "soul": "Душа",
    "bone": "Костяной", "blood": "Кровавый", "death": "Смерть",
    "night": "Ночной", "dawn": "Рассвет", "sunset": "Закат", "moon": "Луна",
    "eternal": "Вечный", "evergreen": "Вечнозелёный", "clockwork": "Заводной",
    "makeshift": "Временный", "swirly": "Спиральный", "elderwood": "Элдервуд",
    "logchopper": "Лесоруб", "hallow": "Хэллоуин", "xmas": "Рождество",
    "candy": "Леденец", "candleflame": "Пламя свечи", "ginger": "Имбирный",
    "cookie": "Печенье", "sugar": "Сахар", "minty": "Мятный",
    "egg": "Яйцо", "bat": "Бита", "batwing": "Летучее крыло", "spider": "Паук",
    "shark": "Акула", "dragon": "Дракон", "wolf": "Волк", "cat": "Кот",
    "bunny": "Кролик", "bear": "Медведь", "fox": "Лис", "phoenix": "Феникс",
    "seer": "Провидец", "tides": "Приливы", "ocean": "Океан",
    "flora": "Флора", "bloom": "Расцвет", "sakura": "Сакура",
    "borealis": "Северное сияние", "australis": "Южное сияние",
    "america": "Америка", "amerilaser": "Америлазер",
    "golden": "Золотой", "silver": "Серебро",
    "chroma": "Хрома", "c.": "Хрома",
    "red": "Красный", "blue": "Синий", "green": "Зелёный",
    "purple": "Фиолетовый", "orange": "Оранжевый", "yellow": "Жёлтый",
    "white": "Белый", "black": "Чёрный", "pink": "Розовый",
    "traveler": "Путешественник", "traveler's": "Путешественника",
    "heart": "Сердце", "cowboy": "Ковбой", "latte": "Латте",
    "cavern": "Пещера", "beach": "Пляж", "broken": "Сломанный",
    "splitter": "Разделитель", "harvest": "Урожай",
}

COMPOUND_SUFFIXES: list[str] = [
    "battleaxe", "raygun", "handsaw", "logchopper",
    "blade", "blaster", "shard", "cane", "beam", "wing", "gun",
    "axe", "saw", "flake",
]

MERGED_COMPOUND_OVERRIDES: dict[str, str] = {
    "icewing": "Ледокрыло", "icebreaker": "Ледокол",
    "icecrusher": "Ледокрушитель", "icepiercer": "Ледопронзатель",
    "iceblaster": "Ледяной бластер", "icebeam": "Ледяной луч",
    "darkshot": "Тёмный выстрел", "darksword": "Тёмный меч",
    "darkbringer": "Несущий тьму", "lightbringer": "Несущий свет",
    "watergun": "Водный пистолет", "snowcannon": "Снежная пушка",
    "lugercane": "Трость-Люгер", "gingerluger": "Имбирный Люгер",
    "hallowgun": "Хэллоу-пистолет", "hallowscythe": "Коса Хэллоуина",
    "plasmabeam": "Плазменный луч", "plasmablade": "Плазменное лезвие",
    "bioblade": "Биолезвие", "frostsaber": "Морозная сабля",
    "gingerblade": "Имбирное лезвие", "boneblade": "Костяное лезвие",
    "ghostblade": "Лезвие призрака", "nightblade": "Ночное лезвие",
    "eggblade": "Лезвие-яйцо", "cookieblade": "Лезвие-печенье",
    "cookiecane": "Печенье-трость", "swirlyblade": "Крутящийся клинок",
    "swirlygun": "Спиральный пистолет", "hallowblade": "Клинок Хэллоуина",
    "hallowsblade": "Клинок Хэллоуина", "elderwoodscythe": "Коса Элдервуд",
    "eternalcane": "Вечная трость", "batwing": "Летучее крыло",
    "makeshift": "Временный",
}

ITEM_ALIASES: dict[str, list[str]] = {
    "icewing": ["ледокрыло", "ледяное крыло", "айсвинг"],
    "icebreaker": ["ледокол", "айсбрекер"],
    "icepiercer": ["ледопронзатель", "айспирсер"],
    "chroma traveler's gun": ["хрома пистолет путешественника", "хрома тревелерс ган"],
    "traveler's gun": ["пистолет путешественника", "тревелер ган"],
    "elderwood scythe": ["коса элдервуд", "элдервуд коса"],
    "candleflame": ["пламя свечи", "кэндлфлейм"],
    "harvester": ["жнец", "харвестер"],
    "batwing": ["летучее крыло", "батвинг", "крыло летучей мыши"],
    "makeshift": ["временный", "самоделка", "самодельный"],
    "corrupt": ["коррупт", "порча"],
    "swirly blade": ["крутящийся клинок", "спиральный клинок"],
    "swirly gun": ["спиральный пистолет"],
    "luger cane": ["трость люгер", "люгер трость", "трость-люгер"],
    "ginger luger": ["имбирный люгер"],
    "hallow's blade": ["клинок хэллоуина", "хэллоуинский клинок"],
}


def _normalize_apostrophe(text: str) -> str:
    return text.replace("'", "'")


def _split_compound_word(word: str) -> list[str]:
    lower = word.lower()
    for suffix in COMPOUND_SUFFIXES:
        if lower.endswith(suffix) and len(lower) > len(suffix):
            head = word[: len(word) - len(suffix)]
            return [head, suffix]
    return [word]


def _translate_token(token: str) -> str:
    normalized = _normalize_apostrophe(token).lower().strip(".,()")
    if normalized in ROOT_TRANSLATIONS:
        return ROOT_TRANSLATIONS[normalized]
    if normalized.endswith("'s"):
        base = normalized[:-2]
        if base in ROOT_TRANSLATIONS:
            return ROOT_TRANSLATIONS[base]
    return token


def auto_translate_ru(name_en: str) -> str:
    raw_words = name_en.split()
    translated_parts: list[str] = []
    i = 0
    n = len(raw_words)
    while i < n:
        raw_word = raw_words[i]
        stripped = raw_word.strip(".,()")
        if i + 1 < n:
            next_stripped = raw_words[i + 1].strip(".,()")
            two_word_key = f"{_normalize_apostrophe(stripped).lower()} {_normalize_apostrophe(next_stripped).lower()}"
            if two_word_key in MERGED_COMPOUND_OVERRIDES:
                translated_parts.append(MERGED_COMPOUND_OVERRIDES[two_word_key])
                i += 2
                continue
            if two_word_key in ROOT_TRANSLATIONS:
                translated_parts.append(ROOT_TRANSLATIONS[two_word_key])
                i += 2
                continue
        merged_key = _normalize_apostrophe(stripped).lower()
        if merged_key in MERGED_COMPOUND_OVERRIDES:
            translated_parts.append(MERGED_COMPOUND_OVERRIDES[merged_key])
            i += 1
            continue
        direct = _translate_token(stripped)
        if direct != stripped:
            translated_parts.append(direct)
            i += 1
            continue
        pieces = _split_compound_word(stripped)
        if len(pieces) > 1:
            translated_parts.append(" ".join(_translate_token(p) for p in pieces))
        else:
            translated_parts.append(raw_word)
        i += 1
    return " ".join(translated_parts)


def _google_translate(text_en: str) -> Optional[str]:
    if not TRANSLATOR_AVAILABLE:
        return None
    try:
        translator = GoogleTranslator(source='auto', target='ru')
        return translator.translate(text_en).strip()
    except Exception as e:
        logger.warning("Google Translate ошибка для '%s': %s", text_en, e)
        return None


def get_ru_name(name_en: str) -> str:
    key = name_en.lower().strip()
    if key in MERGED_COMPOUND_OVERRIDES:
        return MERGED_COMPOUND_OVERRIDES[key]
    cached = state_store.get_translation(name_en)
    if cached:
        return cached
    auto = auto_translate_ru(name_en)
    if normalize_text(auto) != normalize_text(name_en):
        state_store.save_translation(name_en, auto)
        return auto
    gtrans = _google_translate(name_en)
    if gtrans and normalize_text(gtrans) != normalize_text(name_en):
        state_store.save_translation(name_en, gtrans)
        return gtrans
    return name_en


# --------------------------------------------------------------------------- #
# Парсинг
# --------------------------------------------------------------------------- #

STABILITY_MAP_RU = {
    "stable": "Стабилен", "doing well": "Растёт в цене",
    "fluctuating": "Нестабилен", "underpaid for": "Недооценён",
    "unstable": "Нестабилен", "hoarded": "Придерживают",
    "rising": "Растёт в цене", "dropping": "Падает в цене",
    "receding": "Снижается", "improving": "Улучшается",
}

STABILITY_FILTER_OPTIONS: list[tuple[str, str, str]] = [
    ("stable", "Стабилен", "🟢"), ("doing well", "Растёт в цене", "📈"),
    ("fluctuating", "Нестабилен", "🔀"), ("underpaid for", "Недооценён", "💎"),
    ("unstable", "Нестабилен", "⚠️"), ("hoarded", "Придерживают", "🧲"),
    ("dropping", "Падает в цене", "📉"),
]


def _parse_value_to_int(raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw or raw.upper() in ("N/A", "NA"):
        return None
    try:
        return int(raw.replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _normalize_image_src(src: str) -> str:
    src = src.strip()
    if not src:
        return ""
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith(".."):
        src = re.sub(r"^(\.\./)+", "/", src)
        return BASE_URL + src
    if src.startswith("/"):
        return BASE_URL + src
    return BASE_URL + "/" + src.lstrip("/")


_IMG_STOPWORDS = {"the", "a", "an", "of"}
_BRACKET_TAG_RE = re.compile(r"\s*[\[\(][^\]\)]*[\]\)]\s*")


def _strip_name_tags(name: str) -> str:
    return _BRACKET_TAG_RE.sub(" ", name).strip()


def guess_image_filenames(display_name: str) -> list[str]:
    clean = _strip_name_tags(display_name).replace("'", "'")
    is_chroma = False
    words = clean.split()
    if words and words[0].lower() in ("chroma", "c.", "c"):
        is_chroma = True
        words = words[1:]
    if not words:
        words = clean.split()

    def _clean_word(w: str) -> str:
        w = re.sub(r"[^\w']", "", w)
        if w.lower().endswith("'s"):
            w = w[:-2]
        return w

    plain_words = [_clean_word(w) for w in words if _clean_word(w)]
    plain_words = [w for w in plain_words if w.lower() not in _IMG_STOPWORDS]
    if not plain_words:
        fallback_word = re.sub(r"[^\w]", "", clean)
        plain_words = [fallback_word] if fallback_word else []
    prefix = "C" if is_chroma else ""
    candidates: list[str] = []

    def add(name: str):
        if name and name not in candidates:
            candidates.append(name)

    joined_nospace = "".join(plain_words)
    joined_underscore = "_".join(plain_words)
    
    # Ищем только полные слитные названия (исправлен баг, когда находило одиночное слово вместо составного)
    add(prefix + joined_nospace)
    add(prefix + joined_underscore)
    add(joined_nospace)
    add(joined_underscore)
    
    safe_name = re.sub(r"[^\w\s-]", "", clean).strip().replace(" ", "_")
    add(safe_name)
    
    for i, w in enumerate(plain_words):
        if w.lower() in ROMAN_NUMERALS:
            new_words = plain_words.copy()
            new_words[i] = str(ROMAN_NUMERALS[w.lower()])
            add(prefix + "".join(new_words))
            add(prefix + "_".join(new_words))
            add("".join(new_words))
            add("_".join(new_words))
            
    return [c for c in candidates if c]


def fetch_category(slug: str, rarity_label: str) -> list[Item]:
    target_url = f"{BASE_URL}/mm2/{slug}"
    api_url = f"https://api.scrapingant.com/v2/general?url={target_url}&x-api-key={SCRAPINGANT_API_KEY}&browser=true"
    request_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MM2ValuesBot/1.0)",
        "Accept": "application/json", "Connection": "close",
    }
    last_error = None
    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(api_url, timeout=REQUEST_TIMEOUT, headers=request_headers)
            if resp.status_code == 200:
                break
            elif resp.status_code == 409:
                logger.warning("ScrapingAnt 409 для '%s', попытка %d/%d", slug, attempt, MAX_RETRIES)
            else:
                logger.error("ScrapingAnt вернул %d для '%s'", resp.status_code, slug)
            last_error = f"HTTP {resp.status_code}"
        except requests.exceptions.ConnectionError as e:
            logger.warning("Ошибка соединения для '%s': %s", slug, str(e)[:200])
            last_error = "ConnectionError"
        except requests.exceptions.Timeout:
            logger.warning("Таймаут для '%s', попытка %d/%d", slug, attempt, MAX_RETRIES)
            last_error = "Timeout"
        except requests.exceptions.RequestException as e:
            logger.warning("Ошибка запроса для '%s': %s", slug, type(e).__name__)
            last_error = type(e).__name__
        except Exception:
            logger.exception("Неизвестная ошибка при запросе '%s'", slug)
            last_error = "Unknown"
        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(1, 3)
            logger.info("Ожидание %.1f сек. перед повторной попыткой...", delay)
            time.sleep(delay)
    else:
        raise RuntimeError(
            f"Не удалось загрузить категорию '{slug}' после {MAX_RETRIES} попыток (последняя ошибка: {last_error})")
    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[Item] = []
    seen_names: set[str] = set()
    cards = soup.find_all("div", class_="itemcolumn")
    if not cards:
        cards = soup.find_all("tr")
    for card in cards:
        btn_tag = card.find("button")
        display_name = ""
        if btn_tag and btn_tag.get("data-name"):
            display_name = btn_tag.get("data-name").strip()
        else:
            head_tag = card.find("div", class_="itemhead")
            if head_tag:
                display_name = head_tag.get_text(strip=True)
        if not display_name:
            display_name = card.get("data-name", "").strip()
        if not display_name:
            img_probe = card.find("img")
            if img_probe:
                display_name = (img_probe.get("alt") or img_probe.get("title") or "").strip()
        if not display_name or display_name.lower() == "n/a":
            continue
        if display_name.lower() in seen_names:
            continue
        seen_names.add(display_name.lower())
        val_tag = card.find("b", class_="itemvalue")
        if val_tag:
            value_raw = val_tag.get_text(strip=True)
        else:
            value_raw = card.get("data-value", "N/A")
            if value_raw == "N/A":
                text_blob = card.get_text(" ", strip=True)
                m = re.search(r"Value\s*-\s*([\d,]+)", text_blob)
                if m:
                    value_raw = m.group(1)
        value_int = _parse_value_to_int(value_raw)
        value_display = value_raw if value_int is None else f"{value_int:,}".replace(",", " ")
        stability = card.get("data-stability", "") or "Unknown"
        if stability == "Unknown":
            text_blob = card.get_text(" ", strip=True)
            m = re.search(r"Stability\s*-\s*([A-Za-z ]+?)(?:\s{2,}|Demand|$)", text_blob)
            if m:
                stability = m.group(1).strip()
        image_candidates: list[str] = []
        img_tag = card.find("img", class_="itemimage") or card.find("img")
        if img_tag:
            for attr in ("src", "data-src", "data-lazy-src"):
                raw_src = img_tag.get(attr)
                if raw_src and "N_A" not in raw_src.upper() and "placeholder" not in raw_src.lower():
                    normalized = _normalize_image_src(raw_src)
                    if normalized and normalized not in image_candidates:
                        image_candidates.append(normalized)
        media_dir = f"{BASE_URL}/media/mm2{slug}/"
        for guess in guess_image_filenames(display_name):
            candidate = f"{media_dir}{guess}.png"
            if candidate not in image_candidates:
                image_candidates.append(candidate)
        image_url = image_candidates[0] if image_candidates else ""
        origin = card.get("data-event", "")
        if not origin:
            text_blob = card.get_text(" ", strip=True)
            m = re.search(r"Origin\s*-\s*(.+?)(?:\s{2,}|Last Change|$)", text_blob)
            if m:
                origin = m.group(1).strip()
        items.append(Item(
            name=display_name, category_slug=slug, rarity=rarity_label,
            value=value_int, value_display=value_display, ranged_value=None,
            stability=stability, image_url=image_url,
            image_url_candidates=image_candidates, origin=origin,
        ))
    return items


def fetch_all_items() -> list[Item]:
    all_items: list[Item] = []
    for slug, rarity_label, _emoji in CATEGORIES:
        try:
            cat_items = fetch_category(slug, rarity_label)
            logger.info("Категория '%s': найдено %d предметов", slug, len(cat_items))
            all_items.extend(cat_items)
            time.sleep(2.0)
        except Exception:
            logger.exception("Не удалось спарсить категорию '%s'", slug)
    return all_items


# --------------------------------------------------------------------------- #
# Поиск и Кэш
# --------------------------------------------------------------------------- #

@dataclass
class SearchEntry:
    key_norm: str
    key_sorted: str
    item_idx: int


class ValuesCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Item] = []
        self._search_index: list[SearchEntry] = []
        self.last_updated: float = 0.0
        self.last_error: Optional[str] = None

    def _build_search_index(self, items: list[Item]) -> list[SearchEntry]:
        index: list[SearchEntry] = []
        for idx, item in enumerate(items):
            keys_to_add: set[str] = set()
            keys_to_add.add(item.name)
            ru_name = get_ru_name(item.name)
            if ru_name:
                keys_to_add.add(ru_name)
            item_key_lower = item.name.lower().strip()
            if item_key_lower in ITEM_ALIASES:
                for alias in ITEM_ALIASES[item_key_lower]:
                    keys_to_add.add(alias)
            words = item.name.split()
            for i, w in enumerate(words):
                if w.lower() in ROMAN_NUMERALS:
                    new_words = words.copy()
                    new_words[i] = str(ROMAN_NUMERALS[w.lower()])
                    keys_to_add.add(" ".join(new_words))
            for key in keys_to_add:
                k_norm = normalize_text(key)
                k_sorted = token_sorted_text(key)
                if k_norm:
                    index.append(SearchEntry(key_norm=k_norm, key_sorted=k_sorted, item_idx=idx))
        return index

    def refresh(self) -> None:
        logger.info("Запуск обновления кэша ценностей...")
        try:
            items = fetch_all_items()
            if not items:
                raise RuntimeError("Парсинг вернул 0 предметов.")
            known = state_store.load_known_image_urls()
            for item in items:
                key = normalize_text(item.name)
                if key in known:
                    confirmed = known[key]
                    if confirmed not in item.image_url_candidates:
                        item.image_url_candidates.insert(0, confirmed)
                    item.image_url = confirmed
            
            # Собираем индекс ВНЕ лока, чтобы не вешать поиск на 30 секунд
            new_index = self._build_search_index(items)

            with self._lock:
                self._items = items
                self._search_index = new_index
                self.last_updated = time.time()
                self.last_error = None
            logger.info("Кэш обновлён: всего %d предметов.", len(items))
            state_store.notify_cache_refreshed()
        except Exception as e:
            logger.exception("Ошибка обновления кэша")
            with self._lock:
                self.last_error = str(e)
            state_store.notify_cache_refreshed()

    def search(self, query: str, limit: int = 5, filters: Optional["ItemFilters"] = None) -> list[tuple[Item, float]]:
        with self._lock:
            items = self._items
            index = self._search_index
            
        if not items or not index:
            return []
            
        variants = generate_query_variants(query)
        if not variants:
            return []
            
        allowed_idx: Optional[set[int]] = None
        if filters is not None and not filters.is_empty:
            allowed_idx = {i for i, it in enumerate(items) if filters.matches(it)}
            if not allowed_idx:
                return []
                
        best_by_idx: dict[int, float] = {}
        CHROMA_WORDS = {"chroma", "хрома", "c"}

        def _has_chroma_word(norm_text: str) -> bool:
            tokens = norm_text.split()
            return bool(tokens) and tokens[0] in CHROMA_WORDS

        for variant in variants:
            v_norm = normalize_text(variant)
            if not v_norm:
                continue
                
            query_has_chroma = _has_chroma_word(v_norm)
            
            for entry in index:
                if allowed_idx is not None and entry.item_idx not in allowed_idx:
                    continue
                
                # Точное совпадение — абсолютный приоритет
                if v_norm == entry.key_norm:
                    score = 150.0
                elif v_norm in entry.key_norm.split(): # если ищем одно слово, и оно полностью есть
                    score = 100.0
                else:
                    s1 = fuzz.ratio(v_norm, entry.key_norm)
                    s2 = fuzz.token_sort_ratio(v_norm, entry.key_norm)
                    s3 = fuzz.token_set_ratio(v_norm, entry.key_norm)
                    
                    # Штрафуем token_set_ratio, если длины строк сильно различаются
                    # Это предотвращает баг, когда 'Eternal' давал 100% для 'Eternal III' 
                    # или 'Luger' давал 100% для 'Luger Cane'
                    len_ratio = min(len(v_norm), len(entry.key_norm)) / max(1, max(len(v_norm), len(entry.key_norm)))
                    s3_adjusted = s3 * (0.7 + 0.3 * len_ratio)
                    
                    score = max(s1, s2, s3_adjusted)
                
                entry_has_chroma = _has_chroma_word(entry.key_norm)
                if query_has_chroma != entry_has_chroma:
                    score = max(0.0, score - 40.0)
                elif query_has_chroma and entry_has_chroma:
                    score = min(150.0, score + 5.0)
                    
                if score > best_by_idx.get(entry.item_idx, -1):
                    best_by_idx[entry.item_idx] = float(score)
                    
        if not best_by_idx:
            return []
            
        ranked = sorted(best_by_idx.items(), key=lambda kv: kv[1], reverse=True)
        THRESHOLD = 58.0
        result: list[tuple[Item, float]] = []
        for idx, score in ranked:
            if score < THRESHOLD:
                continue
            result.append((items[idx], score))
            if len(result) >= limit:
                break
        return result

    def all_items(self, filters: Optional["ItemFilters"] = None) -> list[Item]:
        with self._lock:
            items = list(self._items)
        if filters is not None and not filters.is_empty:
            items = [it for it in items if filters.matches(it)]
        items.sort(key=lambda it: (it.value is None, -(it.value or 0)))
        return items

    def get_by_name(self, name: str) -> Optional[Item]:
        with self._lock:
            items = self._items
        key = normalize_text(name)
        for it in items:
            if normalize_text(it.name) == key:
                return it
        return None

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def export_items(self) -> list[dict]:
        with self._lock:
            return [item.to_dict() for item in self._items]

    def load_items(self, raw_items: list[dict]) -> None:
        items = [Item.from_dict(d) for d in raw_items]
        with self._lock:
            self._items = items
            self._search_index = self._build_search_index(items)


cache = ValuesCache()

# --------------------------------------------------------------------------- #
# StateStore с добавленной статистикой
# --------------------------------------------------------------------------- #

DEFAULT_LANG = "ru"
SUPPORTED_LANGS = {"ru": "Русский", "en": "English"}
DEFAULT_REFRESH_DAYS = 7
STATE_FILENAME = "mm2bot_state.json"
DEBOUNCE_SECONDS = 20.0

CHANNEL_ID_RAW = os.environ.get("CHANNEL_ID")
if not CHANNEL_ID_RAW:
    raise RuntimeError("Не задан CHANNEL_ID.")
try:
    CHANNEL_ID = int(CHANNEL_ID_RAW)
except ValueError:
    raise RuntimeError("CHANNEL_ID должен быть числом.")


@dataclass
class ItemFilters:
    min_value: int = 0
    max_value: int = -1
    rarity_slug: str = "all"
    stability_key: str = "all"

    @property
    def is_empty(self) -> bool:
        return self.min_value == 0 and self.max_value == -1 and self.rarity_slug == "all" and self.stability_key == "all"

    def matches(self, item: Item) -> bool:
        if item.value is not None:
            if item.value < self.min_value:
                return False
            if self.max_value != -1 and item.value > self.max_value:
                return False
        else:
            if self.min_value > 0 or self.max_value != -1:
                return False
        if self.rarity_slug != "all" and item.category_slug != self.rarity_slug:
            return False
        if self.stability_key != "all":
            if normalize_text(item.stability) != normalize_text(self.stability_key):
                return False
        return True

    def to_dict(self) -> dict:
        return {"min_value": self.min_value, "max_value": self.max_value, "rarity_slug": self.rarity_slug,
                "stability_key": self.stability_key}

    @staticmethod
    def from_dict(d: dict) -> "ItemFilters":
        return ItemFilters(min_value=int(d.get("min_value", 0)), max_value=int(d.get("max_value", -1)),
                           rarity_slug=str(d.get("rarity_slug", "all")),
                           stability_key=str(d.get("stability_key", "all")))


class StateStore:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id
        self._lock = threading.Lock()
        self.user_langs: dict[str, str] = {}
        self.user_filters: dict[str, dict] = {}
        self.known_images: dict[str, str] = {}
        self.translations: dict[str, str] = {}
        self.refresh_interval_days: int = DEFAULT_REFRESH_DAYS
        self.use_premium_emoji: bool = True
        # Статистика
        self.total_users: set[str] = set()
        self.monthly_active: dict[str, float] = {}  # user_id -> last active timestamp
        self.feedback_likes: int = 0
        self.feedback_dislikes: int = 0
        self._dirty_event = threading.Event()
        self._stop_event = threading.Event()
        self._debounce_thread: Optional[threading.Thread] = None

    def _to_state_dict(self) -> dict:
        with self._lock:
            return {
                "version": 4, "saved_at": time.time(),
                "settings": {
                    "refresh_interval_days": self.refresh_interval_days,
                    "user_langs": dict(self.user_langs),
                    "user_filters": {k: dict(v) for k, v in self.user_filters.items()},
                    "known_images": dict(self.known_images),
                    "translations": dict(self.translations),
                    "use_premium_emoji": self.use_premium_emoji,
                },
                "stats": {
                    "total_users": list(self.total_users),
                    "monthly_active": dict(self.monthly_active),
                    "feedback_likes": self.feedback_likes,
                    "feedback_dislikes": self.feedback_dislikes,
                },
                "cache": {"last_updated": cache.last_updated or None, "last_error": cache.last_error,
                          "items": cache.export_items()},
            }

    def _load_state_dict(self, data: dict) -> None:
        settings = data.get("settings", {}) or {}
        cache_info = data.get("cache", {}) or {}
        stats = data.get("stats", {}) or {}
        with self._lock:
            self.refresh_interval_days = int(settings.get("refresh_interval_days", DEFAULT_REFRESH_DAYS))
            self.user_langs = {str(k): v for k, v in (settings.get("user_langs", {}) or {}).items()}
            self.user_filters = {str(k): dict(v) for k, v in (settings.get("user_filters", {}) or {}).items()}
            self.known_images = {str(k): str(v) for k, v in (settings.get("known_images", {}) or {}).items()}
            self.translations = {str(k): str(v) for k, v in (settings.get("translations", {}) or {}).items()}
            self.use_premium_emoji = bool(settings.get("use_premium_emoji", True))
            self.total_users = set(stats.get("total_users", []))
            self.monthly_active = {k: v for k, v in (stats.get("monthly_active", {}) or {}).items()}
            self.feedback_likes = int(stats.get("feedback_likes", 0))
            self.feedback_dislikes = int(stats.get("feedback_dislikes", 0))
        raw_items = cache_info.get("items")
        if raw_items:
            cache.load_items(raw_items)
        with cache._lock:
            cache.last_updated = cache_info.get("last_updated") or 0.0
            cache.last_error = cache_info.get("last_error")

    def record_user_active(self, user_id: int):
        now = time.time()
        uid = str(user_id)
        with self._lock:
            self.total_users.add(uid)
            self.monthly_active[uid] = now
            # Очищаем старые записи (>30 дней)
            self.monthly_active = {k: v for k, v in self.monthly_active.items() if now - v < 30 * 86400}
        self.mark_dirty()

    def add_feedback(self, positive: bool):
        with self._lock:
            if positive:
                self.feedback_likes += 1
            else:
                self.feedback_dislikes += 1
        self.mark_dirty()

    def get_stats_text(self) -> str:
        with self._lock:
            total_users = len(self.total_users)
            monthly_active = len(self.monthly_active)
            likes = self.feedback_likes
            dislikes = self.feedback_dislikes
        accuracy = (likes / (likes + dislikes) * 100) if (likes + dislikes) > 0 else 100
        return (
            f"👥 Всего пользователей: {total_users}\n"
            f"📅 Активных за месяц: {monthly_active}\n"
            f"🎯 Точность поиска: {accuracy:.1f}% (👍{likes} / 👎{dislikes})"
        )

    def load_from_channel(self) -> bool:
        api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
        try:
            resp = requests.get(f"{api_base}/getChat", params={"chat_id": self.channel_id}, timeout=10)
            resp.raise_for_status()
            chat_data = resp.json()
        except Exception:
            logger.exception("Не удалось получить getChat для канала-хранилища")
            return False
        if not chat_data.get("ok"):
            return False
        pinned = chat_data.get("result", {}).get("pinned_message")
        if not pinned:
            return False
        document = pinned.get("document")
        if not document:
            return False
        file_id = document.get("file_id")
        if not file_id:
            return False
        try:
            file_resp = requests.get(f"{api_base}/getFile", params={"file_id": file_id}, timeout=10)
            file_resp.raise_for_status()
            file_data = file_resp.json()
            if not file_data.get("ok"):
                return False
            file_path = file_data["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            content_resp = requests.get(download_url, timeout=15)
            content_resp.raise_for_status()
            state = json.loads(content_resp.content.decode("utf-8"))
        except Exception:
            logger.exception("Не удалось скачать/распарсить снапшот из канала")
            return False
        self._load_state_dict(state)
        logger.info("Состояние восстановлено: %d предметов, %d переводов.", cache.size, len(self.translations))
        return True

    def save_to_channel_now(self) -> bool:
        state = self._to_state_dict()
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
                json.dump(state, tmp, ensure_ascii=False, indent=2)
                tmp_path = tmp.name
            api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
            with open(tmp_path, "rb") as f:
                send_resp = requests.post(f"{api_base}/sendDocument",
                                          data={"chat_id": self.channel_id, "disable_notification": True},
                                          files={"document": (STATE_FILENAME, f, "application/json")}, timeout=60)
            send_resp.raise_for_status()
            send_data = send_resp.json()
            if not send_data.get("ok"):
                return False
            message_id = send_data["result"]["message_id"]
            requests.post(f"{api_base}/pinChatMessage",
                          data={"chat_id": self.channel_id, "message_id": message_id, "disable_notification": True},
                          timeout=20)
            logger.info("Снапшот сохранён и закреплён.")
            return True
        except Exception:
            logger.exception("Ошибка сохранения снапшота")
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def start_debounce_worker(self) -> None:
        if self._debounce_thread is not None:
            return
        self._debounce_thread = threading.Thread(target=self._debounce_loop, daemon=True, name="state-debounce")
        self._debounce_thread.start()

    def _debounce_loop(self) -> None:
        while not self._stop_event.is_set():
            triggered = self._dirty_event.wait(timeout=1.0)
            if not triggered:
                continue
            while True:
                self._dirty_event.clear()
                woke = self._stop_event.wait(timeout=DEBOUNCE_SECONDS)
                if woke:
                    break
                if not self._dirty_event.is_set():
                    break
            self.save_to_channel_now()
            if self._stop_event.is_set():
                break

    def mark_dirty(self) -> None:
        self._dirty_event.set()

    def notify_cache_refreshed(self) -> None:
        threading.Thread(target=self.save_to_channel_now, daemon=True).start()

    def flush_now(self) -> None:
        self.save_to_channel_now()

    def get_user_lang(self, user_id: int) -> str:
        with self._lock:
            return self.user_langs.get(str(user_id), DEFAULT_LANG)

    def set_user_lang(self, user_id: int, lang: str) -> None:
        with self._lock:
            self.user_langs[str(user_id)] = lang
        self.mark_dirty()

    def get_refresh_interval_days(self) -> int:
        with self._lock:
            return self.refresh_interval_days

    def set_refresh_interval_days(self, days: int) -> None:
        with self._lock:
            self.refresh_interval_days = days
        self.mark_dirty()

    def get_user_filters(self, user_id: int) -> ItemFilters:
        with self._lock:
            raw = self.user_filters.get(str(user_id))
        if raw:
            return ItemFilters.from_dict(raw)
        return ItemFilters()

    def set_user_filters(self, user_id: int, filters_obj: ItemFilters) -> None:
        with self._lock:
            self.user_filters[str(user_id)] = filters_obj.to_dict()
        self.mark_dirty()

    def reset_user_filters(self, user_id: int) -> None:
        self.set_user_filters(user_id, ItemFilters())

    def load_known_image_urls(self) -> dict[str, str]:
        with self._lock:
            return dict(self.known_images)

    def save_known_image_url(self, name_key: str, url: str) -> None:
        with self._lock:
            if self.known_images.get(name_key) == url:
                return
            self.known_images[name_key] = url
        self.mark_dirty()

    def get_translation(self, en_text: str) -> Optional[str]:
        with self._lock:
            return self.translations.get(en_text.lower().strip())

    def save_translation(self, en_text: str, ru_text: str) -> None:
        with self._lock:
            key = en_text.lower().strip()
            if self.translations.get(key) == ru_text:
                return
            self.translations[key] = ru_text
        self.mark_dirty()

    def get_use_premium_emoji(self) -> bool:
        with self._lock:
            return self.use_premium_emoji

    def set_use_premium_emoji(self, value: bool) -> None:
        with self._lock:
            if self.use_premium_emoji == value:
                return
            self.use_premium_emoji = value
        self.mark_dirty()


state_store = StateStore(channel_id=CHANNEL_ID)

# --------------------------------------------------------------------------- #
# Локализация
# --------------------------------------------------------------------------- #

RARITY_RU_LABELS = {
    "godlies": "Godly", "chromas": "Chroma", "legendaries": "Legendary",
    "ancients": "Ancient", "vintages": "Vintage", "rares": "Rare",
    "uncommons": "Uncommon", "commons": "Common",
}


def localized_stability(lang: str, stability_en: str) -> str:
    if lang == "en":
        return stability_en
    key = stability_en.strip().lower()
    return STABILITY_MAP_RU.get(key, stability_en)


def stability_label(lang: str, key: str) -> str:
    for k, ru_label, _emoji in STABILITY_FILTER_OPTIONS:
        if k == key:
            return ru_label if lang == "ru" else k.title()
    return key


def rarity_label_localized(lang: str, slug: str) -> str:
    for cslug, label, _emoji in CATEGORIES:
        if cslug == slug:
            if lang == "ru":
                return RARITY_RU_LABELS.get(cslug, label)
            return label
    return slug


TEXTS: dict[str, dict[str, str]] = {"ru": {}, "en": {}}


def emoji_dict() -> dict[str, str]:
    return {
        "wave": emoji("wave"),
        "stats": emoji("stats"),
        "yes": emoji("yes"),
        "no": emoji("trash"), 
        "verify": emoji("verify"),
        "like": emoji("like"),
        "dislike": emoji("dislike"),
        "globe": emoji("lang"),
        "filters": emoji("filters"),
        "list": emoji("list"),
        "value": emoji("value"),
        "name_tag": emoji("name_tag"),
        "chart_up": emoji("chart_up"),
        "chart_down": emoji("chart_down"),
        "money_falling": emoji("money_falling"),
        "gift": emoji("gift"),
        "left": emoji("left"),
        "right": emoji("right"),
        "star": emoji("star"),
        "fire": emoji("fire"),
        "info": emoji("info"),
        "refresh": emoji("refresh"),
        "home": emoji("home"),
        "settings": emoji("settings"),
        "pencil": emoji("pencil"),
        "lock": emoji("lock"),
        "clip": emoji("clip"),
        "loading": emoji("loading"),
        "party": emoji("party"),
        "red_flag": emoji("red_flag"),
        "diamond": emoji("diamond"),
        "top": emoji("top"),
        "candlestick": emoji("candlestick"),
        "alarm": emoji("alarm"),
    }


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else DEFAULT_LANG
    template = TEXTS[lang].get(key, TEXTS[DEFAULT_LANG].get(key, key))
    return template.format(**kwargs) if kwargs else template


def t_em(lang: str, key: str, **kwargs) -> str:
    return t(lang, key, **emoji_dict(), **kwargs)


TEXTS["ru"].update({
    "start": (
        "{wave} <b>Привет!</b> Я бот-оценщик ценности предметов Murder Mystery 2.\n\n"
        "{diamond} Просто напиши название предмета на русском или английском — например: "
        "<i>Хрома пистолет путешественника</i>, <i>Ледокрыло</i> или <i>Nebula</i>.\n\n"
        "📋 <b>Команды:</b>\n"
        "{settings} /settings — язык интерфейса\n"
        "{filters} /filters — настроить фильтры поиска\n"
        "{list} /list — каталог всех предметов\n"
        "{stats} /status — статус базы данных"
    ),
    "help": (
        "{info} Напиши название предмета MM2 — я найду его ценность.\n\n"
        "📋 <b>Команды:</b>\n"
        "{settings} /settings — сменить язык интерфейса\n"
        "{filters} /filters — настроить фильтры (цена, редкость, стабильность)\n"
        "{list} /list — список всех предметов (дорогие → дешёвые)\n"
        "{stats} /status — статус базы данных"
    ),
    "settings_title": "{settings} <b>Выберите язык интерфейса</b>",
    "settings_saved": "{verify} Язык сохранён: <b>{lang_name}</b>",
    "not_found": (
        "{red_flag} Ничего не найдено по запросу «{query}».\n"
        "{pencil} Проверь написание или попробуй другое название предмета.\n"
        "{clip} Возможно, стоит проверить активные /filters."
    ),
    "value_label": "Примерная стоимость",
    "status_label": "Категория",
    "stability_label": "Стабильность",
    "origin_label": "Событие",
    "unknown_stability": "Неизвестно",
    "cache_empty": "{loading} База данных ещё загружается, попробуй через минуту.",
    "status_report": (
        "{stats} Предметов в базе: <b>{count}</b>\n"
        "{refresh} Последнее обновление: <b>{last_update}</b>\n"
        "{alarm} Ошибка последнего обновления: <b>{error}</b>\n"
        "{lock} Хранилище: приватный Telegram-канал"
    ),
    "status_report_ok": (
        "{stats} Предметов в базе: <b>{count}</b>\n"
        "{refresh} Последнее обновление: <b>{last_update}</b>\n"
        "{lock} Хранилище: приватный Telegram-канал"
    ),
    "never": "ещё не обновлялось",
    "no_error": "нет",
    "admin_set_refresh": "{settings} Текущий интервал обновления: {days} дн.\nИспользуйте /setrefresh <число> чтобы изменить (от 1 до 90).",
    "admin_refresh_updated": "{verify} Интервал обновления изменён на {days} дн.",
    "admin_refresh_invalid": "{red_flag} Укажите целое число дней от 1 до 90.",
    "admin_only": "{red_flag} Эта команда доступна только администратору.",
    "filters_title": "{filters} <b>Фильтры поиска</b>\n\nНастрой параметры и нажми «Применить».",
    "filters_btn_min": "Валюта (от): {val}",
    "filters_btn_max": "Валюта (до): {val}",
    "filters_btn_rarity": "Редкость: {val}",
    "filters_btn_stability": "Стабильность: {val}",
    "filters_btn_apply": "Применить",
    "filters_btn_reset": "Сбросить",
    "filters_unlimited": "∞ неограниченно",
    "filters_all": "все",
    "filters_ask_min": "{pencil} Введи <b>минимальное</b> значение цены числом (например: 1000):",
    "filters_ask_max": "{pencil} Введи <b>максимальное</b> значение цены числом, либо -1 для «неограниченно»:",
    "filters_invalid_number": "{red_flag} Это не похоже на корректное число. Попробуй ещё раз:",
    "filters_invalid_range": "{red_flag} Минимум не может быть больше максимума. Попробуй ещё раз:",
    "filters_invalid_negative": "{red_flag} Значение не может быть отрицательным (кроме -1 для «неограниченно»). Попробуй ещё раз:",
    "filters_saved": "{verify} Значение сохранено",
    "filters_applied": "{verify} Фильтры применены!",
    "filters_rarity_title": "🏷 <b>Выберите редкость</b>",
    "filters_stability_title": "{chart_up} <b>Выберите стабильность</b>",
    "filters_option_all": "Все",
    "list_title": "{top} <b>Каталог предметов</b> (дорогие → дешёвые)",
    "list_empty": "{info} По заданным фильтрам ничего не найдено. Проверь /filters.",
    "list_nav_page": "📄 {page}/{total}",
    "feedback_like": "{like} Спасибо за отзыв!",
    "feedback_dislike": "{dislike} Что именно не так?",
    "feedback_reason_bad_result": "Неправильный результат",
    "feedback_reason_bad_translation": "Плохой перевод",
    "feedback_reason_bad_image": "Неверная картинка",
    "feedback_reason_other": "Другое",
    "feedback_ask_details": "{pencil} Опиши подробнее или укажи правильный вариант перевода/названия (или нажми /cancel):",
    "feedback_sent": "{party} Спасибо! Администратор получит твой отзыв.",
    "feedback_cancelled": "{trash} Отзыв отменён.",
})

TEXTS["en"].update({
    "start": (
        "{wave} <b>Hi!</b> I'm a Murder Mystery 2 item value checker bot.\n\n"
        "{diamond} Just type an item name in English or Russian — for example: "
        "<i>Chroma Traveler's Gun</i>, <i>Icewing</i>.\n\n"
        "📋 <b>Commands:</b>\n"
        "{settings} /settings — interface language\n"
        "{filters} /filters — configure search filters\n"
        "{list} /list — item catalog\n"
        "{stats} /status — database status"
    ),
    "help": (
        "{info} Type an MM2 item name — I'll find its value.\n\n"
        "📋 <b>Commands:</b>\n"
        "{settings} /settings — change interface language\n"
        "{filters} /filters — configure filters\n"
        "{list} /list — list of all items\n"
        "{stats} /status — database status"
    ),
    "settings_title": "{settings} <b>Choose interface language</b>",
    "settings_saved": "{verify} Language saved: <b>{lang_name}</b>",
    "not_found": (
        "{red_flag} Nothing found for «{query}».\n"
        "{pencil} Check the spelling or try another query.\n"
        "{clip} You may also want to check your active /filters."
    ),
    "value_label": "Estimated Value",
    "status_label": "Category",
    "stability_label": "Stability",
    "origin_label": "Origin",
    "unknown_stability": "Unknown",
    "cache_empty": "{loading} Database is still loading, please try again in a minute.",
    "status_report": (
        "{stats} Items in database: <b>{count}</b>\n"
        "{refresh} Last update: <b>{last_update}</b>\n"
        "{alarm} Last update error: <b>{error}</b>\n"
        "{lock} Storage: private Telegram channel"
    ),
    "status_report_ok": (
        "{stats} Items in database: <b>{count}</b>\n"
        "{refresh} Last update: <b>{last_update}</b>\n"
        "{lock} Storage: private Telegram channel"
    ),
    "never": "not updated yet",
    "no_error": "none",
    "admin_set_refresh": "{settings} Current refresh interval: {days} days.\nUse /setrefresh <number> to change (1–90).",
    "admin_refresh_updated": "{verify} Refresh interval set to {days} days.",
    "admin_refresh_invalid": "{red_flag} Please enter an integer from 1 to 90.",
    "admin_only": "{red_flag} This command is for the administrator only.",
    "filters_title": "{filters} <b>Search filters</b>\n\nAdjust the parameters and press \"Apply\".",
    "filters_btn_min": "Currency (from): {val}",
    "filters_btn_max": "Currency (to): {val}",
    "filters_btn_rarity": "Rarity: {val}",
    "filters_btn_stability": "Stability: {val}",
    "filters_btn_apply": "Apply",
    "filters_btn_reset": "Reset",
    "filters_unlimited": "∞ unlimited",
    "filters_all": "all",
    "filters_ask_min": "{pencil} Enter minimum price as a number:",
    "filters_ask_max": "{pencil} Enter maximum price as a number, or -1 for unlimited:",
    "filters_invalid_number": "{red_flag} Invalid number. Try again:",
    "filters_invalid_range": "{red_flag} Minimum can't be greater than maximum:",
    "filters_invalid_negative": "{red_flag} Value can't be negative:",
    "filters_saved": "{verify} Value saved",
    "filters_applied": "{verify} Filters applied!",
    "filters_rarity_title": "🏷 <b>Choose rarity</b>",
    "filters_stability_title": "{chart_up} <b>Choose stability</b>",
    "filters_option_all": "All",
    "list_title": "{top} <b>Item catalog</b>",
    "list_empty": "{info} Nothing matches your filters.",
    "list_nav_page": "📄 {page}/{total}",
    "feedback_like": "{like} Thanks for your feedback!",
    "feedback_dislike": "{dislike} What's wrong?",
    "feedback_reason_bad_result": "Wrong result",
    "feedback_reason_bad_translation": "Bad translation",
    "feedback_reason_bad_image": "Wrong image",
    "feedback_reason_other": "Other",
    "feedback_ask_details": "{pencil} Describe the problem or suggest correct translation/name (or /cancel):",
    "feedback_sent": "{party} Thank you! Administrator will receive your feedback.",
    "feedback_cancelled": "{trash} Feedback cancelled.",
})

# --------------------------------------------------------------------------- #
# Шрифты и картинки
# --------------------------------------------------------------------------- #

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
os.makedirs(FONTS_DIR, exist_ok=True)

_INTER_SOURCES = {
    "Inter-Regular.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/static/Inter-Regular.ttf",
    "Inter-Bold.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/static/Inter-Bold.ttf",
}

_FALLBACK_FONT_MAP = {
    "Inter-Regular.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "Inter-Bold.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def ensure_fonts_downloaded() -> None:
    for filename, url in _INTER_SOURCES.items():
        dest = os.path.join(FONTS_DIR, filename)
        if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
            continue
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200 and len(resp.content) > 10_000:
                with open(dest, "wb") as f:
                    f.write(resp.content)
        except Exception as e:
            logger.warning("Ошибка загрузки шрифта %s: %s", filename, e)


def get_font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    weight_file = "Inter-Bold.ttf" if weight in ("bold", "semibold", "extrabold") else "Inter-Regular.ttf"
    cache_key = (weight_file, size)
    if cache_key in _font_cache:
        return _font_cache[cache_key]
    local_path = os.path.join(FONTS_DIR, weight_file)
    path = local_path if os.path.exists(local_path) else _FALLBACK_FONT_MAP.get(weight_file, "")
    try:
        font = ImageFont.truetype(path, size)
    except Exception:
        font = ImageFont.load_default()
    _font_cache[cache_key] = font
    return font


_IMG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://supremevalues.com/",
}
_url_status_cache: dict[str, bool] = {}


def _try_download_single(url: str) -> Optional[Image.Image]:
    if not url or _url_status_cache.get(url) is False:
        return None
    try:
        resp = requests.get(url, headers=_IMG_HEADERS, timeout=12)
        if resp.status_code != 200 or len(resp.content) < 80:
            _url_status_cache[url] = False
            return None
        img = Image.open(io.BytesIO(resp.content))
        img.load()
        _url_status_cache[url] = True
        return img.convert("RGBA")
    except Exception:
        _url_status_cache[url] = False
        return None


def refresh_item_image_candidates(item: Item) -> list[str]:
    slug = item.category_slug
    target_url = f"{BASE_URL}/mm2/{slug}"
    api_url = f"https://api.scrapingant.com/v2/general?url={target_url}&x-api-key={SCRAPINGANT_API_KEY}&browser=true"
    fresh_candidates: list[str] = []
    try:
        resp = requests.get(api_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return fresh_candidates
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("div", class_="itemcolumn") or soup.find_all("tr")
        for card in cards:
            btn = card.find("button")
            name = ""
            if btn and btn.get("data-name"):
                name = btn.get("data-name").strip()
            else:
                head = card.find("div", class_="itemhead")
                if head:
                    name = head.get_text(strip=True)
            if not name or normalize_text(name) != normalize_text(item.name):
                continue
            img_tag = card.find("img", class_="itemimage") or card.find("img")
            if img_tag:
                for attr in ("src", "data-src", "data-lazy-src"):
                    raw = img_tag.get(attr)
                    if raw and "N_A" not in raw.upper() and "placeholder" not in raw.lower():
                        normalized = _normalize_image_src(raw)
                        if normalized and normalized not in fresh_candidates:
                            fresh_candidates.append(normalized)
            media_dir = f"{BASE_URL}/media/mm2{slug}/"
            for guess in guess_image_filenames(name):
                candidate = f"{media_dir}{guess}.png"
                if candidate not in fresh_candidates:
                    fresh_candidates.append(candidate)
            break
    except Exception:
        logger.exception("Не удалось обновить URL изображения для %s", item.name)
    return fresh_candidates


def download_item_image(item: Item) -> Optional[Image.Image]:
    urls_to_try = []
    if item.image_url:
        urls_to_try.append(item.image_url)
    
    for url in item.image_url_candidates:
        if url not in urls_to_try:
            urls_to_try.append(url)
            
    for url in urls_to_try:
        img = _try_download_single(url)
        if img is not None:
            if url != item.image_url:
                item.image_url = url
                state_store.save_known_image_url(normalize_text(item.name), url)
            return img
            
    fresh_candidates = refresh_item_image_candidates(item)
    for fresh_url in fresh_candidates:
        if fresh_url in urls_to_try:
            continue
        img = _try_download_single(fresh_url)
        if img is not None:
            item.image_url = fresh_url
            item.image_url_candidates.insert(0, fresh_url)
            state_store.save_known_image_url(normalize_text(item.name), fresh_url)
            return img
            
    return None


CARD_W, CARD_H = 800, 800
RARITY_GRADIENTS = {
    "godlies": ((255, 196, 64), (120, 40, 140)), "chromas": ((255, 90, 205), (70, 60, 255)),
    "legendaries": ((255, 140, 60), (140, 30, 30)), "ancients": ((170, 100, 255), (40, 20, 90)),
    "vintages": ((190, 150, 110), (60, 40, 30)), "rares": ((80, 150, 255), (20, 40, 110)),
    "uncommons": ((80, 220, 140), (10, 60, 50)), "commons": ((190, 200, 210), (60, 60, 70)),
}
DEFAULT_GRADIENT = ((90, 90, 140), (20, 20, 40))
RARITY_ACCENT = {
    "godlies": (255, 210, 90), "chromas": (255, 110, 220), "legendaries": (255, 150, 70),
    "ancients": (190, 130, 255), "vintages": (205, 170, 130), "rares": (110, 170, 255),
    "uncommons": (100, 230, 160), "commons": (210, 215, 225),
}


def _lerp_color(c1, c2, t_):
    return (int(c1[0] + (c2[0] - c1[0]) * t_), int(c1[1] + (c2[1] - c1[1]) * t_), int(c1[2] + (c2[2] - c1[2]) * t_))


def _make_mesh_background(width, height, slug):
    c1, c2 = RARITY_GRADIENTS.get(slug, DEFAULT_GRADIENT)
    base = Image.new("RGB", (width, height), c1)
    px = base.load()
    for y in range(height):
        for x in range(0, width, 2):
            t_ = ((x * 0.6 + y * 0.4)) / (width * 0.6 + height * 0.4)
            col = _lerp_color(c1, c2, min(1.0, max(0.0, t_)))
            px[x, y] = col
            if x + 1 < width:
                px[x + 1, y] = col
    base = base.convert("RGBA")
    blob_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(blob_layer)
    accent = RARITY_ACCENT.get(slug, (150, 150, 220))
    bdraw.ellipse([width * 0.5, -height * 0.2, width * 1.3, height * 0.6], fill=(*accent, 120))
    bdraw.ellipse([-width * 0.3, height * 0.5, width * 0.5, height * 1.3], fill=(*c2, 130))
    blob_layer = blob_layer.filter(ImageFilter.GaussianBlur(130))
    return Image.alpha_composite(base, blob_layer).convert("RGBA")


def create_item_image(item: Item, lang: str) -> io.BytesIO:
    width, height = CARD_W, CARD_H
    slug = item.category_slug
    canvas = _make_mesh_background(width, height, slug)
    item_img = download_item_image(item)
    panel_center_x = width // 2
    panel_center_y = height // 2
    if item_img:
        margin = 40
        max_w = width - margin * 2
        max_h = height - margin * 2
        ratio = min(max_w / item_img.width, max_h / item_img.height, 1.0)
        if ratio < 1.0 or (item_img.width < max_w and item_img.height < max_h):
            ratio = min(max_w / item_img.width, max_h / item_img.height)
        new_w = max(1, int(item_img.width * ratio))
        new_h = max(1, int(item_img.height * ratio))
        item_img = item_img.resize((new_w, new_h), Image.LANCZOS)
        shadow = Image.new("RGBA", (new_w + 80, new_h + 80), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        sdraw.ellipse([20, new_h - 10, new_w + 60, new_h + 70], fill=(0, 0, 0, 100))
        shadow = shadow.filter(ImageFilter.GaussianBlur(25))
        canvas.paste(shadow, (panel_center_x - (new_w + 80) // 2, panel_center_y - new_h // 2 - 10), shadow)
        canvas.paste(item_img, (panel_center_x - new_w // 2, panel_center_y - new_h // 2), item_img)
    else:
        draw = ImageDraw.Draw(canvas)
        draw.text((panel_center_x, panel_center_y - 20), "🖼", anchor="mm", font=get_font(110), fill=(255, 255, 255, 140))
        draw.text((panel_center_x, panel_center_y + 80), "Нет фото" if lang == "ru" else "No image", anchor="mm",
                  font=get_font(28), fill=(255, 255, 255, 160))
    bio = io.BytesIO()
    canvas.convert("RGB").save(bio, format="JPEG", quality=95)
    bio.seek(0)
    return bio


def format_item_caption(item: Item, lang: str) -> str:
    plate = rarity_plate_html(item.category_slug)
    stab_txt = localized_stability(lang, item.stability) if item.stability else t_em(lang, "unknown_stability")
    name_en = item.name or "???"
    name_ru = get_ru_name(item.name) if lang == "ru" else ""
    
    title_icon = emoji('fire') if "chroma" in item.category_slug else emoji('star')
    if name_ru and normalize_text(name_ru) != normalize_text(name_en):
        title = f"<b>{html.escape(name_en)}</b> (<i>{html.escape(name_ru)}</i>)"
    else:
        title = f"<b>{html.escape(name_en)}</b>"
        
    stab_icon = "chart_up"
    if item.stability and item.stability.lower() in ("dropping", "receding", "unstable", "fluctuating"):
        stab_icon = "chart_down"
    if item.stability and item.stability.lower() in ("underpaid for", "hoarded"):
        stab_icon = "money_falling"

    # Исправлен порядок эмодзи (звездочка/квадратик поменяны местами)
    lines = [
        f"{plate} {title}",
        divider(),
        f"{emoji('value')} <b>{t_em(lang, 'value_label')}:</b> ⛁ <b>{item.value_display or 'N/A'}</b>",
        f"{emoji('name_tag')} <b>{t_em(lang, 'status_label')}:</b> {title_icon} {rarity_label_localized(lang, item.category_slug)}",
        f"{emoji(stab_icon)} <b>{t_em(lang, 'stability_label')}:</b> {stab_txt}",
    ]
    if item.origin:
        lines.append(f"{emoji('gift')} <b>{t_em(lang, 'origin_label')}:</b> {html.escape(item.origin)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Клавиатуры aiogram
# --------------------------------------------------------------------------- #

def make_button(text: str, callback_data: str, icon_name: str = None, style: str = None) -> InlineKeyboardButton:
    kwargs = {"text": text, "callback_data": callback_data}
    if use_premium() and icon_name:
        eid = icon_id(icon_name)
        if eid is not None:
            kwargs["icon_custom_emoji_id"] = str(eid)
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def make_button_with_emoji(text: str, callback_data: str, icon_name: str = None, style: str = None) -> InlineKeyboardButton:
    if use_premium() and icon_name:
        return make_button(text, callback_data, icon_name=icon_name, style=style)
    else:
        fb = FALLBACK_EMOJI.get(icon_name, "")
        display_text = f"{fb} {text}".strip() if fb else text
        return InlineKeyboardButton(text=display_text, callback_data=callback_data, style=style)


def feedback_keyboard(lang: str, item_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            make_button_with_emoji(" ", f"fb:like:{item_name}", icon_name="like", style="primary"),
            make_button_with_emoji(" ", f"fb:dislike:{item_name}", icon_name="dislike", style="danger"),
        ]
    ])


def dislike_reason_keyboard(lang: str, item_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t_em(lang, "feedback_reason_bad_result"),
                              callback_data=f"fb_reason:bad_result:{item_name}")],
        [InlineKeyboardButton(text=t_em(lang, "feedback_reason_bad_translation"),
                              callback_data=f"fb_reason:bad_translation:{item_name}")],
        [InlineKeyboardButton(text=t_em(lang, "feedback_reason_bad_image"),
                              callback_data=f"fb_reason:bad_image:{item_name}")],
        [InlineKeyboardButton(text=t_em(lang, "feedback_reason_other"),
                              callback_data=f"fb_reason:other:{item_name}")],
    ])


LIST_PAGE_SIZE = 8


def _filters_summary_value(lang: str, filters: ItemFilters, kind: str) -> str:
    if kind == "min":
        return str(filters.min_value) if filters.min_value else "0"
    if kind == "max":
        return t_em(lang, "filters_unlimited") if filters.max_value == -1 else str(filters.max_value)
    if kind == "rarity":
        if filters.rarity_slug == "all":
            return t_em(lang, "filters_all")
        return rarity_label_localized(lang, filters.rarity_slug)
    if kind == "stability":
        if filters.stability_key == "all":
            return t_em(lang, "filters_all")
        return stability_label(lang, filters.stability_key)
    return ""


def build_filters_keyboard(lang: str, filters: ItemFilters) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [make_button_with_emoji(
            t_em(lang, "filters_btn_min", val=_filters_summary_value(lang, filters, "min")),
            "filt:ask_min", icon_name="value")],
        [make_button_with_emoji(
            t_em(lang, "filters_btn_max", val=_filters_summary_value(lang, filters, "max")),
            "filt:ask_max", icon_name="value")],
        [make_button_with_emoji(
            t_em(lang, "filters_btn_rarity", val=_filters_summary_value(lang, filters, "rarity")),
            "filt:rarity_menu", icon_name="diamond")],
        [make_button_with_emoji(
            t_em(lang, "filters_btn_stability", val=_filters_summary_value(lang, filters, "stability")),
            "filt:stability_menu", icon_name="chart_up")],
        [
            make_button_with_emoji(t_em(lang, "filters_btn_reset"), "filt:reset", icon_name="trash", style="danger"),
            make_button_with_emoji(t_em(lang, "filters_btn_apply"), "filt:apply", icon_name="verify", style="success"),
        ],
    ])


def build_rarity_menu_keyboard(lang: str, filters: ItemFilters) -> InlineKeyboardMarkup:
    buttons = [
        [make_button_with_emoji(
            t_em(lang, "filters_option_all"),
            "filt:set_rarity:all", icon_name="verify")]
    ]
    for slug, _label, emoji_char in CATEGORIES:
        mark = "✅ " if filters.rarity_slug == slug else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{emoji_char} {rarity_label_localized(lang, slug)}",
            callback_data=f"filt:set_rarity:{slug}")])
    buttons.append([make_button_with_emoji("Назад", "filt:back", icon_name="left", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_stability_menu_keyboard(lang: str, filters: ItemFilters) -> InlineKeyboardMarkup:
    buttons = [
        [make_button_with_emoji(
            t_em(lang, "filters_option_all"),
            "filt:set_stability:all", icon_name="verify")]
    ]
    for key, ru_label, emoji_char in STABILITY_FILTER_OPTIONS:
        label = ru_label if lang == "ru" else key.title()
        mark = "✅ " if filters.stability_key == key else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{emoji_char} {label}",
            callback_data=f"filt:set_stability:{key}")])
    buttons.append([make_button_with_emoji("Назад", "filt:back", icon_name="left", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_list_keyboard(lang: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(make_button_with_emoji(" ", f"list:page:{page - 1}", icon_name="left", style="primary"))
    nav.append(InlineKeyboardButton(
        text=t_em(lang, "list_nav_page", page=page + 1, total=max(total_pages, 1)),
        callback_data="list:noop"))
    if page < total_pages - 1:
        nav.append(make_button_with_emoji(" ", f"list:page:{page + 1}", icon_name="right", style="primary"))
    return InlineKeyboardMarkup(inline_keyboard=[nav])


def render_list_page_text(lang: str, items: list[Item], page: int, total_pages: int) -> str:
    start = page * LIST_PAGE_SIZE
    page_items = items[start:start + LIST_PAGE_SIZE]
    lines = [t_em(lang, "list_title"), ""]
    for i, item in enumerate(page_items, start=start + 1):
        plate = rarity_plate_html(item.category_slug)
        value = item.value_display or "N/A"
        lines.append(f"{i}. {plate} <b>{html.escape(item.name)}</b> — ⛁ {value}")
    return "\n".join(lines)


async def send_item_card(chat_id: int, item: Item, lang: str, bot: Bot) -> None:
    caption = format_item_caption(item, lang)
    try:
        photo = create_item_image(item, lang)
        await bot.send_photo(
            chat_id=chat_id,
            photo=types.BufferedInputFile(photo.getvalue(), filename="item.jpg"),
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=feedback_keyboard(lang, item.name),
        )
    except Exception:
        logger.exception("Не удалось отправить изображение для '%s'", item.name)
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=feedback_keyboard(lang, item.name),
        )


# --------------------------------------------------------------------------- #
# FSM для рекламы
# --------------------------------------------------------------------------- #

class AdvertiseStates(StatesGroup):
    wait_text = State()


# --------------------------------------------------------------------------- #
# Обработчики aiogram
# --------------------------------------------------------------------------- #

dp = Dispatcher()


@dp.message(Command("start"))
async def start_cmd(message: Message):
    state_store.record_user_active(message.from_user.id)
    lang = state_store.get_user_lang(message.from_user.id)
    await message.answer(t_em(lang, "start"), parse_mode=ParseMode.HTML)


@dp.message(Command("help"))
async def help_cmd(message: Message):
    state_store.record_user_active(message.from_user.id)
    lang = state_store.get_user_lang(message.from_user.id)
    await message.answer(t_em(lang, "help"), parse_mode=ParseMode.HTML)


@dp.message(Command("settings"))
async def settings_cmd(message: Message):
    state_store.record_user_active(message.from_user.id)
    lang = state_store.get_user_lang(message.from_user.id)
    builder = InlineKeyboardBuilder()
    for code, name in SUPPORTED_LANGS.items():
        builder.button(text=name, callback_data=f"setlang:{code}")
    await message.answer(t_em(lang, "settings_title"), parse_mode=ParseMode.HTML,
                         reply_markup=builder.as_markup())


@dp.message(Command("status"))
async def status_cmd(message: Message):
    state_store.record_user_active(message.from_user.id)
    lang = state_store.get_user_lang(message.from_user.id)
    count = cache.size
    last_update = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(cache.last_updated)) if cache.last_updated else t_em(lang, "never")
    if cache.last_error:
        text = t_em(lang, "status_report", count=count, last_update=last_update, error=cache.last_error)
    else:
        text = t_em(lang, "status_report_ok", count=count, last_update=last_update)
    
    if message.from_user.id == ADMIN_ID:
        text += "\n\n" + state_store.get_stats_text()
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("setrefresh"))
async def setrefresh_cmd(message: Message):
    lang = state_store.get_user_lang(message.from_user.id)
    if message.from_user.id != ADMIN_ID:
        await message.answer(t_em(lang, "admin_only"))
        return
    args = message.text.split()
    if len(args) < 2:
        days = state_store.get_refresh_interval_days()
        await message.answer(t_em(lang, "admin_set_refresh", days=days))
        return
    try:
        days = int(args[1])
        if not (1 <= days <= 90):
            raise ValueError
    except ValueError:
        await message.answer(t_em(lang, "admin_refresh_invalid"))
        return
    state_store.set_refresh_interval_days(days)
    scheduler = message.bot.get("scheduler") if isinstance(message.bot, dict) else None
    if scheduler:
        try:
            scheduler.reschedule_job("cache_refresh", trigger=IntervalTrigger(days=days))
        except Exception:
            logger.exception("Не удалось перепланировать задачу обновления кэша")
    await message.answer(t_em(lang, "admin_refresh_updated", days=days))


@dp.message(Command("togglepremium"))
async def togglepremium_cmd(message: Message):
    lang = state_store.get_user_lang(message.from_user.id)
    if message.from_user.id != ADMIN_ID:
        await message.answer(t_em(lang, "admin_only"))
        return
    current = state_store.get_use_premium_emoji()
    new_val = not current
    state_store.set_use_premium_emoji(new_val)
    status = "включены" if new_val else "выключены"
    await message.answer(f"Премиум-эмодзи {status}.")


@dp.message(Command("filters"))
async def filters_cmd(message: Message, state: FSMContext):
    state_store.record_user_active(message.from_user.id)
    user_id = message.from_user.id
    lang = state_store.get_user_lang(user_id)
    filters_obj = state_store.get_user_filters(user_id)
    await state.update_data(awaiting_filter_input=None)
    await message.answer(t_em(lang, "filters_title"), parse_mode=ParseMode.HTML,
                         reply_markup=build_filters_keyboard(lang, filters_obj))


@dp.message(Command("list"))
async def list_cmd(message: Message):
    state_store.record_user_active(message.from_user.id)
    user_id = message.from_user.id
    lang = state_store.get_user_lang(user_id)
    if cache.size == 0:
        await message.answer(t_em(lang, "cache_empty"))
        return
    filters_obj = state_store.get_user_filters(user_id)
    items = cache.all_items(filters_obj)
    if not items:
        await message.answer(t_em(lang, "list_empty"), parse_mode=ParseMode.HTML)
        return
    total_pages = max(1, math.ceil(len(items) / LIST_PAGE_SIZE))
    page = 0
    text = render_list_page_text(lang, items, page, total_pages)
    await message.answer(text, parse_mode=ParseMode.HTML,
                         reply_markup=build_list_keyboard(lang, page, total_pages))


@dp.message(Command("refresh12345"))
async def force_refresh_cmd(message: Message):
    lang = state_store.get_user_lang(message.from_user.id)
    if message.from_user.id != ADMIN_ID:
        await message.answer(t_em(lang, "admin_only"))
        return
    await message.answer(f"{emoji('refresh')} Запускаю принудительное обновление кэша...")
    threading.Thread(target=cache.refresh, daemon=True).start()


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    data = await state.get_data()
    current_state = await state.get_state()
    lang = state_store.get_user_lang(message.from_user.id)
    
    if current_state == AdvertiseStates.wait_text:
        await state.clear()
        await message.answer("Отменено.")
    elif data.get("awaiting_feedback"):
        await state.update_data(awaiting_feedback=None, feedback_reason=None)
        await message.answer(t_em(lang, "feedback_cancelled"))
    elif data.get("awaiting_filter_input"):
        await state.update_data(awaiting_filter_input=None)
        await message.answer("Отменено.")
    else:
        await message.answer("Нечего отменять.")


# Команда для рекламы (админ)
@dp.message(Command("advertise"))
async def advertise_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа")
        return
    await state.set_state(AdvertiseStates.wait_text)
    await message.answer("Отправьте текст с эмодзи, который нужно преобразовать и опубликовать (или /cancel для отмены).")


# Исправлен роутинг: теперь ловится только если пользователь находится в состоянии AdvertiseStates.wait_text
@dp.message(AdvertiseStates.wait_text)
async def process_advertise(message: Message, state: FSMContext):
    text = message.text or message.caption or ""
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return
        
    entities = message.entities or message.caption_entities or []
    custom_emojis = sorted(
        [e for e in entities if e.type == "custom_emoji"],
        key=lambda e: e.offset, reverse=True
    )
    encoded = text.encode("utf-16-le")
    for e in custom_emojis:
        start = e.offset * 2
        end = (e.offset + e.length) * 2
        replacement = f'<tg-emoji emoji-id="{e.custom_emoji_id}">⬜</tg-emoji>'.encode("utf-16-le")
        encoded = encoded[:start] + replacement + encoded[end:]
    processed = encoded.decode("utf-16-le")
    try:
        await message.bot.send_message(CHANNEL_ID, processed, parse_mode=ParseMode.HTML)
        await message.answer("Реклама опубликована.")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    await state.clear()


# Обработчик текстовых сообщений (поиск, ввод фильтров, фидбэк)
@dp.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    # Предотвращаем конфликты с другими стейтами, если этот хэндлер случайно запустится
    current_state = await state.get_state()
    if current_state is not None:
        return
        
    user_id = message.from_user.id
    lang = state_store.get_user_lang(user_id)
    query = message.text.strip()
    if not query:
        return

    state_store.record_user_active(user_id)
    data = await state.get_data()

    # Фидбэк
    if data.get("awaiting_feedback"):
        item_name = data["awaiting_feedback"]
        reason = data.get("feedback_reason", "unknown")
        feedback_text = query
        user = message.from_user
        msg = (
            f"📩 <b>Фидбэк от пользователя</b>\n"
            f"👤 {user.full_name} (@{user.username or 'без_юзернейма'} | {user.id})\n"
            f"🏷 <b>Предмет:</b> {item_name}\n"
            f"❓ <b>Причина:</b> {reason}\n"
            f"💬 <b>Комментарий:</b> {html.escape(feedback_text)}"
        )
        try:
            await message.bot.send_message(ADMIN_ID, msg, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await state.update_data(awaiting_feedback=None, feedback_reason=None)
        await message.answer(t_em(lang, "feedback_sent"), parse_mode=ParseMode.HTML)
        return

    # Ввод данных для фильтров
    if data.get("awaiting_filter_input"):
        filter_kind = data["awaiting_filter_input"]
        try:
            val = int(query)
        except ValueError:
            await message.answer(t_em(lang, "filters_invalid_number"))
            return
            
        filters_obj = state_store.get_user_filters(user_id)
        if filter_kind == "min":
            if val < 0:
                await message.answer(t_em(lang, "filters_invalid_negative"))
                return
            if filters_obj.max_value != -1 and val > filters_obj.max_value:
                await message.answer(t_em(lang, "filters_invalid_range"))
                return
            filters_obj.min_value = val
        elif filter_kind == "max":
            if val < -1:
                await message.answer(t_em(lang, "filters_invalid_negative"))
                return
            if val != -1 and val < filters_obj.min_value:
                await message.answer(t_em(lang, "filters_invalid_range"))
                return
            filters_obj.max_value = val

        state_store.set_user_filters(user_id, filters_obj)
        await state.update_data(awaiting_filter_input=None)
        await message.answer(t_em(lang, "filters_saved"), parse_mode=ParseMode.HTML,
                             reply_markup=build_filters_keyboard(lang, filters_obj))
        return

    # Обычный Поиск
    if cache.size == 0:
        await message.answer(t_em(lang, "cache_empty"), parse_mode=ParseMode.HTML)
        return

    filters_obj = state_store.get_user_filters(user_id)
    results = cache.search(query, limit=5, filters=filters_obj)
    if not results:
        await message.answer(t_em(lang, "not_found", query=html.escape(query)), parse_mode=ParseMode.HTML)
        return

    best_item = results[0][0]
    await send_item_card(message.chat.id, best_item, lang, message.bot)

# --------------------------------------------------------------------------- #
# Коллбэки
# --------------------------------------------------------------------------- #

@dp.callback_query(F.data.startswith("setlang:"))
async def cq_setlang(cq: CallbackQuery):
    code = cq.data.split(":")[1]
    state_store.set_user_lang(cq.from_user.id, code)
    await cq.answer(t_em(code, "settings_saved", lang_name=SUPPORTED_LANGS[code]))
    await cq.message.edit_text(t_em(code, "settings_saved", lang_name=SUPPORTED_LANGS[code]), parse_mode=ParseMode.HTML)

@dp.callback_query(F.data.startswith("fb:"))
async def cq_feedback(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":", 2)
    action = parts[1]
    item_name = parts[2]
    lang = state_store.get_user_lang(cq.from_user.id)
    if action == "like":
        state_store.add_feedback(True)
        await cq.answer(t_em(lang, "feedback_like"))
        await cq.message.edit_reply_markup(reply_markup=None)
    elif action == "dislike":
        state_store.add_feedback(False)
        await cq.message.edit_reply_markup(reply_markup=dislike_reason_keyboard(lang, item_name))
        await cq.answer()

@dp.callback_query(F.data.startswith("fb_reason:"))
async def cq_fb_reason(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":", 2)
    reason = parts[1]
    item_name = parts[2]
    lang = state_store.get_user_lang(cq.from_user.id)
    await state.update_data(awaiting_feedback=item_name, feedback_reason=reason)
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer(t_em(lang, "feedback_ask_details"), parse_mode=ParseMode.HTML)
    await cq.answer()

@dp.callback_query(F.data.startswith("filt:"))
async def cq_filters(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":")
    action = parts[1]
    user_id = cq.from_user.id
    lang = state_store.get_user_lang(user_id)
    filters_obj = state_store.get_user_filters(user_id)

    if action == "ask_min":
        await state.update_data(awaiting_filter_input="min")
        await cq.message.answer(t_em(lang, "filters_ask_min"), parse_mode=ParseMode.HTML)
        await cq.answer()
    elif action == "ask_max":
        await state.update_data(awaiting_filter_input="max")
        await cq.message.answer(t_em(lang, "filters_ask_max"), parse_mode=ParseMode.HTML)
        await cq.answer()
    elif action == "rarity_menu":
        await cq.message.edit_reply_markup(reply_markup=build_rarity_menu_keyboard(lang, filters_obj))
        await cq.answer()
    elif action == "stability_menu":
        await cq.message.edit_reply_markup(reply_markup=build_stability_menu_keyboard(lang, filters_obj))
        await cq.answer()
    elif action == "set_rarity":
        filters_obj.rarity_slug = parts[2]
        state_store.set_user_filters(user_id, filters_obj)
        await cq.message.edit_reply_markup(reply_markup=build_rarity_menu_keyboard(lang, filters_obj))
        await cq.answer()
    elif action == "set_stability":
        filters_obj.stability_key = parts[2]
        state_store.set_user_filters(user_id, filters_obj)
        await cq.message.edit_reply_markup(reply_markup=build_stability_menu_keyboard(lang, filters_obj))
        await cq.answer()
    elif action == "reset":
        state_store.reset_user_filters(user_id)
        filters_obj = state_store.get_user_filters(user_id)
        await cq.message.edit_reply_markup(reply_markup=build_filters_keyboard(lang, filters_obj))
        await cq.answer()
    elif action == "apply":
        await cq.message.edit_text(t_em(lang, "filters_applied"), parse_mode=ParseMode.HTML)
        await cq.answer()
    elif action == "back":
        await cq.message.edit_reply_markup(reply_markup=build_filters_keyboard(lang, filters_obj))
        await cq.answer()

@dp.callback_query(F.data.startswith("list:"))
async def cq_list(cq: CallbackQuery):
    parts = cq.data.split(":")
    if parts[1] == "noop":
        await cq.answer()
        return
    page = int(parts[2])
    user_id = cq.from_user.id
    lang = state_store.get_user_lang(user_id)
    filters_obj = state_store.get_user_filters(user_id)
    items = cache.all_items(filters_obj)
    if not items:
        await cq.answer(t_em(lang, "list_empty"), show_alert=True)
        return
    total_pages = max(1, math.ceil(len(items) / LIST_PAGE_SIZE))
    if page < 0 or page >= total_pages:
        await cq.answer()
        return
    text = render_list_page_text(lang, items, page, total_pages)
    await cq.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=build_list_keyboard(lang, page, total_pages))
    await cq.answer()


# --------------------------------------------------------------------------- #
# Инициализация и Запуск
# --------------------------------------------------------------------------- #

async def main():
    ensure_fonts_downloaded()
    state_store.load_from_channel()
    state_store.start_debounce_worker()
    
    if cache.size == 0:
        logger.info("Кэш пуст, запускаю первичное обновление...")
        cache.refresh()

    scheduler = BackgroundScheduler()
    scheduler.add_job(cache.refresh, 'interval', days=state_store.get_refresh_interval_days(), id='cache_refresh')
    scheduler.start()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Bot is running")
    
    server = ThreadingHTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    logger.info("Бот запущен и готов к работе!")
    
    bot._scheduler = scheduler 
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
