import discord
from discord.ext import commands
import json
import os
import asyncio
import aiohttp
import yt_dlp
import calendar
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont

from dotenv import load_dotenv

# =========================
# 경로 / 환경변수
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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

bot = commands.Bot(command_prefix="!", intents=intents)

schedule = []
user_colors = {}
sent_alerts = set()

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
    "default_search": "ytsearch",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")

    @classmethod
    async def from_query(cls, query: str):
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None,
            lambda: ytdl.extract_info(query, download=False)
        )

        if "entries" in data:
            data = data["entries"][0]

        source = discord.FFmpegPCMAudio(data["url"], **FFMPEG_OPTIONS)
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
# 일정 알림 체크
# =========================
async def check_schedule():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        for item in schedule:
            event_dt = item.get("datetime")
            channel_id = item.get("channel_id")

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

                # 정시 알림
                if event_dt not in sent_alerts and now == event_dt:
                    await channel.send(f"🔔 {item['name']}님의 일정 알림: {item['text']}")
                    sent_alerts.add(event_dt)

                # 10분 전 알림
                if item.get("alert_10min", False):
                    before_dt = (event_time - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
                    before_key = f"{event_dt}_10min"

                    if before_key not in sent_alerts and now == before_dt:
                        await channel.send(f"⏰ 10분 전 알림: {item['name']}님의 일정 {item['text']}")
                        sent_alerts.add(before_key)

        await asyncio.sleep(30)


# =========================
# 캘린더 이미지 생성
# =========================
def get_font(size: int):
    if os.path.exists(FONT_FILE):
        return ImageFont.truetype(FONT_FILE, size)
    return ImageFont.load_default()


def create_calendar_image(year: int, month: int):
    width, height = 1200, 900
    image = Image.new("RGB", (width, height), (255, 250, 252))
    draw = ImageDraw.Draw(image)

    title_font = get_font(40)
    header_font = get_font(24)
    day_font = get_font(22)
    small_font = get_font(18)

    # 제목
    draw.text((40, 30), f"{year}년 {month}월", fill=(60, 60, 60), font=title_font)

    # 요일 헤더
    days = ["월", "화", "수", "목", "금", "토", "일"]
    start_x = 50
    start_y = 110
    cell_w = 140
    cell_h = 90

    for i, day_name in enumerate(days):
        color = (80, 80, 80)
        if i == 5:
            color = (80, 120, 220)
        elif i == 6:
            color = (220, 90, 90)
        draw.text((start_x + i * cell_w + 50, start_y), day_name, fill=color, font=header_font)

    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)

    # 일정 맵
    date_map = {}
    for item in schedule:
        dt_str = item.get("datetime", "")
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue

        if dt.year == year and dt.month == month:
            date_map.setdefault(dt.day, []).append(item)

    grid_y = start_y + 50

    for row_idx, week in enumerate(month_days):
        for col_idx, day_num in enumerate(week):
            x1 = start_x + col_idx * cell_w
            y1 = grid_y + row_idx * cell_h
            x2 = x1 + cell_w - 10
            y2 = y1 + cell_h - 10

            fill_color = (255, 255, 255)
            outline_color = (220, 220, 220)

            if day_num != 0 and day_num in date_map:
                first_color = date_map[day_num][0].get("color", [162, 196, 255])
                fill_color = tuple(first_color)

            draw.rounded_rectangle((x1, y1, x2, y2), radius=15, fill=fill_color, outline=outline_color, width=2)

            if day_num != 0:
                text_color = (50, 50, 50)
                if col_idx == 5:
                    text_color = (80, 120, 220)
                elif col_idx == 6:
                    text_color = (220, 90, 90)

                draw.text((x1 + 10, y1 + 8), str(day_num), fill=text_color, font=day_font)

                items = date_map.get(day_num, [])
                preview_y = y1 + 38
                for idx, item in enumerate(items[:2]):
                    preview = f"- {item['text']}"
                    draw.text((x1 + 10, preview_y + idx * 20), preview[:10], fill=(40, 40, 40), font=small_font)

                if len(items) > 2:
                    draw.text((x1 + 10, preview_y + 40), f"... 외 {len(items) - 2}개", fill=(70, 70, 70), font=small_font)

    output_path = os.path.join(BASE_DIR, "calendar_output.png")
    image.save(output_path)
    return output_path


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
        await interaction.response.send_message(
            f"🎨 색 설정 완료: {PASTEL_COLORS[selected_key]['label']}",
            ephemeral=True
        )


class ColorButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🎨 색 선택", style=discord.ButtonStyle.secondary, row=2)

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
        color = user_colors.get(user_id, PASTEL_COLORS["pastel_blue"]["rgb"])

        schedule.append({
            "datetime": f"{self.date.value} {self.time_input.value}",
            "text": self.text.value,
            "name": user_name,
            "color": color,
            "alert_enabled": False,
            "alert_10min": False,
            "channel_id": interaction.channel_id
        })
        save_schedule()
        await interaction.response.send_message("✅ 일정 등록 완료\n새로 !캘린더 입력하면 반영돼", ephemeral=True)


class ScheduleAddButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="일정등록", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddScheduleModal())


class ScheduleSelect(discord.ui.Select):
    def __init__(self, action_type: str):
        self.action_type = action_type

        options = []
        for i, item in enumerate(schedule):
            label = f"{item['datetime']} / {item['text']}"[:100]
            description = f"{item.get('name', '사용자')}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(i)
                )
            )

        super().__init__(
            placeholder="일정을 선택해줘",
            options=options[:25]
        )

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])

        if idx < 0 or idx >= len(schedule):
            await interaction.response.send_message("❌ 잘못된 선택", ephemeral=True)
            return

        if self.action_type == "delete":
            removed = schedule.pop(idx)
            save_schedule()
            await interaction.response.send_message(
                f"🗑️ 삭제 완료: {removed['datetime']} / {removed['text']}",
                ephemeral=True
            )

        elif self.action_type == "add_alert":
            schedule[idx]["alert_enabled"] = True
            schedule[idx]["alert_10min"] = False
            save_schedule()
            await interaction.response.send_message(
                f"🔔 알림 등록 완료: {schedule[idx]['datetime']} / {schedule[idx]['text']}",
                ephemeral=True
            )

        elif self.action_type == "delete_alert":
            schedule[idx]["alert_enabled"] = False
            schedule[idx]["alert_10min"] = False
            save_schedule()
            await interaction.response.send_message(
                f"🔕 알림 삭제 완료: {schedule[idx]['datetime']} / {schedule[idx]['text']}",
                ephemeral=True
            )


class ScheduleSelectView(discord.ui.View):
    def __init__(self, action_type: str):
        super().__init__(timeout=60)
        self.add_item(ScheduleSelect(action_type))


