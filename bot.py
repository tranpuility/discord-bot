# music_removed: clean
# version: 2026-03-28-cookieless-workaround
import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import sys
import asyncio
import calendar
import uuid
import tempfile
import unicodedata
import re
import random
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from PIL import Image, ImageDraw, ImageFont
try:
    from korean_lunar_calendar import KoreanLunarCalendar
    LUNAR_AVAILABLE = True
except Exception:
    KoreanLunarCalendar = None
    LUNAR_AVAILABLE = False
from urllib.request import urlopen
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

from dotenv import load_dotenv
import edge_tts

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

RESTARTING = False

WATCH_TOGETHER_APPLICATION_ID = 880218394199220334
AUTO_VOICE_LEAVE_DELAY = 60
auto_voice_leave_tasks = {}

# =========================
# 기본 설정
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

class SlashBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self):
        try:
            synced_global = await self.tree.sync()
            print(f"[setup_hook] 글로벌 슬래시 동기화 완료: {len(synced_global)}개", flush=True)
        except Exception as e:
            print(f"[setup_hook] 글로벌 슬래시 동기화 실패: {e}", flush=True)

bot = SlashBot()


schedule = []
user_colors = {}
sent_alerts = set()
schedule_task_started = False
voice_monitor_task_started = False
slash_sync_done = False

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

SCHEDULE_CATEGORY_LABELS = {
    "personal": "개인일정",
    "birthday": "생일일정",
    "event": "이벤트일정",
    "update": "업데이트일정",
    "temp_holiday": "임시공휴일",
}
CATEGORY_FIXED_COLORS = {
    "birthday": [255, 182, 193],
    "event": [255, 218, 121],
    "update": [174, 198, 255],
    "temp_holiday": [235, 92, 92],
}
HOLIDAY_COLOR = (235, 92, 92)
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
WEEKDAY_MAP = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}

HOLIDAY_API_KEY = os.getenv("KOREA_HOLIDAY_API_KEY") or os.getenv("DATA_GO_KR_SERVICE_KEY")
HOLIDAY_CACHE_DIR = os.path.join(DATA_DIR, "holiday_cache")
os.makedirs(HOLIDAY_CACHE_DIR, exist_ok=True)

def _holiday_cache_path(year: int, month: int) -> str:
    return os.path.join(HOLIDAY_CACHE_DIR, f"{year:04d}_{month:02d}.json")

def _load_holiday_cache(year: int, month: int):
    cache_path = _holiday_cache_path(year, month)
    if not os.path.isfile(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except Exception:
        return None

def _save_holiday_cache(year: int, month: int, holiday_map: dict):
    cache_path = _holiday_cache_path(year, month)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in holiday_map.items()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[공휴일 캐시 저장 실패] {e}")

def _add_holiday_name(target: dict, day: int, name: str):
    if day <= 0:
        return
    target.setdefault(day, [])
    if name not in target[day]:
        target[day].append(name)

def _merge_holiday_map(base_map: dict, extra_map: dict):
    for day, names in extra_map.items():
        for name in names:
            _add_holiday_name(base_map, int(day), name)

def lunar_to_solar_date(year: int, lunar_month: int, lunar_day: int, is_leap: bool = False):
    if not LUNAR_AVAILABLE:
        return None
    try:
        cal = KoreanLunarCalendar()
        cal.setLunarDate(year, lunar_month, lunar_day, is_leap)
        return datetime.strptime(cal.SolarIsoFormat(), "%Y-%m-%d").date()
    except Exception as e:
        print(f"[음력 변환 실패] {e}")
        return None

def build_local_holiday_map(year: int, month: int):
    holiday_map = {}

    fixed_holidays = {
        (1, 1): "신정",
        (3, 1): "삼일절",
        (5, 5): "어린이날",
        (6, 6): "현충일",
        (8, 15): "광복절",
        (10, 3): "개천절",
        (10, 9): "한글날",
        (12, 25): "성탄절",
    }

    for (m, d), name in fixed_holidays.items():
        if m == month:
            _add_holiday_name(holiday_map, d, name)

    if LUNAR_AVAILABLE:
        seollal = lunar_to_solar_date(year, 1, 1)
        chuseok = lunar_to_solar_date(year, 8, 15)
        buddha = lunar_to_solar_date(year, 4, 8)

        for base_date, name in [(seollal, "설날"), (chuseok, "추석")]:
            if base_date:
                holiday_dates = [
                    (base_date - timedelta(days=1), f"{name} 연휴"),
                    (base_date, name),
                    (base_date + timedelta(days=1), f"{name} 연휴"),
                ]
                for dt_obj, holiday_name in holiday_dates:
                    if dt_obj.year == year and dt_obj.month == month:
                        _add_holiday_name(holiday_map, dt_obj.day, holiday_name)

        if buddha and buddha.year == year and buddha.month == month:
            _add_holiday_name(holiday_map, buddha.day, "부처님오신날")

    # 대체공휴일 처리
    substitute_candidates = []

    def add_substitute_target(dates):
        if not dates:
            return
        if any(dt.weekday() >= 5 for dt, _ in dates):
            substitute_date = max(dt for dt, _ in dates) + timedelta(days=1)
            existing_days = {dt for dt, _ in dates}
            while substitute_date.weekday() >= 5 or substitute_date in existing_days:
                substitute_date += timedelta(days=1)
            substitute_candidates.append(substitute_date)

    # 고정 공휴일 대체
    substitute_fixed = [(3, 1, "삼일절"), (5, 5, "어린이날"), (8, 15, "광복절"), (10, 3, "개천절"), (10, 9, "한글날")]
    for m, d, name in substitute_fixed:
        dt_obj = datetime(year, m, d).date()
        if dt_obj.weekday() >= 5:
            substitute_candidates.append(dt_obj + timedelta(days=1))

    # 음력/부처님오신날 대체
    if LUNAR_AVAILABLE:
        if buddha and buddha.weekday() >= 5:
            substitute_candidates.append(buddha + timedelta(days=1))
        if seollal:
            holiday_dates = [(seollal - timedelta(days=1), "설날 연휴"), (seollal, "설날"), (seollal + timedelta(days=1), "설날 연휴")]
            add_substitute_target(holiday_dates)
        if chuseok:
            holiday_dates = [(chuseok - timedelta(days=1), "추석 연휴"), (chuseok, "추석"), (chuseok + timedelta(days=1), "추석 연휴")]
            add_substitute_target(holiday_dates)

    for dt_obj in substitute_candidates:
        if dt_obj.year == year and dt_obj.month == month:
            _add_holiday_name(holiday_map, dt_obj.day, "대체공휴일")

    return holiday_map

def fetch_holiday_map_from_api(year: int, month: int):
    if not HOLIDAY_API_KEY:
        return None
    try:
        endpoint = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
        query = (
            f"?serviceKey={quote_plus(HOLIDAY_API_KEY)}"
            f"&solYear={year:04d}&solMonth={month:02d}"
            "&_type=json"
        )
        with urlopen(endpoint + query, timeout=8) as response:
            payload = response.read().decode("utf-8", errors="ignore")
        data = json.loads(payload)
        items = (((data.get("response") or {}).get("body") or {}).get("items") or {}).get("item")
        if not items:
            return {}
        if isinstance(items, dict):
            items = [items]
        holiday_map = {}
        for item in items:
            locdate = str(item.get("locdate", ""))
            date_name = str(item.get("dateName", "")).strip()
            is_holiday = str(item.get("isHoliday", "Y")).upper() == "Y"
            if len(locdate) == 8 and is_holiday:
                day = int(locdate[-2:])
                if date_name:
                    _add_holiday_name(holiday_map, day, date_name)
        return holiday_map
    except Exception as e:
        print(f"[공휴일 API 실패] {e}")
        return None

def get_month_holidays(year: int, month: int):
    cached = _load_holiday_cache(year, month)
    if cached:
        holiday_map = cached
    else:
        api_map = fetch_holiday_map_from_api(year, month)
        holiday_map = api_map if api_map is not None else build_local_holiday_map(year, month)
        _save_holiday_cache(year, month, holiday_map)

    # 임시공휴일 일정 병합
    for item in schedule:
        dt = parse_schedule_datetime(item.get("datetime", ""))
        if dt is None or dt.year != year or dt.month != month:
            continue
        if item.get("category") == "temp_holiday":
            _add_holiday_name(holiday_map, dt.day, item.get("text", "임시공휴일"))
    return holiday_map

def normalize_schedule_date(date_text: str) -> str:
    raw = str(date_text or "").strip()
    if not raw:
        raise ValueError("날짜를 입력해줘")

    compact = re.sub(r"\s+", "", raw)
    compact = compact.replace(".", "-").replace("/", "-")
    compact = compact.replace("년", "-").replace("월", "-").replace("일", "")
    compact = re.sub(r"-+", "-", compact).strip("-")

    if re.fullmatch(r"\d{8}", compact):
        year = int(compact[:4])
        month = int(compact[4:6])
        day = int(compact[6:8])
    else:
        parts = [part for part in compact.split("-") if part]
        if len(parts) != 3:
            raise ValueError("날짜는 2026-03-25 또는 20260325처럼 입력해줘")
        year, month, day = map(int, parts)

    try:
        normalized = datetime(year, month, day)
    except ValueError:
        raise ValueError("날짜를 다시 확인해줘")
    return normalized.strftime("%Y-%m-%d")

def normalize_schedule_time(time_text: str) -> str:
    raw = str(time_text or "").strip()
    if not raw:
        raise ValueError("시간을 입력해줘")

    compact = re.sub(r"\s+", "", raw)
    compact = compact.replace(".", ":")

    hour = None
    minute = None

    m = re.fullmatch(r"(\d{1,2})시(?:(\d{1,2})분?)?", compact)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
    elif re.fullmatch(r"\d{3,4}", compact):
        if len(compact) == 3:
            hour = int(compact[0])
            minute = int(compact[1:])
        else:
            hour = int(compact[:2])
            minute = int(compact[2:])
    else:
        normalized = compact.replace("시", ":").replace("분", "")
        parts = [part for part in normalized.split(":") if part]
        if len(parts) == 1 and parts[0].isdigit():
            hour = int(parts[0])
            minute = 0
        elif len(parts) == 2:
            hour, minute = map(int, parts)
        else:
            raise ValueError("시간은 18:00, 1800, 18시, 18시30분처럼 입력해줘")

    if hour is None or minute is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("시간을 다시 확인해줘")
    return f"{hour:02d}:{minute:02d}"

def parse_schedule_datetime(dt_str: str):
    if not dt_str:
        return None
    normalized = str(dt_str).strip().replace(".", "-").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None

def normalize_schedule_category(value: str) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "": "personal",
        "개인": "personal", "개인일정": "personal", "personal": "personal",
        "생일": "birthday", "생일일정": "birthday", "birthday": "birthday",
        "이벤트": "event", "이벤트일정": "event", "event": "event",
        "업데이트": "update", "업데이트일정": "update", "update": "update",
        "임시공휴일": "temp_holiday", "임공": "temp_holiday", "temp_holiday": "temp_holiday",
    }
    return mapping.get(raw, "personal")

