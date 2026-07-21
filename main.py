from __future__ import annotations

import html
import io
import logging
import math
import os
import random
import re
import sqlite3
import signal
import sys
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
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import Conflict, NetworkError, TimedOut, BadRequest

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
DB_PATH = os.environ.get("DB_PATH", "mm2bot_settings.db")

PORT = int(os.environ.get("PORT", "10000"))

BASE_URL = "https://supremevalues.com"

# slug на сайте, отображаемая (англ.) редкость, эмодзи редкости
CATEGORIES: list[tuple[str, str, str]] = [
    ("godlies", "Godly", "🟡"),
    ("chromas", "Chroma", "🌈"),
    ("legendaries", "Legendary", "🟠"),
    ("ancients", "Ancient", "🟣"),
    ("vintages", "Vintage", "🟤"),
    ("rares", "Rare", "🔵"),
    ("uncommons", "Uncommon", "🟢"),
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
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)

# --------------------------------------------------------------------------- #
# Модель данных предмета
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

# --------------------------------------------------------------------------- #
# Нормализация и перевод
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

def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )

def normalize_text(text: str) -> str:
    text = strip_accents(text.lower())
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return " ".join(text.split())

def token_sorted_text(text: str) -> str:
    """Возвращает отсортированные по алфавиту слова без знаков препинания."""
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
    return list(variants)

# --------------------------------------------------------------------------- #
# Автоматический перевод названий и база псевдонимов
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
    "wing": "Крыло", "beam gun": "Лучевой пистолет",
    "ice": "Лёд", "iceflake": "Ледяная снежинка", "icewing": "Ледокрыло",
    "fire": "Огонь", "flame": "Пламя", "flames": "Пламя", "heat": "Жар",
    "frost": "Мороз", "frostbite": "Обморожение", "snow": "Снег",
    "snowflake": "Снежинка", "snowstorm": "Снежная буря", "blizzard": "Метель",
    "winter": "Зима", "summer": "Лето", "spring": "Весна", "autumn": "Осень",
    "chill": "Холод", "midnight": "Полночь", "darkness": "Тьма",
    "shadow": "Тень", "void": "Пустота", "corrupt": "Порча",
    "light": "Светлый", "dark": "Тёмный", "bright": "Яркий",
    "star": "Звезда", "galaxy": "Галактика", "comet": "Комета",
    "meteor": "Метеор", "aurora": "Аврора", "eclipse": "Затмение",
    "nebula": "Туманность", "constellation": "Созвездие", "cosmic": "Космический",
    "crystal": "Кристалл", "gemstone": "Драгоценный камень", "pearl": "Жемчуг",
    "pearlshine": "Жемчужный блеск", "prismatic": "Призматический",
    "rainbow": "Радуга", "pixel": "Пиксель", "virtual": "Виртуальный",
    "plasma": "Плазма", "bio": "Био", "toxic": "Токсичный",
    "electric": "Электрический", "spectral": "Спектральный", "spectre": "Призрак",
    "ghost": "Призрак", "phantom": "Фантом", "soul": "Душа", "spirit": "Дух",
    "bone": "Костяной", "blood": "Кровавый", "death": "Смерть",
    "vampire": "Вампир", "zombie": "Зомби", "skeleton": "Скелет",
    "night": "Ночной", "day": "Дневной", "dawn": "Рассвет",
    "sunrise": "Рассвет", "sunset": "Закат", "moon": "Луна",
    "harvest moon": "Урожайная луна", "eternal": "Вечный",
    "evergreen": "Вечнозелёный", "clockwork": "Заводной механизм",
    "makeshift": "Самодельный", "swirly": "Спиральный", "elderwood": "Элдервуд",
    "logchopper": "Лесоруб", "hallow": "Хэллоуин", "hallows": "Хэллоуин",
    "xmas": "Рождество", "christmas": "Рождество", "jingle": "Звенящий",
    "candy": "Леденец", "candleflame": "Пламя свечи", "peppermint": "Мята перечная",
    "ginger": "Имбирный", "gingermint": "Имбирная мята", "cookie": "Печенье",
    "sugar": "Сахар", "sweet": "Конфета", "treat": "Сладость", "minty": "Мятный",
    "egg": "Яйцо", "pumpking": "Тыквенный король", "turkey": "Индейка",
    "bat": "Летучая мышь", "batwing": "Летучее крыло", "spider": "Паук",
    "shark": "Акула", "dragon": "Дракон", "wolf": "Волк", "cat": "Кот",
    "dog": "Пёс", "bunny": "Кролик", "bear": "Медведь", "fox": "Лис",
    "pig": "Свин", "phoenix": "Феникс", "old glory": "Старая слава",
    "seer": "Провидец", "tides": "Приливы", "waves": "Волны", "ocean": "Океан",
    "flora": "Флора", "bloom": "Расцвет", "blossom": "Цветение",
    "sakura": "Сакура", "ornament": "Украшение", "bauble": "Ёлочный шар",
    "borealis": "Северное сияние", "australis": "Южное сияние",
    "americ": "Америка", "america": "Америка", "amerilaser": "Америлазер",
    "gold": "Золото", "golden": "Золотой", "silver": "Серебро",
    "chroma": "Хрома", "c.": "Хрома", "godly": "Голди",
    "red": "Красный", "blue": "Синий", "green": "Зелёный",
    "purple": "Фиолетовый", "orange": "Оранжевый", "yellow": "Жёлтый",
    "white": "Белый", "black": "Чёрный", "pink": "Розовый",
    "traveler": "Путешественник", "traveler's": "Путешественника",
    "travelers": "Путешественника", "heart": "Сердце", "prince": "Принц",
    "cowboy": "Ковбой", "cotton candy": "Сахарная вата", "latte": "Латте",
    "cavern": "Пещера", "beach": "Пляж", "broken": "Сломанный",
    "splitter": "Разделитель", "harvest": "Урожай",
}

