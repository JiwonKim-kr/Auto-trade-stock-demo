# Cloud Run 배포 이미지 (PLAN §3.1)
# 빌드:   docker build -t toss-trader server/          (컨텍스트 = server/ — .dockerignore 적용)
# 스모크: docker run --rm -e API_KEY=test -p 8080:8080 toss-trader  →  GET /health = 200
#         (DB·토스 미설정으로도 기동해야 한다 — 옵셔널 설계)
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 번들 데이터 경로를 명시(사이트패키지/작업폴더 추측 배제)
    KRX_HOLIDAYS_PATH=/app/data/krx_holidays.json \
    SYMBOL_SOURCE_PATH=/app/data/krx_symbols.json \
    # 컨테이너 FS 는 휘발 + 비루트 쓰기 불가 — 보고서 파일은 /tmp(정본은 DB, §3.9)
    REPORTS_DIR=/tmp/reports \
    # Cloud Logging 이 severity 를 집도록 한 줄 JSON(§3.8). 필요 시 text 로 오버라이드
    LOG_FORMAT=json

COPY pyproject.toml ./
COPY app ./app
COPY data ./data
RUN pip install --no-cache-dir .

# 비루트 실행(§3.1) — /app 은 root 소유(읽기 전용 사용이 의도)
RUN useradd -m runner
USER runner

# Cloud Run 이 $PORT 를 주입한다(기본 8080)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
