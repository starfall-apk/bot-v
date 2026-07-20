"""
MM2 Values Telegram Bot
========================
Ищет ценность скинов/оружия Murder Mystery 2 (Roblox) по названию на русском
или английском языке (с опечатками и вариациями), парся supremevalues.com.

Источник данных: только supremevalues.com.
mm2values.com защищён JS-проверкой (Cloudflare-подобный challenge) и не
отдаёт HTML обычным HTTP-запросом, поэтому не используется — это осознанное
решение, принятое заранее (headless-браузер на бесплатном Render слишком
медленный и нестабильный для задачи "поиск в один клик").

Деплой: Render (Background Worker / Web Service с polling).
Файлы: main.py, requirements.txt — больше ничего не требуется.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process
from apscheduler.schedulers.background import BackgroundScheduler

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

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан токен бота. Установите переменную окружения BOT_TOKEN "
        "(в настройках Render: Environment -> Add Environment Variable)."
    )

# Как часто обновлять кэш ценностей (в секундах). По умолчанию — раз в час.
REFRESH_INTERVAL_SECONDS = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "3600"))

DB_PATH = os.environ.get("DB_PATH", "mm2bot_settings.db")

BASE_URL = "https://supremevalues.com"

# Все категории сайта supremevalues.com для Murder Mystery 2.
CATEGORIES: list[tuple[str, str]] = [
    ("godlies", "Godly"),
    ("chromas", "Chroma"),
    ("legendaries", "Legendary"),
    ("ancients", "Ancient"),
    ("vintages", "Vintage"),
    ("evos", "Evo"),
    ("sets", "Set"),
    ("uniques", "Unique"),
    ("rares", "Rare"),
    ("uncommons", "Uncommon"),
    ("commons", "Common"),
    ("pets", "Pet"),
    ("misc", "Misc"),
    ("untradables", "Untradable"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = 20

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mm2bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Модель данных предмета
# --------------------------------------------------------------------------- #

@dataclass
class Item:
    name: str                      # английское отображаемое имя, как на сайте
    category_slug: str             # godlies / chromas / ...
    rarity: str                    # Godly / Chroma / ...
    value: Optional[int]           # числовое значение (для сортировки/поиска)
    value_display: str             # как показывать ("15,000" или "N/A")
    ranged_value: Optional[str]    # диапазон, если есть, иначе None
    stability: str                 # Stable / Fluctuating / Doing Well / ...
    image_url: str
    origin: str = ""

    @property
    def search_key(self) -> str:
        return normalize_text(self.name)


# --------------------------------------------------------------------------- #
# Нормализация текста / транслитерация
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
# Русские названия предметов
# --------------------------------------------------------------------------- #

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
# Парсинг supremevalues.com
# --------------------------------------------------------------------------- #

RE_VALUE = re.compile(r"Value\s*[-:]\s*([\d,]+|N/?A)", re.IGNORECASE)
RE_RANGED = re.compile(r"Ranged\s*Value\s*[-:]\s*\[?([^\]\n]+)\]?", re.IGNORECASE)
RE_STABILITY = re.compile(r"Stability\s*[-:]\s*([A-Za-z ]+?)(?:\s{2,}|\n|Demand|$)", re.IGNORECASE)
RE_ORIGIN = re.compile(r"Origin\s*[-:]\s*(.+?)(?:\s{2,}|\n|Last Change|$)", re.IGNORECASE)

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
    """Универсальный парсинг категории с supremevalues.com."""
    url = f"{BASE_URL}/mm2/{slug}"
    resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    items: list[Item] = []
    seen_names: set[str] = set()

    # Поиск по стандарту supremevalues (блоки .item-box) или фоллбэк по картинкам
    item_blocks = soup.select(".item-box, .card, div[class*='item']")
    if not item_blocks:
        img_pattern = re.compile(rf"/media/mm2", re.IGNORECASE)
        imgs = soup.find_all("img", src=img_pattern)
        item_blocks = [img.parent for img in imgs]

    for block in item_blocks:
        text_block = block.get_text(" ", strip=True)
        if "Value" not in text_block:
            # Пробуем подняться на пару уровней вверх
            node = block
            for _ in range(3):
                if node.parent:
                    node = node.parent
                    if "Value" in node.get_text():
                        text_block = node.get_text(" ", strip=True)
                        block = node
                        break

        value_match = RE_VALUE.search(text_block)
        if not value_match:
            continue

        # Извлекаем имя
        name_part = ""
        name_elem = block.select_one(".item-name, h3, h4, .title, b")
        if name_elem:
            name_part = name_elem.get_text().strip()
        
        if not name_part:
            img = block.find("img")
            if img and img.get("alt"):
                name_part = img.get("alt").strip()

        if not name_part:
            name_part = text_block[: value_match.start()].strip()
            name_part = re.sub(r"Click on the item's image.*?Features!\s*", "", name_part, flags=re.IGNORECASE).strip()

        if not name_part or len(name_part) < 2:
            continue

        display_name = name_part
        if display_name.lower() in seen_names:
            continue
        seen_names.add(display_name.lower())

        img = block.find("img")
        image_url = ""
        if img and img.get("src"):
            image_url = img.get("src")
            if image_url.startswith("/"):
                image_url = BASE_URL + image_url

        value_raw = value_match.group(1)
        value_int = _parse_value_to_int(value_raw)
        value_display = value_raw if value_int is None else f"{value_int:,}".replace(",", " ")

        ranged_match = RE_RANGED.search(text_block)
        ranged_value = None
        if ranged_match:
            rv = ranged_match.group(1).strip()
            if rv and rv.upper() != "N/A":
                ranged_value = rv

        stability_match = RE_STABILITY.search(text_block)
        stability = stability_match.group(1).strip() if stability_match else "Неизвестно"

        origin_match = RE_ORIGIN.search(text_block)
        origin = origin_match.group(1).strip() if origin_match else ""

        items.append(
            Item(
                name=display_name,
                category_slug=slug,
                rarity=rarity_label,
                value=value_int,
                value_display=value_display,
                ranged_value=ranged_value,
                stability=stability,
                image_url=image_url,
                origin=origin,
            )
        )

    return items


def fetch_all_items() -> list[Item]:
    all_items: list[Item] = []
    with requests.Session() as session:
        for slug, rarity_label in CATEGORIES:
            try:
                cat_items = fetch_category(session, slug, rarity_label)
                logger.info("Категория '%s': найдено %d предметов", slug, len(cat_items))
                all_items.extend(cat_items)
            except Exception:
                logger.exception("Не удалось спарсить категорию '%s'", slug)
    return all_items


# --------------------------------------------------------------------------- #
# Кэш данных
# --------------------------------------------------------------------------- #

class ValuesCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Item] = []
        self._search_index: list[tuple[str, int]] = []
        self.last_updated: float = 0.0
        self.last_error: Optional[str] = None

    def refresh(self) -> None:
        logger.info("Запуск обновления кэша ценностей...")
        try:
            items = fetch_all_items()
            if not items:
                raise RuntimeError("Парсинг вернул 0 предметов — сайт мог изменить структуру.")
            with self._lock:
                self._items = items
                self._search_index = [
                    (it.search_key, idx) for idx, it in enumerate(items) if it.search_key
                ]
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
                variant,
                choices,
                scorer=fuzz.WRatio,
                limit=limit * 3,
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
# Настройки пользователей — SQLite
# --------------------------------------------------------------------------- #

DEFAULT_LANG = "ru"
SUPPORTED_LANGS = {"ru": "Русский", "en": "English"}

_db_lock = threading.Lock()


def init_db() -> None:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                lang TEXT NOT NULL DEFAULT 'ru'
            )
            """
        )
        conn.commit()


