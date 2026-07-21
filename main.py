"""
MM2 Values Telegram Bot (v3.0.0) — "Liquid Glass" Edition
===========================================================
Изменения относительно предыдущей версии:
- Полностью переработанный дизайн карточек предметов: Glassmorphism
  (liquid glass), градиентные фоны, мягкие тени, скруглённые панели,
  качественная типографика на шрифте Inter (с авто-загрузкой шрифта).
- Надёжный поиск изображений предметов: приоритет отдаётся реальному
  src из HTML (там уже лежат официальные короткие имена файлов вида
  CEvergreen.png / Snowflake.png), плюс большой набор эвристик и
  запасных вариантов имени файла на случай отсутствия src.
- Автоматический перенос длинных названий на несколько строк, чтобы
  текст никогда не вылезал за пределы изображения.
- Command /filters — гибкая система фильтров (диапазон цены, редкость,
  стабильность) с инлайн-кнопками, применяется и к ручному вводу,
  и к команде /list.
- Command /list — постраничный каталог всех предметов (дорогие -> дешёвые)
  с инлайн-кнопками, учитывает активные фильтры.
"""

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
from rapidfuzz import fuzz, process
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
    text = re.sub(r"[^a-zа-я0-9]+", "", text)
    return text

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
# Автоматический перевод названий
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
    "ice": "Лёд", "iceflake": "Ледяная снежинка", "icewing": "Ледяное крыло",
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
    "icebreaker": "Ледокол",
    "icecrusher": "Ледокрушитель",
    "icepiercer": "Ледопронзатель",
    "iceblaster": "Ледяной бластер",
    "icebeam": "Ледяной луч",
    "iceflake": "Ледяная снежинка",
    "icewing": "Ледяное крыло",
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

# Канонический порядок фильтра "Стабильность" + эмодзи для UI.
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
    """Приводит относительный/сырой src из HTML к абсолютному URL."""
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

# Слова, которые сайт обычно отбрасывает при генерации коротких кодов
# изображений (артикли, служебные слова).
_IMG_STOPWORDS = {"the", "a", "an", "of"}

_BRACKET_TAG_RE = re.compile(r"\s*[\[\(][^\]\)]*[\]\)]\s*")

def _strip_name_tags(name: str) -> str:
    """Убирает хвостовые пометки вида [XMAS2018], (Knife) из названия."""
    return _BRACKET_TAG_RE.sub(" ", name).strip()

def guess_image_filenames(display_name: str) -> list[str]:
    """
    Строит список правдоподобных вариантов имени файла изображения на
    основе одних только эвристик (используется как РЕЗЕРВ, когда
    реальный src из HTML недоступен или ведёт на несуществующий файл).

    supremevalues.com сокращает длинные имена по неформальным правилам:
        Chroma Evergreen              -> CEvergreen.png
        Chroma Traveler's Gun         -> CTG.png
        Snowflake (Knife) [XMAS2018]  -> Snowflake.png
        Chroma Snow Dagger            -> CDagger.png
        Chroma Vampire's Gun          -> CVG.png

    Общая идея: "Chroma"/"C." в начале почти всегда схлопывается в "C",
    скобочные пометки вида (Knife), [XMAS2018] отбрасываются целиком,
    а из оставшихся слов берётся либо последнее слово целиком, либо
    аббревиатура по первым буквам. Однозначного правила нет, поэтому
    генерируется НЕСКОЛЬКО кандидатов, которые затем перебираются по
    очереди при скачивании.
    """
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

    add(prefix + joined_nospace)                      # CEvergreen / CSnowDagger
    add(prefix + last_word)                            # CDagger / CBaub
    add(prefix + first_word)                            # CSnow
    if len(plain_words) >= 2:
        initials = "".join(w[0].upper() for w in plain_words if w)
        add(prefix + initials)                          # CTG / CVG / CConst
    if plain_words:
        for cut in (6, 5, 4, 3):
            if len(plain_words[0]) > cut:
                add(prefix + plain_words[0][:cut])       # CConst / CSnowst
    add(joined_nospace)                                 # без префикса вообще
    add(last_word)
    add(first_word)
    safe_name = re.sub(r"[^\w\s-]", "", clean).strip().replace(" ", "_")
    add(safe_name)
    add(safe_name.replace("_", ""))
    add("".join(w.capitalize() for w in plain_words))    # PascalCase

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

        # ---------------------------------------------------------------- #
        # Извлечение изображения (по убыванию приоритета):
        #  1. Реальный src <img> из карточки — сайт-сгенерированный
        #     короткий путь (например CEvergreen.png), почти всегда верен.
        #  2. Эвристически построенные имена файлов как резервные
        #     кандидаты — перебираются при скачивании, если первый
        #     вариант не загрузится.
        # ---------------------------------------------------------------- #
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
# Кэш
# --------------------------------------------------------------------------- #

class ValuesCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Item] = []
        self._search_index: list[tuple[str, int]] = []
        self.last_updated: float = 0.0
        self.last_error: Optional[str] = None

    def _build_search_index(self, items: list[Item]) -> list[tuple[str, int]]:
        index = []
        for idx, item in enumerate(items):
            en_key = item.search_key
            if en_key:
                index.append((en_key, idx))
            ru_name = get_ru_name(item.name)
            ru_key = normalize_text(ru_name)
            if ru_key and ru_key != en_key:
                index.append((ru_key, idx))
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

        choices = [key for key, _ in index]
        best_by_idx: dict[int, float] = {}

        for variant in variants:
            for key, idx in index:
                if allowed_idx is not None and idx not in allowed_idx:
                    continue
                if key == variant:
                    best_by_idx[idx] = 100.0
            results = process.extract(variant, choices, scorer=fuzz.WRatio, limit=limit * 6)
            for matched_key, score, pos in results:
                idx = index[pos][1]
                if allowed_idx is not None and idx not in allowed_idx:
                    continue
                if score > best_by_idx.get(idx, -1):
                    best_by_idx[idx] = score

        if not best_by_idx:
            return []

        ranked = sorted(best_by_idx.items(), key=lambda kv: kv[1], reverse=True)
        THRESHOLD = 62.0
        result: list[tuple[Item, float]] = []
        for idx, score in ranked:
            if score < THRESHOLD:
                continue
            result.append((items[idx], score))
            if len(result) >= limit:
                break
        return result

    def all_items(self, filters: Optional["ItemFilters"] = None) -> list[Item]:
        """Все предметы от самых дорогих к самым дешёвым, с учётом фильтров.
        Предметы без числовой цены — в конце списка."""
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
# Локализация редкости/стабильности
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
        "(опечатки не страшны) — например: <i>Nebula</i>, <i>Туманность</i> "
        "или даже <i>тумпннлсть</i>.\n\n"
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
        "📊 /status — статус базы данных (когда обновлялась)"
    ),
    "settings_title": "🌐 <b>Выберите язык интерфейса</b>",
    "settings_saved": "✅ Язык сохранён: <b>{lang_name}</b>",
    "not_found": (
        "😕 Ничего не найдено по запросу «{query}».\n"
        "✏️ Проверь написание или попробуй английское название предмета.\n"
        "🎚 Возможно, стоит проверить активные /filters."
    ),
    "searching": "🔎 Ищу...",
    "value_label": "💰 Примерная стоимость",
    "status_label": "🏷 Категория",
    "stability_label": "📈 Стабильность",
    "origin_label": "🎁 Событие",
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
        "✏️ Just type an item name in English or Russian (typos are fine) — "
        "for example: <i>Nebula</i>, <i>Icewing</i>.\n\n"
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
        "🎚 /filters — configure filters (price, rarity, stability)\n"
        "📜 /list — list of all items (expensive → cheap)\n"
        "📊 /status — database status (last update time)"
    ),
    "settings_title": "🌐 <b>Choose interface language</b>",
    "settings_saved": "✅ Language saved: <b>{lang_name}</b>",
    "not_found": (
        "😕 Nothing found for «{query}».\n"
        "✏️ Check the spelling or try the item's other-language name.\n"
        "🎚 You may also want to check your active /filters."
    ),
    "searching": "🔎 Searching...",
    "value_label": "💰 Estimated value",
    "status_label": "🏷 Category",
    "stability_label": "📈 Stability",
    "origin_label": "🎁 Origin",
    "unknown_stability": "Unknown",
    "cache_empty": "⏳ Database is still loading, please try again in a minute.",
})