# =========================
# 캘린더 UI
# =========================
class CalendarView(discord.ui.View):
    def __init__(self, year, month):
        super().__init__(timeout=None)
        self.year = year
        self.month = month
        self.add_item(ColorButton())
        self.add_item(ScheduleAddButton())

    @discord.ui.button(label="◀ 이전달", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.month -= 1
        if self.month < 1:
            self.month = 12
            self.year -= 1

        file = create_calendar_image(self.year, self.month)
        await interaction.response.edit_message(
            attachments=[discord.File(file)],
            view=self
        )

    @discord.ui.button(label="다음달 ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.month += 1
        if self.month > 12:
            self.month = 1
            self.year += 1

        file = create_calendar_image(self.year, self.month)
        await interaction.response.edit_message(
            attachments=[discord.File(file)],
            view=self
        )

    @discord.ui.button(label="일정삭제", style=discord.ButtonStyle.danger, row=1)
    async def delete_schedule_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not schedule:
            await interaction.response.send_message("📋 등록된 일정이 없어", ephemeral=True)
            return

        await interaction.response.send_message(
            "🗑️ 삭제할 일정을 골라줘",
            view=ScheduleSelectView("delete"),
            ephemeral=True
        )

    @discord.ui.button(label="알림등록", style=discord.ButtonStyle.primary, row=1)
    async def add_alert_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not schedule:
            await interaction.response.send_message("📋 등록된 일정이 없어", ephemeral=True)
            return

        await interaction.response.send_message(
            "🔔 알림 등록할 일정을 골라줘",
            view=ScheduleSelectView("add_alert"),
            ephemeral=True
        )

    @discord.ui.button(label="알림삭제", style=discord.ButtonStyle.secondary, row=2)
    async def delete_alert_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not schedule:
            await interaction.response.send_message("📋 등록된 일정이 없어", ephemeral=True)
            return

        await interaction.response.send_message(
            "🔕 알림 삭제할 일정을 골라줘",
            view=ScheduleSelectView("delete_alert"),
            ephemeral=True
        )


# =========================
# 이벤트
# =========================
@bot.event
async def on_ready():
    print(f"로그인 완료: {bot.user}")
    load_schedule()
    load_colors()
    bot.loop.create_task(check_schedule())


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)


# =========================
# 음악 명령어
# =========================
@bot.command(name="입장")
async def join(ctx):
    if ctx.author.voice is None:
        await ctx.send("먼저 음성 채널에 들어가 있어야 해.")
        return

    channel = ctx.author.voice.channel

    if ctx.voice_client is None:
        await channel.connect()
    else:
        await ctx.voice_client.move_to(channel)

    await ctx.send(f"✅ {channel.name} 입장 완료")


@bot.command(name="퇴장")
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 퇴장 완료")
    else:
        await ctx.send("음성 채널에 없음")


@bot.command(name="재생")
async def play(ctx, *, query: str):
    if ctx.author.voice is None:
        await ctx.send("음성 채널 먼저 들어가줘")
        return

    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()

    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()

    try:
        player = await YTDLSource.from_query(query)
        ctx.voice_client.play(player)
        await ctx.send(f"🎵 재생 중: **{player.title}**")
    except Exception as e:
        await ctx.send(f"오류 발생: {e}")


@bot.command(name="정지")
async def stop(ctx):
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("⏹️ 정지 완료")


@bot.command(name="일시정지")
async def pause(ctx):
    if ctx.voice_client:
        ctx.voice_client.pause()
        await ctx.send("⏸️ 일시정지")


@bot.command(name="다시재생")
async def resume(ctx):
    if ctx.voice_client:
        ctx.voice_client.resume()
        await ctx.send("▶️ 다시 재생")


# =========================
# 가사 기능
# =========================
@bot.command(name="가사")
async def lyrics(ctx, *, song: str = None):
    if song is None:
        await ctx.send("노래 제목 입력해줘")
        return

    async with aiohttp.ClientSession() as session:
        url = f"https://api.lyrics.ovh/v1/{song}"
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("가사 못 찾음")
                return
            data = await resp.json()

    text = data.get("lyrics", "없음")
    if len(text) > 2000:
        text = text[:2000]

    await ctx.send(f"📄 {song}\n```{text}```")


# =========================
# 일정 명령어
# =========================
@bot.command(name="일정추가")
async def add_schedule_cmd(ctx, date, time_input, *, text):
    user_id = str(ctx.author.id)
    user_name = ctx.author.display_name
    color = user_colors.get(user_id, PASTEL_COLORS["pastel_blue"]["rgb"])

    schedule.append({
        "datetime": f"{date} {time_input}",
        "text": text,
        "name": user_name,
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

    file_path = create_calendar_image(year, month)
    view = CalendarView(year, month)
    await ctx.send(file=discord.File(file_path), view=view)


@bot.command(name="일정목록")
async def list_schedule(ctx):
    if not schedule:
        await ctx.send("등록된 일정이 없어")
        return

    lines = []
    for i, item in enumerate(schedule, start=1):
        alert_text = "🔔" if item.get("alert_enabled") else "—"
        lines.append(f"{i}. {item['datetime']} | {item['text']} | {item.get('name', '사용자')} | {alert_text}")

    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900]

    await ctx.send(f"```{text}```")


# =========================
# 실행
# =========================
bot.run(TOKEN)