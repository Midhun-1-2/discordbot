import asyncio
import edge_tts

VOICE = "ml-IN-MidhunNeural"
TEXT = "സ്വാഗതം, zyco!"


async def main():
    print(f"Generating with voice={VOICE}")
    print(f"Text={TEXT!r}")
    communicate = edge_tts.Communicate(TEXT, voice=VOICE)
    await communicate.save("test_output.mp3")
    print("Saved test_output.mp3 — play this file directly and listen.")


asyncio.run(main())