TEXTS["en"].update({
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
})

TEXTS["en"].update({
    "filters_title": "🎚 <b>Search filters</b>\n\nAdjust the parameters and press \"Apply\".",
    "filters_btn_min": "💵 Currency (from): {value}",
    "filters_btn_max": "💰 Currency (to): {value}",
    "filters_btn_rarity": "🏷 Rarity: {value}",
    "filters_btn_stability": "📈 Stability: {value}",
    "filters_btn_apply": "✅ Apply",
    "filters_btn_reset": "♻️ Reset",
    "filters_unlimited": "∞ unlimited",
    "filters_all": "all",
    "filters_ask_min": "✏️ Enter the <b>minimum</b> price as a number (e.g. 1000):",
    "filters_ask_max": "✏️ Enter the <b>maximum</b> price as a number, or -1 for \"unlimited\":",
    "filters_invalid_number": "❌ That doesn't look like a valid number. Try again:",
    "filters_invalid_range": "❌ Minimum can't be greater than maximum. Try again:",
    "filters_invalid_negative": "❌ Value can't be negative (except -1 for \"unlimited\"). Try again:",
    "filters_saved": "✅ Value saved",
    "filters_applied": "✅ Filters applied!",
    "filters_rarity_title": "🏷 <b>Choose rarity</b>",
    "filters_stability_title": "📈 <b>Choose stability</b>",
    "filters_option_all": "✅ All",
    "list_title": "📜 <b>Item catalog</b> (expensive → cheap)",
    "list_empty": "😕 Nothing matches your filters. Check /filters.",
    "list_nav_page": "📄 {page}/{total}",
})

def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else DEFAULT_LANG
    template = TEXTS[lang].get(key, TEXTS[DEFAULT_LANG].get(key, key))
    return template.format(**kwargs) if kwargs else template

# --------------------------------------------------------------------------- #
# Шрифты (Inter, с автозагрузкой + запасным вариантом)
# --------------------------------------------------------------------------- #

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
os.makedirs(FONTS_DIR, exist_ok=True)

# Официальный репозиторий Google Fonts — стабильный публичный источник.
_INTER_SOURCES = {
    "Inter-Regular.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/static/Inter-Regular.ttf",
    "Inter-Medium.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/static/Inter-Medium.ttf",
    "Inter-SemiBold.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/static/Inter-SemiBold.ttf",
    "Inter-Bold.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/static/Inter-Bold.ttf",
    "Inter-ExtraBold.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/static/Inter-ExtraBold.ttf",
    "Inter-Black.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/static/Inter-Black.ttf",
}