COMPOUND_SUFFIXES: list[str] = [
    "battleaxe", "raygun", "handsaw", "logchopper",
    "blade", "blaster", "shard", "cane", "beam", "wing", "gun",
    "axe", "saw", "flake",
]

MERGED_COMPOUND_OVERRIDES: dict[str, str] = {
    "icewing": "Ледокрыло",
    "icebreaker": "Ледокол",
    "icecrusher": "Ледокрушитель",
    "icepiercer": "Ледопронзатель",
    "iceblaster": "Ледяной бластер",
    "icebeam": "Ледяной луч",
    "iceflake": "Ледяная снежинка",
    "darkshot": "Тёмный выстрел",
    "darksword": "Тёмный меч",
    "darkbringer": "Несущий тьму",
    "lightbringer": "Несущий свет",
    "watergun": "Водный пистолет",
    "snowcannon": "Снежная пушка",
    "xenoknife": "Ксенонож",
    "xenoshot": "Ксеновыстрел",
    "alienbeam": "Луч пришельца",
    "hallowgun": "Хэллоу-пистолет",
    "hallowscythe": "Коса Хэллоуина",
    "plasmabeam": "Плазменный луч",
    "plasmablade": "Плазменное лезвие",
    "bioblade": "Биолезвие",
    "frostsaber": "Морозная сабля",
    "gingerblade": "Имбирное лезвие",
    "boneblade": "Костяное лезвие",
    "ghostblade": "Лезвие призрака",
    "nightblade": "Ночное лезвие",
    "eggblade": "Лезвие-яйцо",
    "cookieblade": "Лезвие-печенье",
    "cookiecane": "Печенье-трость",
    "eternalcane": "Вечная трость",
    "lugercane": "Люгер-трость",
    "jinglegun": "Звенящий пистолет",
    "evergun": "Вечнозелёный пистолет",
    "traveler's gun": "Пистолет путешественника",
    "traveler's axe": "Топор путешественника",
}

# Популярные сленговые / дополнительные русские имена для улучшения поиска
ITEM_ALIASES: dict[str, list[str]] = {
    "icewing": ["ледокрыло", "ледяное крыло", "айсвинг"],
    "icebreaker": ["ледокол", "айсбрекер"],
    "icepiercer": ["ледопронзатель", "айспирсер"],
    "chroma traveler's gun": ["хрома пистолет путешественника", "хрома тревелерс ган", "хрома путешественника пистолет"],
    "traveler's gun": ["пистолет путешественника", "тревелер ган", "путешественника пистолет"],
    "elderwood scythe": ["коса элдервуд", "элдервуд коса"],
    "candleflame": ["пламя свечи", "кэндлфлейм"],
    "harvester": ["жнец", "харвестер"],
    "batwing": ["летучее крыло", "батвинг", "крыло летучей мыши"],
    "makeshift": ["самоделка", "самодельный"],
    "corrupt": ["коррупт", "порча"],
}

def _split_compound_word(word: str) -> list[str]:
    lower = word.lower()
    for suffix in COMPOUND_SUFFIXES:
        if lower.endswith(suffix) and len(lower) > len(suffix):
            head = word[: len(word) - len(suffix)]
            return [head, suffix]
    return [word]

def _normalize_apostrophe(text: str) -> str:
    return text.replace("’", "'")

def _translate_token(token: str) -> str:
    normalized = _normalize_apostrophe(token).lower().strip(".,()")
    candidates = [normalized]
    if normalized.endswith("'s"):
        candidates.append(normalized[:-2])
    elif normalized.endswith("s") and not normalized.endswith("'s"):
        candidates.append(normalized[:-1])
    candidates.append(normalized.replace("'", ""))
    for key in candidates:
        if key and key in ROOT_TRANSLATIONS:
            return ROOT_TRANSLATIONS[key]
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
            translated_parts.append(
                " ".join(_translate_token(p) for p in pieces)
            )
        else:
            translated_parts.append(raw_word)
        i += 1

    return " ".join(translated_parts)

def get_ru_name(name_en: str) -> str:
    key = name_en.lower().strip()
    if key in MERGED_COMPOUND_OVERRIDES:
        return MERGED_COMPOUND_OVERRIDES[key]
    return auto_translate_ru(name_en)

# --------------------------------------------------------------------------- #
# Парсинг
# --------------------------------------------------------------------------- #

STABILITY_MAP_RU = {
    "stable": "Стабилен",
    "doing well": "Растёт в цене",
    "fluctuating": "Нестабилен",
    "underpaid for": "Недооценён",
    "unstable": "Нестабилен",
    "hoarded": "Придерживают",
    "rising": "Растёт в цене",
    "dropping": "Падает в цене",
    "receding": "Снижается",
    "improving": "Улучшается",
}

