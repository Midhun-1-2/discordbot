import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Any voice from `edge-tts --list-voices`, e.g. en-US-AriaNeural, en-GB-RyanNeural
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-AriaNeural")

AUDIO_DIR = "audio"

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Check your .env file.")
