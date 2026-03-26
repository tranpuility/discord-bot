# Python 베이스
FROM python:3.11-slim

# 작업 디렉토리
WORKDIR /app

# 시스템 패키지 (ffmpeg 필수)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# requirements 먼저 복사 (캐시 핵심🔥)
COPY requirements.txt .

# pip 업그레이드 + 설치
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 나머지 코드 복사
COPY . .

# 실행
CMD ["python", "bot.py"]