STABILITY_FILTER_OPTIONS: list[tuple[str, str, str]] = [
    ("stable", "Стабилен", "🟢"),
    ("doing well", "Растёт в цене", "📈"),
    ("fluctuating", "Нестабилен", "🔀"),
    ("underpaid for", "Недооценён", "💎"),
    ("unstable", "Нестабилен", "⚠️"),
    ("hoarded", "Придерживают", "🧲"),
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
    clean = _strip_name_tags(display_name)
    clean = clean.replace("’", "'")

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
    first_word = plain_words[0] if plain_words else ""
    last_word = plain_words[-1] if plain_words else ""

    add(prefix + joined_nospace)
    add(prefix + last_word)
    add(prefix + first_word)
    if len(plain_words) >= 2:
        initials = "".join(w[0].upper() for w in plain_words if w)
        add(prefix + initials)
    if plain_words:
        for cut in (6, 5, 4, 3):
            if len(plain_words[0]) > cut:
                add(prefix + plain_words[0][:cut])
    add(joined_nospace)
    add(last_word)
    add(first_word)
    safe_name = re.sub(r"[^\w\s-]", "", clean).strip().replace(" ", "_")
    add(safe_name)
    add(safe_name.replace("_", ""))
    add("".join(w.capitalize() for w in plain_words))

    return [c for c in candidates if c]

def fetch_category(slug: str, rarity_label: str) -> list[Item]:
    target_url = f"{BASE_URL}/mm2/{slug}"
    api_url = f"https://api.scrapingant.com/v2/general?url={target_url}&x-api-key={SCRAPINGANT_API_KEY}&browser=true"

    request_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MM2ValuesBot/1.0)",
        "Accept": "application/json",
        "Connection": "close",
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
            f"Не удалось загрузить категорию '{slug}' после {MAX_RETRIES} попыток (последняя ошибка: {last_error})"
        )

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

        items.append(
            Item(
                name=display_name,
                category_slug=slug,
                rarity=rarity_label,
                value=value_int,
                value_display=value_display,
                ranged_value=None,
                stability=stability,
                image_url=image_url,
                image_url_candidates=image_candidates,
                origin=origin,
            )
        )

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
# Структура поиска и Кэш
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

            # 1. Оригинальное название (EN)
            keys_to_add.add(item.name)

            # 2. Переведенное название (RU)
            ru_name = get_ru_name(item.name)
            if ru_name:
                keys_to_add.add(ru_name)

            # 3. Ручные алиасы и синонимы
            item_key_lower = item.name.lower().strip()
            if item_key_lower in ITEM_ALIASES:
                for alias in ITEM_ALIASES[item_key_lower]:
                    keys_to_add.add(alias)

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
                raise RuntimeError("Парсинг вернул 0 предметов — проверьте API ключ или структуру сайта.")
            known = load_known_image_urls()
            for item in items:
                key = normalize_text(item.name)
                if key in known:
                    confirmed = known[key]
                    if confirmed not in item.image_url_candidates:
                        item.image_url_candidates.insert(0, confirmed)
                    item.image_url = confirmed
            with self._lock:
                self._items = items
                self._search_index = self._build_search_index(items)
                self.last_updated = time.time()
                self.last_error = None
            logger.info("Кэш обновлён: всего %d предметов.", len(items))
        except Exception as e:
            logger.exception("Ошибка обновления кэша")
            with self._lock:
                self.last_error = str(e)

    def search(self, query: str, limit: int = 5, filters: Optional["ItemFilters"] = None) -> list[tuple[Item, float]]:
        with self._lock:
            items = self._items
            index = list(self._search_index)
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

        for variant in variants:
            v_norm = normalize_text(variant)
            v_sorted = token_sorted_text(variant)
            if not v_norm:
                continue

            for entry in index:
                if allowed_idx is not None and entry.item_idx not in allowed_idx:
                    continue

                # 1. Точное совпадение строки или отсортированного порядка слов
                if v_norm == entry.key_norm or v_sorted == entry.key_sorted:
                    best_by_idx[entry.item_idx] = 100.0
                    continue

                # 2. Нечёткое сравнение токенов
                s1 = fuzz.token_sort_ratio(v_norm, entry.key_norm)
                s2 = fuzz.token_set_ratio(v_norm, entry.key_norm)
                s3 = fuzz.ratio(v_sorted, entry.key_sorted)
                score = max(s1, s2, s3)

                # Штрафуем разницу в длине, чтобы короткие запросы типа "лед" не перебивали "ледокрыло"
                len_diff = abs(len(v_norm) - len(entry.key_norm))
                if len_diff > 3 and score < 95.0:
                    score = max(0.0, score - len_diff * 3.0)

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

cache = ValuesCache()

# --------------------------------------------------------------------------- #
# Настройки и БД
# --------------------------------------------------------------------------- #

DEFAULT_LANG = "ru"
SUPPORTED_LANGS = {"ru": "Русский", "en": "English"}
_db_lock = threading.Lock()

def init_db() -> None:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, lang TEXT NOT NULL DEFAULT 'ru')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS global_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO global_settings (key, value) VALUES ('refresh_interval_days', '7')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_filters ("
            " user_id INTEGER PRIMARY KEY,"
            " min_value INTEGER NOT NULL DEFAULT 0,"
            " max_value INTEGER NOT NULL DEFAULT -1,"
            " rarity_slug TEXT NOT NULL DEFAULT 'all',"
            " stability_key TEXT NOT NULL DEFAULT 'all'"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS known_images ("
            " name_key TEXT PRIMARY KEY,"
            " url TEXT NOT NULL"
            ")"
        )
        conn.commit()

def get_user_lang(user_id: int) -> str:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT lang FROM user_settings WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else DEFAULT_LANG

def set_user_lang(user_id: int, lang: str) -> None:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, lang) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang",
            (user_id, lang),
        )
        conn.commit()

def get_refresh_interval_days() -> int:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT value FROM global_settings WHERE key = 'refresh_interval_days'")
        row = cur.fetchone()
        if row:
            try:
                return int(row[0])
            except ValueError:
                return 7
        return 7

def set_refresh_interval_days(days: int) -> None:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO global_settings (key, value) VALUES ('refresh_interval_days', ?)",
            (str(days),),
        )
        conn.commit()

def load_known_image_urls() -> dict[str, str]:
    try:
        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("SELECT name_key, url FROM known_images")
            return {row[0]: row[1] for row in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}

def save_known_image_url(name_key: str, url: str) -> None:
    try:
        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO known_images (name_key, url) VALUES (?, ?) "
                "ON CONFLICT(name_key) DO UPDATE SET url = excluded.url",
                (name_key, url),
            )
            conn.commit()
    except sqlite3.OperationalError:
        pass

