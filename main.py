"""
MM2 Values Telegram Bot (v2.6.1)
=================================
Исправления:
- Исправлен парсинг image_url: если изображение отсутствует или ведёт на N_A,
  URL генерируется по шаблону /media/{slug}/{name}.png.
- Добавлена проверка на N/A имя предмета (игнорируем такие карточки).
- Улучшено извлечение display_name (дополнительно проверяется data-name карточки).
- Устранена проблема с относительными путями без начального слеша.
- Добавлено логирование сгенерированных URL.
"""

from __future__ import annotations

import html
import io
import logging
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
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process
from apscheduler.schedulers.background import BackgroundScheduler
from PIL import Image, ImageDraw, ImageFont

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
from telegram.error import Conflict, NetworkError, TimedOut

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

CATEGORIES: list[tuple[str, str]] = [
    ("godlies", "Godly"),
    ("chromas", "Chroma"),
    ("legendaries", "Legendary"),
    ("ancients", "Ancient"),
    ("vintages", "Vintage"),
    ("rares", "Rare"),
    ("uncommons", "Uncommon"),
    ("commons", "Common"),
]

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
}

def _parse_value_to_int(raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw or raw.upper() in ("N/A", "NA"):
        return None
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return None

def fetch_category(slug: str, rarity_label: str) -> list[Item]:
    target_url = f"{BASE_URL}/mm2/{slug}"
    api_url = f"https://api.scrapingant.com/v2/general?url={target_url}&x-api-key={SCRAPINGANT_API_KEY}&browser=true"

    request_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MM2ValuesBot/1.0)",
        "Accept": "application/json",
        "Connection": "close",
    }

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                api_url,
                timeout=REQUEST_TIMEOUT,
                headers=request_headers,
            )
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
        except Exception as e:
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

    for card in soup.find_all("div", class_="itemcolumn"):
        # --- Имя предмета ---
        btn_tag = card.find("button")
        display_name = ""
        if btn_tag and btn_tag.get("data-name"):
            display_name = btn_tag.get("data-name").strip()
        else:
            head_tag = card.find("div", class_="itemhead")
            if head_tag:
                display_name = head_tag.get_text(strip=True)

        # Дополнительно пробуем data-name самой карточки
        if not display_name:
            display_name = card.get("data-name", "").strip()

        if not display_name or display_name.lower() == "n/a":
            continue

        if display_name.lower() in seen_names:
            continue
        seen_names.add(display_name.lower())

        # --- Значение ---
        val_tag = card.find("b", class_="itemvalue")
        if val_tag:
            value_raw = val_tag.get_text(strip=True)
        else:
            value_raw = card.get("data-value", "N/A")

        value_int = _parse_value_to_int(value_raw)
        value_display = value_raw if value_int is None else f"{value_int:,}".replace(",", " ")

        # --- Стабильность ---
        stability = card.get("data-stability", "Unknown")

        # --- Изображение ---
        image_url = ""
        img_tag = card.find("img", class_="itemimage")
        if img_tag and img_tag.get("src"):
            src = img_tag["src"].strip()
            if src.startswith(".."):
                src = src.replace("..", BASE_URL)
            elif src.startswith("/"):
                src = BASE_URL + src
            elif not src.startswith("http"):
                src = BASE_URL + "/" + src.lstrip("/")
            image_url = src

        # Если URL отсутствует или содержит N_A — генерируем свой
        if not image_url or "N_A" in image_url.upper():
            safe_name = re.sub(r'[^\w\s-]', '', display_name).strip().replace(' ', '_')
            image_url = f"{BASE_URL}/media/{slug}/{safe_name}.png"
            logger.info("Сгенерирован URL изображения для '%s': %s", display_name, image_url)

        origin = card.get("data-event", "")

        logger.info("DEBUG [%s]: Найдено -> Имя: '%s', Value: '%s'", slug, display_name, value_display)

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
                origin=origin,
            )
        )

    return items

def fetch_all_items() -> list[Item]:
    all_items: list[Item] = []

    for slug, rarity_label in CATEGORIES:
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

    def search(self, query: str, limit: int = 5) -> list[tuple[Item, float]]:
        with self._lock:
            items = self._items
            index = list(self._search_index)
        if not items or not index:
            return []

        variants = generate_query_variants(query)
        if not variants:
            return []

        choices = [key for key, _ in index]
        best_by_idx: dict[int, float] = {}

        for variant in variants:
            for key, idx in index:
                if key == variant:
                    best_by_idx[idx] = 100.0
            results = process.extract(
                variant, choices, scorer=fuzz.WRatio, limit=limit * 3
            )
            for matched_key, score, pos in results:
                idx = index[pos][1]
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

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)