def normalize_repeat_input(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())

def needs_weekday_selection(value: str) -> bool:
    normalized = normalize_repeat_input(value).lower()
    return normalized in {"요일반복", "주간반복", "weekly"}

def build_schedule_item_from_values(normalized_date: str, normalized_time: str, text_value: str, user, channel_id: int, category_value: str, repeat_type: str = "none", repeat_days=None):
    if repeat_days is None:
        repeat_days = []
    user_id = str(user.id)
    user_name = user.display_name
    color = user_colors.get(user_id, DEFAULT_COLOR)
    category = normalize_schedule_category(category_value)
    item = {
        "datetime": f"{normalized_date} {normalized_time}",
        "text": text_value.strip(),
        "name": user_name,
        "user_id": user.id,
        "color": color,
        "category": category,
        "repeat_type": repeat_type,
        "repeat_days": repeat_days,
        "alert_enabled": False,
        "alert_10min": False,
        "channel_id": channel_id,
    }
    schedule.append(item)
    save_schedule()
    return item

PENDING_WEEKDAY_SCHEDULES = {}

def parse_repeat_rule(value: str):
    raw = normalize_repeat_input(value)
    if not raw or raw.lower() in {"없음", "안함", "none"}:
        return "none", []
    lowered = raw.lower()
    if raw == "매일" or lowered == "daily":
        return "daily", []
    if raw == "매월" or lowered == "monthly":
        return "monthly", []
    if raw == "매년" or lowered == "yearly":
        return "yearly", []
    if raw == "평일":
        return "weekly", [0, 1, 2, 3, 4]
    if raw == "주말":
        return "weekly", [5, 6]

    days = []
    for key, weekday in WEEKDAY_MAP.items():
        if key in raw:
            days.append(weekday)
    if days:
        return "weekly", sorted(set(days))
    return "none", []

def repeat_rule_to_text(item: dict) -> str:
    repeat_type = item.get("repeat_type", "none")
    repeat_days = item.get("repeat_days", [])
    if repeat_type == "daily":
        return "매일"
    if repeat_type == "monthly":
        return "매월"
    if repeat_type == "yearly":
        return "매년"
    if repeat_type == "weekly":
        labels = [WEEKDAY_KR[idx] for idx in repeat_days if isinstance(idx, int) and 0 <= idx <= 6]
        return "요일반복(" + ",".join(labels) + ")" if labels else "요일반복"
    return "반복없음"

def resolve_schedule_color(item: dict):
    category = item.get("category", "personal")
    if category == "personal":
        color = item.get("color", DEFAULT_COLOR)
        if isinstance(color, list) and len(color) == 3:
            return tuple(color)
        return tuple(DEFAULT_COLOR)
    fixed = CATEGORY_FIXED_COLORS.get(category)
    if fixed:
        return tuple(fixed)
    return tuple(DEFAULT_COLOR)

def get_schedule_category_label(item: dict) -> str:
    return SCHEDULE_CATEGORY_LABELS.get(item.get("category", "personal"), "개인일정")

def schedule_occurs_on_date(item: dict, target_date):
    dt = parse_schedule_datetime(item.get("datetime", ""))
    if dt is None:
        return False
    base_date = dt.date()
    if target_date < base_date:
        return False
    repeat_type = item.get("repeat_type", "none")
    repeat_days = item.get("repeat_days", [])
    if repeat_type == "daily":
        return True
    if repeat_type == "monthly":
        return base_date.day == target_date.day
    if repeat_type == "yearly":
        return base_date.month == target_date.month and base_date.day == target_date.day
    if repeat_type == "weekly":
        return target_date.weekday() in repeat_days
    return base_date == target_date

def format_schedule_detail(item: dict, index: int | None = None) -> str:
    parts = []
    if index is not None:
        parts.append(f"번호: {index + 1}")
    parts.extend([
        f"날짜시각: {item.get('datetime', '-')}",
        f"종류: {get_schedule_category_label(item)}",
        f"반복: {repeat_rule_to_text(item)}",
        f"내용: {item.get('text', '-')}",
        f"등록자: {item.get('name', '사용자')}",
        f"알림: {'켜짐' if item.get('alert_enabled') else '꺼짐'}",
    ])
    return "\n".join(parts)

def find_matching_schedules(keyword: str):
    keyword = str(keyword or "").strip().lower()
    if not keyword:
        return []
    results = []
    for idx, item in enumerate(schedule):
        haystacks = [
            str(item.get("datetime", "")),
            str(item.get("text", "")),
            str(item.get("name", "")),
            get_schedule_category_label(item),
            repeat_rule_to_text(item),
        ]
        normalized = " ".join(haystacks).lower()
        score = 0
        if keyword in normalized:
            score += 100
        compact_norm = normalized.replace("-", "").replace(":", "").replace(" ", "")
        compact_key = keyword.replace("-", "").replace(":", "").replace(" ", "")
        if compact_key and compact_key in compact_norm:
            score += 50
        for part in keyword.split():
            if part and part in normalized:
                score += 10
        if score > 0:
            results.append((score, idx, item))
    results.sort(key=lambda x: (-x[0], x[1]))
    return results

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

# =========================
# 공통 유틸
# =========================
def resolve_font_path():
    candidates = [
        FONT_FILE,
        "/app/onglefont.ttf",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None

def get_font(size: int):
    global FONT_LOGGED
    font_path = resolve_font_path()
    if font_path:
        try:
            if not FONT_LOGGED:
                print(f"적용 폰트: {font_path}", flush=True)
                FONT_LOGGED = True
            return ImageFont.truetype(font_path, size)
        except Exception as e:
            print(f"[폰트 오류] {e} | path={font_path}", flush=True)
    else:
        print("[폰트 오류] onglefont.ttf 파일을 찾지 못함", flush=True)
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
    _, last_day = calendar.monthrange(year, month)
    for day in range(1, last_day + 1):
        target_date = datetime(year, month, day).date()
        items = [item for item in schedule if schedule_occurs_on_date(item, target_date)]
        if items:
            items.sort(key=lambda entry: (parse_schedule_datetime(entry.get("datetime", "")) or datetime.max).time())
            date_map[day] = items
    return date_map

async def send_schedule_list_message(target):
    if not schedule:
        await target.send("등록된 일정이 없어")
        return

    lines = []
    for i, item in enumerate(schedule, start=1):
        alert_text = "🔔" if item.get("alert_enabled") else "—"
        lines.append(f"{i}. {item['datetime']} | {get_schedule_category_label(item)} | {item['text']} | {repeat_rule_to_text(item)} | {item.get('name', '사용자')} | {alert_text}")

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

    title_font = get_font(36)
    header_font = get_font(16)
    day_font = get_font(16)
    schedule_font = get_font(13)
    bottom_title_font = get_font(16)
    bottom_text_font = get_font(13)

    card_x1, card_y1, card_x2, card_y2 = 55, 40, 1045, 1210
    draw.rounded_rectangle((card_x1, card_y1, card_x2, card_y2), radius=28, fill=card_bg, outline=card_outline, width=3)
    draw.rounded_rectangle((card_x1 + 12, card_y1 + 12, card_x2 - 12, card_y2 - 12), radius=24, outline=(232, 228, 237), width=2)

    title = f"{year}년 {month:02d}월"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    title_w = bbox[2] - bbox[0]
    draw.text(((width - title_w) / 2, 78), title, fill=title_color, font=title_font)

    days = ["일", "월", "화", "수", "목", "금", "토"]
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
        draw.text((grid_left + i * (cell_w + gap_x) + (cell_w - tw) / 2, 160), day_name, fill=color, font=header_font)

    cal = calendar.Calendar(firstweekday=6)
    month_days = cal.monthdayscalendar(year, month)
    date_map = get_month_schedule_map(year, month)
    holiday_map = get_month_holidays(year, month)

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

            draw.text((x1 + 12, y1 + 10), str(day_num), fill=day_color, font=day_font)

            holiday_names = holiday_map.get(day_num, [])
            if holiday_names:
                holiday_text = safe_text(", ".join(holiday_names), 10)
                draw.text((x1 + 10, y1 + 30), holiday_text, fill=HOLIDAY_COLOR, font=schedule_font)

            items = date_map.get(day_num, [])
            preview_y = y1 + (54 if holiday_names else 40)

            for idx, item in enumerate(items[:2]):
                dt = parse_schedule_datetime(item.get("datetime", ""))
                time_text = dt.strftime("%H:%M") if dt else "--:--"
                preview = safe_text(f"{time_text} {item['text']}", 12)
                draw.text((x1 + 10, preview_y + idx * 18), preview, fill=resolve_schedule_color(item), font=schedule_font)

            if len(items) > 2:
                more_text = f"+{len(items) - 2}"
                draw.text((x1 + 10, preview_y + 36), more_text, fill=(120, 115, 130), font=schedule_font)

    section_x1, section_y1, section_x2, section_y2 = 95, 1040, 1005, 1170
    draw.rounded_rectangle((section_x1, section_y1, section_x2, section_y2), radius=18, fill=section_bg, outline=cell_outline, width=2)
    draw.text((section_x1 + 18, section_y1 + 16), "오늘 일정", fill=title_color, font=bottom_title_font)

    today_items = []
    today_holidays = []
    if is_current_month:
        today_holidays = holiday_map.get(now.day, [])
        for item in schedule:
            if schedule_occurs_on_date(item, now.date()):
                today_items.append(item)

    y_cursor = section_y1 + 48
    if today_holidays:
        for holiday_name in today_holidays[:2]:
            draw.text((section_x1 + 18, y_cursor), f"- {holiday_name}", fill=HOLIDAY_COLOR, font=bottom_text_font)
            y_cursor += 22

    if today_items:
        for idx, item in enumerate(today_items[:3]):
            dt = parse_schedule_datetime(item.get("datetime", ""))
            time_text = dt.strftime("%H:%M") if dt else "--:--"
            line = f"- {time_text} {safe_text(item['text'], 18)}"
            draw.text((section_x1 + 18, y_cursor), line, fill=resolve_schedule_color(item), font=bottom_text_font)
            y_cursor += 24
    elif not today_holidays:
        draw.text((section_x1 + 18, section_y1 + 52), "오늘 일정 없음", fill=(135, 131, 142), font=bottom_text_font)

    output_file = os.path.join(BASE_DIR, f"calendar_{year}_{month}_{uuid.uuid4().hex[:8]}.png")
    image.save(output_file)
    return output_file

# =========================
# 도움말 UI
# =========================
class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="📅 일정", style=discord.ButtonStyle.success)
    async def schedule_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = (
            "📅 일정 도움말\n\n"
            "/캘린더\n"
            "/캘린더 2026 03\n"
            "/일정추가 날짜 시간 내용\n"
            "/일정삭제 번호\n"
            "/일정목록\n"
            "/일정수정\n\n"
            "일정 종류: 개인 / 생일 / 이벤트 / 업데이트 / 임시공휴일\n"
            "반복 설정: 없음 / 매일 / 매월 / 매년 / 요일반복(월,화,수,목,금,토,일 선택) / 평일 / 주말\n\n"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="🔊 TTS", style=discord.ButtonStyle.primary)
    async def tts_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = build_tts_help_text()
        await interaction.response.send_message(text, ephemeral=True)

class HelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📖 도움말", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        text = (
            "📅 일정 도움말\n\n"
            "/캘린더\n"
            "/캘린더 2026 03\n"
            "/일정추가 날짜 시간 내용\n"
            "/일정삭제 번호\n"
            "/일정목록\n"
            "/일정수정\n\n"
            "일정 종류: 개인 / 생일 / 이벤트 / 업데이트 / 임시공휴일\n"
            "반복 설정: 없음 / 매일 / 매월 / 매년 / 요일반복(월,화,수,목,금,토,일 선택) / 평일 / 주말\n\n"
        )
        await interaction.response.send_message(text, ephemeral=True)

class ScheduleHelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📖 도움말", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        text = (
            "📅 일정 도움말\n\n"
            "/캘린더\n"
            "/캘린더 2026 03\n"
            "/일정추가 날짜 시간 내용\n"
            "/일정삭제 번호\n"
            "/일정목록\n"
            "/일정수정\n\n"
            "일정 종류: 개인 / 생일 / 이벤트 / 업데이트 / 임시공휴일\n"
            "반복 설정: 없음 / 매일 / 매월 / 매년 / 요일반복(월,화,수,목,금,토,일 선택) / 평일 / 주말\n\n"
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
            lines.append(f"{i}. {item['datetime']} | {get_schedule_category_label(item)} | {item['text']} | {repeat_rule_to_text(item)} | {item.get('name', '사용자')} | {alert_text}")

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
    date = discord.ui.TextInput(label="날짜", placeholder="2026-03-25 또는 20260325")
    time_input = discord.ui.TextInput(label="시간", placeholder="18:00 또는 18시")
    text = discord.ui.TextInput(label="일정 내용", placeholder="약속")
    category_input = discord.ui.TextInput(label="일정 종류", placeholder="개인 / 생일 / 이벤트 / 업데이트 / 임시공휴일", required=False)
    repeat_input = discord.ui.TextInput(label="반복 설정", placeholder="없음 / 매일 / 매월 / 매년 / 요일반복", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            normalized_date = normalize_schedule_date(self.date.value)
            normalized_time = normalize_schedule_time(self.time_input.value)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        if needs_weekday_selection(self.repeat_input.value):
            token = str(uuid.uuid4())
            PENDING_WEEKDAY_SCHEDULES[token] = {
                "date": normalized_date,
                "time": normalized_time,
                "text": self.text.value.strip(),
                "category": self.category_input.value,
                "user_id": interaction.user.id,
                "channel_id": interaction.channel_id,
                "selected_days": set(),
            }
            view = WeekdayRepeatSelectView(token)
            await interaction.response.send_message(
                "반복할 요일을 눌러서 선택해줘.\n선택 후 `완료`를 누르면 저장돼.",
                view=view,
                ephemeral=True
            )
            return

        repeat_type, repeat_days = parse_repeat_rule(self.repeat_input.value)
        item = build_schedule_item_from_values(
            normalized_date,
            normalized_time,
            self.text.value,
            interaction.user,
            interaction.channel_id,
            self.category_input.value,
            repeat_type,
            repeat_days
        )

        category_label = SCHEDULE_CATEGORY_LABELS.get(item.get("category", "personal"), "개인일정")
        repeat_label = repeat_rule_to_text(item)
        await interaction.response.send_message(
            f"✅ 일정 등록 완료\n종류: {category_label}\n반복: {repeat_label}\n새로 /캘린더 입력하면 반영돼",
            ephemeral=True
        )

class WeekdayScheduleModal(discord.ui.Modal, title="요일 선택 일정 등록"):
    date = discord.ui.TextInput(label="날짜", placeholder="2026-03-25 또는 20260325")
    time_input = discord.ui.TextInput(label="시간", placeholder="18:00 또는 18시")
    text = discord.ui.TextInput(label="일정 내용", placeholder="약속")
    category_input = discord.ui.TextInput(label="일정 종류", placeholder="개인 / 생일 / 이벤트 / 업데이트 / 임시공휴일", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            normalized_date = normalize_schedule_date(self.date.value)
            normalized_time = normalize_schedule_time(self.time_input.value)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        token = str(uuid.uuid4())
        PENDING_WEEKDAY_SCHEDULES[token] = {
            "date": normalized_date,
            "time": normalized_time,
            "text": self.text.value.strip(),
            "category": self.category_input.value,
            "user_id": interaction.user.id,
            "channel_id": interaction.channel_id,
            "selected_days": set(),
        }
        view = WeekdayRepeatSelectView(token)
        await interaction.response.send_message(
            "반복할 요일을 눌러서 선택해줘.\n선택 후 `완료`를 누르면 저장돼.",
            view=view,
            ephemeral=True
        )

class WeekdayToggleButton(discord.ui.Button):
    def __init__(self, token: str, label: str, weekday_index: int, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.token = token
        self.weekday_index = weekday_index

    async def callback(self, interaction: discord.Interaction):
        pending = PENDING_WEEKDAY_SCHEDULES.get(self.token)
        if not pending:
            await interaction.response.send_message("등록 정보가 만료됐어. 다시 시도해줘.", ephemeral=True)
            return
        selected = pending["selected_days"]
        if self.weekday_index in selected:
            selected.remove(self.weekday_index)
        else:
            selected.add(self.weekday_index)
        for child in self.view.children:
            if isinstance(child, WeekdayToggleButton):
                child.style = discord.ButtonStyle.success if child.weekday_index in selected else discord.ButtonStyle.secondary
        selected_names = [WEEKDAY_KR[idx] for idx in sorted(selected)]
        content = "반복할 요일을 눌러서 선택해줘.\n선택 후 `완료`를 누르면 저장돼."
        if selected_names:
            content += f"\n현재 선택: {', '.join(selected_names)}"
        await interaction.response.edit_message(content=content, view=self.view)

class WeekdayQuickSetButton(discord.ui.Button):
    def __init__(self, token: str, label: str, preset: list[int], row: int):
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=row)
        self.token = token
        self.preset = preset

    async def callback(self, interaction: discord.Interaction):
        pending = PENDING_WEEKDAY_SCHEDULES.get(self.token)
        if not pending:
            await interaction.response.send_message("등록 정보가 만료됐어. 다시 시도해줘.", ephemeral=True)
            return
        pending["selected_days"] = set(self.preset)
        for child in self.view.children:
            if isinstance(child, WeekdayToggleButton):
                child.style = discord.ButtonStyle.success if child.weekday_index in pending["selected_days"] else discord.ButtonStyle.secondary
        selected_names = [WEEKDAY_KR[idx] for idx in sorted(pending["selected_days"])]
        await interaction.response.edit_message(
            content="반복할 요일을 눌러서 선택해줘.\n선택 후 `완료`를 누르면 저장돼.\n현재 선택: " + ", ".join(selected_names),
            view=self.view
        )

class WeekdayRepeatSaveButton(discord.ui.Button):
    def __init__(self, token: str):
        super().__init__(label="완료", style=discord.ButtonStyle.success, row=2)
        self.token = token

    async def callback(self, interaction: discord.Interaction):
        pending = PENDING_WEEKDAY_SCHEDULES.get(self.token)
        if not pending:
            await interaction.response.send_message("등록 정보가 만료됐어. 다시 시도해줘.", ephemeral=True)
            return
        repeat_days = sorted(pending["selected_days"])
        if not repeat_days:
            await interaction.response.send_message("최소 1개 요일을 선택해줘.", ephemeral=True)
            return
        item = build_schedule_item_from_values(
            pending["date"],
            pending["time"],
            pending["text"],
            interaction.user,
            pending["channel_id"],
            pending["category"],
            "weekly",
            repeat_days
        )
        PENDING_WEEKDAY_SCHEDULES.pop(self.token, None)
        await interaction.response.edit_message(
            content=f"✅ 일정 등록 완료\n종류: {get_schedule_category_label(item)}\n반복: {repeat_rule_to_text(item)}\n새로 /캘린더 입력하면 반영돼",
            view=None
        )

class WeekdayRepeatCancelButton(discord.ui.Button):
    def __init__(self, token: str):
        super().__init__(label="취소", style=discord.ButtonStyle.danger, row=2)
        self.token = token

    async def callback(self, interaction: discord.Interaction):
        PENDING_WEEKDAY_SCHEDULES.pop(self.token, None)
        await interaction.response.edit_message(content="요일 선택 등록을 취소했어.", view=None)

class WeekdayRepeatSelectView(discord.ui.View):
    def __init__(self, token: str):
        super().__init__(timeout=300)
        self.token = token
        labels = [("일", 6), ("월", 0), ("화", 1), ("수", 2), ("목", 3), ("금", 4), ("토", 5)]
        for idx, (label, weekday_index) in enumerate(labels):
            self.add_item(WeekdayToggleButton(token, label, weekday_index, row=0 if idx < 4 else 1))
        self.add_item(WeekdayQuickSetButton(token, "평일", [0,1,2,3,4], row=1))
        self.add_item(WeekdayQuickSetButton(token, "주말", [5,6], row=1))
        self.add_item(WeekdayRepeatSaveButton(token))
        self.add_item(WeekdayRepeatCancelButton(token))

class AddScheduleChoiceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="일반 등록", style=discord.ButtonStyle.success)
    async def normal_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddScheduleModal())

    @discord.ui.button(label="요일 선택 등록", style=discord.ButtonStyle.primary)
    async def weekday_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WeekdayScheduleModal())

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

