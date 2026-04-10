# FINAL FIXED BOT

SAFE_VOICES = {
    "인준": "ko-KR-InJoonNeural",
    "선희": "ko-KR-SunHiNeural",
    "유진": "ko-KR-YuJinNeural"
}

def clean_nickname(name: str) -> str:
    if name.endswith("님"):
        return name[:-1]
    return name

async def tts_read(vc, user_name, message):
    name = clean_nickname(user_name)
    text = f"{name}, {message}"
    print(f"[TTS] {text}")

async def check_empty_and_leave(vc):
    if not vc or not vc.channel:
        return
    
    members = [m for m in vc.channel.members if not m.bot]
    
    if len(members) == 0:
        print("사람 없음 → 자동 퇴장")
        await vc.disconnect()

print("bot loaded")