cache = ValuesCache()

# --------------------------------------------------------------------------- #
# Настройки
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

# --------------------------------------------------------------------------- #
# Локализация
# --------------------------------------------------------------------------- #

TEXTS = {
    "ru": {
        "start": (
            "👋 Привет! Я бот для проверки ценности скинов Murder Mystery 2.\n\n"
            "Просто напиши название предмета на русском или английском "
            "(опечатки не страшны) — например: <i>Nebula</i>, <i>Туманность</i> "
            "или даже <i>тумпннлсть</i>.\n\n"
            "Настройки языка: /settings"
        ),
        "help": (
            "Напиши название предмета MM2 — я найду его ценность.\n"
            "Команды:\n"
            "/settings — сменить язык интерфейса\n"
            "/status — статус базы данных (когда обновлялась)"
        ),
        "settings_title": "🌐 Выберите язык интерфейса:",
        "settings_saved": "✅ Язык сохранён: {lang_name}",
        "not_found": (
            "😕 Ничего не найдено по запросу «{query}».\n"
            "Проверь написание или попробуй английское название предмета."
        ),
        "searching": "🔎 Ищу...",
        "value_label": "Примерная стоимость",
        "status_label": "Категория",
        "stability_label": "Стабильность",
        "unknown_stability": "Неизвестно",
        "cache_empty": "⏳ База данных ещё загружается, попробуй через минуту.",
        "status_report": (
            "📊 Предметов в базе: {count}\n"
            "🕒 Последнее обновление: {last_update}\n"
            "⚠️ Ошибка последнего обновления: {error}"
        ),
        "status_report_ok": (
            "📊 Предметов в базе: {count}\n"
            "🕒 Последнее обновление: {last_update}"
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
    },
    "en": {
        "start": (
            "👋 Hi! I'm a bot for checking Murder Mystery 2 item values.\n\n"
            "Just type an item name in Russian or English (typos are fine) — "
            "for example: <i>Nebula</i>, <i>Туманность</i> or even "
            "<i>tumpnnlst</i>.\n\n"
            "Language settings: /settings"
        ),
        "help": (
            "Type an MM2 item name — I'll find its value.\n"
            "Commands:\n"
            "/settings — change interface language\n"
            "/status — database status (last update time)"
        ),
        "settings_title": "🌐 Choose interface language:",
        "settings_saved": "✅ Language saved: {lang_name}",
        "not_found": (
            "😕 Nothing found for «{query}».\n"
            "Check the spelling or try the item's English name."
        ),
        "searching": "🔎 Searching...",
        "value_label": "Estimated value",
        "status_label": "Category",
        "stability_label": "Stability",
        "unknown_stability": "Unknown",
        "cache_empty": "⏳ Database is still loading, please try again in a minute.",
        "status_report": (
            "📊 Items in database: {count}\n"
            "🕒 Last update: {last_update}\n"
            "⚠️ Last update error: {error}"
        ),
        "status_report_ok": (
            "📊 Items in database: {count}\n"
            "🕒 Last update: {last_update}"
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
    },
}

def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else DEFAULT_LANG
    template = TEXTS[lang].get(key, TEXTS[DEFAULT_LANG][key])
    return template.format(**kwargs) if kwargs else template

def localized_stability(lang: str, stability_en: str) -> str:
    if lang == "en":
        return stability_en
    key = stability_en.strip().lower()
    return STABILITY_MAP_RU.get(key, stability_en)

# --------------------------------------------------------------------------- #
# Генерация изображения
# --------------------------------------------------------------------------- #

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    path = FONT_PATH if bold else FONT_PATH_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()

def download_image(url: str) -> Optional[Image.Image]:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as e:
        logger.warning("Не удалось скачать изображение %s: %s", url, e)
    return None

def create_item_image(item: Item, lang: str) -> io.BytesIO:
    width, height = 800, 600
    bg_color1 = (26, 26, 46)
    bg_color2 = (22, 33, 62)
    img = Image.new("RGBA", (width, height), bg_color1)
    draw = ImageDraw.Draw(img)

    for y in range(height):
        r = int(bg_color1[0] + (bg_color2[0] - bg_color1[0]) * y / height)
        g = int(bg_color1[1] + (bg_color2[1] - bg_color1[1]) * y / height)
        b = int(bg_color1[2] + (bg_color2[2] - bg_color1[2]) * y / height)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    overlay = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle([(20, 400), (width - 20, 580)], fill=(0, 0, 0, 160))
    img = Image.alpha_composite(img, overlay)

    item_img = None
    if item.image_url:
        item_img = download_image(item.image_url)
    if item_img:
        max_size = 280
        ratio = min(max_size / item_img.width, max_size / item_img.height, 1.0)
        new_w = int(item_img.width * ratio)
        new_h = int(item_img.height * ratio)
        item_img = item_img.resize((new_w, new_h), Image.LANCZOS)
    else:
        item_img = Image.new("RGBA", (200, 200), (255, 255, 255, 0))
        draw_stub = ImageDraw.Draw(item_img)
        draw_stub.text((60, 50), "?", fill=(200, 200, 200), font=get_font(100))

    item_x = (width - item_img.width) // 2
    item_y = 150 - item_img.height // 2
    img.paste(item_img, (item_x, item_y), item_img)

    title_font = get_font(38, bold=True)
    value_font = get_font(36, bold=True)
    detail_font = get_font(22, bold=False)

    name_en = item.name
    name_ru = get_ru_name(item.name) if lang == "ru" else ""
    title = f"{name_en} / {name_ru}" if (name_ru and name_ru != name_en) else name_en

    value_str = item.value_display
    rarity = item.rarity
    stability = localized_stability(lang, item.stability)

    fill_white = (255, 255, 255, 255)
    fill_light = (220, 220, 220, 255)
    fill_yellow = (255, 215, 0, 255)

    draw.text((width//2 + 2, 22), title, anchor="ma", font=title_font, fill=(0,0,0,120))
    draw.text((width//2, 20), title, anchor="ma", font=title_font, fill=fill_white)

    draw.text((width//2 + 2, 432), f"Supreme: {value_str}", anchor="ma", font=value_font, fill=(0,0,0,120))
    draw.text((width//2, 430), f"Supreme: {value_str}", anchor="ma", font=value_font, fill=fill_yellow)

    draw.text((width//2 + 1, 491), f"{rarity}  ·  {stability}", anchor="ma", font=detail_font, fill=(0,0,0,100))
    draw.text((width//2, 490), f"{rarity}  ·  {stability}", anchor="ma", font=detail_font, fill=fill_light)

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# --------------------------------------------------------------------------- #
# Обработчики Telegram
# --------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "start"), parse_mode=ParseMode.HTML)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "help"))

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    buttons = [
        [InlineKeyboardButton(f"🇷🇺 {SUPPORTED_LANGS['ru']}", callback_data="setlang:ru")],
        [InlineKeyboardButton(f"🇬🇧 {SUPPORTED_LANGS['en']}", callback_data="setlang:en")],
    ]
    await update.message.reply_text(
        t(lang, "settings_title"), reply_markup=InlineKeyboardMarkup(buttons)
    )

async def on_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, lang_code = query.data.split(":", 1)
    if lang_code not in SUPPORTED_LANGS:
        return
    set_user_lang(query.from_user.id, lang_code)
    await query.edit_message_text(
        t(lang_code, "settings_saved", lang_name=SUPPORTED_LANGS[lang_code])
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(update.effective_user.id)
    count = cache.size
    last_update = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cache.last_updated))
        if cache.last_updated
        else t(lang, "never")
    )
    if cache.last_error:
        text = t(lang, "status_report", count=count, last_update=last_update, error=cache.last_error)
    else:
        text = t(lang, "status_report_ok", count=count, last_update=last_update)
    await update.message.reply_text(text)

async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    query_text = update.message.text.strip()
    if not query_text or query_text.startswith("/"):
        return

    user_id = update.effective_user.id
    lang = get_user_lang(user_id)

    if cache.size == 0:
        await update.message.reply_text(t(lang, "cache_empty"))
        return

    results = cache.search(query_text, limit=1)
    if not results:
        await update.message.reply_text(t(lang, "not_found", query=query_text))
        return

    item, _ = results[0]

    try:
        img_bio = create_item_image(item, lang)
        await update.message.reply_photo(
            photo=img_bio,
            caption=f"🔍 Запрос: {html.escape(query_text)}",
        )
    except Exception as e:
        logger.exception("Ошибка при создании изображения: %s", e)
        caption = format_item_caption(item, lang)
        await update.message.reply_text(caption, parse_mode=ParseMode.HTML)

def format_item_caption(item: Item, lang: str) -> str:
    name_en = html.escape(item.name)
    if lang == "ru":
        name_ru = html.escape(get_ru_name(item.name))
        title_line = f"<b>{name_en}</b> ({name_ru})"
    else:
        title_line = f"<b>{name_en}</b>"
    stability_text = localized_stability(lang, item.stability)
    lines = [
        title_line,
        "",
        f"<i>{t(lang, 'value_label')}:</i>",
        f"Supreme: <b>{item.value_display}</b>",
        "",
        f"{t(lang, 'status_label')}: <b>{html.escape(item.rarity)}</b>",
        f"{t(lang, 'stability_label')}: <b>{html.escape(stability_text)}</b>",
    ]
    return "\n".join(lines)

async def cmd_setrefresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        lang = get_user_lang(user_id)
        await update.message.reply_text(t(lang, "admin_only"))
        return

    lang = get_user_lang(user_id)

    if not context.args:
        current_days = get_refresh_interval_days()
        await update.message.reply_text(t(lang, "admin_set_refresh", days=current_days))
        return

    try:
        days = int(context.args[0])
        if days < 1 or days > 90:
            raise ValueError
    except ValueError:
        await update.message.reply_text(t(lang, "admin_refresh_invalid"))
        return

    set_refresh_interval_days(days)
    scheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        seconds = days * 86400
        scheduler.reschedule_job("refresh_values_cache", trigger="interval", seconds=seconds)
        logger.info("Интервал обновления изменён на %d дней", days)
    else:
        logger.error("Планировщик не найден в bot_data")

    await update.message.reply_text(t(lang, "admin_refresh_updated", days=days))

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, Conflict):
        logger.error(
            "Обнаружен конфликт (Conflict): другой экземпляр бота активен "
            "(или уже идёт перезапуск). Ничего не делаем — polling сам "
            "переподключится, либо это сделает внешний retry-цикл в main()."
        )
    elif isinstance(error, NetworkError):
        logger.error("Сетевая ошибка: %s", error)
    elif isinstance(error, TimedOut):
        logger.error("Таймаут запроса к Telegram API: %s", error)
    else:
        logger.error("Ошибка при обработке апдейта: %s", error, exc_info=error)

def reset_webhook_and_cleanup():
    import requests as req
    try:
        resp = req.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
        if resp.status_code == 200:
            logger.info("Вебхук успешно удалён")
        else:
            logger.warning(f"Не удалось удалить вебхук: {resp.text}")
        resp = req.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset=-1&timeout=1")
        if resp.status_code == 200:
            logger.info("Pending updates очищены")
        else:
            logger.warning(f"Не удалось очистить pending updates: {resp.text}")
    except Exception as e:
        logger.error(f"Ошибка при очистке вебхука: {e}")

# --------------------------------------------------------------------------- #
# HTTP-сервер для health-check (Render Web Service)
# --------------------------------------------------------------------------- #

class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass

def start_health_check_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health-check HTTP-сервер запущен на 0.0.0.0:%d", port)
    return server

# --------------------------------------------------------------------------- #
# Главный блок
# --------------------------------------------------------------------------- #

def main() -> None:
    start_health_check_server(PORT)

    logger.info("Очистка вебхука и pending updates...")
    reset_webhook_and_cleanup()

    init_db()

    threading.Thread(target=cache.refresh, daemon=True).start()

    interval_days = get_refresh_interval_days()
    interval_seconds = interval_days * 86400

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        cache.refresh,
        "interval",
        seconds=interval_seconds,
        id="refresh_values_cache",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    application.bot_data["scheduler"] = scheduler

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("setrefresh", cmd_setrefresh))
    application.add_handler(CallbackQueryHandler(on_settings_callback, pattern=r"^setlang:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    application.add_error_handler(on_error)

    def signal_handler(signum, frame):
        logger.info("Получен сигнал завершения, останавливаем бота...")
        if scheduler.running:
            scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("Бот запущен, начинаю polling...")

    max_retries = 5
    retry_count = 0
    while retry_count < max_retries:
        try:
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                close_loop=False
            )
            break
        except Conflict:
            retry_count += 1
            logger.warning(f"Конфликт при запуске (попытка {retry_count}/{max_retries}). Очистка и повтор через 10 сек...")
            reset_webhook_and_cleanup()
            time.sleep(10)
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}", exc_info=True)
            break

    if retry_count >= max_retries:
        logger.error("Не удалось запустить бота после максимального количества попыток")

if __name__ == "__main__":
    main()
