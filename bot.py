# version: 2026-03-28-cookieless-workaround
import discord
from discord.ext import commands
import json
import os
import sys
import asyncio
import aiohttp
import yt_dlp
import wavelink
import calendar
import uuid
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from PIL import Image, ImageDraw, ImageFont

from dotenv import load_dotenv

# =========================
# 경로 / 환경변수
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "/data"
if not os.path.isdir(DATA_DIR):
    DATA_DIR = BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

RESTART_FILE = os.path.join(DATA_DIR, "restart_channel.json")
RESTART_PROCESSING_FILE = os.path.join(DATA_DIR, "restart_channel.processing.json")
MUSIC_STATE_FILE = os.path.join(DATA_DIR, "music_state.json")
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN 환경변수가 비어 있습니다.")

YTDLP_COOKIE_FILE = os.getenv("YTDLP_COOKIE_FILE")
YTDLP_USE_COOKIES = os.getenv("YTDLP_USE_COOKIES", "false").lower() in ("1", "true", "yes", "on")
YTDLP_FORCE_IPV4 = os.getenv("YTDLP_FORCE_IPV4", "true").lower() in ("1", "true", "yes", "on")
YTDLP_USER_AGENT = os.getenv("YTDLP_USER_AGENT") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
YTDLP_DISABLE_WEB_CLIENT = os.getenv("YTDLP_DISABLE_WEB_CLIENT", "false").lower() in ("1", "true", "yes", "on")

SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule.json")
COLORS_FILE = os.path.join(DATA_DIR, "colors.json")
FONT_FILE = os.path.join(BASE_DIR, "onglefont.ttf")

# =========================
# 기본 설정
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def make_queue_item(channel_id: int | None, query: str):
    return (channel_id, query)


def unpack_queue_item(item):
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return item[0], item[1]
    return None, None


def get_text_channel_from_id(channel_id: int | None):
    if not channel_id:
        return None
    return bot.get_channel(channel_id)


async def send_music_message(guild_id: int, text: str, *, view=None):
    state = get_music_state(guild_id)
    channel = get_text_channel_from_id(state.get("last_text_channel_id"))
    if channel:
        try:
            await channel.send(text, view=view)
        except Exception as e:
            print(f"음악 메시지 전송 실패: {e}")


async def ensure_lavalink_ready():
    if getattr(bot, "_lavalink_connected", False):
        return

    host = os.getenv("LAVALINK_HOST")
    port = int(os.getenv("LAVALINK_PORT", "2333"))
    password = os.getenv("LAVALINK_PASSWORD")
    secure = os.getenv("LAVALINK_SECURE", "false").lower() in ("1", "true", "yes", "on")

    if not host or not password:
        raise RuntimeError("LAVALINK_HOST 또는 LAVALINK_PASSWORD 환경변수가 비어 있습니다.")

    if not wavelink.Pool.nodes:
        scheme = "https" if secure else "http"
        node = wavelink.Node(uri=f"{scheme}://{host}:{port}", password=password)
        await wavelink.Pool.connect(nodes=[node], client=bot)

    bot._lavalink_connected = True


def resolve_voice_client(ctx):
    vc = ctx.voice_client
    return vc if isinstance(vc, wavelink.Player) else None


def player_is_playing(player) -> bool:
    if not player:
        return False
    value = getattr(player, "playing", None)
    if value is not None:
        return bool(value)
    checker = getattr(player, "is_playing", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


def player_is_paused(player) -> bool:
    if not player:
        return False
    value = getattr(player, "paused", None)
    if value is not None:
        return bool(value)
    checker = getattr(player, "is_paused", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


async def get_or_connect_player(ctx):
    await ensure_lavalink_ready()

    if ctx.author.voice is None or ctx.author.voice.channel is None:
        raise ValueError("음성 채널 먼저 들어가줘")

    channel = ctx.author.voice.channel
    existing_vc = ctx.voice_client
    player = resolve_voice_client(ctx)

    if existing_vc is not None and player is None:
        try:
            await existing_vc.disconnect(force=True)
        except Exception:
            try:
                await existing_vc.disconnect()
            except Exception:
                pass
        player = None

    if player is None:
        player = await channel.connect(cls=wavelink.Player, self_deaf=False, self_mute=False)
    elif player.channel != channel:
        await player.move_to(channel)

    state = get_music_state(ctx.guild.id)
    state["last_voice_channel_id"] = channel.id
    state["last_text_channel_id"] = ctx.channel.id
    save_music_data()
    return player


async def search_lavalink_track(query: str):
    candidates = build_query_candidates(query)
    last_error = None
    query_norm = normalize_song_text(query)

    def track_score(track):
        title = normalize_song_text(getattr(track, "title", ""))
        author = normalize_song_text(getattr(track, "author", ""))
        score = 0
        if query_norm and query_norm in title:
            score += 15
        artist, song_title = extract_artist_title(query)
        if artist:
            artist_norm = normalize_song_text(artist)
            title_norm = normalize_song_text(song_title)
            if artist_norm in title or artist_norm in author:
                score += 10
            if title_norm and title_norm in title:
                score += 10
        for word in POSITIVE_TITLE_KEYWORDS:
            if word in title:
                score += 2
        for word in NEGATIVE_TITLE_KEYWORDS:
            if word in title:
                score -= 5
        return score

    for candidate in candidates:
        target = candidate
        if not target.startswith(("http://", "https://", "ytsearch:", "ytmsearch:", "scsearch:")):
            target = f"ytsearch:{target}"

        try:
            result = await wavelink.Playable.search(target)
        except Exception as e:
            last_error = e
            continue

        tracks = getattr(result, "tracks", result)
        try:
            tracks = list(tracks)
        except Exception:
            tracks = []

        if tracks:
            tracks.sort(key=track_score, reverse=True)
            return tracks[0], candidate

    if last_error is not None:
        raise ValueError(f"Lavalink 검색 실패: {last_error}")
    raise ValueError("검색 결과가 없습니다.")

RESTARTING = False

schedule = []
user_colors = {}
sent_alerts = set()
schedule_task_started = False

music_queues = {}
music_states = {}

# =========================
# 색상 설정
# =========================
PASTEL_COLORS = {
    "pastel_pink": {"label": "🌸 핑크", "rgb": [245, 168, 184]},
    "pastel_red": {"label": "🍓 레드", "rgb": [239, 154, 154]},
    "pastel_orange": {"label": "🍊 오렌지", "rgb": [255, 183, 128]},
    "pastel_yellow": {"label": "🌼 옐로우", "rgb": [255, 236, 153]},
    "pastel_lime": {"label": "🥝 라임", "rgb": [200, 230, 160]},
    "pastel_green": {"label": "🌿 그린", "rgb": [167, 225, 188]},
    "pastel_mint": {"label": "🍀 민트", "rgb": [170, 240, 209]},
    "pastel_sky": {"label": "☁️ 스카이", "rgb": [173, 216, 255]},
    "pastel_blue": {"label": "🌊 블루", "rgb": [162, 196, 255]},
    "pastel_purple": {"label": "🍇 퍼플", "rgb": [200, 180, 255]},
}
DEFAULT_COLOR = PASTEL_COLORS["pastel_blue"]["rgb"]

# =========================
# 음악 설정
# =========================
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -loglevel panic -bufsize 64k"
}


def resolve_cookie_file():
    if not YTDLP_USE_COOKIES:
        return None

    candidates = [
        YTDLP_COOKIE_FILE,
        os.path.join(DATA_DIR, "cookies.txt"),
        os.path.join(BASE_DIR, "cookies.txt"),
    ]

    for path in candidates:
        if path and os.path.isfile(path):
            return path

    return None



class QuietYTDLPLogger:
    def debug(self, msg):
        lowered = str(msg).lower()
        noisy_keywords = [
            "sign in to confirm",
            "requested format is not available",
            "downloading webpage",
            "downloading player",
            "extracting url",
        ]
        if any(keyword in lowered for keyword in noisy_keywords):
            return
        print(msg)

    def warning(self, msg):
        lowered = str(msg).lower()
        noisy_keywords = [
            "sign in to confirm",
            "requested format is not available",
            "player response",
            "unable to download webpage",
        ]
        if any(keyword in lowered for keyword in noisy_keywords):
            return
        print(msg)

    def error(self, msg):
        lowered = str(msg).lower()
        noisy_keywords = [
            "sign in to confirm",
            "requested format is not available",
        ]
        if any(keyword in lowered for keyword in noisy_keywords):
            return
        print(msg)


YTDL_OPTIONS = {
    "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch5",
    "skip_download": True,
    "retries": int(os.getenv("YTDLP_MAX_RETRIES", "2")),
    "fragment_retries": int(os.getenv("YTDLP_MAX_RETRIES", "2")),
    "socket_timeout": int(os.getenv("YTDLP_TIMEOUT", "10")),
    "nocheckcertificate": True,
    "geo_bypass": True,
    "youtube_include_dash_manifest": False,
    "youtube_include_hls_manifest": False,
    "http_headers": {
        "User-Agent": YTDLP_USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["android"] if not YTDLP_USE_COOKIES else (["android", "ios", "mweb"] if YTDLP_DISABLE_WEB_CLIENT else ["android", "ios", "mweb", "web_creator", "web"]),
            "player_skip": ["webpage", "configs"]
        }
    },
    "logger": QuietYTDLPLogger(),
}
if YTDLP_FORCE_IPV4:
    YTDL_OPTIONS["source_address"] = "0.0.0.0"


