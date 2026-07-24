import os
import re
import asyncio

import discord
from discord.ext import commands
from dotenv import load_dotenv
import edge_tts
import yt_dlp

print("Starting bot...")

# ---------- Config ----------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-AriaNeural")
GREETING_TEMPLATE = os.getenv("GREETING_TEMPLATE", "Welcome, {name}!")
# Voice used just for the person's name, so stylized/English names are
# pronounced correctly even when the rest of the greeting is in another language.
NAME_VOICE = os.getenv("NAME_VOICE", "en-IN-NeerjaNeural")
AUDIO_DIR = "audio"
FFMPEG_PATH = os.getenv(
    "FFMPEG_PATH",
    r"C:\Users\Midhun S\Downloads\ffmpeg-2026-07-23-git-80eb9e99b9-essentials_build\ffmpeg-2026-07-23-git-80eb9e99b9-essentials_build\bin\ffmpeg.exe",
)
# Optional: path to a pre-recorded mp3 (e.g. a custom "Swagatham" clip or jingle)
# to play before the spoken name, instead of generating that part with TTS.
INTRO_AUDIO_PATH = os.getenv("INTRO_AUDIO_PATH", "").strip()

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Check your .env file.")

# voice_states covers greetings; message_content is now needed too, since
# music commands ("zyco play ...") require reading the text of messages.
intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="zyco ", intents=intents)

# One queue per guild so simultaneous joins don't collide.
guild_queues: dict[int, asyncio.Queue] = {}
guild_workers: dict[int, asyncio.Task] = {}


def _safe_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


async def _tts_to_file(text: str, voice: str, path: str):
    communicate = edge_tts.Communicate(text, voice=voice)
    await communicate.save(path)


async def _concat_audio(parts: list[str], output_path: str):
    """Stitches multiple mp3 files together in order using ffmpeg's concat demuxer."""
    list_path = output_path + ".txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in parts:
            abs_p = os.path.abspath(p).replace("\\", "/")
            f.write(f"file '{abs_p}'\n")

    process = await asyncio.create_subprocess_exec(
        FFMPEG_PATH, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
        "-c:a", "libmp3lame", output_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {stderr.decode(errors='ignore')}")


async def generate_welcome_audio(member_name: str) -> str:
    os.makedirs(AUDIO_DIR, exist_ok=True)
    safe = _safe_filename(member_name)

    # Split "Welcome, {name}!" into the part before the name and the part after,
    # so the name can be spoken in its own, more appropriate voice.
    before, _, after = GREETING_TEMPLATE.partition("{name}")

    parts = []

    if INTRO_AUDIO_PATH:
        if not os.path.isfile(INTRO_AUDIO_PATH):
            raise FileNotFoundError(f"INTRO_AUDIO_PATH not found: {INTRO_AUDIO_PATH}")
        parts.append(INTRO_AUDIO_PATH)
    elif before.strip() and re.search(r"\w", before, flags=re.UNICODE):
        before_path = os.path.join(AUDIO_DIR, f"part_before_{safe}.mp3")
        await _tts_to_file(before, TTS_VOICE, before_path)
        parts.append(before_path)

    name_path = os.path.join(AUDIO_DIR, f"part_name_{safe}.mp3")
    await _tts_to_file(member_name, NAME_VOICE, name_path)
    parts.append(name_path)

    if after.strip() and re.search(r"\w", after, flags=re.UNICODE):
        after_path = os.path.join(AUDIO_DIR, f"part_after_{safe}.mp3")
        await _tts_to_file(after, TTS_VOICE, after_path)
        parts.append(after_path)

    final_path = os.path.join(AUDIO_DIR, f"welcome_{safe}.mp3")
    await _concat_audio(parts, final_path)
    return final_path


async def play_welcome(channel: discord.VoiceChannel, audio_path: str):
    guild = channel.guild
    voice_client = guild.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel.id != channel.id:
            await voice_client.move_to(channel)
    else:
        voice_client = await channel.connect()

    if voice_client.is_playing():
        voice_client.stop()

    finished = asyncio.Event()
    source = discord.FFmpegPCMAudio(audio_path, executable=FFMPEG_PATH)
    voice_client.play(source, after=lambda e: finished.set())

    await finished.wait()
    await asyncio.sleep(0.5)

    if voice_client.is_connected():
        await voice_client.disconnect()


async def guild_worker(guild_id: int):
    queue = guild_queues[guild_id]
    while True:
        member, channel = await queue.get()
        try:
            audio_path = await generate_welcome_audio(member.display_name)
            await play_welcome(channel, audio_path)
        except Exception as e:
            print(f"Error greeting {member.display_name}: {e}")
        finally:
            queue.task_done()


def ensure_worker(guild_id: int):
    if guild_id not in guild_queues:
        guild_queues[guild_id] = asyncio.Queue()
    if guild_id not in guild_workers or guild_workers[guild_id].done():
        guild_workers[guild_id] = bot.loop.create_task(guild_worker(guild_id))


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    joined_a_channel = after.channel is not None and before.channel != after.channel
    if not joined_a_channel:
        return

    guild_id = after.channel.guild.id
    ensure_worker(guild_id)
    await guild_queues[guild_id].put((member, after.channel))


async def extract_audio_stream(query: str):
    """Resolves a URL or search text to a direct, streamable audio URL + title."""
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "default_search": "ytsearch",
    }

    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info["url"], info.get("title", "Unknown title")

    return await loop.run_in_executor(None, _extract)


async def _ensure_voice_connected(channel: discord.VoiceChannel, retries: int = 3):
    """Connects/moves to a channel, retrying briefly if the initial handshake drops (error 4006)."""
    guild = channel.guild
    last_error = None
    for attempt in range(retries):
        voice_client = guild.voice_client
        try:
            if voice_client and voice_client.is_connected():
                if voice_client.channel.id != channel.id:
                    await voice_client.move_to(channel)
                return voice_client
            else:
                return await channel.connect(timeout=15, reconnect=True)
        except Exception as e:
            last_error = e
            await asyncio.sleep(1.5)
    raise last_error


@bot.command(name="play")
async def play(ctx, *, query: str):
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("Join a voice channel first, then try again.")
        return

    channel = ctx.author.voice.channel

    try:
        voice_client = await _ensure_voice_connected(channel)
    except Exception as e:
        await ctx.send(f"Couldn't connect to the voice channel: {e}")
        return

    await ctx.send(f"🔎 Looking up: {query}")

    try:
        stream_url, title = await extract_audio_stream(query)
    except Exception as e:
        await ctx.send(f"Couldn't play that: {e}")
        return

    if not voice_client.is_connected():
        await ctx.send("Lost the voice connection, please try again.")
        return

    if voice_client.is_playing():
        voice_client.stop()

    ffmpeg_before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    source = discord.FFmpegPCMAudio(
        stream_url, executable=FFMPEG_PATH, before_options=ffmpeg_before_options
    )
    voice_client.play(source)
    await ctx.send(f"▶️ Now playing: {title}")


@bot.command(name="stop")
async def stop(ctx):
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await ctx.send("⏹️ Stopped.")
    else:
        await ctx.send("Nothing is playing right now.")


@bot.command(name="leave")
async def leave(ctx):
    voice_client = ctx.guild.voice_client
    if voice_client:
        await voice_client.disconnect()
        await ctx.send("👋 Left the voice channel.")
    else:
        await ctx.send("I'm not in a voice channel.")


bot.run(TOKEN)