def get_user_lang(user_id: int) -> str:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT lang FROM user_settings WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            return row[0]
        return DEFAULT_LANG


def set_user_lang(user_id: int, lang: str) -> None:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, lang) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang
            """,
            (user_id, lang),
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
            "(опечатки не страшны) — например: <i>Nebula</i>, <i>Туманность</i>.\n\n"
            "Настройки языка: /settings"
        ),
        "help": (
            "Напиши название предмета MM2 — я найду его ценность.\n"
            "Команды:\n"
            "/settings — сменить язык интерфейса\n"
            "/status — статус базы данных"
        ),
        "settings_title": "🌐 Выберите язык интерфейса:",
        "settings_saved": "✅ Язык сохранён: {lang}",
        "not_found": (
            "😕 Ничего не найдено по запросу «{query}».\n"
            "Проверь написание или попробуй английское название предмета."
        ),
        "value_label": "Примерная стоимость",
        "status_label": "Статус",
        "stability_label": "Стабильность",
        "cache_empty": "⏳ База данных загружается, попробуй через минуту.",
        "status_report": "📊 Предметов в базе: {count}\n🕒 Последнее обновление: {last_update}\n⚠️ Ошибка: {error}",
        "status_report_ok": "📊 Предметов в базе: {count}\n🕒 Последнее обновление: {last_update}",
        "never": "ещё не обновлялось",
    },
    "en": {
        "start": (
            "👋 Hi! I'm a bot for checking Murder Mystery 2 item values.\n\n"
            "Just type an item name in Russian or English — for example: <i>Nebula</i>.\n\n"
            "Language settings: /settings"
        ),
        "help": "Type an MM2 item name.\nCommands:\n/settings — change language\n/status — database status",
        "settings_title": "🌐 Choose interface language:",
        "settings_saved": "✅ Language saved: {lang}",
        "not_found": "😕 Nothing found for «{query}».",
        "value_label": "Estimated value",
        "status_label": "Status",
        "stability_label": "Stability",
        "cache_empty": "⏳ Database is loading, please try again in a minute.",
        "status_report": "📊 Items: {count}\n🕒 Last update: {last_update}\n⚠️ Error: {error}",
        "status_report_ok": "📊 Items: {count}\n🕒 Last update: {last_update}",
        "never": "not updated yet",
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


def format_item_caption(item: Item, lang: str) -> str:
    name_en = html.escape(item.name)

    if lang == "ru":
        name_ru = html.escape(get_ru_name(item.name))
        title_line = f"<b>{name_en}</b> ({name_ru})"
    else:
        title_line = f"<b>{name_en}</b>"

    value_line = item.value_display
    if item.ranged_value:
        value_line = f"{item.value_display} [{item.ranged_value}]"

    stability_text = localized_stability(lang, item.stability)

    lines = [
        title_line,
        "",
        f"<i>{t(lang, 'value_label')}:</i>",
        f"Supreme: <b>{value_line}</b>",
        "",
        f"{t(lang, 'status_label')}: <b>{html.escape(item.rarity)}</b>",
        f"{t(lang, 'stability_label')}: <b>{html.escape(stability_text)}</b>",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Обработчики
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
        t(lang_code, "settings_saved", lang=SUPPORTED_LANGS[lang_code])
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

    item, score = results[0]
    caption = format_item_caption(item, lang)

    try:
        if item.image_url:
            await update.message.reply_photo(
                photo=item.image_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(caption, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Ошибка отправки фото, отправляю текстом")
        await update.message.reply_text(caption, parse_mode=ParseMode.HTML)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка обработки апдейта: %s", context.error, exc_info=context.error)


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def main() -> None:
    init_db()
    cache.refresh()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        cache.refresh,
        "interval",
        seconds=REFRESH_INTERVAL_SECONDS,
        id="refresh_values_cache",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CallbackQueryHandler(on_settings_callback, pattern=r"^setlang:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    application.add_error_handler(on_error)

    logger.info("Бот запущен, начинаю polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    # Явное создание asyncio event loop для предотвращения "no current event loop"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        main()
    finally:
        loop.close()