def create_ytdl():

    options = dict(YTDL_OPTIONS)
    cookie_file = resolve_cookie_file()
    if cookie_file:
        options["cookiefile"] = cookie_file
    return yt_dlp.YoutubeDL(options)


def is_blocked_music_error(error_text: str) -> bool:
    lowered = error_text.lower()
    blocked_keywords = [
        "429",
        "too many requests",
        "sign in to confirm",
        "not a bot",
        "confirm you're not a bot",
        "use --cookies-from-browser",
        "video unavailable",
        "requested format is not available",
        "unable to download api page",
        "precondition check failed",
        "this content isn't available",
        "signature solving failed",
        "only images are available for download",
        "failed to extract any player response",
        "unable to fetch gvs po token"
    ]
    return any(keyword in lowered for keyword in blocked_keywords)


def sanitize_music_error(error: Exception) -> str:
    error_text = str(error)
    if is_blocked_music_error(error_text):
        if resolve_cookie_file():
            return "❌ 유튜브 요청 제한에 걸렸어... 잠시 후 다시 시도해줘!"
        return "❌ 유튜브 요청 제한에 걸렸어. cookies.txt를 넣어주면 훨씬 안정적으로 재생할 수 있어!"
    lowered = error_text.lower()
    if "no results" in lowered or "not found" in lowered:
        return "❌ 검색 결과를 찾지 못했어. 가수명이나 곡명을 더 정확하게 입력해줘!"
    return "❌ 노래를 재생할 수 없어. 다른 노래로 시도해줘!"



POPULAR_SONG_HINTS = {
    "아이유": ["아이유 좋은날", "아이유 밤편지", "아이유 blueming", "아이유 라일락"],
    "iu": ["IU Good Day", "IU Through the Night", "IU Blueming", "IU LILAC"],
    "뉴진스": ["NewJeans Hype Boy", "NewJeans Ditto", "NewJeans Super Shy", "NewJeans Attention"],
    "newjeans": ["NewJeans Hype Boy", "NewJeans Ditto", "NewJeans Super Shy", "NewJeans Attention"],
    "아이브": ["IVE I AM", "IVE LOVE DIVE", "IVE After LIKE", "IVE 해야"],
    "ive": ["IVE I AM", "IVE LOVE DIVE", "IVE After LIKE", "IVE 해야"],
    "방탄소년단": ["BTS Dynamite", "BTS 봄날", "BTS Butter", "BTS 작은 것들을 위한 시"],
    "bts": ["BTS Dynamite", "BTS Spring Day", "BTS Butter", "BTS Boy With Luv"],
    "블랙핑크": ["BLACKPINK How You Like That", "BLACKPINK Pink Venom", "BLACKPINK Shut Down"],
    "blackpink": ["BLACKPINK How You Like That", "BLACKPINK Pink Venom", "BLACKPINK Shut Down"],
    "에스파": ["aespa Supernova", "aespa Drama", "aespa Next Level"],
    "aespa": ["aespa Supernova", "aespa Drama", "aespa Next Level"],
}

NEGATIVE_TITLE_KEYWORDS = [
    "cover", "karaoke", "mr", "inst", "instrumental", "reaction", "shorts",
    "sped up", "speed up", "slowed", "reverb", "live clip", "teaser", "preview"
]

POSITIVE_TITLE_KEYWORDS = [
    "official audio", "official", "audio", "topic", "lyrics", "mv", "music video"
]


def normalize_song_text(text_value: str) -> str:
    return " ".join((text_value or "").strip().lower().split())


def guess_artist_song_candidates(query: str):
    normalized = normalize_song_text(query)
    candidates = []

    for artist_key, songs in POPULAR_SONG_HINTS.items():
        if normalized == artist_key or normalized == f"{artist_key} 노래" or normalized == f"{artist_key} 노래 추천":
            candidates.extend(songs)
            break

    if not candidates and normalized.endswith(" 노래"):
        artist = normalized[:-3].strip()
        for artist_key, songs in POPULAR_SONG_HINTS.items():
            if artist == artist_key:
                candidates.extend(songs)
                break

    unique = []
    for item in candidates:
        if item not in unique:
            unique.append(item)
    return unique[:4]


def score_entry_for_query(entry: dict, query: str) -> int:
    title = normalize_song_text(entry.get("title", ""))
    uploader = normalize_song_text(entry.get("uploader", ""))
    description = normalize_song_text(entry.get("description", ""))[:500]
    query_norm = normalize_song_text(query)

    score = 0
    if query_norm and query_norm in title:
        score += 10

    artist, song_title = extract_artist_title(query)
    if artist:
        artist_norm = normalize_song_text(artist)
        title_norm = normalize_song_text(song_title)
        if artist_norm in title or artist_norm in uploader:
            score += 12
        if title_norm and title_norm in title:
            score += 12

    for word in POSITIVE_TITLE_KEYWORDS:
        if word in title or word in description:
            score += 4

    for word in NEGATIVE_TITLE_KEYWORDS:
        if word in title or word in description:
            score -= 8

    if "topic" in uploader:
        score += 6

    duration = entry.get("duration")
    if isinstance(duration, (int, float)):
        if 90 <= duration <= 420:
            score += 3
        elif duration < 45 or duration > 900:
            score -= 6

    return score


