import discord
from discord.ext import commands
import json
import os
import sys
import asyncio
import aiohttp
import yt_dlp
import calendar
import nacl  # PyNaCl import check
import uuid
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont

from dotenv import load_dotenv

# =========================
# 경로 / 환경변수
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESTART_FILE = os.path.join(BASE_DIR, "restart_channel.json")
RESTART_PROCESSING_FILE = os.path.join(BASE_DIR, "restart_channel.processing.json")
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN 환경변수가 비어 있습니다.")

SCHEDULE_FILE = os.path.join(BASE_DIR, "schedule.json")
COLORS_FILE = os.path.join(BASE_DIR, "colors.json")
FONT_FILE = os.path.join(BASE_DIR, "온글잎 박다현체.ttf")

# =========================
# 기본 설정
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

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
    "options": "-vn"
}

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch"
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


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
        data = await loop.run_in_executor(
            None,
            lambda: ytdl.extract_info(query, download=False)
        )

        if "entries" in data:
            entries = data.get("entries")
            if not entries:
                raise ValueError("검색 결과가 없습니다.")
            data = entries[0]

        audio_url = data.get("url")
        if not audio_url:
            raise ValueError("재생 가능한 오디오 URL을 찾지 못했어.")

        source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
        return cls(source, data=data)


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
def get_font(size: int):
    if os.path.exists(FONT_FILE):
        return ImageFont.truetype(FONT_FILE, size)
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
            "last_voice_channel_id": None
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

    if not queue:
        state["current"] = None
        return

    ctx, query = queue.pop(0)

    if ctx.voice_client is None:
        state["current"] = None
        return

    try:
        player = await YTDLSource.from_query(query)

        state["current"] = {
            "title": player.title,
            "query": query,
            "url": player.webpage_url or player.original_url
        }
        state["last_query"] = query
        if ctx.voice_client and ctx.voice_client.channel:
            state["last_voice_channel_id"] = ctx.voice_client.channel.id

        def after_play(error):
            if error:
                print(f"재생 후 오류: {error}")

            if state["current"]:
                if state["repeat"]:
                    queue.insert(0, (ctx, state["current"]["query"]))
                else:
                    state["history"].append(state["current"]["query"])

            future = asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
            try:
                future.result()
            except Exception as e:
                print(f"다음 곡 처리 오류: {e}")

        ctx.voice_client.play(player, after=after_play)

        await ctx.send(
            f"🎵 재생 중: **{player.title}**\n"
            f"대기열: {len(queue)}곡",
            view=MusicView(ctx)
        )

    except Exception as e:
        await ctx.send(f"오류 발생: {e}")
        await play_next(guild_id)


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

    title_font = get_font(42)
    header_font = get_font(18)
    day_font = get_font(20)
    schedule_font = get_font(15)
    bottom_title_font = get_font(18)
    bottom_text_font = get_font(15)

    card_x1, card_y1, card_x2, card_y2 = 55, 40, 1045, 1210
    draw.rounded_rectangle((card_x1, card_y1, card_x2, card_y2), radius=28, fill=card_bg, outline=card_outline, width=3)
    draw.rounded_rectangle((card_x1 + 12, card_y1 + 12, card_x2 - 12, card_y2 - 12), radius=24, outline=(232, 228, 237), width=2)

    title = f"{year}년 {month:02d}월"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    title_w = bbox[2] - bbox[0]
    draw.text(((width - title_w) / 2, 90), title, fill=title_color, font=title_font)

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
        draw.text((grid_left + i * (cell_w + gap_x) + (cell_w - tw) / 2, 170), day_name, fill=color, font=header_font)

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

            draw.text((x1 + 14, y1 + 10), str(day_num), fill=day_color, font=day_font)

            items = date_map.get(day_num, [])
            preview_y = y1 + 40

            for idx, item in enumerate(items[:2]):
                preview = safe_text(item["text"], 9)
                draw.text((x1 + 10, preview_y + idx * 18), preview, fill=(85, 83, 92), font=schedule_font)

            if len(items) > 2:
                more_text = f"+{len(items) - 2}"
                draw.text((x1 + 10, preview_y + 36), more_text, fill=(120, 115, 130), font=schedule_font)

    section_x1, section_y1, section_x2, section_y2 = 95, 1040, 1005, 1170
    draw.rounded_rectangle((section_x1, section_y1, section_x2, section_y2), radius=18, fill=section_bg, outline=cell_outline, width=2)
    draw.text((section_x1 + 18, section_y1 + 16), "오늘 일정", fill=title_color, font=bottom_title_font)

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
            draw.text((section_x1 + 18, section_y1 + 48 + idx * 24), line, fill=text_main, font=bottom_text_font)
    else:
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

    @discord.ui.button(label="🎵 노래", style=discord.ButtonStyle.primary)
    async def music_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = (
            "🎵 노래 명령어\n\n"
            "!입장\n"
            "!퇴장\n"
            "!재생 노래이름\n"
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
            "!일정목록"
        )
        await interaction.response.send_message(text, ephemeral=True)


class HelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📖 도움말", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("보고 싶은 기능을 골라줘", view=HelpView(), ephemeral=True)


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


class AddSongModal(discord.ui.Modal, title="노래 추가"):
    song = discord.ui.TextInput(label="노래 제목 또는 URL", placeholder="예: 아이유 밤편지 / 유튜브 링크")

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = self.ctx.guild.id
        queue = get_guild_queue(guild_id)
        queue.append((self.ctx, self.song.value))

        voice_client = self.ctx.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            await interaction.response.send_message(f"🎶 대기열 추가됨: {self.song.value}", ephemeral=True)
        else:
            await interaction.response.send_message(f"▶️ 바로 재생 시도: {self.song.value}", ephemeral=True)
            await play_next(guild_id)


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
        self.add_item(HelpButton())

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
    def __init__(self, ctx):
        super().__init__(timeout=3600)
        self.ctx = ctx

    @discord.ui.button(label="⏮ 이전곡", style=discord.ButtonStyle.secondary, row=0)
    async def prev_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        state = get_music_state(guild_id)
        queue = get_guild_queue(guild_id)

        if not state["history"]:
            await interaction.response.send_message("이전곡이 없어", ephemeral=True)
            return

        prev_query = state["history"].pop()
        queue.insert(0, (self.ctx, prev_query))

        if self.ctx.voice_client:
            self.ctx.voice_client.stop()
            await interaction.response.send_message(f"⏮ 이전곡으로 이동: {prev_query}", ephemeral=True)
        else:
            await interaction.response.send_message("음성 채널에 없어", ephemeral=True)

    @discord.ui.button(label="⏭ 다음곡", style=discord.ButtonStyle.secondary, row=0)
    async def next_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        queue = get_guild_queue(guild_id)

        if not queue:
            await interaction.response.send_message("다음곡이 없어", ephemeral=True)
            return

        if self.ctx.voice_client:
            self.ctx.voice_client.stop()
            await interaction.response.send_message("⏭ 다음곡으로 넘어갈게", ephemeral=True)
        else:
            await interaction.response.send_message("음성 채널에 없음", ephemeral=True)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, row=0)
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.ctx.voice_client
        if vc is None:
            await interaction.response.send_message("음성 채널에 없어", ephemeral=True)
            return

        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ 일시정지", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ 다시 재생", ephemeral=True)
        else:
            await interaction.response.send_message("현재 재생 중인 노래가 없어", ephemeral=True)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.danger, row=0)
    async def stop_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        music_queues[guild_id] = []
        state = get_music_state(guild_id)
        state["current"] = None

        if self.ctx.voice_client:
            self.ctx.voice_client.stop()
            await interaction.response.send_message("⏹️ 정지 완료", ephemeral=True)
        else:
            await interaction.response.send_message("음성 채널에 없음", ephemeral=True)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.success, row=0)
    async def repeat_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        state = get_music_state(guild_id)
        state["repeat"] = not state["repeat"]
        text = "🔁 반복 켜짐" if state["repeat"] else "➡️ 반복 꺼짐"
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="📜 노래리스트", style=discord.ButtonStyle.secondary, row=1)
    async def queue_list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        state = get_music_state(guild_id)
        queue = get_guild_queue(guild_id)

        lines = []
        if state["current"]:
            lines.append(f"🎵 현재곡: {state['current']['title']}")
        else:
            lines.append("🎵 현재곡 없음")

        lines.append("")

        if queue:
            lines.append("📜 대기열")
            for i, (_, query) in enumerate(queue, start=1):
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
        guild_id = self.ctx.guild.id
        state = get_music_state(guild_id)
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
            "!재생 노래이름\n"
            "!정지\n"
            "!일시정지\n"
            "!다시재생\n"
            "!노래리스트\n"
            "!가사 가수 - 제목"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="➕ 노래추가", style=discord.ButtonStyle.success, row=1)
    async def add_song_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddSongModal(self.ctx))

    @discord.ui.button(label="🗑 노래삭제", style=discord.ButtonStyle.danger, row=1)
    async def remove_song_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        queue = get_guild_queue(guild_id)

        if not queue:
            await interaction.response.send_message("대기열이 비어 있어", ephemeral=True)
            return

        await interaction.response.send_message("삭제할 노래를 골라줘", view=MusicDeleteView(guild_id), ephemeral=True)


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

        # 재시동 완료 메시지 처리
        if os.path.exists(RESTART_FILE):
            try:
                with open(RESTART_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    channel_id = data.get("channel_id")
                    message_id = data.get("message_id")

                if channel_id and message_id:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        try:
                            msg = await channel.fetch_message(message_id)
                            await msg.edit(content="✅ 재시동 완료!")
                        except Exception:
                            await channel.send("✅ 재시동 완료!")

                os.remove(RESTART_FILE)
            except Exception as e:
                print(f"재시동 완료 처리 실패: {e}")

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


# =========================
# 음악 명령어
# =========================
@bot.command(name="재시동")
@commands.is_owner()
async def restart(ctx):
    restart_msg = await ctx.send("🔄 봇 재시작 중...")

    # 채널 ID + 메시지 ID 저장
    try:
        with open(RESTART_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "channel_id": ctx.channel.id,
                "message_id": restart_msg.id
            }, f)
    except Exception as e:
        print(f"재시작 채널 저장 실패: {e}")

    try:
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
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
        if ctx.voice_client is None:
            await channel.connect()
        else:
            await ctx.voice_client.move_to(channel)

        state = get_music_state(ctx.guild.id)
        state["last_voice_channel_id"] = channel.id

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

        await ctx.voice_client.disconnect()
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

    if ctx.voice_client is None:
        if ctx.author.voice is None:
            await ctx.send("음성 채널 먼저 들어가줘")
            return
        await ctx.author.voice.channel.connect()
        state["last_voice_channel_id"] = ctx.author.voice.channel.id
    elif ctx.voice_client.channel:
        state["last_voice_channel_id"] = ctx.voice_client.channel.id

    queue = get_guild_queue(guild_id)
    queue.append((ctx, query))
    state["last_query"] = query

    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        await ctx.send(f"🎶 대기열 추가됨: {query}", view=MusicView(ctx))
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

    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("⏹️ 정지 완료")
    else:
        await ctx.send("음성 채널에 없음")


@bot.command(name="일시정지")
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ 일시정지")
    else:
        await ctx.send("현재 재생 중인 노래가 없어")


@bot.command(name="다시재생")
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ 다시 재생")
    else:
        await ctx.send("일시정지된 노래가 없어")


@bot.command(name="노래리스트")
async def queue_list(ctx):
    guild_id = ctx.guild.id
    await send_queue_list(ctx, guild_id)


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
