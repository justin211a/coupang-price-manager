@echo off
chcp 65001 >nul
echo ========================================
echo   쿠팡 가격관리 - 자동 배포
echo ========================================
echo.

cd /d %~dp0

echo [1/3] GitHub에서 최신 코드 가져오는 중...
git pull origin main
if errorlevel 1 (
    echo 에러: git pull 실패
    pause
    exit /b 1
)
echo 완료!
echo.

echo [2/3] Cloud Run에 배포 중... (2~3분 소요)
gcloud run deploy coupang-price-manager --source . --region asia-northeast3 --allow-unauthenticated
if errorlevel 1 (
    echo 에러: 배포 실패
    pause
    exit /b 1
)
echo.

echo ========================================
echo   배포 완료!
echo   https://coupang-price-manager-221865276835.asia-northeast3.run.app
echo ========================================
pause