def is_youtube_playlist_url(query: str) -> bool:
    try:
        parsed = urlparse(query.strip())
        if parsed.netloc and ("youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc):
            qs = parse_qs(parsed.query)
            return "list" in qs and not ("v" in qs and query.strip().lower().startswith("ytsearch"))
    except Exception:
        return False
    return False


async def extract_playlist_entries(query: str):
    loop = asyncio.get_running_loop()

    def extract():
        options = dict(YTDL_OPTIONS)
        options["extract_flat"] = True
        options["skip_download"] = True
        options["noplaylist"] = False
        cookie_file = resolve_cookie_file()
        if cookie_file:
            options["cookiefile"] = cookie_file
        return yt_dlp.YoutubeDL(options).extract_info(query, download=False)

    data = await loop.run_in_executor(None, extract)
    entries = []
    if isinstance(data, dict):
        for entry in (data.get("entries") or []):
            if not entry:
                continue
            url = entry.get("url")
            webpage_url = entry.get("webpage_url")
            title = entry.get("title") or "제목 없음"
            if webpage_url:
                entries.append((title, webpage_url))
            elif url:
                if str(url).startswith("http"):
                    entries.append((title, url))
                else:
                    entries.append((title, f"https://www.youtube.com/watch?v={url}"))
    return entries



def build_query_candidates(query: str):
    candidates = []

    def add(value: str):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    artist, title = extract_artist_title(query)

    add(query)
    add(f"ytsearch1:{query}")

    if artist and title:
        base = f"{artist} {title}".strip()
        add(f"ytsearch1:{base} official audio")
        add(f"ytsearch1:{base} topic")
        add(f"{artist} - {title}")
    else:
        add(f"ytsearch1:{query} official audio")
        add(f"ytsearch1:{query} topic")

    guessed = guess_artist_song_candidates(query)
    if guessed:
        add(guessed[0])

    return candidates[:4]



class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.webpage_url = data.get("webpage_url")
        self.original_url = data.get("original_url")

    @classmethod
    async def from_query(cls, query: str):
        loop = asyncio.get_running_loop()

        def extract_once(target_query: str):
            return create_ytdl().extract_info(target_query, download=False)

        def normalize_entries(data):
            if not data:
                return []
            if isinstance(data, dict) and "entries" in data:
                return [entry for entry in (data.get("entries") or []) if entry]
            return [data]

        blocked_error_seen = False
        last_error = None

        for candidate in build_query_candidates(query):
            try:
                data = await loop.run_in_executor(None, lambda c=candidate: extract_once(c))
            except Exception as e:
                last_error = e
                if is_blocked_music_error(str(e)):
                    blocked_error_seen = True
                continue

            entries = normalize_entries(data)
            if not entries:
                continue

            # 검색 품질 점수화
            scored_entries = []
            for entry in entries[:4]:
                if not entry:
                    continue
                score = score_entry_for_query(entry, query)
                scored_entries.append((score, entry))
            scored_entries.sort(key=lambda item: item[0], reverse=True)

            for _, entry in scored_entries[:1]:
                current_data = entry
                audio_url = current_data.get("url")

                if not audio_url:
                    webpage_url = current_data.get("webpage_url") or current_data.get("original_url")
                    if webpage_url:
                        try:
                            current_data = await loop.run_in_executor(None, lambda u=webpage_url: extract_once(u))
                            audio_url = current_data.get("url")
                        except Exception as e:
                            last_error = e
                            if is_blocked_music_error(str(e)):
                                blocked_error_seen = True
                            continue

                if not audio_url:
                    continue

                try:
                    source = discord.FFmpegPCMAudio(audio_url, executable="ffmpeg", **FFMPEG_OPTIONS)
                    return cls(source, data=current_data)
                except Exception as e:
                    last_error = e
                    continue

        if blocked_error_seen:
            if resolve_cookie_file():
                raise ValueError("유튜브 요청이 잠시 많아서 재생이 어려워. 잠깐 뒤에 다시 시도해줘.")
            raise ValueError("유튜브 요청 제한 때문에 재생이 막혔어. cookies.txt를 넣은 뒤 다시 시도해줘.")
        if last_error is not None:
            raise ValueError(sanitize_music_error(last_error).replace("❌ ", ""))
        raise ValueError("검색 결과가 없습니다.")




def build_resolve_attempts(query: str):
    attempts = []

    def add(value: str):
        value = (value or "").strip()
        if value and value not in attempts:
            attempts.append(value)

    add(query)

    artist, title = extract_artist_title(query)
    if artist and title:
        add(f"{artist} {title} official audio")
        add(f"{artist} {title} topic")
    else:
        add(f"{query} official audio")
        guessed = guess_artist_song_candidates(query)
        if guessed:
            add(guessed[0])

    return attempts[:3]



async def try_resolve_player_with_fallback(query: str):
    attempted_queries = []
    last_error = None

    for candidate in build_resolve_attempts(query):
        attempted_queries.append(candidate)
        try:
            player = await YTDLSource.from_query(candidate)
            return player, attempted_queries
        except Exception as e:
            last_error = e
            if is_blocked_music_error(str(e)):
                break
            continue

    if last_error is not None:
        raise last_error

    player = await YTDLSource.from_query(query)
    return player, attempted_queries or [query]

# =========================
# 파일 저장 / 불러오기
# =========================
def save_schedule():
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(schedule, f, ensure_ascii=False, indent=2)


def load_schedule():
    global schedule
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            schedule = json.load(f)
    else:
        schedule = []


def save_colors():
    with open(COLORS_FILE, "w", encoding="utf-8") as f:
        json.dump(user_colors, f, ensure_ascii=False, indent=2)


def load_colors():
    global user_colors
    if os.path.exists(COLORS_FILE):
        with open(COLORS_FILE, "r", encoding="utf-8") as f:
            user_colors = json.load(f)
    else:
        user_colors = {}


def save_music_data():
    data = {}

    guild_ids = set(music_states.keys()) | set(music_queues.keys())
    for guild_id in guild_ids:
        state = get_music_state(guild_id)
        queue = get_guild_queue(guild_id)

        current_query = None
        if state.get("current") and state["current"].get("query"):
            current_query = state["current"]["query"]

        queue_queries = [query for _, query in queue if isinstance(query, str)]

        data[str(guild_id)] = {
            "last_query": state.get("last_query") or current_query,
            "last_voice_channel_id": state.get("last_voice_channel_id"),
            "repeat": state.get("repeat", False),
            "history": [item for item in state.get("history", []) if isinstance(item, str)][-20:],
            "current_query": current_query,
            "queue": queue_queries,
        }

    with open(MUSIC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_music_data():
    if not os.path.exists(MUSIC_STATE_FILE):
        return

    try:
        with open(MUSIC_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"music_state.json 로드 실패: {e}")
        return

    if not isinstance(data, dict):
        return

    for guild_id_text, saved in data.items():
        try:
            guild_id = int(guild_id_text)
        except (TypeError, ValueError):
            continue

        state = get_music_state(guild_id)
        state["last_query"] = saved.get("last_query") or saved.get("current_query")
        state["last_voice_channel_id"] = saved.get("last_voice_channel_id")
        state["repeat"] = bool(saved.get("repeat", False))
        state["history"] = [item for item in saved.get("history", []) if isinstance(item, str)][-20:]
        state["restored_queue"] = [item for item in saved.get("queue", []) if isinstance(item, str)]


# =========================
# 공통 유틸
# =========================
def resolve_font_path():
    candidates = [
        FONT_FILE,
        os.path.join(BASE_DIR, "onglefont.ttf"),
        os.path.join(os.getcwd(), "onglefont.ttf"),
        os.path.join(DATA_DIR, "onglefont.ttf"),
        "/mnt/data/onglefont.ttf",
    ]

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    return None


def get_font(size: int):
    font_path = resolve_font_path()
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception as e:
            print(f"[폰트 오류] {e} | path={font_path}")
    else:
        print("[폰트 오류] onglefont.ttf 파일을 찾지 못함")
    return ImageFont.load_default()

def safe_text(text: str, limit: int):
    return text if len(text) <= limit else text[:limit]


def split_text(text: str, size: int = 1900):
    if not text:
        return ["내용 없음"]
    return [text[i:i + size] for i in range(0, len(text), size)]


def extract_artist_title(song: str):
    if " - " in song:
        artist, title = song.split(" - ", 1)
        return artist.strip(), title.strip()
    return None, song.strip()


def get_month_schedule_map(year: int, month: int):
    date_map = {}
    for item in schedule:
        dt_str = item.get("datetime", "")
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue

        if dt.year == year and dt.month == month:
            date_map.setdefault(dt.day, []).append(item)

    return date_map


def get_guild_queue(guild_id: int):
    if guild_id not in music_queues:
        music_queues[guild_id] = []
    return music_queues[guild_id]


def get_music_state(guild_id: int):
    if guild_id not in music_states:
        music_states[guild_id] = {
            "current": None,
            "repeat": False,
            "history": [],
            "last_query": None,
            "last_voice_channel_id": None,
            "restored_queue": [],
            "fail_count": 0,
            "blocked_fail_count": 0,
            "auto_skipped_count": 0
        }
    return music_states[guild_id]


async def send_queue_list(channel, guild_id: int):
    queue = get_guild_queue(guild_id)
    state = get_music_state(guild_id)

    lines = []

    if state["current"]:
        lines.append(f"🎵 현재곡: {state['current']['title']}")
    else:
        lines.append("🎵 현재 재생 중인 곡 없음")

    lines.append("")

    if queue:
        lines.append("📜 대기열")
        for i, (_, query) in enumerate(queue, start=1):
            lines.append(f"{i}. {query}")
    else:
        lines.append("📜 대기열 비어 있음")

    text = "\n".join(lines)
    for chunk in split_text(text, 1800):
        await channel.send(f"```{chunk}```")


async def send_schedule_list_message(target):
    if not schedule:
        await target.send("등록된 일정이 없어")
        return

    lines = []
    for i, item in enumerate(schedule, start=1):
        alert_text = "🔔" if item.get("alert_enabled") else "—"
        lines.append(f"{i}. {item['datetime']} | {item['text']} | {item.get('name', '사용자')} | {alert_text}")

    text = "\n".join(lines)
    for chunk in split_text(text, 1800):
        await target.send(f"```{chunk}```")


# =========================
# 일정 알림 체크
# =========================
async def check_schedule():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        for item in schedule:
            event_dt = item.get("datetime")
            channel_id = item.get("channel_id")
            user_id = item.get("user_id")

            if not event_dt or not channel_id:
                continue

            channel = bot.get_channel(channel_id)
            if channel is None:
                continue

            if item.get("alert_enabled", False):
                try:
                    event_time = datetime.strptime(event_dt, "%Y-%m-%d %H:%M")
                except ValueError:
                    continue

                if event_dt not in sent_alerts and now == event_dt:
                    try:
                        await channel.send(f"🔔 {item['name']}님의 일정 알림: {item['text']}")
                    except Exception:
                        pass

                    if user_id:
                        user = bot.get_user(user_id)
                        if user is None:
                            try:
                                user = await bot.fetch_user(user_id)
                            except Exception:
                                user = None

                        if user:
                            try:
                                await user.send(f"🔔 일정 알림: {item['text']} ({event_dt})")
                            except Exception:
                                pass

                    sent_alerts.add(event_dt)

                if item.get("alert_10min", False):
                    before_dt = (event_time - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
                    before_key = f"{event_dt}_10min"

                    if before_key not in sent_alerts and now == before_dt:
                        try:
                            await channel.send(f"⏰ 10분 전 알림: {item['name']}님의 일정 {item['text']}")
                        except Exception:
                            pass

                        if user_id:
                            user = bot.get_user(user_id)
                            if user is None:
                                try:
                                    user = await bot.fetch_user(user_id)
                                except Exception:
                                    user = None

                            if user:
                                try:
                                    await user.send(f"⏰ 10분 전 알림: {item['text']} ({event_dt})")
                                except Exception:
                                    pass

                        sent_alerts.add(before_key)

        await asyncio.sleep(30)


# =========================
# 음악 재생
# =========================
async def play_next(guild_id: int):
    queue = get_guild_queue(guild_id)
    state = get_music_state(guild_id)
    guild = bot.get_guild(guild_id)

    if guild is None:
        state["current"] = None
        save_music_data()
        return

    player = guild.voice_client if isinstance(guild.voice_client, wavelink.Player) else None

    if not queue:
        state["current"] = None
        save_music_data()
        return

    channel_id, query = unpack_queue_item(queue.pop(0))
    if channel_id:
        state["last_text_channel_id"] = channel_id

    if player is None:
        state["current"] = None
        save_music_data()
        await send_music_message(guild_id, "❌ 음성 채널 연결이 끊어졌어. 다시 !입장 후 !재생 해줘")
        return

    try:
        track, matched_query = await search_lavalink_track(query)

        state["current"] = {
            "title": getattr(track, "title", query),
            "query": query,
            "url": getattr(track, "uri", None)
        }
        state["last_query"] = query
        state["fail_count"] = 0
        if getattr(player, "channel", None):
            state["last_voice_channel_id"] = player.channel.id
        state["restored_queue"] = [saved_query for _, saved_query in (unpack_queue_item(item) for item in queue) if isinstance(saved_query, str)]
        save_music_data()

        await player.play(track)

        extra_line = ""
        if matched_query != query:
            extra_line = f"\n검색 보정: `{matched_query}`"

        await send_music_message(
            guild_id,
            f"🎵 재생 중: **{getattr(track, 'title', query)}**\n대기열: {len(queue)}곡{extra_line}",
            view=MusicView(guild_id)
        )

    except Exception as e:
        error_text = str(e)
        print(f"곡 재생 실패, 자동 스킵: {query} | {error_text}")

        state["fail_count"] = state.get("fail_count", 0) + 1
        state["auto_skipped_count"] = state.get("auto_skipped_count", 0) + 1
        state["current"] = None
        save_music_data()

        if queue:
            await send_music_message(guild_id, f"⚠️ `{query}` 재생 실패 → 자동으로 다음 곡으로 넘어갈게")
            await asyncio.sleep(1)
            await play_next(guild_id)
        else:
            await send_music_message(guild_id, f"⚠️ `{query}` 재생 실패했고, 다음 곡이 없어서 정지할게")


# =========================
# 캘린더 이미지 생성
# =========================
def create_calendar_image(year: int, month: int):
    width, height = 1100, 1300
    image = Image.new("RGB", (width, height), (20, 20, 24))
    draw = ImageDraw.Draw(image)

    card_bg = (245, 243, 247)
    card_outline = (223, 219, 231)
    cell_bg = (240, 238, 242)
    cell_outline = (226, 221, 232)
    title_color = (110, 101, 171)
    text_main = (84, 82, 96)
    sat_blue = (120, 146, 227)
    sun_red = (222, 128, 134)
    today_outline = (246, 102, 102)
    today_fill = (250, 247, 248)
    section_bg = (236, 234, 239)

    title_font = get_font(44)
    header_font = get_font(22)
    day_font = get_font(24)
    schedule_font = get_font(17)
    bottom_title_font = get_font(22)
    bottom_text_font = get_font(17)

    card_x1, card_y1, card_x2, card_y2 = 55, 40, 1045, 1210
    draw.rounded_rectangle((card_x1, card_y1, card_x2, card_y2), radius=28, fill=card_bg, outline=card_outline, width=3)
    draw.rounded_rectangle((card_x1 + 12, card_y1 + 12, card_x2 - 12, card_y2 - 12), radius=24, outline=(232, 228, 237), width=2)

    title = f"{year}년 {month:02d}월"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    title_w = bbox[2] - bbox[0]
    draw.text(((width - title_w) / 2, 78), title, fill=title_color, font=title_font)

    days = ["월", "화", "수", "목", "금", "토", "일"]
    grid_left = 105
    grid_top = 210
    cell_w = 124
    cell_h = 110
    gap_x = 10
    gap_y = 10

    for i, day_name in enumerate(days):
        color = text_main
        if i == 5:
            color = sat_blue
        elif i == 6:
            color = sun_red

        bbox = draw.textbbox((0, 0), day_name, font=header_font)
        tw = bbox[2] - bbox[0]
        draw.text((grid_left + i * (cell_w + gap_x) + (cell_w - tw) / 2, 156), day_name, fill=color, font=header_font)

    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)
    date_map = get_month_schedule_map(year, month)

    now = datetime.now()
    is_current_month = (now.year == year and now.month == month)

    for row_idx, week in enumerate(month_days):
        for col_idx, day_num in enumerate(week):
            x1 = grid_left + col_idx * (cell_w + gap_x)
            y1 = grid_top + row_idx * (cell_h + gap_y)
            x2 = x1 + cell_w
            y2 = y1 + cell_h

            draw.rounded_rectangle((x1, y1, x2, y2), radius=16, fill=cell_bg, outline=cell_outline, width=2)

            if day_num == 0:
                continue

            day_color = text_main
            if col_idx == 5:
                day_color = sat_blue
            elif col_idx == 6:
                day_color = sun_red

            if is_current_month and day_num == now.day:
                draw.rounded_rectangle((x1, y1, x2, y2), radius=16, fill=today_fill, outline=today_outline, width=4)

            draw.text((x1 + 12, y1 + 8), str(day_num), fill=day_color, font=day_font)

            items = date_map.get(day_num, [])
            preview_y = y1 + 44

            for idx, item in enumerate(items[:2]):
                preview = safe_text(item["text"], 8)
                draw.text((x1 + 10, preview_y + idx * 22), preview, fill=(85, 83, 92), font=schedule_font)

            if len(items) > 2:
                more_text = f"+{len(items) - 2}"
                draw.text((x1 + 10, preview_y + 44), more_text, fill=(120, 115, 130), font=schedule_font)

    section_x1, section_y1, section_x2, section_y2 = 95, 1040, 1005, 1170
    draw.rounded_rectangle((section_x1, section_y1, section_x2, section_y2), radius=18, fill=section_bg, outline=cell_outline, width=2)
    draw.text((section_x1 + 18, section_y1 + 14), "오늘 일정", fill=title_color, font=bottom_title_font)

    today_items = []
    for item in schedule:
        dt_str = item.get("datetime", "")
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue

        if dt.year == year and dt.month == month and is_current_month and dt.day == now.day:
            today_items.append(item)

    if today_items:
        for idx, item in enumerate(today_items[:3]):
            line = f"- {item['datetime'][11:16]} {item['text']}"
            draw.text((section_x1 + 18, section_y1 + 52 + idx * 26), line, fill=text_main, font=bottom_text_font)
    else:
        draw.text((section_x1 + 18, section_y1 + 56), "오늘 일정 없음", fill=(135, 131, 142), font=bottom_text_font)

    output_file = os.path.join(BASE_DIR, f"calendar_{year}_{month}_{uuid.uuid4().hex[:8]}.png")
    image.save(output_file)
    return output_file


# =========================
# 도움말 UI
# =========================
class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="🎵 노래", style=discord.ButtonStyle.primary)
    async def music_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = (
            "🎵 노래 명령어\n\n"
            "!입장\n"
            "!퇴장\n"
            "!재생 노래이름\n!재생 유튜브플레이리스트URL\n"
            "!정지\n"
            "!일시정지\n"
            "!다시재생\n"
            "!가사 가수 - 제목\n"
            "!노래리스트"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="📅 일정", style=discord.ButtonStyle.success)
    async def schedule_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = (
            "📅 일정 명령어\n\n"
            "!캘린더\n"
            "!캘린더 2026 03\n"
            "!일정추가 날짜 시간 내용\n"
            "!일정삭제 번호\n"
            "!일정목록\n!월이동 또는 버튼 사용"
        )
        await interaction.response.send_message(text, ephemeral=True)


class HelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📖 도움말", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("보고 싶은 기능을 골라줘", view=HelpView(), ephemeral=True)


class ScheduleHelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📖 도움말", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        text = (
            "📅 일정 명령어\n\n"
            "!캘린더\n"
            "!캘린더 2026 03\n"
            "!일정추가 날짜 시간 내용\n"
            "!일정삭제 번호\n"
            "!일정목록"
        )
        await interaction.response.send_message(text, ephemeral=True)


class ScheduleListButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📋 일정리스트", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not schedule:
            await interaction.response.send_message("등록된 일정이 없어", ephemeral=True)
            return

        lines = []
        for i, item in enumerate(schedule, start=1):
            alert_text = "🔔" if item.get("alert_enabled") else "—"
            lines.append(f"{i}. {item['datetime']} | {item['text']} | {item.get('name', '사용자')} | {alert_text}")

        text = "\n".join(lines)
        chunks = split_text(text, 1800)

        await interaction.response.send_message(f"```{chunks[0]}```", ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(f"```{chunk}```", ephemeral=True)


# =========================
# 색상 UI
# =========================
class ColorSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=PASTEL_COLORS["pastel_pink"]["label"], value="pastel_pink"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_red"]["label"], value="pastel_red"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_orange"]["label"], value="pastel_orange"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_yellow"]["label"], value="pastel_yellow"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_lime"]["label"], value="pastel_lime"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_green"]["label"], value="pastel_green"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_mint"]["label"], value="pastel_mint"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_sky"]["label"], value="pastel_sky"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_blue"]["label"], value="pastel_blue"),
            discord.SelectOption(label=PASTEL_COLORS["pastel_purple"]["label"], value="pastel_purple"),
        ]
        super().__init__(placeholder="원하는 파스텔 색을 골라줘", options=options)

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        selected_key = self.values[0]
        user_colors[user_id] = PASTEL_COLORS[selected_key]["rgb"]
        save_colors()
        await interaction.response.send_message(f"🎨 색 설정 완료: {PASTEL_COLORS[selected_key]['label']}", ephemeral=True)


class ColorButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🎨 색 선택", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        view = discord.ui.View()
        view.add_item(ColorSelect())
        await interaction.response.send_message("색을 보고 골라줘", view=view, ephemeral=True)


# =========================
# 모달 UI
# =========================
class AddScheduleModal(discord.ui.Modal, title="일정 등록"):
    date = discord.ui.TextInput(label="날짜", placeholder="2026-03-25")
    time_input = discord.ui.TextInput(label="시간", placeholder="18:00")
    text = discord.ui.TextInput(label="일정 내용", placeholder="약속")

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name
        color = user_colors.get(user_id, DEFAULT_COLOR)

        schedule.append({
            "datetime": f"{self.date.value} {self.time_input.value}",
            "text": self.text.value,
            "name": user_name,
            "user_id": interaction.user.id,
            "color": color,
            "alert_enabled": False,
            "alert_10min": False,
            "channel_id": interaction.channel_id
        })
        save_schedule()
        await interaction.response.send_message("✅ 일정 등록 완료\n새로 !캘린더 입력하면 반영돼", ephemeral=True)





class GoToMonthModal(discord.ui.Modal, title="원하는 달로 이동"):
    year = discord.ui.TextInput(label="연도", placeholder="예: 2026", min_length=4, max_length=4)
    month = discord.ui.TextInput(label="월", placeholder="예: 3 또는 03", min_length=1, max_length=2)

    def __init__(self, calendar_view):
        super().__init__()
        self.calendar_view = calendar_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            year = int(str(self.year.value).strip())
            month = int(str(self.month.value).strip())
        except ValueError:
            await interaction.response.send_message("연도와 월은 숫자로 입력해줘", ephemeral=True)
            return

        if month < 1 or month > 12:
            await interaction.response.send_message("월은 1~12 사이로 입력해줘", ephemeral=True)
            return

        self.calendar_view.year = year
        self.calendar_view.month = month

        file_path = await asyncio.to_thread(create_calendar_image, year, month)
        await interaction.response.edit_message(attachments=[discord.File(file_path)], view=self.calendar_view)

class AddSongModal(discord.ui.Modal, title="노래 추가"):
    song = discord.ui.TextInput(label="노래 제목 또는 URL", placeholder="예: 아이유 밤편지 / 유튜브 링크")

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        queue = get_guild_queue(self.guild_id)
        queue.append(make_queue_item(self.channel_id, self.song.value))
        state = get_music_state(self.guild_id)
        state["last_query"] = self.song.value
        state["last_text_channel_id"] = self.channel_id
        save_music_data()

        guild = bot.get_guild(self.guild_id)
        player = guild.voice_client if guild and isinstance(guild.voice_client, wavelink.Player) else None

        if player and (player_is_playing(player) or player_is_paused(player)):
            await interaction.response.send_message(f"🎶 대기열 추가됨: {self.song.value}", ephemeral=True)
        else:
            await interaction.response.send_message(f"▶️ 바로 재생 시도: {self.song.value}", ephemeral=True)
            await play_next(self.guild_id)


