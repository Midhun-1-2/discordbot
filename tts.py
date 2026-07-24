import os
import re
import edge_tts

from config import TTS_VOICE, AUDIO_DIR


def _safe_filename(name: str) -> str:
    """Strip characters that aren't safe in a filename."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


async def generate_welcome_audio(member_name: str) -> str:
    """
    Generates a spoken 'Welcome, <name>!' mp3 and returns its file path.
    """
    os.makedirs(AUDIO_DIR, exist_ok=True)

    text = f"Welcome, {member_name}!"
    filename = f"welcome_{_safe_filename(member_name)}.mp3"
    filepath = os.path.join(AUDIO_DIR, filename)

    communicate = edge_tts.Communicate(text, voice=TTS_VOICE)
    await communicate.save(filepath)

    return filepath