# --------------------------------------------------------------------------- #
# Фильтры
# --------------------------------------------------------------------------- #

@dataclass
class ItemFilters:
    min_value: int = 0
    max_value: int = -1          # -1 = неограниченно
    rarity_slug: str = "all"     # "all" или slug категории
    stability_key: str = "all"   # "all" или нормализованный ключ стабильности

    @property
    def is_empty(self) -> bool:
        return (
            self.min_value == 0
            and self.max_value == -1
            and self.rarity_slug == "all"
            and self.stability_key == "all"
        )

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

def get_user_filters(user_id: int) -> ItemFilters:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT min_value, max_value, rarity_slug, stability_key FROM user_filters WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            return ItemFilters(min_value=row[0], max_value=row[1], rarity_slug=row[2], stability_key=row[3])
        return ItemFilters()

def set_user_filters(user_id: int, filters: ItemFilters) -> None:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO user_filters (user_id, min_value, max_value, rarity_slug, stability_key) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "min_value = excluded.min_value, max_value = excluded.max_value, "
            "rarity_slug = excluded.rarity_slug, stability_key = excluded.stability_key",
            (user_id, filters.min_value, filters.max_value, filters.rarity_slug, filters.stability_key),
        )
        conn.commit()

def reset_user_filters(user_id: int) -> None:
    set_user_filters(user_id, ItemFilters())

# --------------------------------------------------------------------------- #
# Локализация
# --------------------------------------------------------------------------- #

