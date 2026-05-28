import os
import logging
import math
import asyncio
from collections import deque
from datetime import date
from urllib.parse import quote_plus
from openai import AsyncOpenAI
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

SYSTEM_PROMPT = (
    "Ты полезный помощник, который помогает пользователям с выбором и принятием решений. "
    "Когда пользователь описывает ситуацию или задаёт вопрос о выборе, давай чёткие рекомендации "
    "с объяснением плюсов и минусов каждого варианта. Говори на том языке, на котором пишет пользователь. "
    "Всегда обращайся к пользователю по имени, которое тебе передаётся в начале сообщения."
)

IMAGE_DAILY_LIMIT = 3
JIKAN_BASE = "https://api.jikan.moe/v4"
EPS_PER_BOT_PAGE = 100
EPS_PER_ROW = 5
EPS_PER_JIKAN_PAGE = 100
ANIME_PER_PAGE = 10
MAX_ANIME_PAGES = 10  # Топ 100 аниме

# Per-user state
user_histories: dict = {}
user_states: dict = {}
image_usage: dict = {}

# Watch history: user_id -> deque of (anime_id, ep_num), max 300
watch_history: dict[int, deque] = {}
# Last watched episode per anime: user_id -> {anime_id: ep_num}
last_watched: dict[int, dict] = {}
# anime_id -> title (for history display)
anime_title_map: dict[int, str] = {}

WATCH_HISTORY_LIMIT = 300
HIST_PER_PAGE = 10

# Caches
anime_list_cache: dict = {}
anime_detail_cache: dict = {}
episodes_cache: dict = {}
sequels_cache: dict = {}
movies_cache: dict = {}
genre_cache: dict = {}  # (genre_id_str, page) -> list

GENRE_CATEGORIES = [
    ("📡 В эфире сейчас",    "popular"),
    ("🔝 Популярное",        "top"),
    ("⚔️ Экшн",             "1"),
    ("🌿 Фэнтези",          "10"),
    ("🚀 Исекай",           "62"),
    ("💝 Романтика",        "22"),
    ("😂 Комедия",          "4"),
    ("🏫 Школа",            "23"),
    ("🔬 Sci-Fi",           "24"),
    ("👻 Ужасы",            "14"),
    ("⚽ Спорт",            "30"),
    ("🧠 Психологическое",  "40"),
    ("🗺️ Приключения",      "2"),
]


def record_watch(uid: int, anime_id: int, ep_num: int) -> None:
    if uid not in watch_history:
        watch_history[uid] = deque(maxlen=WATCH_HISTORY_LIMIT)
    if uid not in last_watched:
        last_watched[uid] = {}
    watch_history[uid].append((anime_id, ep_num))
    last_watched[uid][anime_id] = ep_num
    # Store title for history display
    if anime_id in anime_detail_cache and anime_id not in anime_title_map:
        anime_title_map[anime_id] = anime_detail_cache[anime_id].get("title", "?")


def get_watched_set(uid: int, anime_id: int) -> set:
    if uid not in watch_history:
        return set()
    return {ep for (aid, ep) in watch_history[uid] if aid == anime_id}


def get_last_ep(uid: int, anime_id: int) -> int | None:
    return last_watched.get(uid, {}).get(anime_id)


def streaming_buttons(title: str) -> list:
    q = quote_plus(title)
    return [
        [
            InlineKeyboardButton("🎙 AniLibria", url=f"https://anilib.me/search?q={q}"),
            InlineKeyboardButton("🎙 AniDUB", url=f"https://anidub.com/?do=search&subaction=search&q={q}"),
        ],
        [
            InlineKeyboardButton("🎭 Дубляж RU", url=f"https://animejoy.ru/index.php?do=search&subaction=search&q={q}"),
            InlineKeyboardButton("🎭 AnimeGo RU", url=f"https://animego.org/search?q={q}"),
        ],
        [
            InlineKeyboardButton("🇯🇵 Субтитры", url=f"https://www.crunchyroll.com/search?q={q}"),
            InlineKeyboardButton("🇺🇸 Eng Dub", url=f"https://www.funimation.com/search/?q={q}"),
        ],
    ]


