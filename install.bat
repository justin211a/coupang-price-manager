@echo off
echo Installing required packages...

pip install flask apscheduler google-cloud-bigquery google-auth google-auth-oauthlib requests gunicorn pytz

echo.
echo Done!
pause
