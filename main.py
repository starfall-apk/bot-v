"""
MM2 Values Telegram Bot (v2.3.1)
=================================
Исправление совместимости с urllib3 >= 2.0: параметр maxheaders заменён на max_header_size.
"""

from __future__ import annotations

import asyncio
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
from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3 import __version__ as urllib3_version
from urllib3.poolmanager import PoolManager
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
# Адаптер с увеличенным лимитом заголовков, совместимый с urllib3 1.x и 2.x
# --------------------------------------------------------------------------- #

class HeaderRichHTTPAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        # urllib3 >= 2.0 использует max_header_size (байты), иначе maxheaders (количество)
        major, minor, *_ = map(int, urllib3_version.split('.'))
        if (major, minor) >= (2, 0):
            kwargs['max_header_size'] = 32768   # 32 КБ — более чем достаточно
        else:
            kwargs['maxheaders'] = 200
        return super().init_poolmanager(*args, **kwargs)

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

BASE_URL = "https://supremevalues.com"

CATEGORIES: list[tuple[str, str]] = [
    ("godlies", "Godly"),
    ("chromas", "Chroma"),
    ("legendaries", "Legendary"),
    ("ancients", "Ancient"),
    ("vintages", "Vintage"),
    ("evos", "Evo"),
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

RU_NAMES: dict[str, str] = {
    "nebula": "Туманность",
    "traveler's gun": "Пистолет путешественника",
    "travelers gun": "Пистолет путешественника",
    "evergun": "Вечнозелёный пистолет",
    "constellation": "Созвездие",
    "evergreen": "Вечнозелёный",
    "turkey": "Индейка",
    "alienbeam": "Луч пришельца",
    "vampire's gun": "Пистолет вампира",
    "vampires gun": "Пистолет вампира",
    "darkshot": "Тёмный выстрел",
    "darksword": "Тёмный меч",
    "raygun": "Лучемёт",
    "blossom": "Цветение",
    "sakura": "Сакура",
    "sunrise": "Рассвет",
    "bauble": "Ёлочный шар",
    "snowcannon": "Снежная пушка",
    "sunset": "Закат",
    "soul": "Душа",
    "spirit": "Дух",
    "rainbow gun": "Радужный пистолет",
    "flora": "Флора",
    "rainbow": "Радуга",
    "bloom": "Расцвет",
    "heart wand": "Сердечный жезл",
    "ocean": "Океан",
    "waves": "Волны",
    "xenoknife": "Ксенонож",
    "xenoshot": "Ксеновыстрел",
    "flowerwood gun": "Цветочный пистолет",
    "blizzard": "Метель",
    "flowerwood": "Цветочное дерево",
    "snow dagger": "Снежный кинжал",
    "snowstorm": "Снежная буря",
    "watergun": "Водный пистолет",
    "treat": "Сладость",
    "sweet": "Конфета",
    "borealis": "Северное сияние",
    "australis": "Южное сияние",
    "bat": "Летучая мышь",
    "pearlshine": "Жемчужный блеск",
    "pearl": "Жемчуг",
    "candy": "Леденец",
    "heartblade": "Клинок сердца",
    "luger": "Люгер",
    "red luger": "Красный люгер",
    "green luger": "Зелёный люгер",
    "ginger luger": "Имбирный люгер",
    "makeshift": "Самодельный",
    "phantom": "Фантом",
    "spectre": "Призрак",
    "candleflame": "Пламя свечи",
    "darkbringer": "Несущий тьму",
    "elderwood blade": "Клинок древнего дерева",
    "elderwood revolver": "Револьвер древнего дерева",
    "iceblaster": "Ледяной бластер",
    "lightbringer": "Несущий свет",
    "sugar": "Сахар",
    "ornament": "Украшение",
    "amerilaser": "Америлазер",
    "laser": "Лазер",
    "hallowgun": "Хэллоу-пистолет",
    "icebeam": "Ледяной луч",
    "nightblade": "Ночной клинок",
    "shark": "Акула",
    "plasmabeam": "Плазменный луч",
    "swirly gun": "Спиральный пистолет",
    "battleaxe ii": "Боевой топор II",
    "blaster": "Бластер",
    "iceflake": "Ледяная снежинка",
    "pixel": "Пиксель",
    "plasmablade": "Плазменный клинок",
    "gemstone": "Драгоценный камень",
    "old glory": "Старая слава",
    "slasher": "Потрошитель",
    "vampire's edge": "Клинок вампира",
    "vampires edge": "Клинок вампира",
    "cookiecane": "Печенье-трость",
    "deathshard": "Осколок смерти",
    "eternalcane": "Вечная трость",
    "gingerblade": "Имбирный клинок",
    "gingermint": "Имбирная мята",
    "jinglegun": "Звенящий пистолет",
    "lugercane": "Люгер-трость",
    "minty": "Мятный",
    "swirly blade": "Спиральный клинок",
    "virtual": "Виртуальный",
    "battleaxe": "Боевой топор",
    "chill": "Холод",
    "clockwork": "Заводной механизм",
    "fang": "Клык",
    "frostsaber": "Морозная сабля",
    "heat": "Жар",
    "spider": "Паук",
    "tides": "Приливы",
    "bioblade": "Биоклинок",
    "eternal iii": "Вечный III",
    "eternal iv": "Вечный IV",
    "hallow's blade": "Клинок Хэллоуина",
    "hallows blade": "Клинок Хэллоуина",
    "hallow's edge": "Грань Хэллоуина",
    "hallows edge": "Грань Хэллоуина",
    "handsaw": "Ножовка",
    "boneblade": "Костяной клинок",
    "eternal": "Вечный",
    "eternal ii": "Вечный II",
    "frostbite": "Обморожение",
    "ghostblade": "Клинок призрака",
    "ice dragon": "Ледяной дракон",
    "ice shard": "Ледяной осколок",
    "prismatic": "Призматический",
    "pumpking": "Тыквенный король",
    "saw": "Пила",
    "xmas": "Рождество",
    "eggblade": "Клинок-яйцо",
    "flames": "Пламя",
    "snowflake": "Снежинка",
    "winter's edge": "Зимняя грань",
    "winters edge": "Зимняя грань",
    "peppermint": "Мята перечная",
    "cookieblade": "Клинок-печенье",
    "blue seer": "Синий провидец",
    "purple seer": "Фиолетовый провидец",
    "red seer": "Красный провидец",
    "seer": "Провидец",
    "orange seer": "Оранжевый провидец",
    "yellow seer": "Жёлтый провидец",
    "chroma evergreen": "Хрома Вечнозелёный",
    "chroma raygun": "Хрома Лучемёт",
    "chroma sunrise": "Хрома Рассвет",
    "chroma sunset": "Хрома Закат",
    "chroma snow dagger": "Хрома Снежный кинжал",
    "chroma darkbringer": "Хрома Несущий тьму",
    "chroma lightbringer": "Хрома Несущий свет",
    "chroma luger": "Хрома Люгер",
    "chroma candleflame": "Хрома Пламя свечи",
    "chroma laser": "Хрома Лазер",
    "chroma elderwood blade": "Хрома Клинок древнего дерева",
    "chroma swirly gun": "Хрома Спиральный пистолет",
    "chroma deathshard": "Хрома Осколок смерти",
    "chroma cookiecane": "Хрома Печенье-трость",
    "chroma slasher": "Хрома Потрошитель",
    "chroma fang": "Хрома Клык",
    "chroma gemstone": "Хрома Драгоценный камень",
    "chroma shark": "Хрома Акула",
    "chroma heat": "Хрома Жар",
    "chroma seer": "Хрома Провидец",
    "chroma gingerblade": "Хрома Имбирный клинок",
    "chroma tides": "Хрома Приливы",
    "chroma saw": "Хрома Пила",
    "chroma boneblade": "Хрома Костяной клинок",
    "darkness": "Тьма",
    "corrupt": "Порча",
    "frost": "Мороз",
    "midnight": "Полночь",
    "crystal": "Кристалл",
    "godly gun": "Голди пистолет",
    "harvest moon": "Урожайная луна",
    "golden gun": "Золотой пистолет",
    "silver gun": "Серебряный пистолет",
    "chroma": "Хрома",
    "ice": "Лёд",
    "fire": "Огонь",
    "gold": "Золото",
    "silver": "Серебро",
    "shadow": "Тень",
    "void": "Пустота",
    "star": "Звезда",
    "galaxy": "Галактика",
    "comet": "Комета",
    "meteor": "Метеор",
    "aurora": "Аврора",
    "eclipse": "Затмение",
    "phoenix": "Феникс",
    "dragon": "Дракон",
    "wolf": "Волк",
    "cat": "Кот",
    "dog": "Пёс",
    "bunny": "Кролик",
    "bear": "Медведь",
    "fox": "Лис",
    "pig": "Свин",
}

WORD_TRANSLATIONS: dict[str, str] = {
    "chroma": "Хрома", "c.": "Хрома", "gun": "Пистолет", "blade": "Клинок",
    "knife": "Нож", "sword": "Меч", "axe": "Топор", "edge": "Грань",
    "shard": "Осколок", "fire": "Огонь", "ice": "Лёд", "gold": "Золото",
    "silver": "Серебро", "red": "Красный", "blue": "Синий", "green": "Зелёный",
    "purple": "Фиолетовый", "orange": "Оранжевый", "yellow": "Жёлтый",
    "white": "Белый", "black": "Чёрный", "dark": "Тёмный", "light": "Светлый",
    "snow": "Снег", "winter": "Зима", "summer": "Лето", "xmas": "Рождество",
    "christmas": "Рождество", "hallow's": "Хэллоуин", "hallows": "Хэллоуин",
    "valentine": "Валентин", "easter": "Пасха", "seer": "Провидец",
}

def auto_translate_ru(name_en: str) -> str:
    words = name_en.split()
    result = []
    for w in words:
        key = w.lower().strip(".,'()")
        result.append(WORD_TRANSLATIONS.get(key, w))
    return " ".join(result)

def get_ru_name(name_en: str) -> str:
    key = name_en.lower().strip()
    if key in RU_NAMES:
        return RU_NAMES[key]
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

def fetch_category(session: requests.Session, slug: str, rarity_label: str) -> list[Item]:
    target_url = f"{BASE_URL}/mm2/{slug}"
    api_url = f"https://api.scrapingant.com/v2/general?url={target_url}&x-api-key={SCRAPINGANT_API_KEY}&browser=true"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(api_url, timeout=REQUEST_TIMEOUT)
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
        head_tag = card.find("div", class_="itemhead")
        btn_tag = card.find("button")
        display_name = ""
        if btn_tag and btn_tag.get("data-name"):
            display_name = btn_tag.get("data-name").strip()
        elif head_tag:
            display_name = head_tag.get_text(strip=True)
        if not display_name:
            continue
        if display_name.lower() in seen_names:
            continue
        seen_names.add(display_name.lower())

        val_tag = card.find("b", class_="itemvalue")
        value_raw = val_tag.get_text(strip=True) if val_tag else card.get("data-value", "N/A")
        value_int = _parse_value_to_int(value_raw)
        value_display = value_raw if value_int is None else f"{value_int:,}".replace(",", " ")
        stability = card.get("data-stability", "Неизвестно")
        img_tag = card.find("img", class_="itemimage")
        image_url = ""
        if img_tag and img_tag.get("src"):
            src = img_tag["src"]
            if src.startswith(".."):
                src = src.replace("..", BASE_URL)
            elif src.startswith("/"):
                src = BASE_URL + src
            image_url = src
        origin = card.get("data-event", "")
        items.append(Item(
            name=display_name,
            category_slug=slug,
            rarity=rarity_label,
            value=value_int,
            value_display=value_display,
            ranged_value=None,
            stability=stability,
            image_url=image_url,
            origin=origin,
        ))
    return items

def fetch_all_items() -> list[Item]:
    all_items: list[Item] = []
    session = requests.Session()
    adapter = HeaderRichHTTPAdapter()
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    for slug, rarity_label in CATEGORIES:
        try:
            cat_items = fetch_category(session, slug, rarity_label)
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
        logger.error("Обнаружен конфликт (Conflict): другой экземпляр бота активен. Перезапуск...")
        app = context.application
        await app.stop()
        await asyncio.sleep(5)
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
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
# Главный блок
# --------------------------------------------------------------------------- #

def main() -> None:
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