_FALLBACK_FONT_MAP = {
    "Inter-Regular.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "Inter-Medium.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "Inter-SemiBold.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "Inter-Bold.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "Inter-ExtraBold.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "Inter-Black.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_fonts_ready = threading.Event()

def ensure_fonts_downloaded() -> None:
    """Скачивает шрифты Inter один раз при старте бота. При любой ошибке
    сети просто оставляет системный DejaVu — рендер не сломается."""
    for filename, url in _INTER_SOURCES.items():
        dest = os.path.join(FONTS_DIR, filename)
        if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
            continue
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200 and len(resp.content) > 10_000:
                with open(dest, "wb") as f:
                    f.write(resp.content)
                logger.info("Шрифт %s загружен (%d байт)", filename, len(resp.content))
            else:
                logger.warning("Не удалось скачать шрифт %s: HTTP %s", filename, resp.status_code)
        except Exception as e:
            logger.warning("Ошибка загрузки шрифта %s: %s", filename, e)
    _fonts_ready.set()

def _font_path(weight_file: str) -> str:
    local_path = os.path.join(FONTS_DIR, weight_file)
    if os.path.exists(local_path) and os.path.getsize(local_path) > 10_000:
        return local_path
    return _FALLBACK_FONT_MAP.get(weight_file, _FALLBACK_FONT_MAP["Inter-Regular.ttf"])

def get_font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    weight_file = {
        "regular": "Inter-Regular.ttf",
        "medium": "Inter-Medium.ttf",
        "semibold": "Inter-SemiBold.ttf",
        "bold": "Inter-Bold.ttf",
        "extrabold": "Inter-ExtraBold.ttf",
        "black": "Inter-Black.ttf",
    }.get(weight, "Inter-Regular.ttf")

    cache_key = (weight_file, size)
    if cache_key in _font_cache:
        return _font_cache[cache_key]

    path = _font_path(weight_file)
    try:
        font = ImageFont.truetype(path, size)
    except OSError:
        logger.warning("Не удалось загрузить шрифт %s, использую стандартный.", path)
        font = ImageFont.load_default()
    _font_cache[cache_key] = font
    return font

# --------------------------------------------------------------------------- #
# Загрузка изображений предметов
# --------------------------------------------------------------------------- #

_IMG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://supremevalues.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

# Небольшой процессный кэш "URL -> сработал ли" чтобы не пробивать одни и
# те же битые ссылки повторно в рамках сессии.
_url_status_cache: dict[str, bool] = {}

def _try_download_single(url: str) -> Optional[Image.Image]:
    if not url:
        return None
    if _url_status_cache.get(url) is False:
        return None
    try:
        resp = requests.get(url, headers=_IMG_HEADERS, timeout=12)
        if resp.status_code != 200:
            _url_status_cache[url] = False
            return None
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/") and len(resp.content) < 100:
            _url_status_cache[url] = False
            return None
        if len(resp.content) < 80:
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
    """
    Перебирает все кандидаты URL изображения для предмета (реальный src
    из HTML первым, затем эвристические варианты коротких имён файлов) и
    возвращает первое успешно загруженное изображение. При успехе
    запоминает рабочий URL в БД, чтобы при следующем обновлении кэша он
    сразу оказался первым кандидатом.
    """
    candidates = list(item.image_url_candidates) or ([item.image_url] if item.image_url else [])
    if not candidates:
        return None

    # Также пробуем вариант с подчёркиваниями вместо пробелов для
    # каждого кандидата — на случай экзотических путей.
    extra: list[str] = []
    for c in candidates:
        alt = c.replace(" ", "_")
        if alt != c and alt not in candidates and alt not in extra:
            extra.append(alt)
    all_candidates = candidates + extra

    for url in all_candidates:
        img = _try_download_single(url)
        if img is not None:
            if url != item.image_url:
                item.image_url = url
            save_known_image_url(normalize_text(item.name), url)
            return img
    return None

# --------------------------------------------------------------------------- #
# Генерация изображения карточки (Glassmorphism / Liquid Glass)
# --------------------------------------------------------------------------- #

CARD_W, CARD_H = 1000, 720

# Палитры фона по редкости — мягкие насыщенные градиенты, характерные
# именно для этой категории предметов.
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
DEFAULT_ACCENT = (150, 150, 220)

STABILITY_COLORS: dict[str, tuple[int, int, int]] = {
    "stable": (110, 231, 165),
    "doing well": (110, 200, 255),
    "improving": (110, 200, 255),
    "fluctuating": (255, 196, 92),
    "underpaid for": (255, 140, 210),
    "unstable": (255, 120, 120),
    "hoarded": (200, 160, 255),
    "rising": (110, 231, 165),
    "dropping": (255, 120, 120),
    "receding": (255, 160, 120),
}
DEFAULT_STABILITY_COLOR = (200, 205, 220)

def _lerp_color(c1: tuple[int, int, int], c2: tuple[int, int, int], t_: float) -> tuple[int, int, int]:
    return (
        int(c1[0] + (c2[0] - c1[0]) * t_),
        int(c1[1] + (c2[1] - c1[1]) * t_),
        int(c1[2] + (c2[2] - c1[2]) * t_),
    )

def _make_mesh_background(width: int, height: int, slug: str) -> Image.Image:
    """Мягкий диагональный градиент + два расплывчатых цветных пятна —
    имитация фонового "mesh gradient", характерного для glassmorphism-UI."""
    c1, c2 = RARITY_GRADIENTS.get(slug, DEFAULT_GRADIENT)
    base = Image.new("RGB", (width, height), c1)
    px = base.load()
    diag = (width ** 2 + height ** 2) ** 0.5
    for y in range(height):
        for x in range(0, width, 2):
            t_ = ((x * 0.6 + y * 0.4)) / (width * 0.6 + height * 0.4)
            col = _lerp_color(c1, c2, min(1.0, max(0.0, t_)))
            px[x, y] = col
            if x + 1 < width:
                px[x + 1, y] = col
    base = base.convert("RGBA")

    # Цветные световые пятна (blobs), сильно размытые — добавляют глубину
    blob_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(blob_layer)
    accent = RARITY_ACCENT.get(slug, DEFAULT_ACCENT)
    bdraw.ellipse(
        [width * 0.65, -height * 0.25, width * 1.35, height * 0.55],
        fill=(*accent, 130),
    )
    bdraw.ellipse(
        [-width * 0.35, height * 0.55, width * 0.45, height * 1.35],
        fill=(*c2, 140),
    )
    blob_layer = blob_layer.filter(ImageFilter.GaussianBlur(120))
    base = Image.alpha_composite(base, blob_layer)

    # Лёгкий шум/виньетка для не-плоского вида
    vignette = Image.new("L", (width, height), 0)
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse([-width * 0.2, -height * 0.2, width * 1.2, height * 1.2], fill=60)
    vignette = vignette.filter(ImageFilter.GaussianBlur(180))
    dark_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 90))
    base = Image.composite(base, Image.alpha_composite(base, dark_overlay), vignette.point(lambda p: 255 - p))

    return base.convert("RGBA")

