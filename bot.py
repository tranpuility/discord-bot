
# version: 2026-03-29-clean-minimal-music-core
import os
import asyncio
from typing import Optional

import discord
from discord.ext import commands
import wavelink
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TOKEN")
LAVALINK_HOST = os.getenv("LAVALINK_HOST")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD")
LAVALINK_SECURE = os.getenv("LAVALINK_SECURE", "false").lower() in ("1", "true", "yes", "on")

if not TOKEN:
    raise RuntimeError("TOKEN 환경변수가 비어 있습니다.")
if not LAVALINK_HOST or not LAVALINK_PASSWORD:
    raise RuntimeError("LAVALINK_HOST 또는 LAVALINK_PASSWORD 환경변수가 비어 있습니다.")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

track_started_flags: dict[int, bool] = {}
last_text_channel_ids: dict[int, int] = {}

def is_player(vc: object) -> bool:
    return isinstance(vc, wavelink.Player)

def get_text_channel(guild_id: int) -> Optional[discord.TextChannel]:
    channel_id = last_text_channel_ids.get(guild_id)
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    return channel if isinstance(channel, discord.TextChannel) else None

async def send_text(guild_id: int, text: str, view: Optional[discord.ui.View] = None):
    channel = get_text_channel(guild_id)
    if channel:
        await channel.send(text, view=view)

async def ensure_node():
    if wavelink.Pool.nodes:
        return
    scheme = "https" if LAVALINK_SECURE else "http"
    node = wavelink.Node(uri=f"{scheme}://{LAVALINK_HOST}:{LAVALINK_PORT}", password=LAVALINK_PASSWORD)
    await wavelink.Pool.connect(client=bot, nodes=[node])
    print(f"[LAVALINK] connected host={LAVALINK_HOST} port={LAVALINK_PORT} secure={LAVALINK_SECURE}")

async def hard_disconnect(guild: discord.Guild):
    vc = guild.voice_client
    if not vc:
        return
    try:
        await vc.disconnect(force=True)
    except Exception:
        try:
            await vc.disconnect()
        except Exception:
            pass
    await asyncio.sleep(2)

async def connect_player(ctx: commands.Context) -> wavelink.Player:
    await ensure_node()

    if not ctx.author.voice or not ctx.author.voice.channel:
        raise ValueError("음성 채널 먼저 들어가줘")

    target = ctx.author.voice.channel
    last_text_channel_ids[ctx.guild.id] = ctx.channel.id

    vc = ctx.guild.voice_client
    if vc and not is_player(vc):
        await hard_disconnect(ctx.guild)
        vc = None

    player = vc if is_player(vc) else None

    if player and getattr(player, "channel", None) and player.channel.id != target.id:
        await hard_disconnect(ctx.guild)
        player = None

    if player is None:
        player = await asyncio.wait_for(
            target.connect(cls=wavelink.Player, self_deaf=False, self_mute=False),
            timeout=45
        )
        print(f"[VOICE] joined guild={ctx.guild.id} channel={target.id}")
    else:
        print(f"[VOICE] reuse guild={ctx.guild.id} channel={target.id}")

    try:
        await player.set_volume(100)
    except Exception as e:
        print(f"[VOICE] set_volume failed: {e}")

    return player

class MusicView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=3600)
        self.guild_id = guild_id

    def player(self) -> Optional[wavelink.Player]:
        guild = bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        return vc if is_player(vc) else None

    @discord.ui.button(label="▶/⏸", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player()
        if not player:
            await interaction.response.send_message("음성 채널에 없어", ephemeral=True)
            return
        if getattr(player, "paused", False):
            await player.pause(False)
            await interaction.response.send_message("▶️ 다시 재생", ephemeral=True)
        elif getattr(player, "playing", False):
            await player.pause(True)
            await interaction.response.send_message("⏸️ 일시정지", ephemeral=True)
        else:
            await interaction.response.send_message("현재 재생 중인 노래가 없어", ephemeral=True)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.danger, row=0)
    async def stop_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player()
        if not player:
            await interaction.response.send_message("음성 채널에 없어", ephemeral=True)
            return
        await player.stop()
        await interaction.response.send_message("⏹️ 정지 완료", ephemeral=True)

