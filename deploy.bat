@echo off
chcp 65001 >nul
setlocal

SET PROJECT_ID=novatra-test
SET REGION=asia-northeast3
SET SERVICE_NAME=coupang-price-manager
SET IMAGE_NAME=gcr.io/%PROJECT_ID%/%SERVICE_NAME%
SET GOOGLE_CLIENT_ID=221865276835-alff74k8g6mcjlmf60mos900no46hqeh.apps.googleusercontent.com

echo ========================================
echo Coupang Price Manager v27 - Cloud Run Deploy
echo ========================================
echo.

echo [1/4] Setting GCP project...
call gcloud config set project %PROJECT_ID%

echo [2/4] Building and pushing Docker image...
call gcloud builds submit --tag %IMAGE_NAME%

echo [3/4] Deploying to Cloud Run...
call gcloud run deploy %SERVICE_NAME% --image %IMAGE_NAME% --platform managed --region %REGION% --allow-unauthenticated --memory 512Mi --timeout 300 --set-env-vars=TZ=Asia/Seoul,AUTH_REQUIRED=true,GOOGLE_CLIENT_ID=%GOOGLE_CLIENT_ID%

echo [4/4] Done!
echo.
echo Service URL:
call gcloud run services describe %SERVICE_NAME% --region %REGION% --format="value(status.url)"

echo.
echo ========================================
echo Allowed accounts:
echo - justin@terabiotech.com
echo - shjung4196@gmail.com
echo ========================================
pause