RARITY_RU_LABELS = {
    "godlies": "Godly",
    "chromas": "Chroma",
    "legendaries": "Legendary",
    "ancients": "Ancient",
    "vintages": "Vintage",
    "rares": "Rare",
    "uncommons": "Uncommon",
    "commons": "Common",
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

TEXTS["ru"].update({
    "start": (
        "👋 <b>Привет!</b> Я бот-оценщик ценности предметов Murder Mystery 2.\n\n"
        "✏️ Просто напиши название предмета на русском или английском "
        "(опечатки и порядок слов не страшны) — например: <i>Хрома пистолет путешественника</i>, "
        "<i>Ледокрыло</i> или <i>Nebula</i>.\n\n"
        "📋 <b>Команды:</b>\n"
        "🌐 /settings — язык интерфейса\n"
        "🎚 /filters — настроить фильтры поиска\n"
        "📜 /list — каталог всех предметов\n"
        "📊 /status — статус базы данных"
    ),
    "help": (
        "ℹ️ Напиши название предмета MM2 — я найду его ценность.\n\n"
        "📋 <b>Команды:</b>\n"
        "🌐 /settings — сменить язык интерфейса\n"
        "🎚 /filters — настроить фильтры (цена, редкость, стабильность)\n"
        "📜 /list — список всех предметов (дорогие → дешёвые)\n"
        "📊 /status — статус базы данных"
    ),
    "settings_title": "🌐 <b>Выберите язык интерфейса</b>",
    "settings_saved": "✅ Язык сохранён: <b>{lang_name}</b>",
    "not_found": (
        "😕 Ничего не найдено по запросу «{query}».\n"
        "✏️ Проверь написание или попробуй другое название предмета.\n"
        "🎚 Возможно, стоит проверить активные /filters."
    ),
    "searching": "🔎 Ищу...",
    "value_label": "Примерная стоимость",
    "status_label": "Категория",
    "stability_label": "Стабильность",
    "origin_label": "Событие",
    "unknown_stability": "Неизвестно",
    "cache_empty": "⏳ База данных ещё загружается, попробуй через минуту.",
    "status_report": (
        "📊 Предметов в базе: <b>{count}</b>\n"
        "🕒 Последнее обновление: <b>{last_update}</b>\n"
        "⚠️ Ошибка последнего обновления: <b>{error}</b>"
    ),
    "status_report_ok": (
        "📊 Предметов в базе: <b>{count}</b>\n"
        "🕒 Последнее обновление: <b>{last_update}</b>"
    ),
    "never": "ещё не обновлялось",
    "no_error": "нет",
    "admin_set_refresh": (
        "⚙️ Текущий интервал обновления: {days} дн.\n"
        "Используйте /setrefresh <число> чтобы изменить (от 1 до 90)."
    ),
    "admin_refresh_updated": "✅ Интервал обновления изменён на {days} дн.",
    "admin_refresh_invalid": "❌ Укажите целое число дней от 1 до 90.",
    "admin_only": "⛔ Эта команда доступна только администратору.",
})

TEXTS["ru"].update({
    "filters_title": "🎚 <b>Фильтры поиска</b>\n\nНастрой параметры и нажми «Применить».",
    "filters_btn_min": "💵 Валюта (от): {value}",
    "filters_btn_max": "💰 Валюта (до): {value}",
    "filters_btn_rarity": "🏷 Редкость: {value}",
    "filters_btn_stability": "📈 Стабильность: {value}",
    "filters_btn_apply": "✅ Применить",
    "filters_btn_reset": "♻️ Сбросить",
    "filters_unlimited": "∞ неограниченно",
    "filters_all": "все",
    "filters_ask_min": "✏️ Введи <b>минимальное</b> значение цены числом (например: 1000):",
    "filters_ask_max": "✏️ Введи <b>максимальное</b> значение цены числом, либо -1 для «неограниченно»:",
    "filters_invalid_number": "❌ Это не похоже на корректное число. Попробуй ещё раз:",
    "filters_invalid_range": "❌ Минимум не может быть больше максимума. Попробуй ещё раз:",
    "filters_invalid_negative": "❌ Значение не может быть отрицательным (кроме -1 для «неограниченно»). Попробуй ещё раз:",
    "filters_saved": "✅ Значение сохранено",
    "filters_applied": "✅ Фильтры применены!",
    "filters_rarity_title": "🏷 <b>Выберите редкость</b>",
    "filters_stability_title": "📈 <b>Выберите стабильность</b>",
    "filters_option_all": "✅ Все",
    "list_title": "📜 <b>Каталог предметов</b> (дорогие → дешёвые)",
    "list_empty": "😕 По заданным фильтрам ничего не найдено. Проверь /filters.",
    "list_nav_page": "📄 {page}/{total}",
})

TEXTS["en"].update({
    "start": (
        "👋 <b>Hi!</b> I'm a Murder Mystery 2 item value checker bot.\n\n"
        "✏️ Just type an item name in English or Russian — "
        "for example: <i>Chroma Traveler's Gun</i>, <i>Icewing</i>.\n\n"
        "📋 <b>Commands:</b>\n"
        "🌐 /settings — interface language\n"
        "🎚 /filters — configure search filters\n"
        "📜 /list — item catalog\n"
        "📊 /status — database status"
    ),
    "help": (
        "ℹ️ Type an MM2 item name — I'll find its value.\n\n"
        "📋 <b>Commands:</b>\n"
        "🌐 /settings — change interface language\n"
        "🎚 /filters — configure filters\n"
        "📜 /list — list of all items\n"
        "📊 /status — database status"
    ),
    "settings_title": "🌐 <b>Choose interface language</b>",
    "settings_saved": "✅ Language saved: <b>{lang_name}</b>",
    "not_found": (
        "😕 Nothing found for «{query}».\n"
        "✏️ Check the spelling or try another query.\n"
        "🎚 You may also want to check your active /filters."
    ),
    "searching": "🔎 Searching...",
    "value_label": "Estimated Value",
    "status_label": "Category",
    "stability_label": "Stability",
    "origin_label": "Origin",
    "unknown_stability": "Unknown",
    "cache_empty": "⏳ Database is still loading, please try again in a minute.",
    "status_report": (
        "📊 Items in database: <b>{count}</b>\n"
        "🕒 Last update: <b>{last_update}</b>\n"
        "⚠️ Last update error: <b>{error}</b>"
    ),
    "status_report_ok": (
        "📊 Items in database: <b>{count}</b>\n"
        "🕒 Last update: <b>{last_update}</b>"
    ),
    "never": "not updated yet",
    "no_error": "none",
    "admin_set_refresh": (
        "⚙️ Current refresh interval: {days} days.\n"
        "Use /setrefresh <number> to change (1–90)."
    ),
    "admin_refresh_updated": "✅ Refresh interval set to {days} days.",
    "admin_refresh_invalid": "❌ Please enter an integer from 1 to 90.",
    "admin_only": "⛔ This command is for the administrator only.",
    "filters_title": "🎚 <b>Search filters</b>\n\nAdjust the parameters and press \"Apply\".",
    "filters_btn_min": "💵 Currency (from): {value}",
    "filters_btn_max": "💰 Currency (to): {value}",
    "filters_btn_rarity": "🏷 Rarity: {value}",
    "filters_btn_stability": "📈 Stability: {value}",
    "filters_btn_apply": "✅ Apply",
    "filters_btn_reset": "♻️ Reset",
    "filters_unlimited": "∞ unlimited",
    "filters_all": "all",
    "filters_ask_min": "✏️ Enter minimum price as a number:",
    "filters_ask_max": "✏️ Enter maximum price as a number, or -1 for unlimited:",
    "filters_invalid_number": "❌ Invalid number. Try again:",
    "filters_invalid_range": "❌ Minimum can't be greater than maximum:",
    "filters_invalid_negative": "❌ Value can't be negative:",
    "filters_saved": "✅ Value saved",
    "filters_applied": "✅ Filters applied!",
    "filters_rarity_title": "🏷 <b>Choose rarity</b>",
    "filters_stability_title": "📈 <b>Choose stability</b>",
    "filters_option_all": "✅ All",
    "list_title": "📜 <b>Item catalog</b>",
    "list_empty": "😕 Nothing matches your filters.",
    "list_nav_page": "📄 {page}/{total}",
})

def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else DEFAULT_LANG
    template = TEXTS[lang].get(key, TEXTS[DEFAULT_LANG].get(key, key))
    return template.format(**kwargs) if kwargs else template

# --------------------------------------------------------------------------- #
# Шрифты
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

# --------------------------------------------------------------------------- #
# Скачивание картинок
# --------------------------------------------------------------------------- #

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

def download_item_image(item: Item) -> Optional[Image.Image]:
    candidates = list(item.image_url_candidates) or ([item.image_url] if item.image_url else [])
    for url in candidates:
        img = _try_download_single(url)
        if img is not None:
            if url != item.image_url:
                item.image_url = url
            save_known_image_url(normalize_text(item.name), url)
            return img
    return None

# --------------------------------------------------------------------------- #
# Генерация изображения и подписи
# --------------------------------------------------------------------------- #

CARD_W, CARD_H = 800, 800

RARITY_GRADIENTS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "godlies":     ((255, 196, 64), (120, 40, 140)),
    "chromas":     ((255, 90, 205), (70, 60, 255)),
    "legendaries": ((255, 140, 60), (140, 30, 30)),
    "ancients":    ((170, 100, 255), (40, 20, 90)),
    "vintages":    ((190, 150, 110), (60, 40, 30)),
    "rares":       ((80, 150, 255), (20, 40, 110)),
    "uncommons":   ((80, 220, 140), (10, 60, 50)),
    "commons":     ((190, 200, 210), (60, 60, 70)),
}
DEFAULT_GRADIENT = ((90, 90, 140), (20, 20, 40))

