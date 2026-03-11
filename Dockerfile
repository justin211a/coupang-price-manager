# 쿠팡 가격 경쟁 관리 - Cloud Run 배포용
FROM python:3.11-slim

WORKDIR /app

# 시스템 패키지
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 한국 시간대 설정 (로그용)
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Python 패키지
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 파일 복사
COPY . .

# Cloud Run은 PORT 환경변수 사용
ENV PORT=8080
EXPOSE 8080

# 서버 실행
CMD ["python", "server.py"]
