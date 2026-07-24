import asyncio
import discord


async def play_welcome(channel: discord.VoiceChannel, audio_path: str):
    """
    Connects (or moves) to the given voice channel, plays audio_path,
    then disconnects once playback finishes.
    """
    guild = channel.guild
    voice_client = guild.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel.id != channel.id:
            await voice_client.move_to(channel)
    else:
        voice_client = await channel.connect()

    # Stop anything currently playing before starting the new greeting
    if voice_client.is_playing():
        voice_client.stop()

    finished = asyncio.Event()

    source = discord.FFmpegPCMAudio(audio_path)
    voice_client.play(source, after=lambda e: finished.set())

    await finished.wait()

    # Small pause so the tail of the audio isn't cut off
    await asyncio.sleep(0.5)

    if voice_client.is_connected():
        await voice_client.disconnect()