RARITY_ACCENT: dict[str, tuple[int, int, int]] = {
    "godlies":     (255, 210, 90),
    "chromas":     (255, 110, 220),
    "legendaries": (255, 150, 70),
    "ancients":    (190, 130, 255),
    "vintages":    (205, 170, 130),
    "rares":       (110, 170, 255),
    "uncommons":   (100, 230, 160),
    "commons":     (210, 215, 225),
}

def _lerp_color(c1: tuple[int, int, int], c2: tuple[int, int, int], t_: float) -> tuple[int, int, int]:
    return (
        int(c1[0] + (c2[0] - c1[0]) * t_),
        int(c1[1] + (c2[1] - c1[1]) * t_),
        int(c1[2] + (c2[2] - c1[2]) * t_),
    )

def _make_mesh_background(width: int, height: int, slug: str) -> Image.Image:
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
    """Генерирует минималистичную картинку предмета на градиентном фоне без текста."""
    width, height = CARD_W, CARD_H
    slug = item.category_slug

    canvas = _make_mesh_background(width, height, slug)
    item_img = download_item_image(item)

    panel_center_x = width // 2
    panel_center_y = height // 2

    if item_img:
        max_size = 540
        ratio = min(max_size / item_img.width, max_size / item_img.height, 1.0)
        new_w = max(1, int(item_img.width * ratio))
        new_h = max(1, int(item_img.height * ratio))
        item_img = item_img.resize((new_w, new_h), Image.LANCZOS)

        # Тень под предметом
        shadow = Image.new("RGBA", (new_w + 80, new_h + 80), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        sdraw.ellipse([20, new_h - 10, new_w + 60, new_h + 70], fill=(0, 0, 0, 100))
        shadow = shadow.filter(ImageFilter.GaussianBlur(25))

        canvas.paste(
            shadow,
            (panel_center_x - (new_w + 80) // 2, panel_center_y - new_h // 2 - 10),
            shadow,
        )
        canvas.paste(
            item_img,
            (panel_center_x - new_w // 2, panel_center_y - new_h // 2),
            item_img,
        )
    else:
        draw = ImageDraw.Draw(canvas)
        draw.text(
            (panel_center_x, panel_center_y - 20), "🖼", anchor="mm", font=get_font(110, "regular"),
            fill=(255, 255, 255, 140),
        )
        draw.text(
            (panel_center_x, panel_center_y + 80), "Нет фото" if lang == "ru" else "No image",
            anchor="mm", font=get_font(28, "medium"), fill=(255, 255, 255, 160),
        )

    bio = io.BytesIO()
    canvas.convert("RGB").save(bio, format="JPEG", quality=95)
    bio.seek(0)
    return bio

def format_item_caption(item: Item, lang: str) -> str:
    """Форматирует эстетичную подпись к изображению товара."""
    emoji = RARITY_EMOJI.get(item.category_slug, "🟡")
    rarity_txt = rarity_label_localized(lang, item.category_slug)
    stab_txt = localized_stability(lang, item.stability) if item.stability else t(lang, "unknown_stability")

    name_en = item.name or "???"
    name_ru = get_ru_name(item.name) if (lang == "ru" and item.name) else ""

    if name_ru and normalize_text(name_ru) != normalize_text(name_en):
        title = f"<b>{html.escape(name_en)}</b> (<i>{html.escape(name_ru)}</i>)"
    else:
        title = f"<b>{html.escape(name_en)}</b>"

    lines = [
        f"✨ {title}",
        f"━━━━━━━━━━━━━━━━━━",
        f"💰 <b>{t(lang, 'value_label')}:</b> ⛁ <b>{item.value_display or 'N/A'}</b>",
        f"🏷 <b>{t(lang, 'status_label')}:</b> {emoji} {rarity_txt}",
        f"📈 <b>{t(lang, 'stability_label')}:</b> {stab_txt}",
    ]
    if item.origin:
        lines.append(f"🎁 <b>{t(lang, 'origin_label')}:</b> {html.escape(item.origin)}")

    return "\n".join(lines)

# --------------------------------------------------------------------------- #
# Вспомогательные функции UI
# --------------------------------------------------------------------------- #

LIST_PAGE_SIZE = 8

def _filters_summary_value(lang: str, filters: ItemFilters, kind: str) -> str:
    if kind == "min":
        return str(filters.min_value) if filters.min_value else "0"
    if kind == "max":
        return t(lang, "filters_unlimited") if filters.max_value == -1 else str(filters.max_value)
    if kind == "rarity":
        if filters.rarity_slug == "all":
            return t(lang, "filters_all")
        return rarity_label_localized(lang, filters.rarity_slug)
    if kind == "stability":
        if filters.stability_key == "all":
            return t(lang, "filters_all")
        return stability_label(lang, filters.stability_key)
    return ""

def build_filters_keyboard(lang: str, filters: ItemFilters) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            t(lang, "filters_btn_min", value=_filters_summary_value(lang, filters, "min")),
            callback_data="filt:ask_min",
        )],
        [InlineKeyboardButton(
            t(lang, "filters_btn_max", value=_filters_summary_value(lang, filters, "max")),
            callback_data="filt:ask_max",
        )],
        [InlineKeyboardButton(
            t(lang, "filters_btn_rarity", value=_filters_summary_value(lang, filters, "rarity")),
            callback_data="filt:rarity_menu",
        )],
        [InlineKeyboardButton(
            t(lang, "filters_btn_stability", value=_filters_summary_value(lang, filters, "stability")),
            callback_data="filt:stability_menu",
        )],
        [
            InlineKeyboardButton(t(lang, "filters_btn_reset"), callback_data="filt:reset"),
            InlineKeyboardButton(t(lang, "filters_btn_apply"), callback_data="filt:apply"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def build_rarity_menu_keyboard(lang: str, filters: ItemFilters) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        ("✅ " if filters.rarity_slug == "all" else "") + t(lang, "filters_option_all"),
        callback_data="filt:set_rarity:all",
    )]]
    for slug, _label, emoji in CATEGORIES:
        mark = "✅ " if filters.rarity_slug == slug else ""
        rows.append([InlineKeyboardButton(
            f"{mark}{emoji} {rarity_label_localized(lang, slug)}",
            callback_data=f"filt:set_rarity:{slug}",
        )])
    rows.append([InlineKeyboardButton("⬅️", callback_data="filt:back")])
    return InlineKeyboardMarkup(rows)

