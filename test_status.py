import sys
sys.path.insert(0, '/home/claude/v32-refactor')

# 직접 API 호출
import json
import urllib.request
import ssl
import hmac
import hashlib
from datetime import datetime, timezone

config = json.load(open('config.json'))
access_key = config['api']['access_key']
secret_key = config['api']['secret_key']
vendor_id = config['api']['vendor_id']

coupon_id = "90248815"
request_id = "24193172575129163629263"

# HMAC 생성
now_gmt = datetime.now(timezone.utc)
datetime_str = now_gmt.strftime('%y%m%d') + 'T' + now_gmt.strftime('%H%M%S') + 'Z'
path = f"/v2/providers/fms/apis/api/v1/vendors/{vendor_id}/coupons/{coupon_id}/items/requested/{request_id}"
message = datetime_str + "GET" + path
signature = hmac.new(secret_key.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
authorization = f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={datetime_str}, signature={signature}"

url = f"https://api-gateway.coupang.com{path}"
headers = {
    "Content-Type": "application/json;charset=UTF-8",
    "Authorization": authorization
}

try:
    req = urllib.request.Request(url, headers=headers, method="GET")
    context = ssl.create_default_context()
    with urllib.request.urlopen(req, context=context, timeout=30) as response:
        data = json.loads(response.read().decode('utf-8'))
        print(json.dumps(data, indent=2, ensure_ascii=False))
except urllib.error.HTTPError as e:
    print(f"HTTP Error: {e.code}")
    print(e.read().decode('utf-8'))