class ScheduleSelectView(discord.ui.View):
    def __init__(self, action_type: str):
        super().__init__(timeout=60)
        self.add_item(ScheduleSelect(action_type))

class GoToMonthModal(discord.ui.Modal, title="월 이동"):
    year_input = discord.ui.TextInput(label="연도", placeholder="2026")
    month_input = discord.ui.TextInput(label="월", placeholder="3")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            year = int(str(self.year_input.value).strip())
            month = int(str(self.month_input.value).strip())
            if not (1 <= month <= 12):
                raise ValueError
        except Exception:
            await interaction.response.send_message("❌ 연도/월을 다시 확인해줘. 예: 2026 / 3", ephemeral=True)
            return

        await interaction.response.defer()
        file_path = await asyncio.to_thread(create_calendar_image, year, month)
        await interaction.message.edit(attachments=[discord.File(file_path)], view=FinalCalendarView(year, month))

class GoToMonthButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📅 월이동", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(GoToMonthModal())

class ScheduleSearchModal(discord.ui.Modal, title="일정 검색"):
    keyword = discord.ui.TextInput(
        label="검색어",
        placeholder="날짜 / 시간 / 내용 / 이름 아무거나 입력",
    )

    async def on_submit(self, interaction: discord.Interaction):
        results = find_matching_schedules(self.keyword.value)
        if not results:
            await interaction.response.send_message("검색 결과가 없어", ephemeral=True)
            return

        lines = []
        for _, idx, item in results[:10]:
            lines.append(format_schedule_detail(item, idx))
            lines.append("")

        await interaction.response.send_message("```" + "\n".join(lines).strip() + "```", ephemeral=True)

class ScheduleSearchButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔎 일정검색", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ScheduleSearchModal())

class ScheduleViewSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for i, item in enumerate(schedule[:25]):
            label = safe_text(f"{item['datetime']} / {item['text']}", 100)
            desc = safe_text(f"{get_schedule_category_label(item)} / {repeat_rule_to_text(item)}", 100)
            options.append(discord.SelectOption(label=label, description=desc, value=str(i)))

        if not options:
            options.append(discord.SelectOption(label="등록된 일정 없음", value="none"))

        super().__init__(placeholder="확인할 일정을 선택해줘", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("등록된 일정이 없어", ephemeral=True)
            return
        idx = int(self.values[0])
        await interaction.response.send_message("```" + format_schedule_detail(schedule[idx], idx) + "```", ephemeral=True)

class ScheduleViewSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(ScheduleViewSelect())

class ScheduleViewButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📋 일정보기", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not schedule:
            await interaction.response.send_message("등록된 일정이 없어", ephemeral=True)
            return
        await interaction.response.send_message("확인할 일정을 골라줘", view=ScheduleViewSelectView(), ephemeral=True)

class AddScheduleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ 일정등록", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddScheduleModal())

class DeleteScheduleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🗑 일정삭제", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not schedule:
            await interaction.response.send_message("등록된 일정이 없어", ephemeral=True)
            return
        await interaction.response.send_message("삭제할 일정을 골라줘", view=ScheduleSelectView("delete"), ephemeral=True)

class AddAlertButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔔 알림등록", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not schedule:
            await interaction.response.send_message("등록된 일정이 없어", ephemeral=True)
            return
        await interaction.response.send_message("알림 등록할 일정을 골라줘", view=ScheduleSelectView("add_alert"), ephemeral=True)

class DeleteAlertButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔕 알림삭제", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not schedule:
            await interaction.response.send_message("등록된 일정이 없어", ephemeral=True)
            return
        await interaction.response.send_message("알림 삭제할 일정을 골라줘", view=ScheduleSelectView("delete_alert"), ephemeral=True)

class CalendarOptionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ColorButton())
        self.add_item(ScheduleViewButton())
        self.add_item(ScheduleSearchButton())
        self.add_item(AddScheduleButton())
        self.add_item(DeleteScheduleButton())
        self.add_item(AddAlertButton())
        self.add_item(DeleteAlertButton())

class OptionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="⚙ 옵션", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("원하는 기능을 골라줘", view=CalendarOptionView(), ephemeral=True)