def build_stability_menu_keyboard(lang: str, filters: ItemFilters) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        ("✅ " if filters.stability_key == "all" else "") + t(lang, "filters_option_all"),
        callback_data="filt:set_stability:all",
    )]]
    for key, ru_label, emoji in STABILITY_FILTER_OPTIONS:
        label = ru_label if lang == "ru" else key.title()
        mark = "✅ " if filters.stability_key == key else ""
        rows.append([InlineKeyboardButton(
            f"{mark}{emoji} {label}",
            callback_data=f"filt:set_stability:{key}",
        )])
    rows.append([InlineKeyboardButton("⬅️", callback_data="filt:back")])
    return InlineKeyboardMarkup(rows)

def build_list_keyboard(lang: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"list:page:{page - 1}"))
    nav.append(InlineKeyboardButton(
        t(lang, "list_nav_page", page=page + 1, total=max(total_pages, 1)),
        callback_data="list:noop",
    ))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"list:page:{page + 1}"))
    return InlineKeyboardMarkup([nav])

def render_list_page_text(lang: str, items: list[Item], page: int, total_pages: int) -> str:
    start = page * LIST_PAGE_SIZE
    page_items = items[start:start + LIST_PAGE_SIZE]
    lines = [t(lang, "list_title"), ""]
    for i, item in enumerate(page_items, start=start + 1):
        emoji = RARITY_EMOJI.get(item.category_slug, "•")
        value = item.value_display or "N/A"
        lines.append(f"{i}. {emoji} <b>{html.escape(item.name)}</b> — ⛁ {value}")
    return "\n".join(lines)

async def send_item_card(update: Update, context: ContextTypes.DEFAULT_TYPE, item: Item, lang: str) -> None:
    chat_id = update.effective_chat.id
    caption = format_item_caption(item, lang)
    try:
        photo = create_item_image(item, lang)
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        logger.exception("Не удалось отправить изображение для '%s'", item.name)
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML)

# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "start"), parse_mode=ParseMode.HTML)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "help"), parse_mode=ParseMode.HTML)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    rows = [
        [InlineKeyboardButton(name, callback_data=f"setlang:{code}")]
        for code, name in SUPPORTED_LANGS.items()
    ]
    await update.message.reply_text(
        t(lang, "settings_title"), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    count = cache.size
    last_update = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cache.last_updated))
        if cache.last_updated else t(lang, "never")
    )
    if cache.last_error:
        text = t(lang, "status_report", count=count, last_update=last_update, error=cache.last_error)
    else:
        text = t(lang, "status_report_ok", count=count, last_update=last_update)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def setrefresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(t(lang, "admin_only"))
        return
    args = context.args
    if not args:
        days = get_refresh_interval_days()
        await update.message.reply_text(t(lang, "admin_set_refresh", days=days))
        return
    try:
        days = int(args[0])
        if not (1 <= days <= 90):
            raise ValueError
    except ValueError:
        await update.message.reply_text(t(lang, "admin_refresh_invalid"))
        return
    set_refresh_interval_days(days)
    await update.message.reply_text(t(lang, "admin_refresh_updated", days=days))

async def filters_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    context.user_data.pop("awaiting_filter_input", None)
    filters_obj = get_user_filters(user_id)
    await update.message.reply_text(
        t(lang, "filters_title"), parse_mode=ParseMode.HTML,
        reply_markup=build_filters_keyboard(lang, filters_obj),
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    if cache.size == 0:
        await update.message.reply_text(t(lang, "cache_empty"))
        return
    filters_obj = get_user_filters(user_id)
    items = cache.all_items(filters_obj)
    if not items:
        await update.message.reply_text(t(lang, "list_empty"), parse_mode=ParseMode.HTML)
        return
    total_pages = max(1, math.ceil(len(items) / LIST_PAGE_SIZE))
    page = 0
    text = render_list_page_text(lang, items, page, total_pages)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=build_list_keyboard(lang, page, total_pages),
    )

async def search_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    query = (update.message.text or "").strip()
    if not query:
        return

    awaiting = context.user_data.get("awaiting_filter_input")
    if awaiting in ("min", "max"):
        try:
            value = int(query.strip())
        except ValueError:
            await update.message.reply_text(t(lang, "filters_invalid_number"))
            return
        filters_obj = get_user_filters(user_id)
        if awaiting == "min":
            if value < 0:
                await update.message.reply_text(t(lang, "filters_invalid_negative"))
                return
            if filters_obj.max_value != -1 and value > filters_obj.max_value:
                await update.message.reply_text(t(lang, "filters_invalid_range"))
                return
            filters_obj.min_value = value
        else:
            if value != -1 and value < 0:
                await update.message.reply_text(t(lang, "filters_invalid_negative"))
                return
            if value != -1 and value < filters_obj.min_value:
                await update.message.reply_text(t(lang, "filters_invalid_range"))
                return
            filters_obj.max_value = value
        set_user_filters(user_id, filters_obj)
        context.user_data.pop("awaiting_filter_input", None)
        await update.message.reply_text(
            t(lang, "filters_title"), parse_mode=ParseMode.HTML,
            reply_markup=build_filters_keyboard(lang, filters_obj),
        )
        return

    if cache.size == 0:
        await update.message.reply_text(t(lang, "cache_empty"))
        return

    filters_obj = get_user_filters(user_id)
    results = cache.search(query, limit=5, filters=filters_obj)
    if not results:
        await update.message.reply_text(
            t(lang, "not_found", query=html.escape(query)), parse_mode=ParseMode.HTML,
        )
        return

    best_item, _score = results[0]
    await send_item_card(update, context, best_item, lang)

