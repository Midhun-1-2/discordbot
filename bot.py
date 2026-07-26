import os
import re
import time
import shutil
import asyncio

import numpy as np
from scipy import signal

import discord
from discord.ext import commands
from discord.ext import voice_recv
from dotenv import load_dotenv
import edge_tts
import yt_dlp
from faster_whisper import WhisperModel

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
# Optional: path to a cookies.txt file (exported from a real logged-in browser
# session) so yt-dlp's YouTube requests aren't flagged as bot traffic —
# especially needed when running on datacenter IPs like AWS/GCP.
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
# Word that must be heard before a spoken command is acted on, e.g. "zyco".
WAKE_WORD = os.getenv("WAKE_WORD", "zyco").strip().lower()
# "tiny" is the smallest/fastest Whisper model — important on small servers.
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "tiny")

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


MUSIC_DIR = "music_downloads"

# Loaded once at startup — runs on CPU since this is a small server, "tiny"
# model keeps it as light as possible.
print(f"Loading Whisper model ({WHISPER_MODEL_SIZE})...")
whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
print("Whisper model loaded.")


def _pcm_to_whisper_input(pcm_bytes: bytes) -> np.ndarray:
    """Converts Discord's raw PCM (48kHz, stereo, 16-bit) into the 16kHz mono
    float32 format Whisper expects."""
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    audio = audio.reshape(-1, 2).mean(axis=1)  # stereo -> mono
    audio_16k = signal.resample_poly(audio, 1, 3)  # 48000Hz -> 16000Hz
    return (audio_16k / 32768.0).astype(np.float32)


def _parse_voice_command(text: str):
    """Returns ('disconnect', target_name_or_None) if the wake word + a
    disconnect-style instruction was heard, else None."""
    text_l = text.lower()
    if WAKE_WORD not in text_l:
        return None
    for keyword in ("disconnect", "kick", "remove"):
        idx = text_l.find(keyword)
        if idx != -1:
            target = text_l[idx + len(keyword):].strip(" .,!?")
            return ("disconnect", target or None)
    return None


async def _handle_voice_command(guild: discord.Guild, speaker: discord.Member, action: str, target_name: str | None):
    if action != "disconnect":
        return

    voice_client = guild.voice_client
    if not voice_client or not voice_client.channel:
        return

    target_member = None
    if target_name:
        for m in voice_client.channel.members:
            if m.bot:
                continue
            if target_name in m.display_name.lower():
                target_member = m
                break

    # Fall back to the person who spoke the command (e.g. "zyco disconnect me").
    if target_member is None:
        target_member = speaker

    try:
        await target_member.move_to(None)
        print(f"Voice command: disconnected {target_member.display_name}")
    except discord.Forbidden:
        print("Voice command failed: bot needs 'Move Members' permission.")
    except Exception as e:
        print(f"Voice command failed: {e}")


