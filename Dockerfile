# Python 베이스 (더 가벼운 이미지)
FROM python:3.11-slim

# 작업 디렉토리
WORKDIR /app

# 환경 변수 (속도 + 안정성)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 시스템 패키지 (ffmpeg)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# requirements 먼저 복사 (캐시 활용 핵심🔥)
COPY requirements.txt .

# pip 설치 최적화
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 코드 복사 (마지막 단계)
COPY . .

# 실행
CMD ["python", "bot.py"]