class ScheduleSelect(discord.ui.Select):

    def __init__(self, action_type: str):
        self.action_type = action_type

        options = []
        for i, item in enumerate(schedule):
            label = safe_text(f"{item['datetime']} / {item['text']}", 100)
            description = safe_text(f"{item.get('name', '사용자')}", 100)
            options.append(discord.SelectOption(label=label, description=description, value=str(i)))

        if not options:
            options.append(discord.SelectOption(label="등록된 일정 없음", value="none"))

        super().__init__(placeholder="일정을 선택해줘", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("등록된 일정이 없어", ephemeral=True)
            return

        idx = int(self.values[0])

        if idx < 0 or idx >= len(schedule):
            await interaction.response.send_message("❌ 잘못된 선택", ephemeral=True)
            return

        if self.action_type == "delete":
            removed = schedule.pop(idx)
            save_schedule()
            await interaction.response.send_message(f"🗑️ 삭제 완료: {removed['datetime']} / {removed['text']}", ephemeral=True)

        elif self.action_type == "add_alert":
            schedule[idx]["alert_enabled"] = True
            schedule[idx]["alert_10min"] = False
            save_schedule()
            await interaction.response.send_message(f"🔔 알림 등록 완료: {schedule[idx]['datetime']} / {schedule[idx]['text']}", ephemeral=True)

        elif self.action_type == "delete_alert":
            schedule[idx]["alert_enabled"] = False
            schedule[idx]["alert_10min"] = False
            save_schedule()
            await interaction.response.send_message(f"🔕 알림 삭제 완료: {schedule[idx]['datetime']} / {schedule[idx]['text']}", ephemeral=True)


class MusicDeleteSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        queue = get_guild_queue(guild_id)

        options = []
        for i, (_, query) in enumerate(queue[:25]):
            options.append(
                discord.SelectOption(
                    label=safe_text(query, 100),
                    value=str(i),
                    description=f"{i + 1}번 대기곡"
                )
            )

        if not options:
            options.append(discord.SelectOption(label="삭제할 곡 없음", value="none", description="대기열이 비어 있음"))

        super().__init__(placeholder="삭제할 노래를 골라줘", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("삭제할 노래가 없어", ephemeral=True)
            return

        queue = get_guild_queue(self.guild_id)
        idx = int(self.values[0])

        if idx < 0 or idx >= len(queue):
            await interaction.response.send_message("잘못된 선택이야", ephemeral=True)
            return

        _, removed_query = queue.pop(idx)
        save_music_data()
        await interaction.response.send_message(f"🗑️ 대기열에서 삭제 완료: {removed_query}", ephemeral=True)


class ScheduleSelectView(discord.ui.View):
    def __init__(self, action_type: str):
        super().__init__(timeout=60)
        self.add_item(ScheduleSelect(action_type))


class MusicDeleteView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=60)
        self.add_item(MusicDeleteSelect(guild_id))


# =========================
# 캘린더 UI
# =========================
class CalendarView(discord.ui.View):
    def __init__(self, year, month):
        super().__init__(timeout=3600)
        self.year = year
        self.month = month

        self.add_item(ColorButton())
        self.add_item(ScheduleListButton())
        self.add_item(ScheduleHelpButton())

    @discord.ui.button(label="◀ 이전달", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.month -= 1
        if self.month < 1:
            self.month = 12
            self.year -= 1

        file_path = await asyncio.to_thread(create_calendar_image, self.year, self.month)
        await interaction.message.edit(attachments=[discord.File(file_path)], view=self)

    @discord.ui.button(label="다음달 ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.month += 1
        if self.month > 12:
            self.month = 1
            self.year += 1

        file_path = await asyncio.to_thread(create_calendar_image, self.year, self.month)
        await interaction.message.edit(attachments=[discord.File(file_path)], view=self)

    @discord.ui.button(label="📅 월이동", style=discord.ButtonStyle.primary, row=0)
    async def goto_month_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GoToMonthModal(self))

    @discord.ui.button(label="일정등록", style=discord.ButtonStyle.success, row=1)
    async def add_schedule_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddScheduleModal())

    @discord.ui.button(label="일정삭제", style=discord.ButtonStyle.danger, row=1)
    async def delete_schedule_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not schedule:
            await interaction.response.send_message("📋 등록된 일정이 없어", ephemeral=True)
            return

        await interaction.response.send_message("🗑️ 삭제할 일정을 골라줘", view=ScheduleSelectView("delete"), ephemeral=True)

    @discord.ui.button(label="알림등록", style=discord.ButtonStyle.primary, row=1)
    async def add_alert_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not schedule:
            await interaction.response.send_message("📋 등록된 일정이 없어", ephemeral=True)
            return

        await interaction.response.send_message("🔔 알림 등록할 일정을 골라줘", view=ScheduleSelectView("add_alert"), ephemeral=True)

    @discord.ui.button(label="알림삭제", style=discord.ButtonStyle.secondary, row=1)
    async def delete_alert_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not schedule:
            await interaction.response.send_message("📋 등록된 일정이 없어", ephemeral=True)
            return

        await interaction.response.send_message("🔕 알림 삭제할 일정을 골라줘", view=ScheduleSelectView("delete_alert"), ephemeral=True)


# =========================
# 음악 UI
# =========================

class MusicView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=3600)
        self.guild_id = guild_id

    def get_player(self):
        guild = bot.get_guild(self.guild_id)
        if guild and isinstance(guild.voice_client, wavelink.Player):
            return guild.voice_client
        return None

    @discord.ui.button(label="⏮ 이전곡", style=discord.ButtonStyle.secondary, row=0)
    async def prev_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        queue = get_guild_queue(self.guild_id)
        player = self.get_player()

        if not state["history"]:
            await interaction.response.send_message("이전곡이 없어", ephemeral=True)
            return

        prev_query = state["history"].pop()
        queue.insert(0, make_queue_item(interaction.channel_id, prev_query))
        state["last_text_channel_id"] = interaction.channel_id
        save_music_data()

        if player:
            await player.stop()
            await interaction.response.send_message(f"⏮ 이전곡으로 이동: {prev_query}", ephemeral=True)
        else:
            await interaction.response.send_message("음성 채널에 없어", ephemeral=True)

    @discord.ui.button(label="⏭ 다음곡", style=discord.ButtonStyle.secondary, row=0)
    async def next_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = get_guild_queue(self.guild_id)
        player = self.get_player()

        if not queue:
            await interaction.response.send_message("다음곡이 없어", ephemeral=True)
            return

        if player:
            state = get_music_state(self.guild_id)
            state["last_text_channel_id"] = interaction.channel_id
            save_music_data()
            await player.stop()
            await interaction.response.send_message("⏭ 다음곡으로 넘어갈게", ephemeral=True)
        else:
            await interaction.response.send_message("음성 채널에 없음", ephemeral=True)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, row=0)
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.get_player()
        if player is None:
            await interaction.response.send_message("음성 채널에 없어", ephemeral=True)
            return

        if player_is_playing(player):
            await player.pause(True)
            await interaction.response.send_message("⏸️ 일시정지", ephemeral=True)
        elif player_is_paused(player):
            await player.pause(False)
            await interaction.response.send_message("▶️ 다시 재생", ephemeral=True)
        else:
            await interaction.response.send_message("현재 재생 중인 노래가 없어", ephemeral=True)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.danger, row=0)
    async def stop_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        music_queues[self.guild_id] = []
        state = get_music_state(self.guild_id)
        state["current"] = None
        state["restored_queue"] = []
        state["last_text_channel_id"] = interaction.channel_id
        save_music_data()

        player = self.get_player()
        if player:
            await player.stop()
            await interaction.response.send_message("⏹️ 정지 완료", ephemeral=True)
        else:
            await interaction.response.send_message("음성 채널에 없음", ephemeral=True)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.success, row=0)
    async def repeat_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state["repeat"] = not state["repeat"]
        state["last_text_channel_id"] = interaction.channel_id
        save_music_data()
        text = "🔁 반복 켜짐" if state["repeat"] else "➡️ 반복 꺼짐"
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="📜 노래리스트", style=discord.ButtonStyle.secondary, row=1)
    async def queue_list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        queue = get_guild_queue(self.guild_id)

        lines = []
        if state["current"]:
            lines.append(f"🎵 현재곡: {state['current']['title']}")
        else:
            lines.append("🎵 현재곡 없음")

        lines.append("")

        if queue:
            lines.append("📜 대기열")
            for i, item in enumerate(queue, start=1):
                _, query = unpack_queue_item(item)
                lines.append(f"{i}. {query}")
        else:
            lines.append("📜 대기열 비어 있음")

        text = "\n".join(lines)
        chunks = split_text(text, 1800)

        await interaction.response.send_message(f"```{chunks[0]}```", ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(f"```{chunk}```", ephemeral=True)

    @discord.ui.button(label="📄 가사", style=discord.ButtonStyle.secondary, row=1)
    async def lyrics_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        current = state["current"]

        if not current:
            await interaction.response.send_message("현재 재생 중인 곡이 없어", ephemeral=True)
            return

        song = current["title"]
        artist, title = extract_artist_title(song)

        if not artist:
            await interaction.response.send_message(
                f"현재곡 제목이 `{song}` 형태라서 자동 가사 검색이 어려워.\n`!가사 가수 - 제목` 형식으로 입력해줘.",
                ephemeral=True
            )
            return

        async with aiohttp.ClientSession() as session:
            url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    await interaction.response.send_message("가사를 못 찾았어", ephemeral=True)
                    return
                data = await resp.json()

        text = data.get("lyrics", "없음")
        chunks = split_text(text, 1800)

        await interaction.response.send_message(f"📄 **{artist} - {title}**\n```{chunks[0]}```", ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(f"```{chunk}```", ephemeral=True)

    @discord.ui.button(label="📖 도움말", style=discord.ButtonStyle.primary, row=1)
    async def music_help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = (
            "🎵 노래 명령어\n\n"
            "!입장\n"
            "!퇴장\n"
            "!재생 노래이름\n!재생 유튜브플레이리스트URL\n"
            "!정지\n"
            "!일시정지\n"
            "!다시재생\n"
            "!노래리스트\n!플레이리스트정보 URL\n"
            "!가사 가수 - 제목"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="➕ 노래추가", style=discord.ButtonStyle.success, row=1)
    async def add_song_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddSongModal(self.guild_id, interaction.channel_id))

    @discord.ui.button(label="🗑 노래삭제", style=discord.ButtonStyle.danger, row=1)
    async def remove_song_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = get_guild_queue(self.guild_id)

        if not queue:
            await interaction.response.send_message("대기열이 비어 있어", ephemeral=True)
            return

        await interaction.response.send_message("삭제할 노래를 골라줘", view=MusicDeleteView(self.guild_id), ephemeral=True)


# =========================
# 이벤트
# =========================
@bot.event
async def on_ready():
    global schedule_task_started
    print(f"로그인 완료: {bot.user}")

    try:
        load_schedule()
        load_colors()
        load_music_data()

        cookie_file = resolve_cookie_file()
        if cookie_file:
            print(f"yt-dlp cookies 적용됨: {cookie_file}")
        else:
            print("yt-dlp cookies 미적용: 쿠키 없이 우회 모드로 시도할게")
        print(f"yt-dlp IPv4 강제: {'켜짐' if YTDLP_FORCE_IPV4 else '꺼짐'}")
        print(f"yt-dlp web client 비활성화: {'켜짐' if YTDLP_DISABLE_WEB_CLIENT else '꺼짐'}")

        await ensure_lavalink_ready()

        if os.path.exists(RESTART_FILE):
            try:
                os.replace(RESTART_FILE, RESTART_PROCESSING_FILE)

                with open(RESTART_PROCESSING_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

                channel_id = data.get("channel_id")
                message_id = data.get("message_id")
                guild_id = data.get("guild_id")
                voice_channel_id = data.get("voice_channel_id")
                last_query = data.get("last_query")
                queue_data = data.get("queue", [])
                repeat_state = data.get("repeat", False)

                if channel_id and message_id:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        try:
                            msg = await channel.fetch_message(message_id)
                            await msg.edit(content="✅ 재시동 완료!")
                        except Exception:
                            await channel.send("✅ 재시동 완료!")

                if guild_id:
                    guild = bot.get_guild(guild_id)
                    if guild:
                        state = get_music_state(guild_id)
                        if last_query:
                            state["last_query"] = last_query
                        state["repeat"] = repeat_state
                        state["restored_queue"] = [q for q in queue_data if isinstance(q, str)]

                        if voice_channel_id:
                            voice_channel = bot.get_channel(voice_channel_id)
                            if voice_channel and getattr(voice_channel, "connect", None):
                                try:
                                    if guild.voice_client is None:
                                        await voice_channel.connect(cls=wavelink.Player, self_deaf=False, self_mute=False)
                                    else:
                                        player = guild.voice_client
                                        if isinstance(player, wavelink.Player):
                                            await player.move_to(voice_channel)
                                        else:
                                            await guild.voice_client.move_to(voice_channel)
                                    state["last_voice_channel_id"] = voice_channel_id
                                except Exception as e:
                                    print(f"자동 재입장 실패: {e}")

                if os.path.exists(RESTART_PROCESSING_FILE):
                    os.remove(RESTART_PROCESSING_FILE)
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"재시동 완료 처리 실패: {e}")
                if os.path.exists(RESTART_PROCESSING_FILE):
                    os.remove(RESTART_PROCESSING_FILE)

        if not schedule_task_started:
            bot.loop.create_task(check_schedule())
            schedule_task_started = True

    except Exception as e:
        print(f"초기화 오류: {e}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)


@bot.event
async def on_wavelink_track_end(payload):
    player = getattr(payload, "player", None)
    if not player or not getattr(player, "guild", None):
        return

    guild_id = player.guild.id
    state = get_music_state(guild_id)

    current = state.get("current")
    if current:
        if state.get("repeat"):
            queue = get_guild_queue(guild_id)
            queue.insert(0, make_queue_item(state.get("last_text_channel_id"), current["query"]))
        else:
            state["history"].append(current["query"])

    state["current"] = None
    save_music_data()
    await play_next(guild_id)


@bot.event
async def on_wavelink_track_exception(payload):
    player = getattr(payload, "player", None)
    if not player or not getattr(player, "guild", None):
        return
    guild_id = player.guild.id
    await send_music_message(guild_id, "⚠️ 재생 중 오류가 발생해서 다음 곡으로 넘어갈게")
    await play_next(guild_id)


# =========================
# 음악 명령어
# =========================
@bot.command(name="재시동")
@commands.is_owner()
async def restart(ctx):
    global RESTARTING

    if RESTARTING:
        return

    RESTARTING = True
    restart_msg = await ctx.send("🔄 봇 재시작 중...")

    guild_id = ctx.guild.id if ctx.guild else None
    voice_channel_id = None
    last_query = None
    queue_data = []
    repeat_state = False

    if guild_id:
        state = get_music_state(guild_id)
        repeat_state = state.get("repeat", False)
        last_query = state.get("last_query")

        current = state.get("current")
        if current and not last_query:
            last_query = current.get("query")

        queue = get_guild_queue(guild_id)
        queue_data = [query for _, query in (unpack_queue_item(item) for item in queue) if isinstance(query, str)]

        if ctx.voice_client and ctx.voice_client.channel:
            voice_channel_id = ctx.voice_client.channel.id
        elif state.get("last_voice_channel_id"):
            voice_channel_id = state.get("last_voice_channel_id")

    try:
        with open(RESTART_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "channel_id": ctx.channel.id,
                "message_id": restart_msg.id,
                "guild_id": guild_id,
                "voice_channel_id": voice_channel_id,
                "last_query": last_query,
                "queue": queue_data,
                "repeat": repeat_state
            }, f)
    except Exception as e:
        print(f"재시작 채널 저장 실패: {e}")

    save_music_data()

    try:
        if ctx.voice_client:
            player = resolve_voice_client(ctx) or ctx.voice_client
            await player.disconnect()
    except Exception:
        pass

    await bot.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.command(name="입장")
async def join(ctx):
    if ctx.author.voice is None:
        await ctx.send("먼저 음성 채널에 들어가 있어야 해.")
        return

    channel = ctx.author.voice.channel

    try:
        await ensure_lavalink_ready()
        existing_vc = ctx.voice_client
        player = resolve_voice_client(ctx)
        if existing_vc is not None and player is None:
            try:
                await existing_vc.disconnect(force=True)
            except Exception:
                try:
                    await existing_vc.disconnect()
                except Exception:
                    pass
        player = resolve_voice_client(ctx)
        if player is None:
            await channel.connect(cls=wavelink.Player, self_deaf=False, self_mute=False)
        else:
            await player.move_to(channel)

        state = get_music_state(ctx.guild.id)
        state["last_voice_channel_id"] = channel.id
        state["last_text_channel_id"] = ctx.channel.id
        save_music_data()

        await ctx.send(f"✅ {channel.name} 입장 완료")

    except Exception as e:
        await ctx.send(f"❌ 입장 실패: {e}")


@bot.command(name="퇴장")
async def leave(ctx):
    if ctx.voice_client:
        guild_id = ctx.guild.id
        music_queues[guild_id] = []

        state = get_music_state(guild_id)
        if state.get("current") and state["current"].get("query"):
            state["last_query"] = state["current"]["query"]
        state["current"] = None
        state["history"] = []
        state["repeat"] = False
        state["restored_queue"] = []
        save_music_data()

        player = resolve_voice_client(ctx) or ctx.voice_client
        await player.disconnect()
        await ctx.send("👋 퇴장 완료")
    else:
        await ctx.send("음성 채널에 없음")


@bot.command(name="재생")
async def play(ctx, *, query: str = None):
    guild_id = ctx.guild.id
    state = get_music_state(guild_id)

    if query is None:
        if state.get("current") and state["current"].get("query"):
            query = state["current"]["query"]
        elif state.get("last_query"):
            query = state["last_query"]
        else:
            await ctx.send("재생할 노래를 먼저 입력해줘")
            return

    try:
        player = await get_or_connect_player(ctx)
    except Exception as e:
        await ctx.send(str(e))
        return

    queue = get_guild_queue(guild_id)
    state["last_text_channel_id"] = ctx.channel.id

    restored_queue = state.get("restored_queue", [])
    if restored_queue:
        for restored_query in restored_queue:
            queue.append(make_queue_item(ctx.channel.id, restored_query))
        state["restored_queue"] = []

    if is_youtube_playlist_url(query):
        try:
            playlist_entries = await extract_playlist_entries(query)
        except Exception as e:
            await ctx.send(f"❌ 플레이리스트를 불러오지 못했어: {sanitize_music_error(e)}")
            return

        if not playlist_entries:
            await ctx.send("플레이리스트 안에서 재생할 곡을 찾지 못했어")
            return

        added_queries = []
        for _, entry_url in playlist_entries:
            queue.append(make_queue_item(ctx.channel.id, entry_url))
            added_queries.append(entry_url)

        state["last_query"] = query
        save_music_data()

        await ctx.send(f"📃 플레이리스트 추가 완료: {len(added_queries)}곡", view=MusicView(guild_id))

        if not player_is_playing(player) and not player_is_paused(player):
            await play_next(guild_id)
        return

    queue.append(make_queue_item(ctx.channel.id, query))
    state["last_query"] = query
    save_music_data()

    if player_is_playing(player) or player_is_paused(player):
        await ctx.send(f"🎶 대기열 추가됨: {query}", view=MusicView(guild_id))
    else:
        await play_next(guild_id)


@bot.command(name="정지")
async def stop(ctx):
    guild_id = ctx.guild.id
    music_queues[guild_id] = []

    state = get_music_state(guild_id)
    if state.get("current") and state["current"].get("query"):
        state["last_query"] = state["current"]["query"]
    state["current"] = None
    state["restored_queue"] = []
    state["last_text_channel_id"] = ctx.channel.id
    save_music_data()

    player = resolve_voice_client(ctx) or ctx.voice_client
    if player:
        try:
            if isinstance(player, wavelink.Player):
                await player.stop()
            else:
                player.stop()
            await ctx.send("⏹️ 정지 완료")
        except Exception as e:
            await ctx.send(f"❌ 정지 실패: {e}")
    else:
        await ctx.send("음성 채널에 없음")


@bot.command(name="일시정지")
async def pause(ctx):
    player = resolve_voice_client(ctx)
    if player and player_is_playing(player):
        await player.pause(True)
        await ctx.send("⏸️ 일시정지")
    else:
        await ctx.send("현재 재생 중인 노래가 없어")


@bot.command(name="다시재생")
async def resume(ctx):
    player = resolve_voice_client(ctx)
    if player and player_is_paused(player):
        await player.pause(False)
        await ctx.send("▶️ 다시 재생")
    else:
        await ctx.send("일시정지된 노래가 없어")


@bot.command(name="노래리스트")
async def queue_list(ctx):
    guild_id = ctx.guild.id
    await send_queue_list(ctx, guild_id)


# =========================
# 플레이리스트 기능
# =========================
@bot.command(name="플레이리스트정보")
async def playlist_info(ctx, *, query: str):
    if not is_youtube_playlist_url(query):
        await ctx.send("유튜브 플레이리스트 URL을 넣어줘")
        return

    try:
        playlist_entries = await extract_playlist_entries(query)
    except Exception as e:
        await ctx.send(f"❌ 플레이리스트 정보를 불러오지 못했어: {sanitize_music_error(e)}")
        return

    if not playlist_entries:
        await ctx.send("플레이리스트 곡을 찾지 못했어")
        return

    lines = [f"📃 플레이리스트 곡 수: {len(playlist_entries)}", ""]
    for idx, (title, _) in enumerate(playlist_entries[:20], start=1):
        lines.append(f"{idx}. {title}")

    if len(playlist_entries) > 20:
        lines.append(f"... 외 {len(playlist_entries) - 20}곡")

    for chunk in split_text("\n".join(lines), 1800):
        await ctx.send(f"```{chunk}```")


# =========================
# 가사 기능
# =========================
@bot.command(name="가사")
async def lyrics(ctx, *, song: str = None):
    guild_id = ctx.guild.id if ctx.guild else None

    if song is None and guild_id:
        state = get_music_state(guild_id)
        current = state.get("current")
        if current:
            song = current["title"]

    if song is None:
        await ctx.send("노래 제목 입력해줘. 예시: `!가사 아이유 - 밤편지`")
        return

    artist, title = extract_artist_title(song)

    if not artist:
        await ctx.send("가사는 `가수 - 제목` 형식이 가장 잘 돼. 예: `!가사 아이유 - 밤편지`")
        return

    async with aiohttp.ClientSession() as session:
        url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("가사 못 찾음")
                return
            data = await resp.json()

    text = data.get("lyrics", "없음")
    chunks = split_text(text, 1800)

    await ctx.send(f"📄 {artist} - {title}\n```{chunks[0]}```")
    for chunk in chunks[1:]:
        await ctx.send(f"```{chunk}```")


# =========================
# 도움말 명령어
# =========================
@bot.command(name="도움말")
async def help_command(ctx):
    await ctx.send("보고 싶은 기능을 골라줘", view=HelpView())


@bot.command(name="쿠키상태")
async def cookie_status(ctx):
    cookie_file = resolve_cookie_file()
    if cookie_file:
        await ctx.send(
            f"✅ cookies 적용 중\n경로: `{cookie_file}`\n"
            f"IPv4 강제: {'켜짐' if YTDLP_FORCE_IPV4 else '꺼짐'} | "
            f"web client 비활성화: {'켜짐' if YTDLP_DISABLE_WEB_CLIENT else '꺼짐'}"
        )
    else:
        await ctx.send(
            "⚠️ cookies.txt가 없어. 유튜브 차단이 걸리면 재생이 안 될 수 있어.\n"
            f"IPv4 강제: {'켜짐' if YTDLP_FORCE_IPV4 else '꺼짐'} | "
            f"web client 비활성화: {'켜짐' if YTDLP_DISABLE_WEB_CLIENT else '꺼짐'}"
        )


@bot.command(name="우회상태")
async def bypass_status(ctx):
    mode = "쿠키 사용" if resolve_cookie_file() else "쿠키 없이 우회 모드"
    await ctx.send(
        f"🛠 재생 모드: {mode}\n"
        f"- web client 비활성화: {'켜짐' if YTDLP_DISABLE_WEB_CLIENT else '꺼짐'}\n"
        f"- IPv4 강제: {'켜짐' if YTDLP_FORCE_IPV4 else '꺼짐'}"
    )


@bot.command(name="음악상태")
async def music_status(ctx):
    guild_id = ctx.guild.id
    state = get_music_state(guild_id)
    await ctx.send(
        "🎧 음악 상태\n"
        f"- 마지막 곡: {state.get('last_query') or '없음'}\n"
        f"- 반복: {'켜짐' if state.get('repeat') else '꺼짐'}\n"
        f"- 자동 스킵 수: {state.get('auto_skipped_count', 0)}\n"
        f"- 차단 감지 수: {state.get('blocked_fail_count', 0)}"
    )


# =========================
# 일정 명령어
# =========================
@bot.command(name="일정추가")
async def add_schedule_cmd(ctx, date, time_input, *, text):
    user_id = str(ctx.author.id)
    user_name = ctx.author.display_name
    color = user_colors.get(user_id, DEFAULT_COLOR)

    schedule.append({
        "datetime": f"{date} {time_input}",
        "text": text,
        "name": user_name,
        "user_id": ctx.author.id,
        "color": color,
        "alert_enabled": False,
        "alert_10min": False,
        "channel_id": ctx.channel.id
    })

    save_schedule()
    await ctx.send("✅ 일정 추가 완료")


@bot.command(name="일정삭제")
async def delete_schedule_cmd(ctx, index: int):
    if index < 1 or index > len(schedule):
        await ctx.send("❌ 잘못된 번호")
        return

    removed = schedule.pop(index - 1)
    save_schedule()
    await ctx.send(f"🗑️ 삭제 완료: {removed['datetime']} / {removed['text']}")


@bot.command(name="캘린더")
async def show_calendar(ctx, year: int = None, month: int = None):
    now = datetime.now()
    year = year or now.year
    month = month or now.month

    file_path = await asyncio.to_thread(create_calendar_image, year, month)
    view = CalendarView(year, month)
    await ctx.send(file=discord.File(file_path), view=view)


@bot.command(name="일정목록")
async def list_schedule(ctx):
    await send_schedule_list_message(ctx)


# =========================
# 실행
# =========================
@restart.error
async def restart_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        await ctx.send("❌ 이 명령어는 봇 관리자만 사용할 수 있어.")


bot.run(TOKEN)