class CommandListenSink(voice_recv.AudioSink):
    """Buffers each speaker's audio, and once they pause, transcribes what
    they said and checks it for a wake word + command."""

    SILENCE_SECONDS = 0.9   # how long a pause means "they finished talking"
    MIN_AUDIO_SECONDS = 0.5  # ignore extremely short blips

    def __init__(self, guild: discord.Guild, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.guild = guild
        self.loop = loop
        self.buffers: dict[int, bytearray] = {}
        self.last_packet_time: dict[int, float] = {}
        self.speakers: dict[int, discord.Member] = {}
        self._flush_task = loop.create_task(self._flush_loop())

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if user is None or user.bot:
            return
        buf = self.buffers.setdefault(user.id, bytearray())
        buf.extend(data.pcm)
        self.last_packet_time[user.id] = time.time()
        self.speakers[user.id] = user

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(0.3)
            now = time.time()
            for user_id in list(self.buffers.keys()):
                last_time = self.last_packet_time.get(user_id, 0)
                if now - last_time < self.SILENCE_SECONDS:
                    continue  # still talking (or mid-pause), wait longer

                pcm_bytes = bytes(self.buffers.pop(user_id, b""))
                self.last_packet_time.pop(user_id, None)
                speaker = self.speakers.pop(user_id, None)
                if not pcm_bytes or speaker is None:
                    continue

                seconds = len(pcm_bytes) / (48000 * 2 * 2)  # 48kHz, stereo, 16-bit
                if seconds < self.MIN_AUDIO_SECONDS:
                    continue

                asyncio.create_task(self._transcribe_and_handle(pcm_bytes, speaker))

    async def _transcribe_and_handle(self, pcm_bytes: bytes, speaker: discord.Member):
        try:
            audio = await self.loop.run_in_executor(None, _pcm_to_whisper_input, pcm_bytes)
            segments, _ = await self.loop.run_in_executor(
                None, lambda: whisper_model.transcribe(audio, language="en", beam_size=1)
            )
            text = " ".join(seg.text for seg in segments).strip()
            if not text:
                return
            print(f"[voice heard from {speaker.display_name}]: {text}")

            command = _parse_voice_command(text)
            if command:
                action, target_name = command
                await _handle_voice_command(self.guild, speaker, action, target_name)
        except Exception as e:
            print(f"Transcription error: {e}")

    def cleanup(self):
        self._flush_task.cancel()


guild_listen_sinks: dict[int, CommandListenSink] = {}


@bot.command(name="listen")
async def listen(ctx):
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("Join a voice channel first, then try again.")
        return

    channel = ctx.author.voice.channel
    guild = ctx.guild

    voice_client = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
    elif not isinstance(voice_client, voice_recv.VoiceRecvClient):
        await voice_client.disconnect()
        voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)

    sink = CommandListenSink(guild, bot.loop)
    guild_listen_sinks[guild.id] = sink
    voice_client.listen(sink)

    await ctx.send(
        f"🎙️ Listening for voice commands. Say **\"{WAKE_WORD} disconnect <name>\"** "
        f"(or just **\"{WAKE_WORD} disconnect me\"**) in the voice channel."
    )


@bot.command(name="stoplisten")
async def stoplisten(ctx):
    guild = ctx.guild
    sink = guild_listen_sinks.pop(guild.id, None)
    if sink:
        sink.cleanup()
    voice_client = guild.voice_client
    if voice_client:
        voice_client.stop_listening()
    await ctx.send("🔇 Stopped listening for voice commands.")




async def download_audio(query: str):
    """Downloads the audio to a local file and returns (filepath, title)."""
    os.makedirs(MUSIC_DIR, exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "default_search": "ytsearch",
        "remote_components": {"ejs:github"},
        "outtmpl": os.path.join(MUSIC_DIR, "%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
        }],
    }
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        # Never let yt-dlp write back to the original export — it rewrites the
        # cookie file after each use and has been observed to drop essential
        # login cookies (like LOGIN_INFO) over repeated runs. Always hand it
        # a disposable copy instead, so the master file stays intact.
        scratch_cookies = COOKIES_FILE + ".scratch"
        shutil.copyfile(COOKIES_FILE, scratch_cookies)
        ydl_opts["cookiefile"] = scratch_cookies

    loop = asyncio.get_event_loop()

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if "entries" in info:
                info = info["entries"][0]
            filepath = os.path.join(MUSIC_DIR, f"{info['id']}.mp3")
            return filepath, info.get("title", "Unknown title")

    return await loop.run_in_executor(None, _download)


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

    await ctx.send(f"⬇️ Downloading: {query}")

    try:
        filepath, title = await download_audio(query)
    except Exception as e:
        await ctx.send(f"Couldn't play that: {e}")
        return

    if not voice_client.is_connected():
        await ctx.send("Lost the voice connection, please try again.")
        if os.path.exists(filepath):
            os.remove(filepath)
        return

    if voice_client.is_playing():
        voice_client.stop()

    source = discord.FFmpegPCMAudio(filepath, executable=FFMPEG_PATH)

    def _after(error):
        if error:
            print(f"Playback error: {error}")
        # Clean up the downloaded file once playback has finished.
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception as cleanup_error:
                print(f"Couldn't delete {filepath}: {cleanup_error}")

    voice_client.play(source, after=_after)
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