class FinalCalendarView(discord.ui.View):
    def __init__(self, year, month):
        super().__init__(timeout=3600)
        self.year = year
        self.month = month
        self.add_item(GoToMonthButton())
        self.add_item(HelpButton())
        self.add_item(OptionButton())

    @discord.ui.button(label="◀ 이전달", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.month -= 1
        if self.month < 1:
            self.month = 12
            self.year -= 1
        file_path = await asyncio.to_thread(create_calendar_image, self.year, self.month)
        await interaction.message.edit(attachments=[discord.File(file_path)], view=FinalCalendarView(self.year, self.month))

    @discord.ui.button(label="다음달 ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.month += 1
        if self.month > 12:
            self.month = 1
            self.year += 1
        file_path = await asyncio.to_thread(create_calendar_image, self.year, self.month)
        await interaction.message.edit(attachments=[discord.File(file_path)], view=FinalCalendarView(self.year, self.month))

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

    @discord.ui.button(label="일정등록", style=discord.ButtonStyle.success, row=1)
    async def add_schedule_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("등록 방법을 골라줘", view=AddScheduleChoiceView(), ephemeral=True)

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
# 이벤트
# =========================
@bot.event
async def on_ready():
    global schedule_task_started, voice_monitor_task_started, slash_sync_done
    print(f"로그인 완료: {bot.user}", flush=True)

    if not slash_sync_done:
        slash_sync_done = True
        print("슬래시 명령어 동기화는 setup_hook에서 글로벌 1회만 실행됨", flush=True)

    try:
        load_schedule()
        load_colors()

        cleared = 0
        for guild in bot.guilds:
            try:
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
                cleared += 1
            except Exception as e:
                print(f"[slash-cleanup] {guild.name} 길드 명령어 정리 실패: {e}", flush=True)
        if cleared:
            print(f"기존 길드 슬래시 명령어 정리 완료: {cleared}개 길드", flush=True)

        if not schedule_task_started:
            bot.loop.create_task(check_schedule())
            schedule_task_started = True

        if not voice_monitor_task_started:
            bot.loop.create_task(voice_idle_watchdog())
            voice_monitor_task_started = True

    except Exception as e:
        print(f"초기화 오류: {e}", flush=True)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)

# =========================
# TTS 명령어
# =========================

TTS_TEMP_DIR = os.path.join(DATA_DIR, "tts_cache")
os.makedirs(TTS_TEMP_DIR, exist_ok=True)

@bot.command(name="캘린더")
async def show_calendar(ctx, year: int = None, month: int = None):
    now = datetime.now()
    year = year or now.year
    month = month or now.month

    file_path = await asyncio.to_thread(create_calendar_image, year, month)
    view = FinalCalendarView(year, month)
    await ctx.send(file=discord.File(file_path), view=view)

@bot.command(name="일정목록")
async def list_schedule(ctx):
    await send_schedule_list_message(ctx)

# =========================
# 실행
# =========================
# =========================
# 슬래시 컨텍스트 / 슬래시 명령어
# =========================
class InteractionCtx:
    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.voice_client = interaction.guild.voice_client if interaction.guild else None

    async def send(self, content=None, **kwargs):
        if not self.interaction.response.is_done():
            return await self.interaction.response.send_message(content=content, **kwargs)
        return await self.interaction.followup.send(content=content, **kwargs)

def _schedule_sort_key(item):
    dt = parse_schedule_datetime(item.get("datetime", "")) or datetime.max
    return (dt, item.get("text", ""))

@bot.tree.command(name="캘린더", description="캘린더를 표시합니다")
@app_commands.describe(year="연도", month="월")
async def slash_show_calendar(interaction: discord.Interaction, year: int | None = None, month: int | None = None):
    await interaction.response.defer(thinking=True)
    await show_calendar(InteractionCtx(interaction), year=year, month=month)

@bot.tree.command(name="일정목록", description="등록된 일정 목록을 보여줍니다")
async def slash_list_schedule(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    await list_schedule(InteractionCtx(interaction))

@bot.tree.command(name="일정추가", description="일정을 등록합니다")
@app_commands.describe(
    date="날짜 예: 2026-03-25 또는 20260325",
    time_input="시간 예: 18:00 또는 18시",
    text="일정 내용",
    category="개인/생일/이벤트/업데이트/임시공휴일",
    repeat="없음/매일/매월/매년/요일반복/평일/주말 또는 월,수,금"
)
@app_commands.choices(
    category=[
        app_commands.Choice(name="개인일정", value="개인"),
        app_commands.Choice(name="생일일정", value="생일"),
        app_commands.Choice(name="이벤트일정", value="이벤트"),
        app_commands.Choice(name="업데이트일정", value="업데이트"),
        app_commands.Choice(name="임시공휴일", value="임시공휴일"),
    ],
    repeat=[
        app_commands.Choice(name="없음", value="없음"),
        app_commands.Choice(name="매일", value="매일"),
        app_commands.Choice(name="매월", value="매월"),
        app_commands.Choice(name="매년", value="매년"),
        app_commands.Choice(name="요일반복(선택창 열기)", value="요일반복"),
        app_commands.Choice(name="평일", value="평일"),
        app_commands.Choice(name="주말", value="주말"),
    ],
)
async def slash_add_schedule(interaction: discord.Interaction, date: str, time_input: str, text: str, category: str = "개인", repeat: str = "없음"):
    try:
        normalized_date = normalize_schedule_date(date)
        normalized_time = normalize_schedule_time(time_input)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    if needs_weekday_selection(repeat):
        PENDING_WEEKDAY_SCHEDULES[interaction.user.id] = {
            "date": normalized_date,
            "time": normalized_time,
            "text": text,
            "category": category,
            "channel_id": interaction.channel_id,
        }
        await interaction.response.send_message("요일을 골라줘", view=WeekdayRepeatPickerView(interaction.user.id), ephemeral=True)
        return

    repeat_type, repeat_days = parse_repeat_rule(repeat)
    item = build_schedule_item_from_values(
        normalized_date, normalized_time, text, interaction.user, interaction.channel_id, category, repeat_type, repeat_days
    )
    await interaction.response.send_message(
        f"✅ 일정 등록 완료\n종류: {get_schedule_category_label(item)}\n반복: {repeat_rule_to_text(item)}\n새로: /캘린더 입력하면 반영돼",
        ephemeral=True
    )

class ScheduleEditModal(discord.ui.Modal, title="일정 수정"):
    def __init__(self, item_index: int):
        super().__init__()
        self.item_index = item_index
        item = schedule[item_index]
        dt = parse_schedule_datetime(item.get("datetime", "")) or datetime.now()

        self.date_input = discord.ui.TextInput(
            label="날짜",
            default=dt.strftime("%Y-%m-%d"),
            placeholder="2026-03-25 또는 20260325",
            required=True,
            max_length=20,
        )
        self.time_input = discord.ui.TextInput(
            label="시간",
            default=dt.strftime("%H:%M"),
            placeholder="18:00 또는 18시",
            required=True,
            max_length=20,
        )
        self.text_input = discord.ui.TextInput(
            label="일정 내용",
            default=item.get("text", ""),
            required=True,
            max_length=100,
        )
        self.category_input = discord.ui.TextInput(
            label="일정 종류",
            default=get_schedule_category_label(item).replace("일정", ""),
            placeholder="개인 / 생일 / 이벤트 / 업데이트 / 임시공휴일",
            required=False,
            max_length=30,
        )
        self.repeat_input = discord.ui.TextInput(
            label="반복 설정",
            default=repeat_rule_to_text(item).replace("반복없음", "없음"),
            placeholder="없음 / 매일 / 매월 / 매년 / 평일 / 주말 / 월,수,금",
            required=False,
            max_length=50,
        )

        self.add_item(self.date_input)
        self.add_item(self.time_input)
        self.add_item(self.text_input)
        self.add_item(self.category_input)
        self.add_item(self.repeat_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            normalized_date = normalize_schedule_date(str(self.date_input.value))
            normalized_time = normalize_schedule_time(str(self.time_input.value))
            category = normalize_schedule_category(str(self.category_input.value or "개인"))
            repeat_type, repeat_days = parse_repeat_rule(str(self.repeat_input.value or "없음"))
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        item = schedule[self.item_index]
        item["datetime"] = f"{normalized_date} {normalized_time}"
        item["text"] = str(self.text_input.value).strip()
        item["category"] = category
        item["repeat_type"] = repeat_type
        item["repeat_days"] = repeat_days
        save_schedule()

        await interaction.response.send_message(
            "✅ 일정 수정 완료\n```" + format_schedule_detail(item, self.item_index) + "```",
            ephemeral=True
        )

class ScheduleEditSelect(discord.ui.Select):
    def __init__(self, matched_items: list[tuple[int, dict]]):
        options = []
        for item_index, item in matched_items[:25]:
            dt = parse_schedule_datetime(item.get("datetime", "")) or datetime.now()
            label = f"{dt.strftime('%H:%M')} | {safe_text(item.get('text', ''), 40)}"
            description = f"{get_schedule_category_label(item)} / {repeat_rule_to_text(item)}"
            options.append(discord.SelectOption(label=label[:100], description=description[:100], value=str(item_index)))
        super().__init__(placeholder="수정할 일정을 골라줘", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        item_index = int(self.values[0])
        await interaction.response.send_modal(ScheduleEditModal(item_index))

class ScheduleEditSelectView(discord.ui.View):
    def __init__(self, matched_items: list[tuple[int, dict]]):
        super().__init__(timeout=120)
        self.add_item(ScheduleEditSelect(matched_items))

@bot.tree.command(name="일정수정", description="날짜를 선택한 뒤 일정을 골라 수정합니다")
@app_commands.describe(date="수정할 일정의 날짜 (예: 2026-03-25)")
async def slash_edit_schedule(interaction: discord.Interaction, date: str):
    try:
        normalized_date = normalize_schedule_date(date)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    matched_items = []
    for item_index, item in enumerate(schedule):
        if item.get("datetime", "").startswith(normalized_date):
            matched_items.append((item_index, item))

    if not matched_items:
        await interaction.response.send_message("❌ 해당 날짜에 일정이 없어", ephemeral=True)
        return

    if len(matched_items) == 1:
        await interaction.response.send_modal(ScheduleEditModal(matched_items[0][0]))
        return

    lines = [f"📅 {normalized_date} 일정 목록"]
    for show_idx, (item_index, item) in enumerate(matched_items, start=1):
        dt = parse_schedule_datetime(item.get("datetime", "")) or datetime.now()
        lines.append(f"{show_idx}. {dt.strftime('%H:%M')} | {item.get('text', '')} | {get_schedule_category_label(item)} | {repeat_rule_to_text(item)}")

    await interaction.response.send_message(
        "\n".join(lines),
        view=ScheduleEditSelectView(matched_items),
        ephemeral=True
    )

@bot.tree.command(name="일정삭제", description="등록된 일정을 삭제합니다")
@app_commands.describe(index="삭제할 일정 번호")
async def slash_delete_schedule(interaction: discord.Interaction, index: int):
    await interaction.response.defer(thinking=True)
    await delete_schedule_cmd(InteractionCtx(interaction), index=index)

# =========================
# TTS 자동 읽기 전용 설정
# =========================
TTS_SETTINGS_FILE = os.path.join(DATA_DIR, "tts_settings.json")
TTS_VOICE_CHOICES = {
    "선하": {"voice": "ko-KR-SunHiNeural", "tone": "밝고 부드러운 여성톤"},
    "지민": {"voice": "ko-KR-JiMinNeural", "tone": "부드럽고 자연스러운 여성톤"},
    "서현": {"voice": "ko-KR-SeoHyeonNeural", "tone": "맑고 안정적인 여성톤"},
    "인준": {"voice": "ko-KR-InJoonNeural", "tone": "차분한 남성톤"},
    "봉진": {"voice": "ko-KR-BongJinNeural", "tone": "또렷하고 단정한 남성톤"},
    "현수": {"voice": "ko-KR-HyunSuNeural", "tone": "부드럽고 젊은 남성톤"},
}

def get_voice_info(label: str) -> dict:
    return TTS_VOICE_CHOICES.get(label, {"voice": "ko-KR-SunHiNeural", "tone": "밝고 부드러운 여성톤"})

def build_voice_guide_text() -> str:
    lines = []
    for label, info in TTS_VOICE_CHOICES.items():
        lines.append(f"- {label}: {info['tone']}")
    return "\n".join(lines)

def build_tts_help_text() -> str:
    return f"""🔊 TTS 도움말

기본 명령어
/입장 : 현재 음성 채널에 들어가고 자동 읽기를 켬
/퇴장 : 음성 채널에서 나가고 자동 읽기를 끔

설정
/읽기채널 : 현재 채널을 읽기 채널로 설정
/닉네임읽기 : 닉네임을 같이 읽을지 설정
/음성 : 한국어 이름으로 TTS 목소리 선택
/속도 : 읽는 속도 조절 (예: 0.8 ~ 1.2)
/톤 : 목소리 높낮이 조절 (예: 0.8 ~ 1.2)
/읽기최적화 : 메시지 길이에 따라 속도와 간격을 자동 보정
/우선순위처리 : 중요한 메시지를 먼저 읽기

유저 필터
/읽기제외추가, /읽기제외삭제 : 특정 유저 제외
/읽기포함추가, /읽기포함삭제 : 특정 유저만 읽기
/읽기대상초기화 : 포함/제외 설정 초기화

상태 확인
/읽기상태 : 현재 자동 읽기 상태 확인
/워치투게더 : 현재 음성 채널용 워치 투게더 초대 링크 생성
/tts도움말 : 이 도움말 다시 보기

목소리 선택 목록
{build_voice_guide_text()}

🎤 목소리 설정 추천 조합
💗 부드러운 기본 (추천)
→ /음성 선하 + /속도 1.0 + /톤 1.0

🧑 차분한 남성톤
→ /음성 인준 + /속도 0.9 + /톤 0.9

⚡ 빠르고 밝게
→ /음성 선하 + /속도 1.2 + /톤 1.1

🎧 방송 느낌 (또박또박)
→ /음성 지민 + /속도 0.95 + /톤 1.0

😴 느리고 안정적인 톤
→ /음성 서현 + /속도 0.8 + /톤 0.9

💡 팁 : 속도는 0.9 ~ 1.1 사이가 가장 자연스럽고, 톤은 0.9 ~ 1.1 사이가 무난해요"""

TTS_DEFAULT_SETTINGS = {
    "enabled": False,
    "read_nickname": True,
    "text_channel_id": None,
    "voice_channel_id": None,
    "voice": "ko-KR-SunHiNeural",
    "voice_label": "선하",
    "rate": "+0%",
    "pitch": "+0Hz",
    "include_user_ids": [],
    "exclude_user_ids": [],
    "between_delay": 0.15,
    "auto_optimize": True,
    "priority_mode": True,
}

tts_settings = {}
tts_queues = {}
tts_workers = {}
tts_worker_locks = {}

def load_tts_settings():
    global tts_settings
    if os.path.exists(TTS_SETTINGS_FILE):
        try:
            with open(TTS_SETTINGS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            tts_settings = {str(k): {**TTS_DEFAULT_SETTINGS, **v} for k, v in raw.items() if isinstance(v, dict)}
        except Exception as e:
            print(f"tts_settings 로드 실패: {e}", flush=True)
            tts_settings = {}
    else:
        tts_settings = {}

def save_tts_settings():
    try:
        with open(TTS_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(tts_settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"tts_settings 저장 실패: {e}", flush=True)

def get_tts_settings(guild_id: int):
    key = str(guild_id)
    if key not in tts_settings:
        tts_settings[key] = dict(TTS_DEFAULT_SETTINGS)
    settings = tts_settings[key]
    settings.setdefault("include_user_ids", [])
    settings.setdefault("exclude_user_ids", [])
    settings.setdefault("voice", "ko-KR-SunHiNeural")
    settings.setdefault("voice_label", "선하")
    settings.setdefault("rate", "+0%")
    settings.setdefault("pitch", "+0Hz")
    settings.setdefault("between_delay", 0.15)
    return settings

def normalize_rate(value: float) -> str:
    value = max(0.5, min(2.0, float(value)))
    percent = int(round((value - 1.0) * 100))
    return f"{percent:+d}%"

def normalize_pitch(value: float) -> str:
    value = max(0.5, min(1.5, float(value)))
    hz = int(round((value - 1.0) * 50))
    return f"{hz:+d}Hz"

def _rate_to_percent(rate_value: str) -> int:
    m = re.match(r'([+-]?\d+)%', str(rate_value or '+0%').strip())
    return int(m.group(1)) if m else 0

def _pitch_to_hz(pitch_value: str) -> int:
    m = re.match(r'([+-]?\d+)Hz', str(pitch_value or '+0Hz').strip())
    return int(m.group(1)) if m else 0

def get_dynamic_tts_profile(text_value: str, settings: dict) -> dict:
    base_rate = _rate_to_percent(settings.get('rate', '+0%'))
    base_pitch = _pitch_to_hz(settings.get('pitch', '+0Hz'))
    delay = max(0.0, float(settings.get('between_delay', 0.15)))

    if not settings.get('auto_optimize', True):
        return {'rate': f'{base_rate:+d}%', 'pitch': f'{base_pitch:+d}Hz', 'between_delay': delay}

    length = len(text_value.strip())
    punctuation_count = sum(text_value.count(ch) for ch in '.!?…')
    extra_pause = min(0.18, punctuation_count * 0.03)

    if length <= 12:
        base_rate += 12
        delay = max(0.04, delay - 0.07)
    elif length <= 35:
        base_rate += 6
        delay = max(0.06, delay - 0.04)
    elif length >= 140:
        base_rate -= 8
        delay += 0.08
    elif length >= 80:
        base_rate -= 4
        delay += 0.04

    if '?' in text_value:
        base_pitch += 4
    if '!' in text_value:
        base_pitch += 6

    base_rate = max(-50, min(80, base_rate))
    base_pitch = max(-60, min(60, base_pitch))
    return {
        'rate': f'{base_rate:+d}%',
        'pitch': f'{base_pitch:+d}Hz',
        'between_delay': round(delay + extra_pause, 3),
    }

def compute_tts_priority(message: discord.Message, text_value: str, settings: dict) -> int:
    if not settings.get('priority_mode', True):
        return 5

    stripped = (text_value or '').strip()
    urgent_prefixes = ('중요', '긴급', '급함', '빨리', '공지')
    if any(stripped.startswith(prefix) for prefix in urgent_prefixes):
        return 0
    if bot.user and bot.user.mention in message.content:
        return 0
    if '@everyone' in message.content or '@here' in message.content:
        return 1
    if len(stripped) <= 12:
        return 2
    if len(stripped) >= 140:
        return 7
    return 5

def clean_tts_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r'https?://\S+', '링크가 포함된 메시지', text)
    text = re.sub(r'<a?:\w+:\d+>', '이모지', text)
    text = re.sub(r'<@!?\d+>', '멘션', text)
    text = re.sub(r'<#\d+>', '채널 멘션', text)
    text = re.sub(r'@everyone|@here', '전체 멘션', text)
    text = re.sub(r'(ㅋ){3,}', 'ㅋㅋ', text)
    text = re.sub(r'(ㅎ){3,}', 'ㅎㅎ', text)
    text = re.sub(r'(ㅠ){3,}', 'ㅠㅠ', text)
    text = re.sub(r'(ㅜ){3,}', 'ㅜㅜ', text)
    text = re.sub(r'[`*_~|]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def should_read_user(settings: dict, user_id: int) -> bool:
    include_ids = set(settings.get("include_user_ids") or [])
    exclude_ids = set(settings.get("exclude_user_ids") or [])
    if include_ids:
        return user_id in include_ids
    return user_id not in exclude_ids

def build_voice_choices_text() -> str:
    return ", ".join(TTS_VOICE_CHOICES.keys())

def build_target_summary(settings: dict, guild: discord.Guild) -> str:
    include_ids = settings.get("include_user_ids") or []
    exclude_ids = settings.get("exclude_user_ids") or []
    if include_ids:
        names = []
        for uid in include_ids:
            member = guild.get_member(uid)
            names.append(member.display_name if member else str(uid))
        return "포함 전용: " + ", ".join(names)
    if exclude_ids:
        names = []
        for uid in exclude_ids:
            member = guild.get_member(uid)
            names.append(member.display_name if member else str(uid))
        return "제외: " + ", ".join(names)
    return "전체"

async def ensure_connected_to_saved_voice(guild: discord.Guild, settings: dict):
    if guild is None:
        raise RuntimeError("서버 안에서만 사용할 수 있어")
    voice_channel_id = settings.get("voice_channel_id")
    if not voice_channel_id:
        raise RuntimeError("먼저 /입장으로 음성 채널에 연결해줘")

    channel = guild.get_channel(voice_channel_id)
    if channel is None:
        channel = await bot.fetch_channel(voice_channel_id)

    voice_client = guild.voice_client

    if voice_client is None:
        voice_client = await channel.connect(self_deaf=False, self_mute=False)
    elif voice_client.channel != channel:
        await voice_client.move_to(channel)

    return voice_client

async def synthesize_edge_tts(text_value: str, settings: dict, rate: str | None = None, pitch: str | None = None) -> str:
    temp_path = os.path.join(TTS_TEMP_DIR, f"tts_{uuid.uuid4().hex}.mp3")
    communicate = edge_tts.Communicate(
        text_value,
        voice=settings.get("voice", "ko-KR-SunHiNeural"),
        rate=rate or settings.get("rate", "+0%"),
        pitch=pitch or settings.get("pitch", "+0Hz"),
    )
    await communicate.save(temp_path)
    return temp_path

async def play_tts_for_guild(guild: discord.Guild, text_value: str):
    text_value = clean_tts_text(text_value)
    if not text_value:
        return

    settings = get_tts_settings(guild.id)
    voice_client = await ensure_connected_to_saved_voice(guild, settings)
    profile = get_dynamic_tts_profile(text_value, settings)
    temp_path = await synthesize_edge_tts(text_value, settings, profile["rate"], profile["pitch"])

    finished = asyncio.Event()

    def after_play(error):
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        if error:
            print(f"자동 TTS 재생 오류: {error}", flush=True)
        bot.loop.call_soon_threadsafe(finished.set)

    source = discord.FFmpegPCMAudio(temp_path)
    voice_client.play(source, after=after_play)
    await finished.wait()
    await asyncio.sleep(max(0.0, float(profile.get("between_delay", settings.get("between_delay", 0.15)))))

tts_queue_counters = {}

async def enqueue_tts_message(guild: discord.Guild, text_value: str, priority: int = 5):
    queue = tts_queues.setdefault(guild.id, asyncio.PriorityQueue())
    counter = tts_queue_counters.get(guild.id, 0) + 1
    tts_queue_counters[guild.id] = counter
    await queue.put((priority, counter, text_value))

    worker = tts_workers.get(guild.id)
    if worker is None or worker.done():
        tts_workers[guild.id] = bot.loop.create_task(tts_worker(guild))

async def tts_worker(guild: discord.Guild):
    queue = tts_queues.setdefault(guild.id, asyncio.PriorityQueue())
    lock = tts_worker_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        while True:
            try:
                _, _, text_value = await asyncio.wait_for(queue.get(), timeout=180)
            except asyncio.TimeoutError:
                break

            try:
                await play_tts_for_guild(guild, text_value)
            except Exception as e:
                print(f"tts worker 오류: {e}", flush=True)
            finally:
                queue.task_done()

    tts_workers.pop(guild.id, None)

def clear_tts_queue(guild_id: int):
    queue = tts_queues.get(guild_id)
    if queue is None:
        return
    try:
        while True:
            queue.get_nowait()
            queue.task_done()
    except asyncio.QueueEmpty:
        pass

def cancel_auto_voice_leave(guild_id: int):
    task = auto_voice_leave_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()

def voice_channel_has_human_members(channel: discord.VoiceChannel | discord.StageChannel | None) -> bool:
    if channel is None:
        return False
    return any(not member.bot for member in channel.members)

async def stop_voice_session(guild: discord.Guild, *, disable_tts: bool = True, clear_queue: bool = True):
    cancel_auto_voice_leave(guild.id)
    settings = get_tts_settings(guild.id)
    settings["enabled"] = not disable_tts and settings.get("enabled", False)
    if disable_tts:
        settings["voice_channel_id"] = None
    save_tts_settings()

    if clear_queue:
        clear_tts_queue(guild.id)

    worker = tts_workers.get(guild.id)
    if worker and not worker.done():
        worker.cancel()

    vc = guild.voice_client
    if vc:
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        except Exception:
            pass
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

async def schedule_auto_voice_leave(guild: discord.Guild):
    cancel_auto_voice_leave(guild.id)

    async def _runner():
        try:
            await asyncio.sleep(AUTO_VOICE_LEAVE_DELAY)
            vc = guild.voice_client
            if vc is None or vc.channel is None:
                return
            if voice_channel_has_human_members(vc.channel):
                return
            await stop_voice_session(guild, disable_tts=True, clear_queue=True)
        except asyncio.CancelledError:
            pass
        finally:
            auto_voice_leave_tasks.pop(guild.id, None)

    auto_voice_leave_tasks[guild.id] = bot.loop.create_task(_runner())

async def voice_idle_watchdog():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                vc = guild.voice_client
                if vc is None or vc.channel is None:
                    cancel_auto_voice_leave(guild.id)
                    continue
                if voice_channel_has_human_members(vc.channel):
                    cancel_auto_voice_leave(guild.id)
                    continue
                task = auto_voice_leave_tasks.get(guild.id)
                if task is None or task.done():
                    await schedule_auto_voice_leave(guild)
        except Exception as e:
            print(f"voice idle watchdog 오류: {e}", flush=True)
        await asyncio.sleep(15)

async def enable_auto_tts(ctx_like, voice_channel: discord.VoiceChannel, text_channel: discord.TextChannel):
    guild = ctx_like.guild
    if guild is None:
        raise RuntimeError("서버 안에서만 사용할 수 있어")
    voice_client = guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect(self_deaf=False, self_mute=False)
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)

    settings = get_tts_settings(guild.id)
    settings["enabled"] = True
    settings["voice_channel_id"] = voice_channel.id
    settings["text_channel_id"] = text_channel.id
    save_tts_settings()
    return settings

async def disable_auto_tts(guild: discord.Guild):
    await stop_voice_session(guild, disable_tts=True, clear_queue=True)

def build_auto_tts_status(settings: dict, guild: discord.Guild, channel: discord.TextChannel | None = None) -> str:
    read_nickname = "켜짐" if settings.get("read_nickname", True) else "꺼짐"
    enabled = "켜짐" if settings.get("enabled") else "꺼짐"
    channel_text = f"<#{channel.id}>" if channel else "없음"
    voice_label = settings.get("voice_label", "선하")
    voice_info = get_voice_info(voice_label)
    voice_value = settings.get("voice", voice_info["voice"])
    voice_tone = voice_info["tone"]
    target_summary = build_target_summary(settings, guild)
    return (
        f"자동 읽기: {enabled}\n"
        f"읽기 채널: {channel_text}\n"
        f"닉네임 읽기: {read_nickname}\n"
        f"목소리: {voice_label} - {voice_tone} ({voice_value})\n"
        f"속도: {settings.get('rate', '+0%')}\n"
        f"톤: {settings.get('pitch', '+0Hz')}\n"
        f"읽기 대상: {target_summary}"
    )

    try:
        bot.tree.remove_command(_name)
    except Exception:
        pass

@bot.event
async def on_ready():
    global schedule_task_started, slash_sync_done
    print(f"로그인 완료: {bot.user}", flush=True)

    if not slash_sync_done:
        slash_sync_done = True
        print("슬래시 명령어 동기화는 setup_hook에서 글로벌 1회만 실행됨", flush=True)

    try:
        load_schedule()
        load_colors()
        load_tts_settings()

        cleared = 0
        for guild in bot.guilds:
            try:
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
                cleared += 1
            except Exception as e:
                print(f"[slash-cleanup] {guild.name} 길드 명령어 정리 실패: {e}", flush=True)
        if cleared:
            print(f"기존 길드 슬래시 명령어 정리 완료: {cleared}개 길드", flush=True)

        if not schedule_task_started:
            bot.loop.create_task(check_schedule())
            schedule_task_started = True

    except Exception as e:
        print(f"초기화 오류: {e}", flush=True)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    vc = guild.voice_client
    if vc is None or vc.channel is None:
        cancel_auto_voice_leave(guild.id)
        return

    watched_channel = vc.channel
    if before.channel != watched_channel and after.channel != watched_channel:
        return

    if voice_channel_has_human_members(watched_channel):
        cancel_auto_voice_leave(guild.id)
        return

    await schedule_auto_voice_leave(guild)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    settings = get_tts_settings(message.guild.id)
    if not settings.get("enabled"):
        return

    if message.channel.id != settings.get("text_channel_id"):
        return

    if not message.guild.voice_client:
        return

    if not should_read_user(settings, message.author.id):
        return

    text_value = clean_tts_text(message.content)
    if not text_value:
        return

    if settings.get("read_nickname", True):
        text_value = f"{message.author.display_name} {text_value}"

    priority = compute_tts_priority(message, text_value, settings)
    await enqueue_tts_message(message.guild, text_value, priority)

@bot.tree.command(name="워치투게더", description="현재 음성 채널용 워치 투게더 초대 링크를 만듭니다")
async def slash_watch_together(interaction: discord.Interaction):
    try:
        if interaction.guild is None:
            await interaction.response.send_message("❌ 서버 안에서만 사용할 수 있어", ephemeral=True)
            return
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ 먼저 음성 채널에 들어가 있어야 해", ephemeral=True)
            return

        voice_channel = interaction.user.voice.channel
        invite_target = getattr(discord, "InviteTarget", None)
        invite_kwargs = {
            "max_age": 86400,
            "max_uses": 0,
            "temporary": False,
            "unique": False,
            "target_application_id": WATCH_TOGETHER_APPLICATION_ID,
        }
        if invite_target and hasattr(invite_target, "embedded_application"):
            invite_kwargs["target_type"] = invite_target.embedded_application
        else:
            invite_kwargs["target_type"] = 2

        invite = await voice_channel.create_invite(**invite_kwargs)
        await interaction.response.send_message(
            f"▶ 워치 투게더 링크를 만들었어\n{invite.url}\n음성 채널에서 링크를 눌러서 시작해줘",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.response.send_message("❌ 초대 링크를 만들 권한이 없어. 봇에 초대 만들기 권한을 줘야 해", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ 워치 투게더 링크 생성 실패: {e}", ephemeral=True)

@bot.tree.command(name="입장", description="현재 음성 채널에 들어가고 이 채널의 채팅을 자동으로 읽습니다")
async def slash_tts_join(interaction: discord.Interaction):
    try:
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ 먼저 음성 채널에 들어가 있어야 해", ephemeral=True)
            return
        settings = await enable_auto_tts(InteractionCtx(interaction), interaction.user.voice.channel, interaction.channel)
        cancel_auto_voice_leave(interaction.guild.id)
        await interaction.response.send_message(
            "✅ 들어왔어. 자세한 상태는 /음성상태 로 확인해줘",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ 입장 실패: {e}", ephemeral=True)

@bot.tree.command(name="퇴장", description="음성 채널에서 나가고 자동 읽기를 끕니다")
async def slash_tts_leave(interaction: discord.Interaction):
    try:
        await disable_auto_tts(interaction.guild)
        await interaction.response.send_message("👋 음성 채널에서 나갔고 자동 읽기를 껐어", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ 퇴장 실패: {e}", ephemeral=True)

@bot.tree.command(name="닉네임읽기", description="자동 읽기에서 닉네임을 같이 읽을지 설정합니다")
@app_commands.describe(mode="켜기 또는 끄기")
@app_commands.choices(mode=[
    app_commands.Choice(name="켜기", value="켜기"),
    app_commands.Choice(name="끄기", value="끄기"),
])
async def slash_nickname_tts(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    settings = get_tts_settings(interaction.guild.id)
    settings["read_nickname"] = (mode.value == "켜기")
    save_tts_settings()
    await interaction.response.send_message(
        f"✅ 닉네임 읽기: {'켜짐' if settings['read_nickname'] else '꺼짐'}",
        ephemeral=True,
    )

@bot.tree.command(name="읽기채널", description="현재 채널을 자동 읽기 채널로 설정합니다")
async def slash_reading_channel(interaction: discord.Interaction):
    settings = get_tts_settings(interaction.guild.id)
    settings["text_channel_id"] = interaction.channel.id
    save_tts_settings()
    await interaction.response.send_message(
        f"✅ 이제 이 채널에서 올라오는 채팅만 자동으로 읽어줄게: {interaction.channel.mention}",
        ephemeral=True,
    )

@bot.tree.command(name="tts도움말", description="TTS 자동 읽기 명령어 도움말을 보여줍니다")
async def slash_tts_help(interaction: discord.Interaction):
    await interaction.response.send_message(build_tts_help_text(), ephemeral=True)

@bot.tree.command(name="음성", description="자동 읽기 TTS 목소리를 바꿉니다")
@app_commands.describe(종류="이름과 톤 설명을 보고 원하는 목소리를 선택")
@app_commands.choices(종류=[
    app_commands.Choice(name="선하 - 밝고 부드러운 여성톤", value="선하"),
    app_commands.Choice(name="지민 - 부드럽고 자연스러운 여성톤", value="지민"),
    app_commands.Choice(name="서현 - 맑고 안정적인 여성톤", value="서현"),
    app_commands.Choice(name="인준 - 차분한 남성톤", value="인준"),
    app_commands.Choice(name="봉진 - 또렷하고 단정한 남성톤", value="봉진"),
    app_commands.Choice(name="현수 - 부드럽고 젊은 남성톤", value="현수"),
])
async def slash_tts_voice(interaction: discord.Interaction, 종류: app_commands.Choice[str]):
    settings = get_tts_settings(interaction.guild.id)
    voice_info = get_voice_info(종류.value)
    settings["voice_label"] = 종류.value
    settings["voice"] = voice_info["voice"]
    save_tts_settings()
    await interaction.response.send_message(
        f"✅ 목소리를 {종류.value}로 바꿨어\n톤: {voice_info['tone']}\n현재 음성: {settings['voice']}",
        ephemeral=True,
    )

@bot.tree.command(name="속도", description="자동 읽기 속도를 조절합니다")
@app_commands.describe(배속="0.5 ~ 2.0 사이로 입력, 기본은 1.0")
async def slash_tts_rate(interaction: discord.Interaction, 배속: float):
    settings = get_tts_settings(interaction.guild.id)
    settings["rate"] = normalize_rate(배속)
    save_tts_settings()
    await interaction.response.send_message(
        f"✅ 읽기 속도를 {배속:.2f}배 느낌으로 바꿨어 ({settings['rate']})",
        ephemeral=True,
    )

@bot.tree.command(name="톤", description="자동 읽기 톤을 조절합니다")
@app_commands.describe(높이="0.5 ~ 1.5 사이로 입력, 기본은 1.0")
async def slash_tts_pitch(interaction: discord.Interaction, 높이: float):
    settings = get_tts_settings(interaction.guild.id)
    settings["pitch"] = normalize_pitch(높이)
    save_tts_settings()
    await interaction.response.send_message(
        f"✅ 읽기 톤을 {높이:.2f} 기준으로 바꿨어 ({settings['pitch']})",
        ephemeral=True,
    )

@bot.tree.command(name="읽기최적화", description="메시지 길이에 따라 속도와 간격을 자동 보정할지 설정합니다")
@app_commands.describe(mode="켜기 또는 끄기")
@app_commands.choices(mode=[
    app_commands.Choice(name="켜기", value="켜기"),
    app_commands.Choice(name="끄기", value="끄기"),
])
async def slash_tts_auto_optimize(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    settings = get_tts_settings(interaction.guild.id)
    settings["auto_optimize"] = (mode.value == "켜기")
    save_tts_settings()
    await interaction.response.send_message(f"✅ 읽기 최적화: {'켜짐' if settings['auto_optimize'] else '꺼짐'}", ephemeral=True)

@bot.tree.command(name="우선순위처리", description="중요/짧은 메시지를 먼저 읽을지 설정합니다")
@app_commands.describe(mode="켜기 또는 끄기")
@app_commands.choices(mode=[
    app_commands.Choice(name="켜기", value="켜기"),
    app_commands.Choice(name="끄기", value="끄기"),
])
async def slash_tts_priority_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    settings = get_tts_settings(interaction.guild.id)
    settings["priority_mode"] = (mode.value == "켜기")
    save_tts_settings()
    await interaction.response.send_message(
        f"✅ 우선순위 처리: {'켜짐' if settings['priority_mode'] else '꺼짐'}\n"
        "우선순위가 켜져 있으면 '중요', '긴급'으로 시작하는 메시지와 짧은 메시지를 조금 더 먼저 읽어줘",
        ephemeral=True,
    )

@bot.tree.command(name="읽기제외추가", description="특정 유저를 자동 읽기에서 제외합니다")
@app_commands.describe(유저="읽지 않을 유저")
async def slash_tts_exclude_add(interaction: discord.Interaction, 유저: discord.Member):
    settings = get_tts_settings(interaction.guild.id)
    exclude_ids = set(settings.get("exclude_user_ids") or [])
    exclude_ids.add(유저.id)
    include_ids = set(settings.get("include_user_ids") or [])
    if 유저.id in include_ids:
        include_ids.remove(유저.id)
    settings["exclude_user_ids"] = list(exclude_ids)
    settings["include_user_ids"] = list(include_ids)
    save_tts_settings()
    await interaction.response.send_message(f"✅ {유저.display_name} 님을 읽기 제외에 추가했어", ephemeral=True)

@bot.tree.command(name="읽기제외삭제", description="자동 읽기 제외 목록에서 유저를 제거합니다")
@app_commands.describe(유저="다시 읽을 유저")
async def slash_tts_exclude_remove(interaction: discord.Interaction, 유저: discord.Member):
    settings = get_tts_settings(interaction.guild.id)
    exclude_ids = set(settings.get("exclude_user_ids") or [])
    exclude_ids.discard(유저.id)
    settings["exclude_user_ids"] = list(exclude_ids)
    save_tts_settings()
    await interaction.response.send_message(f"✅ {유저.display_name} 님을 제외 목록에서 뺐어", ephemeral=True)

@bot.tree.command(name="읽기포함추가", description="특정 유저만 읽도록 포함 목록에 추가합니다")
@app_commands.describe(유저="읽을 유저")
async def slash_tts_include_add(interaction: discord.Interaction, 유저: discord.Member):
    settings = get_tts_settings(interaction.guild.id)
    include_ids = set(settings.get("include_user_ids") or [])
    include_ids.add(유저.id)
    exclude_ids = set(settings.get("exclude_user_ids") or [])
    if 유저.id in exclude_ids:
        exclude_ids.remove(유저.id)
    settings["include_user_ids"] = list(include_ids)
    settings["exclude_user_ids"] = list(exclude_ids)
    save_tts_settings()
    await interaction.response.send_message(
        f"✅ {유저.display_name} 님을 포함 목록에 추가했어\n포함 목록이 하나라도 있으면 그 사람들만 읽어",
        ephemeral=True,
    )

@bot.tree.command(name="읽기포함삭제", description="포함 목록에서 유저를 제거합니다")
@app_commands.describe(유저="제거할 유저")
async def slash_tts_include_remove(interaction: discord.Interaction, 유저: discord.Member):
    settings = get_tts_settings(interaction.guild.id)
    include_ids = set(settings.get("include_user_ids") or [])
    include_ids.discard(유저.id)
    settings["include_user_ids"] = list(include_ids)
    save_tts_settings()
    await interaction.response.send_message(f"✅ {유저.display_name} 님을 포함 목록에서 뺐어", ephemeral=True)

@bot.tree.command(name="읽기대상초기화", description="포함/제외 대상을 전부 초기화하고 다시 전체 읽기로 돌립니다")
async def slash_tts_target_reset(interaction: discord.Interaction):
    settings = get_tts_settings(interaction.guild.id)
    settings["include_user_ids"] = []
    settings["exclude_user_ids"] = []
    save_tts_settings()
    await interaction.response.send_message("✅ 읽기 대상 설정을 초기화했어. 이제 다시 전체를 읽어", ephemeral=True)

@bot.tree.command(name="읽기상태", description="자동 읽기 상태와 목소리 설정을 보여줍니다")
async def slash_tts_status(interaction: discord.Interaction):
    settings = get_tts_settings(interaction.guild.id)
    channel = interaction.guild.get_channel(settings.get("text_channel_id")) if settings.get("text_channel_id") else None
    await interaction.response.send_message(
        "ℹ️ 현재 자동 읽기 상태\n" + build_auto_tts_status(settings, interaction.guild, channel),
        ephemeral=True,
    )


@bot.tree.command(name="주사위", description="주사위를 굴립니다")
@app_commands.describe(면수="주사위 면수", 개수="굴릴 개수")
async def slash_dice(interaction: discord.Interaction, 면수: app_commands.Range[int, 2, 1000] = 6, 개수: app_commands.Range[int, 1, 20] = 1):
    results = [random.randint(1, 면수) for _ in range(개수)]
    total = sum(results)
    await interaction.response.send_message(
        f"🎲 주사위 결과 ({개수}개 d{면수})\n결과: {', '.join(map(str, results))}\n합계: {total}",
        ephemeral=True,
    )

@bot.tree.command(name="랜덤", description="입력한 항목 중 하나를 랜덤으로 골라줍니다")
@app_commands.describe(항목들="쉼표(,)로 구분해서 입력해줘. 예: 치킨, 피자, 햄버거")
async def slash_random_pick(interaction: discord.Interaction, 항목들: str):
    items = [item.strip() for item in re.split(r'[,/\n]', 항목들) if item.strip()]
    if len(items) < 2:
        await interaction.response.send_message("❌ 항목을 2개 이상 입력해줘. 예: 치킨, 피자, 햄버거", ephemeral=True)
        return
    picked = random.choice(items)
    await interaction.response.send_message(
        f"🎯 랜덤 선택 결과\n후보: {', '.join(items)}\n선택: **{picked}**",
        ephemeral=True,
    )

@bot.tree.command(name="투표", description="간단한 투표를 만들고 반응 이모지를 붙입니다")
@app_commands.describe(제목="투표 제목", 항목들="쉼표(,)로 구분해서 입력해줘. 최대 9개")
async def slash_poll(interaction: discord.Interaction, 제목: str, 항목들: str):
    items = [item.strip() for item in re.split(r'[,/\n]', 항목들) if item.strip()]
    if len(items) < 2:
        await interaction.response.send_message("❌ 투표 항목을 2개 이상 입력해줘.", ephemeral=True)
        return
    if len(items) > 9:
        await interaction.response.send_message("❌ 투표 항목은 최대 9개까지 가능해.", ephemeral=True)
        return

    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    description = "\n".join(f"{number_emojis[i]} {item}" for i, item in enumerate(items))
    await interaction.response.send_message(f"📊 **{제목}**\n\n{description}")
    message = await interaction.original_response()
    for i in range(len(items)):
        await message.add_reaction(number_emojis[i])

@bot.tree.command(name="음성상태", description="자동 읽기 상태와 목소리 설정을 보여줍니다")
async def slash_voice_status(interaction: discord.Interaction):
    settings = get_tts_settings(interaction.guild.id)
    channel = interaction.guild.get_channel(settings.get("text_channel_id")) if settings.get("text_channel_id") else None
    await interaction.response.send_message(
        "ℹ️ 현재 음성 상태\n" + build_auto_tts_status(settings, interaction.guild, channel),
        ephemeral=True,
    )

bot.run(TOKEN)