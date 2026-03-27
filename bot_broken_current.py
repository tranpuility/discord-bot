# version: 1.0.1  # 🔥 Railway 캐시 강제 무효화용

# 기존 코드 그대로 유지 + 아래 수정 추가

import os

# 🔥 환경변수 안전 처리 (추가 안정화)
YTDLP_MAX_RETRIES = int(os.getenv("YTDLP_MAX_RETRIES", "2"))

# =========================
# 기존 코드 시작
# =========================

# ⚠️ 중요: try_resolve_player_with_fallback 반드시 존재
async def try_resolve_player_with_fallback(query: str):
    """유튜브 검색 실패 시 다양한 방식으로 재시도"""
    attempted = []

    queries = [
        query,
        f"{query} audio",
        f"{query} official",
    ]

    for q in queries:
        try:
            attempted.append(q)
            player = await create_player(q)
            if player:
                return player, attempted
        except Exception:
            continue

    return None, attempted


# 🔥 play_next 내부 수정 (없으면 추가)
async def play_next(ctx, query):
    player, attempted_queries = await try_resolve_player_with_fallback(query)

    if not player:
        await ctx.send(f"⚠️ 재생 실패: {query} | 시도: {', '.join(attempted_queries)}")
        return

    ctx.voice_client.play(player)

# =========================
# 기존 코드 유지
# =========================

# ⚠️ 아래는 반드시 기존 코드와 연결되도록 유지
# create_player 함수는 기존 코드에 있어야 함

# =========================
# 끝
# =========================