def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask

def _glass_panel(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    radius: int = 32,
    fill_alpha: int = 60,
    border_alpha: int = 90,
    blur_radius: int = 18,
) -> Image.Image:
    """
    Рисует полупрозрачную "стеклянную" панель поверх canvas в заданной
    области box=(x0,y0,x1,y1): берёт часть фона под панелью, размывает её
    (эффект backdrop-blur), осветляет полупрозрачной заливкой и обводит
    тонкой светлой рамкой — классический liquid-glass вид.
    Возвращает обновлённый canvas (RGBA).
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return canvas

    pad = blur_radius * 2
    src_box = (
        max(0, x0 - pad), max(0, y0 - pad),
        min(canvas.width, x1 + pad), min(canvas.height, y1 + pad),
    )
    region = canvas.crop(src_box).filter(ImageFilter.GaussianBlur(blur_radius))
    # Вырезаем обратно нужный кусок (без паддинга)
    rel_box = (x0 - src_box[0], y0 - src_box[1], x0 - src_box[0] + w, y0 - src_box[1] + h)
    blurred = region.crop(rel_box)

    # Полупрозрачный белый слой поверх размытого фона (эффект матового стекла)
    glass_fill = Image.new("RGBA", (w, h), (255, 255, 255, fill_alpha))
    glass = Image.alpha_composite(blurred.convert("RGBA"), glass_fill)

    mask = _rounded_mask((w, h), radius)
    canvas.paste(glass, (x0, y0), mask)

    # Тонкая светлая рамка + мягкая тень контура
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        [x0, y0, x1 - 1, y1 - 1], radius=radius,
        outline=(255, 255, 255, border_alpha), width=2,
    )
    # едва заметная внутренняя светлая линия сверху для "стеклянного" блика
    highlight = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    hdraw = ImageDraw.Draw(highlight)
    hdraw.rounded_rectangle(
        [1, 1, w - 2, h * 0.45], radius=radius,
        fill=(255, 255, 255, 26),
    )
    canvas.paste(Image.alpha_composite(Image.new("RGBA", (w, h), (0, 0, 0, 0)), highlight), (x0, y0), mask)

    return canvas

def _wrap_text_to_width(
    text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw
) -> list[str]:
    """Переносит текст по словам так, чтобы каждая строка помещалась в
    max_width. Если одно слово само по себе шире max_width (длинные
    английские составные названия), оно разбивается посимвольно как
    крайний случай — гарантируя, что текст никогда не вылезет за рамку."""
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = ""

    def width_of(s: str) -> float:
        bbox = draw.textbbox((0, 0), s, font=font)
        return bbox[2] - bbox[0]

    for word in words:
        candidate = f"{current} {word}".strip()
        if width_of(candidate) <= max_width or not current:
            if width_of(candidate) <= max_width:
                current = candidate
                continue
            # само слово шире строки — разбиваем посимвольно
            chunk = ""
            for ch in word:
                if width_of(chunk + ch) <= max_width or not chunk:
                    chunk += ch
                else:
                    lines.append(chunk)
                    chunk = ch
            current = chunk
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def _fit_font_and_wrap(
    text: str, max_width: int, max_lines: int, start_size: int, min_size: int,
    weight: str, draw: ImageDraw.ImageDraw,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Подбирает наибольший размер шрифта (от start_size вниз до min_size),
    при котором текст помещается в max_lines строк шириной max_width.
    Гарантирует, что название предмета никогда не выходит за границы
    изображения — при необходимости шрифт уменьшается или добавляется
    перенос."""
    size = start_size
    while size >= min_size:
        font = get_font(size, weight)
        lines = _wrap_text_to_width(text, font, max_width, draw)
        if len(lines) <= max_lines:
            return font, lines
        size -= 2
    font = get_font(min_size, weight)
    lines = _wrap_text_to_width(text, font, max_width, draw)
    # Жёстко обрезаем, если даже на минимальном размере не влезло —
    # лучше многоточие, чем текст за краями изображения.
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while draw.textbbox((0, 0), last + "…", font=font)[2] > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return font, lines