async def fetch_jikan(path: str, params: dict = None) -> dict | None:
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(f"{JIKAN_BASE}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Jikan API error for %s: %s", path, e)
            return None


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎌 Аниме", callback_data="anime_genres")],
    ])


async def get_sequels(anime_id: int) -> list:
    if anime_id in sequels_cache:
        return sequels_cache[anime_id]
    data = await fetch_jikan(f"/anime/{anime_id}/relations")
    if not data:
        sequels_cache[anime_id] = []
        return []
    sequels = []
    for rel in data.get("data", []):
        if rel.get("relation") == "Sequel":
            for entry in rel.get("entry", []):
                if entry.get("type") == "anime":
                    sequels.append({"mal_id": entry["mal_id"], "name": entry["name"]})
    sequels_cache[anime_id] = sequels
    return sequels


async def get_movies(anime_id: int) -> list:
    if anime_id in movies_cache:
        return movies_cache[anime_id]
    data = await fetch_jikan(f"/anime/{anime_id}/relations")
    if not data:
        movies_cache[anime_id] = []
        return []

    related_ids = []
    for rel in data.get("data", []):
        for entry in rel.get("entry", []):
            if entry.get("type") == "anime":
                related_ids.append(entry["mal_id"])

    async def check_movie(mid: int):
        await asyncio.sleep(0.3)
        detail = await fetch_jikan(f"/anime/{mid}")
        if detail and detail.get("data"):
            d = detail["data"]
            if d.get("type") == "Movie":
                return {"mal_id": mid, "title": d.get("title", "?")}
        return None

    results = await asyncio.gather(*[check_movie(mid) for mid in related_ids[:12]])
    movies = [r for r in results if r is not None]
    movies_cache[anime_id] = movies
    return movies


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name
    uid = update.effective_user.id
    user_histories[uid] = []
    user_states[uid] = "chat"
    await update.message.reply_text(
        f"Привет, {name}! Я помогу тебе с выбором. Напиши любой вопрос или выбери действие:",
        reply_markup=main_menu_keyboard()
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    uid = user.id
    name = user.first_name
    text = update.message.text
    state = user_states.get(uid, "chat")

    if state == "waiting_image":
        user_states[uid] = "chat"
        await _do_generate_image(update, text, name, uid)
        return

    if state == "waiting_anime_search":
        user_states[uid] = "chat"
        await _do_anime_search(update, text)
        return

    if uid not in user_histories:
        user_histories[uid] = []

    user_histories[uid].append({
        "role": "user",
        "content": f"[Меня зовут {name}] {text}"
    })

    thinking = await update.message.reply_text("Думаю...")
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + user_histories[uid]
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=1000,
        )
        answer = response.choices[0].message.content
        user_histories[uid].append({"role": "assistant", "content": answer})
        if len(user_histories[uid]) > 20:
            user_histories[uid] = user_histories[uid][-20:]
        await thinking.delete()
        await update.message.reply_text(answer, reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error("OpenAI error: %s", e)
        await thinking.delete()
        await update.message.reply_text(f"Произошла ошибка, {name}. Попробуй позже.")


async def _do_generate_image(update: Update, prompt: str, name: str, uid: int) -> None:
    today = date.today()
    usage = image_usage.get(uid, {"date": today, "count": 0})
    if usage["date"] != today:
        usage = {"date": today, "count": 0}
    if usage["count"] >= IMAGE_DAILY_LIMIT:
        await update.message.reply_text(
            f"{name}, ты исчерпал дневной лимит ({IMAGE_DAILY_LIMIT} фото). Приходи завтра! 🌙",
            reply_markup=main_menu_keyboard()
        )
        return
    waiting = await update.message.reply_text("🎨 Генерирую изображение, подожди...")
    try:
        response = await openai_client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
        url = response.data[0].url
        usage["count"] += 1
        image_usage[uid] = usage
        remaining = IMAGE_DAILY_LIMIT - usage["count"]
        await waiting.delete()
        await update.message.reply_photo(
            photo=url,
            caption=f"🎨 Готово, {name}! Осталось генераций: {remaining}/{IMAGE_DAILY_LIMIT}",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.error("DALL-E error: %s", e)
        await waiting.delete()
        await update.message.reply_text(
            f"Не удалось сгенерировать изображение, {name}. Попробуй позже.",
            reply_markup=main_menu_keyboard()
        )


async def _do_anime_search(update: Update, query_text: str) -> None:
    searching = await update.message.reply_text(f"🔍 Ищу «{query_text}»...")
    data = await fetch_jikan("/anime", {"q": query_text, "limit": 10, "sfw": True})

    if not data or not data.get("data"):
        await searching.delete()
        await update.message.reply_text(
            "😕 Ничего не найдено. Попробуй другой запрос.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Искать снова", callback_data="anime_search"),
                InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres"),
            ]])
        )
        return

    results = data["data"]
    buttons = []
    for anime in results:
        title = anime.get("title", "?")
        score = anime.get("score") or "?"
        eps = anime.get("episodes") or "?"
        label = f"{title[:28]}… | ⭐{score} | 📺{eps}" if len(title) > 28 else f"{title} | ⭐{score} | 📺{eps}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"anime_d_{anime['mal_id']}")])

    buttons.append([
        InlineKeyboardButton("🔍 Искать снова", callback_data="anime_search"),
        InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres"),
    ])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await searching.delete()
    await update.message.reply_text(
        f"🔍 Результаты по запросу «{query_text}» ({len(results)} найдено):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id
    name = update.effective_user.first_name

    if data == "main_menu":
        user_states[uid] = "chat"
        await query.edit_message_text(
            f"{name}, выбери действие:",
            reply_markup=main_menu_keyboard()
        )

    elif data == "img_start":
        user_states[uid] = "waiting_image"
        await query.edit_message_text(
            f"🎨 {name}, напиши описание изображения:\n"
            f"(дневной лимит: {IMAGE_DAILY_LIMIT} фото)"
        )

    elif data == "anime_genres":
        await show_genre_select(query)

    elif data == "anime_search":
        user_states[uid] = "waiting_anime_search"
        await query.edit_message_text(
            "🔍 Введи название аниме для поиска:"
        )

    elif data.startswith("hist_"):
        hpage = int(data.split("_")[1])
        await show_history(query, uid, hpage)

    elif data.startswith("genre_"):
        parts = data.split("_", 2)
        gid = parts[1]
        gpage = int(parts[2]) if len(parts) > 2 else 1
        await show_genre_anime(query, gid, gpage)

    elif data.startswith("anime_p_"):
        page = int(data.split("_")[2])
        await show_anime_list(query, page)

    elif data.startswith("anime_d_"):
        anime_id = int(data.split("_")[2])
        await show_anime_detail(query, anime_id)

    elif data.startswith("seasons_"):
        anime_id = int(data.split("_")[1])
        await show_seasons(query, anime_id)

    elif data.startswith("movies_"):
        anime_id = int(data.split("_")[1])
        await show_movies(query, anime_id)

    elif data.startswith("eps_"):
        parts = data.split("_")
        anime_id = int(parts[1])
        bot_page = int(parts[2])
        await show_episode_list(query, anime_id, bot_page, uid)

    elif data.startswith("ep_"):
        parts = data.split("_")
        anime_id = int(parts[1])
        ep_num = int(parts[2])
        await show_episode_detail(query, anime_id, ep_num, uid)


async def show_history(query, uid: int, page: int) -> None:
    page = max(1, page)
    hist = list(reversed(watch_history.get(uid, [])))  # newest first

    if not hist:
        await query.edit_message_text(
            "📋 История пуста — начни смотреть аниме!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres"),
            ]])
        )
        return

    # Deduplicate: keep only last-watched ep per anime, preserving order
    seen_ids: set = set()
    unique: list = []
    for anime_id, ep_num in hist:
        if anime_id not in seen_ids:
            seen_ids.add(anime_id)
            unique.append((anime_id, ep_num))

    total = len(unique)
    total_pages = max(1, math.ceil(total / HIST_PER_PAGE))
    page = min(page, total_pages)
    start = (page - 1) * HIST_PER_PAGE
    chunk = unique[start: start + HIST_PER_PAGE]

    buttons = []
    for anime_id, ep_num in chunk:
        title = anime_title_map.get(anime_id, f"Аниме {anime_id}")
        short = title[:22] + "…" if len(title) > 22 else title
        bot_page = max(1, (ep_num - 1) // EPS_PER_BOT_PAGE + 1)
        label = f"▶ {short} — сер. {ep_num}"
        buttons.append([InlineKeyboardButton(
            label, callback_data=f"eps_{anime_id}_{bot_page}"
        )])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"hist_{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("▶ Вперёд", callback_data=f"hist_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres"),
        InlineKeyboardButton("🏠 Меню", callback_data="main_menu"),
    ])

    await query.edit_message_text(
        f"📋 История просмотра — стр. {page}/{total_pages} ({total} аниме):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_genre_select(query) -> None:
    buttons = []
    row = []
    for label, gid in GENRE_CATEGORIES:
        row.append(InlineKeyboardButton(label, callback_data=f"genre_{gid}_1"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("📋 История", callback_data="hist_1"),
        InlineKeyboardButton("🔍 Поиск", callback_data="anime_search"),
    ])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    await query.edit_message_text(
        "🎌 Выбери категорию:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_genre_anime(query, gid: str, page: int) -> None:
    page = max(1, page)
    cache_key = (gid, page)

    # Never cache the airing list — always fetch fresh so new episodes show up
    if gid == "popular" and cache_key in genre_cache:
        del genre_cache[cache_key]

    if cache_key not in genre_cache:
        await query.edit_message_text("⏳ Загружаю аниме...")
        if gid == "popular":
            data = await fetch_jikan(
                "/top/anime",
                {"page": page, "limit": ANIME_PER_PAGE, "filter": "airing"}
            )
        elif gid == "top":
            data = await fetch_jikan(
                "/top/anime",
                {"page": page, "limit": ANIME_PER_PAGE, "filter": "bypopularity"}
            )
        else:
            data = await fetch_jikan(
                "/anime",
                {"genres": gid, "order_by": "score", "sort": "desc",
                 "limit": ANIME_PER_PAGE, "page": page, "sfw": "true"}
            )
        if not data or not data.get("data"):
            await query.edit_message_text(
                "❌ Не удалось загрузить список. Попробуй позже.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres"),
                    InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
                ]])
            )
            return
        genre_cache[cache_key] = data["data"]

    anime_list = genre_cache[cache_key]
    label = next((lbl for lbl, g in GENRE_CATEGORIES if g == gid), "Аниме")
    buttons = []
    for i, anime in enumerate(anime_list):
        rank = (page - 1) * ANIME_PER_PAGE + i + 1
        title = anime.get("title", "?")
        score = anime.get("score") or "?"
        short = title[:28] + "…" if len(title) > 28 else title
        buttons.append([InlineKeyboardButton(
            f"{rank}. {short}  ⭐{score}", callback_data=f"anime_d_{anime['mal_id']}"
        )])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"genre_{gid}_{page - 1}"))
    if len(anime_list) == ANIME_PER_PAGE:
        nav.append(InlineKeyboardButton("▶ Вперёд", callback_data=f"genre_{gid}_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton("🔍 Поиск", callback_data="anime_search"),
        InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres"),
    ])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await query.edit_message_text(
        f"{label} — страница {page}:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_anime_list(query, page: int) -> None:
    page = max(1, min(page, MAX_ANIME_PAGES))

    if page not in anime_list_cache:
        await query.edit_message_text("⏳ Загружаю список аниме...")
        data = await fetch_jikan(
            "/top/anime",
            {"page": page, "limit": ANIME_PER_PAGE, "filter": "bypopularity"}
        )
        if not data or not data.get("data"):
            await query.edit_message_text(
                "❌ Не удалось загрузить список. Попробуй позже.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
                ]])
            )
            return
        anime_list_cache[page] = data["data"]

    anime_list = anime_list_cache[page]
    buttons = []
    for i, anime in enumerate(anime_list):
        rank = (page - 1) * ANIME_PER_PAGE + i + 1
        title = anime["title"]
        if len(title) > 33:
            title = title[:33] + "..."
        buttons.append([InlineKeyboardButton(
            f"{rank}. {title}", callback_data=f"anime_d_{anime['mal_id']}"
        )])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"anime_p_{page - 1}"))
    if page < MAX_ANIME_PAGES:
        nav.append(InlineKeyboardButton("▶ Вперёд", callback_data=f"anime_p_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton("🔍 Поиск", callback_data="anime_search"),
        InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres"),
    ])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await query.edit_message_text(
        f"🔥 Популярные — страница {page} / {MAX_ANIME_PAGES}:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_anime_detail(query, anime_id: int) -> None:
    if anime_id not in anime_detail_cache:
        await query.edit_message_text("⏳ Загружаю информацию...")
        data = await fetch_jikan(f"/anime/{anime_id}")
        if not data or not data.get("data"):
            await query.edit_message_text(
                "❌ Не удалось загрузить информацию.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres")
                ]])
            )
            return
        anime_detail_cache[anime_id] = data["data"]

    anime = anime_detail_cache[anime_id]
    title = anime.get("title", "Неизвестно")
    score = anime.get("score") or "?"
    episodes = anime.get("episodes") or "?"
    status = anime.get("status") or "?"
    genres = ", ".join(g["name"] for g in anime.get("genres", [])[:4])
    synopsis = anime.get("synopsis") or "Нет описания."
    if len(synopsis) > 350:
        synopsis = synopsis[:350] + "..."

    text = f"🎌 *{title}*\n⭐ Оценка: {score}  |  📺 Серий: {episodes}\n📊 {status}"
    if genres:
        text += f"  |  🏷 {genres}"
    text += f"\n\n{synopsis}"

    buttons = [
        [InlineKeyboardButton("🗓 Сезоны", callback_data=f"seasons_{anime_id}"),
         InlineKeyboardButton("🎬 Фильмы", callback_data=f"movies_{anime_id}")],
        [InlineKeyboardButton("⬅ К категориям", callback_data="anime_genres")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )


async def show_seasons(query, anime_id: int) -> None:
    await query.edit_message_text("⏳ Загружаю сезоны...")

    if anime_id not in anime_detail_cache:
        data = await fetch_jikan(f"/anime/{anime_id}")
        if data and data.get("data"):
            anime_detail_cache[anime_id] = data["data"]

    root_title = anime_detail_cache.get(anime_id, {}).get("title", "Сезон 1")
    sequels = await get_sequels(anime_id)

    buttons = []
    s1_title = root_title if len(root_title) <= 32 else root_title[:32] + "..."
    buttons.append([InlineKeyboardButton(
        f"📺 Сезон 1: {s1_title}", callback_data=f"eps_{anime_id}_1"
    )])

    for i, sequel in enumerate(sequels, start=2):
        s_title = sequel["name"] if len(sequel["name"]) <= 28 else sequel["name"][:28] + "..."
        buttons.append([InlineKeyboardButton(
            f"📺 Сезон {i}: {s_title}", callback_data=f"eps_{sequel['mal_id']}_1"
        )])

    if not sequels:
        buttons.append([InlineKeyboardButton(
            "ℹ️ Других сезонов нет", callback_data=f"anime_d_{anime_id}"
        )])

    buttons.append([InlineKeyboardButton("⬅ К аниме", callback_data=f"anime_d_{anime_id}")])
    buttons.append([
        InlineKeyboardButton("🎌 Категории", callback_data="anime_genres"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
    ])

    await query.edit_message_text(
        "🗓 Выбери сезон:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_movies(query, anime_id: int) -> None:
    await query.edit_message_text("⏳ Ищу фильмы...")
    movies = await get_movies(anime_id)

    if not movies:
        await query.edit_message_text(
            "🎬 Фильмов не найдено.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ К аниме", callback_data=f"anime_d_{anime_id}")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ])
        )
        return

    buttons = []
    for movie in movies:
        title = movie["title"] if len(movie["title"]) <= 35 else movie["title"][:35] + "..."
        buttons.append([InlineKeyboardButton(
            f"🎬 {title}", callback_data=f"anime_d_{movie['mal_id']}"
        )])

    buttons.append([InlineKeyboardButton("⬅ К аниме", callback_data=f"anime_d_{anime_id}")])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await query.edit_message_text(
        f"🎬 Фильмы ({len(movies)} найдено):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_episode_list(query, anime_id: int, bot_page: int, uid: int = 0) -> None:
    # Each bot page = 1 Jikan page = up to 100 episodes
    cache_key = (anime_id, bot_page)

    # For airing anime, always fetch fresh so new episodes appear immediately
    is_airing = anime_detail_cache.get(anime_id, {}).get("status") == "Currently Airing"
    if is_airing and cache_key in episodes_cache:
        del episodes_cache[cache_key]

    if cache_key not in episodes_cache:
        await query.edit_message_text("⏳ Загружаю список серий...")
        data = await fetch_jikan(f"/anime/{anime_id}/episodes", {"page": bot_page})
        if not data or not data.get("data"):
            await query.edit_message_text(
                "😕 Серии не найдены.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅ К аниме", callback_data=f"anime_d_{anime_id}")
                ]])
            )
            return
        episodes_cache[cache_key] = {
            "episodes": data["data"],
            "has_next": data.get("pagination", {}).get("has_next_page", False)
        }

    cached = episodes_cache[cache_key]
    eps = cached["episodes"]
    has_prev = bot_page > 1
    has_next = cached["has_next"]

    if not eps:
        await query.edit_message_text(
            "😕 Серий больше нет.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅ К аниме", callback_data=f"anime_d_{anime_id}")
            ]])
        )
        return

    # Watch history markers
    watched = get_watched_set(uid, anime_id)
    last_ep = get_last_ep(uid, anime_id)

    # Build 5-per-row grid with markers
    buttons = []
    row = []
    for i, ep in enumerate(eps):
        ep_num = ep.get("mal_id", i + 1)
        if ep_num == last_ep:
            label = f"▶ {ep_num}"   # current stopped position
        elif ep_num in watched:
            label = f"× {ep_num}"   # already watched
        else:
            label = str(ep_num)
        row.append(InlineKeyboardButton(
            label, callback_data=f"ep_{anime_id}_{ep_num}"
        ))
        if len(row) == EPS_PER_ROW:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Navigation row at the bottom
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"eps_{anime_id}_{bot_page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("▶ Вперёд", callback_data=f"eps_{anime_id}_{bot_page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("⬅ К сезонам", callback_data=f"seasons_{anime_id}"),
        InlineKeyboardButton("⬅ К аниме", callback_data=f"anime_d_{anime_id}"),
    ])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    first_ep_num = eps[0].get("mal_id", 1)
    last_ep_num = eps[-1].get("mal_id", len(eps))
    legend = "  |  ▶ — остановился  ×— просмотрено" if watched else ""
    await query.edit_message_text(
        f"📺 Серии {first_ep_num}–{last_ep_num} (стр. {bot_page}):{legend}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_episode_detail(query, anime_id: int, ep_num: int, uid: int = 0) -> None:
    record_watch(uid, anime_id, ep_num)
    await query.edit_message_text("⏳ Загружаю информацию о серии...")
    data = await fetch_jikan(f"/anime/{anime_id}/episodes/{ep_num}")

    if not data or not data.get("data"):
        await query.edit_message_text(
            "❌ Не удалось загрузить информацию о серии.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅ К сериям", callback_data=f"eps_{anime_id}_1")
            ]])
        )
        return

    ep = data["data"]
    title = ep.get("title") or f"Серия {ep_num}"
    title_jp = ep.get("title_japanese") or ""
    aired = ep.get("aired") or "Неизвестно"
    duration = ep.get("duration")
    score = ep.get("score") or "?"

    # Get anime title for streaming links
    anime_title = anime_detail_cache.get(anime_id, {}).get("title", "anime")

    text = f"📺 *Серия {ep_num}: {title}*\n"
    if title_jp:
        text += f"🇯🇵 _{title_jp}_\n"
    text += f"\n📅 Дата выхода: {aired}\n"
    if duration:
        text += f"⏱ Длительность: {duration} мин.\n"
    text += f"⭐ Оценка: {score}\n\n"
    text += "🎬 *Выбери озвучку и качество:*"

    buttons = streaming_buttons(anime_title)

    nav = []
    if ep_num > 1:
        nav.append(InlineKeyboardButton("◀ Пред. серия", callback_data=f"ep_{anime_id}_{ep_num - 1}"))
    nav.append(InlineKeyboardButton("▶ След. серия", callback_data=f"ep_{anime_id}_{ep_num + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅ К сериям", callback_data=f"eps_{anime_id}_1")])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