# --------------------------------------------------------------------------- #
# Callback Query
# --------------------------------------------------------------------------- #

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    data = query.data or ""

    try:
        if data.startswith("setlang:"):
            new_lang = data.split(":", 1)[1]
            if new_lang in SUPPORTED_LANGS:
                set_user_lang(user_id, new_lang)
                lang = new_lang
                await query.answer()
                await query.edit_message_text(
                    t(lang, "settings_saved", lang_name=SUPPORTED_LANGS[new_lang]),
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.answer()
            return

        if data.startswith("filt:"):
            action = data[len("filt:"):]
            filters_obj = get_user_filters(user_id)

            if action == "ask_min":
                context.user_data["awaiting_filter_input"] = "min"
                await query.answer()
                await query.edit_message_text(t(lang, "filters_ask_min"), parse_mode=ParseMode.HTML)
                return

            if action == "ask_max":
                context.user_data["awaiting_filter_input"] = "max"
                await query.answer()
                await query.edit_message_text(t(lang, "filters_ask_max"), parse_mode=ParseMode.HTML)
                return

            if action == "rarity_menu":
                await query.answer()
                await query.edit_message_text(
                    t(lang, "filters_rarity_title"), parse_mode=ParseMode.HTML,
                    reply_markup=build_rarity_menu_keyboard(lang, filters_obj),
                )
                return

            if action == "stability_menu":
                await query.answer()
                await query.edit_message_text(
                    t(lang, "filters_stability_title"), parse_mode=ParseMode.HTML,
                    reply_markup=build_stability_menu_keyboard(lang, filters_obj),
                )
                return

            if action.startswith("set_rarity:"):
                slug = action.split(":", 1)[1]
                filters_obj.rarity_slug = slug
                set_user_filters(user_id, filters_obj)
                await query.answer(t(lang, "filters_saved"))
                await query.edit_message_text(
                    t(lang, "filters_title"), parse_mode=ParseMode.HTML,
                    reply_markup=build_filters_keyboard(lang, filters_obj),
                )
                return

            if action.startswith("set_stability:"):
                key = action.split(":", 1)[1]
                filters_obj.stability_key = key
                set_user_filters(user_id, filters_obj)
                await query.answer(t(lang, "filters_saved"))
                await query.edit_message_text(
                    t(lang, "filters_title"), parse_mode=ParseMode.HTML,
                    reply_markup=build_filters_keyboard(lang, filters_obj),
                )
                return

            if action == "reset":
                reset_user_filters(user_id)
                context.user_data.pop("awaiting_filter_input", None)
                filters_obj = get_user_filters(user_id)
                await query.answer(t(lang, "filters_saved"))
                await query.edit_message_text(
                    t(lang, "filters_title"), parse_mode=ParseMode.HTML,
                    reply_markup=build_filters_keyboard(lang, filters_obj),
                )
                return

            if action == "apply":
                context.user_data.pop("awaiting_filter_input", None)
                await query.answer(t(lang, "filters_applied"))
                await query.edit_message_text(
                    t(lang, "filters_applied"), parse_mode=ParseMode.HTML,
                )
                return

            if action == "back":
                await query.answer()
                await query.edit_message_text(
                    t(lang, "filters_title"), parse_mode=ParseMode.HTML,
                    reply_markup=build_filters_keyboard(lang, filters_obj),
                )
                return

            await query.answer()
            return

        if data.startswith("list:"):
            action = data[len("list:"):]
            if action == "noop":
                await query.answer()
                return
            if action.startswith("page:"):
                page = int(action.split(":", 1)[1])
                filters_obj = get_user_filters(user_id)
                items = cache.all_items(filters_obj)
                total_pages = max(1, math.ceil(len(items) / LIST_PAGE_SIZE))
                page = max(0, min(page, total_pages - 1))
                text = render_list_page_text(lang, items, page, total_pages)
                await query.answer()
                await query.edit_message_text(
                    text, parse_mode=ParseMode.HTML,
                    reply_markup=build_list_keyboard(lang, page, total_pages),
                )
                return
            await query.answer()
            return

        await query.answer()
    except BadRequest as e:
        logger.warning("BadRequest в callback_query_handler: %s", e)
        try:
            await query.answer()
        except Exception:
            pass
    except Exception:
        logger.exception("Ошибка в обработке callback_query")
        try:
            await query.answer()
        except Exception:
            pass

# --------------------------------------------------------------------------- #
# Инициализация и сервер
# --------------------------------------------------------------------------- #

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Необработанное исключение при обработке апдейта", exc_info=context.error)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def run_health_check_server():
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, HealthCheckHandler)
    logger.info("Health check HTTP-сервер запущен на порту %d", PORT)
    httpd.serve_forever()

if __name__ == "__main__":
    init_db()
    ensure_fonts_downloaded()

    threading.Thread(target=cache.refresh, daemon=True).start()
    threading.Thread(target=run_health_check_server, daemon=True).start()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        cache.refresh,
        "interval",
        days=get_refresh_interval_days(),
        id="cache_refresh",
    )
    scheduler.start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("setrefresh", setrefresh_command))
    application.add_handler(CommandHandler("filters", filters_command))
    application.add_handler(CommandHandler("list", list_command))

    application.add_handler(CallbackQueryHandler(callback_query_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_text_message))

    application.add_error_handler(error_handler)

    logger.info("Запуск Telegram бота (polling)...")
    application.run_polling(drop_pending_updates=True)