def _draw_text_with_shadow(
    draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
    font: ImageFont.FreeTypeFont, fill: tuple[int, int, int, int],
    anchor: str = "ma", shadow_alpha: int = 130, shadow_offset: int = 2,
) -> None:
    x, y = xy
    draw.text((x + shadow_offset, y + shadow_offset), text, anchor=anchor, font=font, fill=(0, 0, 0, shadow_alpha))
    draw.text((x, y), text, anchor=anchor, font=font, fill=fill)

def create_item_image(item: Item, lang: str) -> io.BytesIO:
    width, height = CARD_W, CARD_H
    slug = item.category_slug
    accent = RARITY_ACCENT.get(slug, DEFAULT_ACCENT)
    accent_rgba = (*accent, 255)

    canvas = _make_mesh_background(width, height, slug)
    draw = ImageDraw.Draw(canvas)

    margin = 40
    content_w = width - margin * 2

    # ---------------------------------------------------------------- #
    # Верхняя "стеклянная" панель — редкость + событие
    # ---------------------------------------------------------------- #
    top_panel_h = 62
    canvas = _glass_panel(canvas, (margin, margin, width - margin, margin + top_panel_h), radius=22, fill_alpha=55)
    draw = ImageDraw.Draw(canvas)

    rarity_emoji = RARITY_EMOJI.get(slug, "◆")
    rarity_text = rarity_label_localized(lang, slug)
    badge_font = get_font(26, "semibold")
    draw.text(
        (margin + 24, margin + top_panel_h / 2), f"{rarity_emoji}  {rarity_text}",
        anchor="lm", font=badge_font, fill=(255, 255, 255, 245),
    )
    if item.origin:
        origin_font = get_font(20, "medium")
        origin_text = f"🎁 {item.origin}"
        bbox = draw.textbbox((0, 0), origin_text, font=origin_font)
        otw = bbox[2] - bbox[0]
        max_origin_w = content_w - 260
        if otw > max_origin_w:
            while otw > max_origin_w and len(origin_text) > 4:
                origin_text = origin_text[:-2]
                bbox = draw.textbbox((0, 0), origin_text + "…", font=origin_font)
                otw = bbox[2] - bbox[0]
            origin_text = origin_text.rstrip() + "…"
        draw.text(
            (width - margin - 24, margin + top_panel_h / 2), origin_text,
            anchor="rm", font=origin_font, fill=(230, 230, 240, 220),
        )

    # ---------------------------------------------------------------- #
    # Центральная зона: крупная стеклянная панель под изображение предмета
    # ---------------------------------------------------------------- #
    img_panel_top = margin + top_panel_h + 20
    img_panel_bottom = img_panel_top + 340
    canvas = _glass_panel(
        canvas, (margin, img_panel_top, width - margin, img_panel_bottom),
        radius=32, fill_alpha=38, blur_radius=22,
    )
    draw = ImageDraw.Draw(canvas)

    item_img = download_item_image(item)
    panel_center_x = width // 2
    panel_center_y = (img_panel_top + img_panel_bottom) // 2

    if item_img:
        max_size = 300
        ratio = min(max_size / item_img.width, max_size / item_img.height, 1.0)
        new_w = max(1, int(item_img.width * ratio))
        new_h = max(1, int(item_img.height * ratio))
        item_img = item_img.resize((new_w, new_h), Image.LANCZOS)

        # мягкая тень под предметом для глубины
        shadow = Image.new("RGBA", (new_w + 60, new_h + 60), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        sdraw.ellipse([20, new_h - 10, new_w + 40, new_h + 50], fill=(0, 0, 0, 110))
        shadow = shadow.filter(ImageFilter.GaussianBlur(18))
        canvas.paste(
            shadow,
            (panel_center_x - (new_w + 60) // 2, panel_center_y - new_h // 2 - 10),
            shadow,
        )
        canvas.paste(
            item_img,
            (panel_center_x - new_w // 2, panel_center_y - new_h // 2),
            item_img,
        )
    else:
        stub_font = get_font(90, "bold")
        draw = ImageDraw.Draw(canvas)
        draw.text(
            (panel_center_x, panel_center_y), "🖼", anchor="mm", font=get_font(80, "regular"),
            fill=(255, 255, 255, 130),
        )
        draw.text(
            (panel_center_x, panel_center_y + 70), "нет фото" if lang == "ru" else "no image",
            anchor="mm", font=get_font(22, "medium"), fill=(255, 255, 255, 150),
        )

    draw = ImageDraw.Draw(canvas)

    # ---------------------------------------------------------------- #
    # Панель с названием предмета — с гарантированным автопереносом,
    # чтобы длинные имена никогда не выходили за края изображения.
    # ---------------------------------------------------------------- #
    name_en = item.name or "???"
    name_ru = get_ru_name(item.name) if (lang == "ru" and item.name) else ""
    if name_ru and normalize_text(name_ru) != normalize_text(name_en):
        title_text = f"{name_en} · {name_ru}"
    else:
        title_text = name_en

    title_panel_top = img_panel_bottom + 18
    title_max_w = content_w - 56
    title_font, title_lines = _fit_font_and_wrap(
        title_text, title_max_w, max_lines=2, start_size=40, min_size=24,
        weight="extrabold", draw=draw,
    )
    line_h = int(title_font.size * 1.22)
    title_panel_h = 28 + line_h * len(title_lines) + 22

    canvas = _glass_panel(
        canvas, (margin, title_panel_top, width - margin, title_panel_top + title_panel_h),
        radius=26, fill_alpha=50,
    )
    draw = ImageDraw.Draw(canvas)

    ty = title_panel_top + 24
    for line in title_lines:
        _draw_text_with_shadow(
            draw, (width / 2, ty), line, title_font,
            fill=(255, 255, 255, 255), anchor="ma", shadow_alpha=150,
        )
        ty += line_h

    # ---------------------------------------------------------------- #
    # Панель со стоимостью — крупно, акцентным цветом редкости
    # ---------------------------------------------------------------- #
    value_panel_top = title_panel_top + title_panel_h + 16
    value_panel_h = 100
    canvas = _glass_panel(
        canvas, (margin, value_panel_top, width - margin, value_panel_top + value_panel_h),
        radius=26, fill_alpha=48,
    )
    draw = ImageDraw.Draw(canvas)

    value_label = t(lang, "value_label").split(" ", 1)[-1] if " " in t(lang, "value_label") else t(lang, "value_label")
    label_font = get_font(19, "medium")
    draw.text(
        (width / 2, value_panel_top + 22), value_label.upper(), anchor="ma",
        font=label_font, fill=(230, 230, 240, 190),
    )

    value_str = item.value_display or "N/A"
    value_text = f"⛁ {value_str}"
    value_font, value_lines = _fit_font_and_wrap(
        value_text, content_w - 80, max_lines=1, start_size=46, min_size=26,
        weight="extrabold", draw=draw,
    )
    _draw_text_with_shadow(
        draw, (width / 2, value_panel_top + 46), value_lines[0], value_font,
        fill=accent_rgba, anchor="ma", shadow_alpha=160,
    )

    # ---------------------------------------------------------------- #
    # Нижняя панель — стабильность (с цветным индикатором)
    # ---------------------------------------------------------------- #
    stability_panel_top = value_panel_top + value_panel_h + 16
    stability_panel_h = 64
    stability_panel_bottom = min(stability_panel_top + stability_panel_h, height - margin)
    canvas = _glass_panel(
        canvas, (margin, stability_panel_top, width - margin, stability_panel_bottom),
        radius=22, fill_alpha=45,
    )
    draw = ImageDraw.Draw(canvas)

    stability_text = localized_stability(lang, item.stability) if item.stability else t(lang, "unknown_stability")
    stab_color = STABILITY_COLORS.get(normalize_text(item.stability).replace(" ", ""), None)
    # normalize_text уже убрал пробелы — сопоставим по исходному ключу отдельно
    stab_key = (item.stability or "").strip().lower()
    stab_color = STABILITY_COLORS.get(stab_key, DEFAULT_STABILITY_COLOR)

    dot_r = 8
    dot_cy = (stability_panel_top + stability_panel_bottom) / 2
    dot_cx = margin + 30
    draw.ellipse(
        [dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
        fill=(*stab_color, 255),
    )
    stab_font = get_font(22, "semibold")
    draw.text(
        (dot_cx + 20, dot_cy), stability_text, anchor="lm", font=stab_font,
        fill=(255, 255, 255, 240),
    )

    # брендинг справа
    brand_font = get_font(18, "medium")
    draw.text(
        (width - margin - 24, dot_cy), "MM2 Values", anchor="rm", font=brand_font,
        fill=(255, 255, 255, 140),
    )

    bio = io.BytesIO()
    canvas.convert("RGB").save(bio, format="JPEG", quality=92)
    bio.seek(0)
    return bio