async def play_query(ctx: commands.Context, query: str):
    player = await connect_player(ctx)

    target = query if query.startswith(("http://", "https://", "ytsearch:", "ytmsearch:", "scsearch:")) else f"ytsearch:{query}"
    result = await wavelink.Playable.search(target)
    tracks = getattr(result, "tracks", result)
    tracks = list(tracks) if tracks else []
    if not tracks:
        raise ValueError("검색 결과가 없습니다.")

    track = tracks[0]
    print(f"[TRACK] selected title={getattr(track, 'title', query)} uri={getattr(track, 'uri', None)} author={getattr(track, 'author', None)}")

    track_started_flags[ctx.guild.id] = False
    await player.play(track)
    print(f"[TRACK] play requested guild={ctx.guild.id}")

    try:
        await player.set_volume(100)
    except Exception as e:
        print(f"[TRACK] volume set failed: {e}")

    async def confirm():
        await asyncio.sleep(5)
        vc = ctx.guild.voice_client
        started = track_started_flags.get(ctx.guild.id, False) or bool(getattr(vc, "playing", False))
        if started:
            return
        await send_text(ctx.guild.id, "⚠️ 재생 요청은 들어갔는데 실제 시작이 안 됐어. 이 경우는 코드보다 Lavalink/VPS 음성 경로 문제일 가능성이 커.")
        print(f"[TRACK] start timeout guild={ctx.guild.id}")

    asyncio.create_task(confirm())
    await ctx.send(f"🎵 재생 중: **{getattr(track, 'title', query)}**", view=MusicView(ctx.guild.id))

@bot.event
async def on_ready():
    print(f"로그인 완료: {bot.user}")
    print("yt-dlp cookies 미적용: 쿠키 없이 우회 모드로 시도할게")
    print("yt-dlp IPv4 강제: 켜짐")
    print("yt-dlp web client 비활성화: 켜짐")
    try:
        await ensure_node()
    except Exception as e:
        print(f"초기화 오류: {e}")

@bot.event
async def on_wavelink_node_ready(payload):
    node = getattr(payload, "node", None)
    sid = getattr(node, "session_id", "unknown") if node else "unknown"
    print(f"[LAVALINK] node ready: {sid}")

@bot.event
async def on_wavelink_track_start(payload):
    player = getattr(payload, "player", None)
    track = getattr(payload, "track", None)
    guild = getattr(player, "guild", None)
    if guild:
        track_started_flags[guild.id] = True
        print(f"[LAVALINK] track start guild={guild.id} title={getattr(track, 'title', None)}")

@bot.event
async def on_wavelink_track_end(payload):
    player = getattr(payload, "player", None)
    track = getattr(payload, "track", None)
    guild = getattr(player, "guild", None)
    reason = getattr(payload, "reason", None)
    if guild:
        print(f"[LAVALINK] track end guild={guild.id} title={getattr(track, 'title', None)} reason={reason}")

@bot.event
async def on_wavelink_track_exception(payload):
    player = getattr(payload, "player", None)
    guild = getattr(getattr(player, "guild", None), "id", None)
    exception = getattr(payload, "exception", None)
    print(f"[LAVALINK] track exception guild={guild} error={exception}")

@bot.command(name="입장")
async def join(ctx: commands.Context):
    try:
        await connect_player(ctx)
        await ctx.send(f"✅ {ctx.author.voice.channel.name} 입장 완료")
    except Exception as e:
        await ctx.send(f"❌ 입장 실패: {e}")

@bot.command(name="퇴장")
async def leave(ctx: commands.Context):
    if not ctx.guild.voice_client:
        await ctx.send("음성 채널에 없음")
        return
    await hard_disconnect(ctx.guild)
    print(f"[VOICE] left guild={ctx.guild.id}")
    await ctx.send("👋 퇴장 완료")

@bot.command(name="재생")
async def play(ctx: commands.Context, *, query: str):
    try:
        await play_query(ctx, query)
    except Exception as e:
        await ctx.send(f"❌ 재생 실패: {e}")

@bot.command(name="정지")
async def stop(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if not is_player(vc):
        await ctx.send("음성 채널에 없음")
        return
    await vc.stop()
    await ctx.send("⏹️ 정지 완료")

@bot.command(name="일시정지")
async def pause(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if not is_player(vc) or not getattr(vc, "playing", False):
        await ctx.send("현재 재생 중인 노래가 없어")
        return
    await vc.pause(True)
    await ctx.send("⏸️ 일시정지")

@bot.command(name="다시재생")
async def resume(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if not is_player(vc) or not getattr(vc, "paused", False):
        await ctx.send("일시정지된 노래가 없어")
        return
    await vc.pause(False)
    await ctx.send("▶️ 다시 재생")

bot.run(TOKEN)
