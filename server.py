#!/usr/bin/env python3
"""
쿠팡 가격 경쟁 관리 - Cloud Run 배포용

실행 (로컬):
  pip install flask apscheduler google-cloud-bigquery google-auth google-auth-oauthlib requests
  python server.py
  
브라우저에서 http://localhost:8080 접속
"""

import os
import sys
import time as time_module
import hmac
import hashlib
import urllib.request
import ssl
import json
import secrets
import threading
import functools
import re
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request, send_from_directory, Response, redirect, session, make_response

# 크롤링
try:
    from bs4 import BeautifulSoup
    import requests as http_requests
    HAS_CRAWLING = True
except ImportError:
    HAS_CRAWLING = False
    print("⚠️ beautifulsoup4/requests 미설치. 경쟁사 자동 크롤링 비활성화")

# Google OAuth
try:
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
    HAS_GOOGLE_AUTH = True
except ImportError:
    HAS_GOOGLE_AUTH = False
    print("⚠️ google-auth 미설치. OAuth 인증 비활성화")

# APScheduler for background tasks
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False
    print("⚠️ apscheduler 미설치. 자동 체크 알림 기능 비활성화")

# Google Cloud Storage for config persistence
try:
    from google.cloud import storage
    HAS_GCS = True
except ImportError:
    HAS_GCS = False
    print("⚠️ google-cloud-storage 미설치. 로컬 파일 저장 사용")

# GCS 설정
GCS_BUCKET_NAME = os.environ.get('GCS_BUCKET', 'coupang-price-manager-config')
GCS_CONFIG_PATH = 'config.json'
GCS_HISTORY_PATH = 'price_history.json'

# 버전 정보
APP_VERSION = "33.9"
BUILD_DATE = "2026-03-12"

# 한국 시간대 (UTC+9)
KST = timezone(timedelta(hours=9))

def get_kst_now():
    """한국 시간 기준 현재 시각"""
    return datetime.now(KST)

def format_kst_datetime(dt=None, offset_minutes=0):
    """한국 시간 포맷 (YYYY-MM-DD HH:mm:ss)"""
    if dt is None:
        dt = get_kst_now()
    if offset_minutes:
        dt = dt + timedelta(minutes=offset_minutes)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# GMT 시간대 설정 (쿠팡 API HMAC용)
os.environ['TZ'] = 'GMT+0'
try:
    time.tzset()
except:
    pass

app = Flask(__name__, static_folder='.')

# 세션 시크릿 키 (환경변수 또는 랜덤 생성)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# ==================== Google OAuth 설정 ====================
# GCP Console에서 OAuth 2.0 클라이언트 ID 생성 후 여기에 입력
# https://console.cloud.google.com/apis/credentials

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '221865276835-alff74k8g6mcjlmf60mos900no46hqeh.apps.googleusercontent.com')

# 허용된 이메일 목록
ALLOWED_EMAILS = [
    'justin@terabiotech.com',
    'shjung4196@gmail.com',
    'david@terabiotech.com',
    'jun@terabiotech.com',
    'andrew@terabiotech.com',
    'reina@terabiotech.com',
]

# 인증 필요 여부 (개발 시 False로 설정 가능)
AUTH_REQUIRED = os.environ.get('AUTH_REQUIRED', 'true').lower() == 'true'

# ==================== 인증 헬퍼 함수 ====================

def is_authenticated():
    """세션에서 인증 상태 확인"""
    if not AUTH_REQUIRED:
        return True
    return session.get('authenticated', False)

def get_current_user():
    """현재 로그인한 사용자 이메일 반환"""
    return session.get('email', None)

def login_required(f):
    """인증 필요 데코레이터"""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            # API 요청인 경우
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized', 'login_required': True}), 401
            # 페이지 요청인 경우
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


# 설정 파일 경로
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
PRICE_HISTORY_FILE = os.path.join(BASE_DIR, 'price_history.json')

# 로그 저장 (메모리)
action_logs = []

# 스케줄러
scheduler = None


# ==================== 유틸리티 ====================

def log_action(action_type, message, data=None):
    """액션 로그 저장 (한국 시간 기준)"""
    log_entry = {
        "timestamp": get_kst_now().isoformat(),
        "type": action_type,
        "message": message,
        "data": data
    }
    action_logs.append(log_entry)
    if len(action_logs) > 100:
        action_logs.pop(0)
    print(f"[{log_entry['timestamp']}] {action_type}: {message}")


def load_config():
    """설정 파일 로드 (GCS 우선, 없으면 로컬)"""
    config = None
    
    # 1. GCS에서 먼저 시도
    if HAS_GCS:
        try:
            client = storage.Client()
            bucket = client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(GCS_CONFIG_PATH)
            
            if blob.exists():
                content = blob.download_as_text()
                config = json.loads(content)
                print(f"[Config] GCS에서 로드: gs://{GCS_BUCKET_NAME}/{GCS_CONFIG_PATH}")
        except Exception as e:
            print(f"[Config] GCS 로드 실패: {e}")
    
    # 2. GCS에 없으면 로컬 파일
    if config is None and os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            print(f"[Config] 로컬 파일에서 로드: {CONFIG_FILE}")
            
            # 로컬에만 있으면 GCS에 업로드
            if HAS_GCS:
                try:
                    save_to_gcs(config, GCS_CONFIG_PATH)
                    print(f"[Config] GCS에 초기 업로드 완료")
                except Exception as e:
                    print(f"[Config] GCS 초기 업로드 실패: {e}")
    
    return config


def save_config(config):
    """설정 파일 저장 (GCS + 로컬 둘 다)"""
    # 1. 로컬 저장 (백업)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    
    # 2. GCS 저장 (메인)
    if HAS_GCS:
        try:
            save_to_gcs(config, GCS_CONFIG_PATH)
            print(f"[Config] GCS 저장 완료: gs://{GCS_BUCKET_NAME}/{GCS_CONFIG_PATH}")
        except Exception as e:
            print(f"[Config] GCS 저장 실패 (로컬은 저장됨): {e}")


def save_to_gcs(data, path):
    """GCS에 JSON 데이터 저장"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(path)
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type='application/json'
    )


# ==================== 가격 이력 관리 ====================

def load_price_history():
    """가격 변동 이력 로드"""
    if not os.path.exists(PRICE_HISTORY_FILE):
        return {"history": [], "competitor_history": []}
    with open(PRICE_HISTORY_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_price_history(history):
    """가격 변동 이력 저장"""
    with open(PRICE_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def record_price_change(product_key, old_price, new_price, method='manual'):
    """가격 변경 기록 (한국 시간 기준)"""
    history = load_price_history()
    history['history'].append({
        'timestamp': get_kst_now().isoformat(),
        'product_key': product_key,
        'old_price': old_price,
        'new_price': new_price,
        'method': method
    })
    if len(history['history']) > 500:
        history['history'] = history['history'][-500:]
    save_price_history(history)


def record_competitor_price(price):
    """경쟁사 가격 기록 (한국 시간 기준)"""
    history = load_price_history()
    today = get_kst_now().strftime('%Y-%m-%d')
    if history['competitor_history']:
        last = history['competitor_history'][-1]
        if last.get('date') == today and last.get('price') == price:
            return
    
    history['competitor_history'].append({
        'timestamp': get_kst_now().isoformat(),
        'date': today,
        'price': price
    })
    if len(history['competitor_history']) > 365:
        history['competitor_history'] = history['competitor_history'][-365:]
    save_price_history(history)


# ==================== BigQuery 연동 ====================

HAS_BIGQUERY = False
try:
    from google.cloud import bigquery
    HAS_BIGQUERY = True
except ImportError:
    pass


def get_bigquery_client():
    """BigQuery 클라이언트 생성"""
    if not HAS_BIGQUERY:
        return None
    
    config = load_config()
    key_path = config.get('bigquery', {}).get('service_account_key', 'service_account.json')
    
    if os.path.exists(key_path):
        return bigquery.Client.from_service_account_json(key_path)
    
    # Cloud Run: 기본 서비스 계정 사용
    try:
        return bigquery.Client(project='novatra-test')
    except:
        return None


# ==================== 쿠팡 API ====================

class CoupangAPI:
    """쿠팡 Open API 클라이언트"""
    
    def __init__(self, config):
        self.access_key = config['api']['access_key']
        self.secret_key = config['api']['secret_key']
        self.vendor_id = config['api']['vendor_id']
        self.base_url = config['api']['base_url']
    
    def _generate_hmac(self, method, path, query=""):
        """HMAC 서명 생성 - GMT 시간 사용"""
        from datetime import datetime, timezone
        now_gmt = datetime.now(timezone.utc)
        datetime_str = now_gmt.strftime('%y%m%d') + 'T' + now_gmt.strftime('%H%M%S') + 'Z'
        message = datetime_str + method + path + query
        
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        authorization = f"CEA algorithm=HmacSHA256, access-key={self.access_key}, signed-date={datetime_str}, signature={signature}"
        return authorization
    
    def _request(self, method, path, query="", data=None):
        """API 요청 실행"""
        authorization = self._generate_hmac(method, path, query)
        
        url = f"{self.base_url}{path}"
        if query:
            url += f"?{query}"
        
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": authorization
        }
        
        try:
            body = json.dumps(data).encode('utf-8') if data else None
            req = urllib.request.Request(url, headers=headers, method=method, data=body)
            context = ssl.create_default_context()
            
            with urllib.request.urlopen(req, context=context, timeout=30) as response:
                result = response.read().decode('utf-8')
                return {"success": True, "data": json.loads(result) if result else {}}
                
        except urllib.error.HTTPError as e:
            try:
                error_body = e.read().decode('utf-8')
            except:
                error_body = str(e)
            return {"success": False, "error": f"HTTP {e.code}", "body": error_body}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def get_inventory(self, vendor_item_id):
        """상품 재고/가격/상태 조회"""
        path = f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendor_item_id}/inventories"
        return self._request("GET", path)
    
    def update_price(self, vendor_item_id, price):
        """판매가 변경"""
        path = f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendor_item_id}/prices/{price}"
        query = "forceSalePriceUpdate=true"
        return self._request("PUT", path, query)
    
    def update_original_price(self, vendor_item_id, price):
        """정가 변경"""
        path = f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendor_item_id}/original-prices/{price}"
        return self._request("PUT", path)
    
    # ==================== 계약서 API ====================
    
    def get_contracts(self):
        """계약서 목록 조회 - contractId 확인용"""
        path = f"/v2/providers/fms/apis/api/v2/vendors/{self.vendor_id}/contracts"
        return self._request("GET", path)
    
    def get_contract(self, contract_id):
        """계약서 단건 조회"""
        path = f"/v2/providers/fms/apis/api/v2/vendors/{self.vendor_id}/contract"
        query = f"contractId={contract_id}"
        return self._request("GET", path, query)
    
    def get_budget(self):
        """예산현황 조회"""
        path = f"/v2/providers/fms/apis/api/v2/vendors/{self.vendor_id}/budgets"
        return self._request("GET", path)
    
    # ==================== 즉시할인쿠폰 API ====================
    
    def get_coupons(self, status="APPLIED", page=1, size=50):
        """즉시할인쿠폰 목록 조회 (status별)"""
        path = f"/v2/providers/fms/apis/api/v2/vendors/{self.vendor_id}/coupons"
        query = f"status={status}&page={page}&size={size}&sort=desc"
        return self._request("GET", path, query)
    
    def get_coupon_by_vendor_item(self, coupon_id, vendor_item_id):
        """특정 상품의 쿠폰 상세 조회"""
        path = f"/v2/providers/fms/apis/api/v1/vendors/{self.vendor_id}/coupons/{coupon_id}/items/{vendor_item_id}"
        query = "type=vendorItemId"
        return self._request("GET", path, query)
    
    def create_instant_coupon(self, vendor_item_ids, discount_amount, contract_id, 
                               hours=None, end_date=None, title=None, discount_type="PRICE"):
        """
        즉시할인쿠폰 생성 + 상품 연결 (전체 프로세스)
        
        Args:
            vendor_item_ids: 상품 옵션ID 리스트 (예: [92195127705, 92195127720])
            discount_amount: 할인 금액 (정액) 또는 할인율 (정률)
            contract_id: 계약서 ID (필수! WING에서 예산 설정 시 생성됨)
            hours: 쿠폰 유효 시간 (end_date와 택1)
            end_date: 종료일 (YYYY-MM-DD HH:mm:ss 형식)
            title: 쿠폰명
            discount_type: "PRICE" (정액) 또는 "RATE" (정률)
        """
        import time as time_module
        
        # 엔드포인트 주의! /coupon (단수)
        path = f"/v2/providers/fms/apis/api/v2/vendors/{self.vendor_id}/coupon"
        
        # 한국 시간 기준 (KST = UTC+9)
        now = get_kst_now()
        
        # 시작 시간: 현재 + 1분 (API 처리 지연 고려)
        start_dt = now + timedelta(minutes=1)
        start_date = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        
        if end_date:
            end_date_str = end_date
        elif hours:
            # 종료 시간: 시작 시간 + hours
            end_dt = start_dt + timedelta(hours=hours)
            end_date_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            # 기본 5시간
            end_dt = start_dt + timedelta(hours=5)
            end_date_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        
        # vendorItemIds를 정수 리스트로 변환 (쿠팡 API 요구사항)
        if isinstance(vendor_item_ids, (int, str)):
            vendor_item_ids_list = [int(vendor_item_ids)]
        else:
            vendor_item_ids_list = [int(vid) for vid in vendor_item_ids]
        
        # 프로모션명 (필수!) - 기본값: "본사이언스 프라임 NMN 할인쿠폰"
        coupon_name = title or "본사이언스 프라임 NMN 할인쿠폰"
        
        # 쿠폰 생성 요청 데이터 - vendorItemIds는 정수 배열!
        data = {
            "contractId": str(contract_id),
            "vendorItemIds": vendor_item_ids_list,  # 정수 배열
            "startAt": start_date,
            "endAt": end_date_str,
            "type": discount_type,  # "PRICE" or "RATE"
            "discount": int(discount_amount),
            "name": coupon_name,  # 필수 필드!
            "maxDiscountPrice": int(discount_amount)  # 정액 할인에서도 필수
        }
        
        print(f"[쿠폰 생성] 요청 데이터: {data}")
        
        # 1단계: 쿠폰 생성 요청
        result = self._request("POST", path, data=data)
        
        if not result.get('success'):
            return result
        
        # 2단계: requestedId 추출 (응답 구조: data.data.content.requestedId)
        response_data = result.get('data', {})
        requested_id = None
        
        if isinstance(response_data, dict):
            inner_data = response_data.get('data', {})
            if isinstance(inner_data, dict):
                # content 안에서 찾기
                content = inner_data.get('content', {})
                if isinstance(content, dict):
                    requested_id = content.get('requestedId')
        
        print(f"[쿠폰 생성] requestedId: {requested_id}")
        
        coupon_id = None
        
        if requested_id:
            print(f"[COUPON] requestedId: {requested_id}, checking status...")
            
            # Wait max 3 seconds (1s x 3 attempts)
            for i in range(3):
                time_module.sleep(1)
                status_result = self.get_coupon_request_status(requested_id)
                
                if status_result.get('success'):
                    status_data = status_result.get('data', {})
                    
                    status = None
                    
                    if isinstance(status_data, dict):
                        inner_data = status_data.get('data', {})
                        if isinstance(inner_data, dict):
                            content = inner_data.get('content', inner_data)
                            if isinstance(content, dict):
                                status = content.get('status')
                                coupon_id = content.get('couponId')
                            else:
                                status = inner_data.get('status')
                                coupon_id = inner_data.get('couponId')
                    
                    print(f"[COUPON] Attempt {i+1}: status={status}, couponId={coupon_id}")
                    
                    if status in ['COMPLETED', 'DONE'] and coupon_id:
                        break
                    elif status in ['FAILED', 'FAIL']:
                        error_msg = ''
                        if isinstance(inner_data, dict):
                            error_msg = inner_data.get('errorMessage', inner_data.get('message', 'Coupon creation failed'))
                        result['coupon_creation_failed'] = True
                        result['error_detail'] = error_msg
                        return result
        
        # 쿠폰 생성 완료 후 상품 연결 (v1 add_coupon_items 필수 - v2 생성만으로는 상품 자동연결 안 됨)
        if coupon_id:
            print(f"[COUPON] Created! couponId: {coupon_id}")
            print(f"[COUPON] Adding items via v1 API: {vendor_item_ids_list}")
            
            add_result = self.add_coupon_items(coupon_id, vendor_item_ids_list)
            print(f"[COUPON] add_result: {add_result}")
            
            # [CIR08] 충돌 감지 → 충돌 쿠폰 파기 후 재시도
            failed_items = add_result.get('failed_items', [])
            if failed_items:
                for fi in failed_items:
                    reason = fi.get('reason', '')
                    if '[CIR08]' in reason:
                        # 충돌 쿠폰 ID 추출: "...another coupon (12345678)."
                        import re
                        match = re.search(r'another coupon \((\d+)\)', reason)
                        if match:
                            conflict_coupon_id = int(match.group(1))
                            conflict_vid = fi.get('vendorItemId')
                            print(f"[COUPON] CIR08 conflict: vendorItem {conflict_vid} blocked by coupon {conflict_coupon_id}")
                            
                            # 충돌 쿠폰이 고정 쿠폰인지 확인
                            is_fixed = self._is_fixed_coupon(conflict_coupon_id)
                            
                            if is_fixed:
                                print(f"[COUPON] Conflict coupon {conflict_coupon_id} is FIXED - cannot auto-resolve")
                                result['items_added'] = False
                                result['blocked_by_fixed'] = {
                                    'coupon_id': conflict_coupon_id,
                                    'vendor_item_id': conflict_vid,
                                    'reason': reason
                                }
                            else:
                                # 자동 쿠폰 → 파기 후 재시도
                                print(f"[COUPON] Cancelling conflict coupon {conflict_coupon_id}...")
                                cancel_r = self.cancel_coupon(conflict_coupon_id)
                                if cancel_r.get('success'):
                                    print(f"[COUPON] Conflict coupon cancelled. Retrying add_items...")
                                    time_module.sleep(2)
                                    retry_result = self.add_coupon_items(coupon_id, vendor_item_ids_list)
                                    print(f"[COUPON] Retry result: {retry_result}")
                                    
                                    retry_failed = retry_result.get('failed_items', [])
                                    if retry_result.get('item_add_confirmed') or (not retry_failed):
                                        result['items_added'] = True
                                        result['conflict_resolved'] = conflict_coupon_id
                                    else:
                                        result['items_added'] = False
                                        result['items_add_error'] = retry_result
                                else:
                                    print(f"[COUPON] Failed to cancel conflict coupon {conflict_coupon_id}")
                                    result['items_added'] = False
                                    result['items_add_error'] = cancel_r
                        else:
                            result['items_added'] = False
                            result['items_add_error'] = add_result
            elif add_result.get('item_add_confirmed') or add_result.get('success', False):
                result['items_added'] = True
            else:
                result['items_added'] = False
                result['items_add_error'] = add_result
            
            result['coupon_id'] = coupon_id
        else:
            print(f"[COUPON] Warning: coupon created but couponId not confirmed (requestedId: {requested_id})")
            result['items_added'] = False
        
        result['requested_id'] = requested_id
        return result
    
    def _is_fixed_coupon(self, coupon_id):
        """고정 쿠폰 여부 확인
        
        고정 쿠폰: "본사이언스 2천원 할인쿠폰" 등 (여러 상품에 범용 적용, 상품명 없음)
        자동 쿠폰: "본사이언스 레스베라트롤 할인쿠폰 14,100원" 등 (특정 상품 전용, 상품명 있음)
        
        판별 기준: 쿠폰명에 상품 관련 키워드가 있으면 자동(파기 가능), 없으면 고정(파기 불가)
        """
        fixed_keywords = ['2천원', '3천원', '5천원', '1만원', '피크하이트']
        product_keywords = ['레스베라트롤', 'NMN', 'nmn', '프라임', 'PRIME', '닥터스', 'DiGU', '디구',
                           '멜라토닌', '피크하이트', '그로우', '증량']
        
        coupons_result = self.get_coupons("APPLIED")
        if not coupons_result.get('success'):
            return False
        
        coupon_data = coupons_result.get('data', {})
        if isinstance(coupon_data, dict):
            inner = coupon_data.get('data', {})
            if isinstance(inner, dict):
                coupon_list = inner.get('content', [])
            else:
                coupon_list = []
        else:
            coupon_list = []
        
        for c in coupon_list:
            if c.get('couponId') == coupon_id:
                name = c.get('promotionName', '')
                
                # 상품 관련 키워드가 있으면 → 특정 상품 전용 쿠폰 → 고정 아님 (파기 가능)
                if any(pk in name for pk in product_keywords):
                    print(f"[FIXED_CHECK] {coupon_id} '{name}' → product keyword found → NOT fixed (can cancel)")
                    return False
                
                # 상품 키워드 없고 고정 키워드 있으면 → 고정 쿠폰 (파기 불가)
                if any(fk in name for fk in fixed_keywords):
                    print(f"[FIXED_CHECK] {coupon_id} '{name}' → fixed keyword found, no product keyword → FIXED")
                    return True
                
                # 어느 쪽에도 안 걸리면 → 안전하게 고정 아님
                print(f"[FIXED_CHECK] {coupon_id} '{name}' → no match → NOT fixed")
                return False
        
        print(f"[FIXED_CHECK] {coupon_id} not found in coupon list → NOT fixed")
        return False
    
    def add_coupon_items(self, coupon_id, vendor_item_ids):
        """기존 쿠폰에 상품 추가 (v1 API) - 비동기 상태 확인 포함"""
        # v1 엔드포인트: POST /coupons/{couponId}/items
        path = f"/v2/providers/fms/apis/api/v1/vendors/{self.vendor_id}/coupons/{coupon_id}/items"
        
        if isinstance(vendor_item_ids, (int, str)):
            vendor_item_ids = [int(vendor_item_ids)]
        else:
            vendor_item_ids = [int(vid) for vid in vendor_item_ids]
        
        data = {
            "vendorItems": vendor_item_ids
        }
        
        print(f"[ADD_ITEMS] couponId={coupon_id}, vendorItems={vendor_item_ids}")
        result = self._request("POST", path, data=data)
        print(f"[ADD_ITEMS] response: {result}")
        
        # 비동기 상태 확인 - requestedId가 있으면 폴링
        if result.get('success'):
            response_data = result.get('data', {})
            requested_id = None
            
            # requestedId 추출 (다양한 응답 구조 대응)
            if isinstance(response_data, dict):
                inner_data = response_data.get('data', {})
                if isinstance(inner_data, dict):
                    content = inner_data.get('content', inner_data)
                    if isinstance(content, dict):
                        requested_id = content.get('requestedId')
            
            if requested_id:
                result['requested_id'] = requested_id
                print(f"[ADD_ITEMS] requestedId: {requested_id}, polling status...")
                
                # Wait max 10 seconds (2s x 5 attempts)
                for i in range(5):
                    time_module.sleep(2)
                    status_result = self.get_item_add_status(coupon_id, requested_id)
                    
                    if status_result.get('success'):
                        status_data = status_result.get('data', {})
                        status = None
                        failed_items = []
                        succeeded = 0
                        
                        if isinstance(status_data, dict):
                            inner_data = status_data.get('data', {})
                            if isinstance(inner_data, dict):
                                content = inner_data.get('content', inner_data)
                                if isinstance(content, dict):
                                    status = content.get('status')
                                    failed_items = content.get('failedVendorItems', [])
                                    succeeded = content.get('succeeded', 0)
                                else:
                                    status = inner_data.get('status')
                        
                        print(f"[ADD_ITEMS] Poll {i+1}/5: status={status}, succeeded={succeeded}, failed={len(failed_items)}")
                        
                        if status in ['COMPLETED', 'DONE']:
                            result['item_add_confirmed'] = True
                            result['succeeded_count'] = succeeded
                            print(f"[ADD_ITEMS] SUCCESS! {succeeded} items added")
                            break
                        elif status in ['FAILED', 'FAIL']:
                            result['item_add_failed'] = True
                            result['failed_items'] = failed_items
                            print(f"[ADD_ITEMS] FAILED! Failed items: {failed_items}")
                            break
                    else:
                        print(f"[ADD_ITEMS] Poll {i+1}/5: status check failed: {status_result}")
            else:
                print(f"[ADD_ITEMS] No requestedId in response")
        
        return result
    
    def get_item_add_status(self, coupon_id, request_id):
        """상품 추가 요청 상태 확인 - 공통 요청상태 확인 API 사용"""
        # 올바른 경로: /v2/providers/fms/apis/api/v1/vendors/{vendorId}/requested/{requestedId}
        path = f"/v2/providers/fms/apis/api/v1/vendors/{self.vendor_id}/requested/{request_id}"
        result = self._request("GET", path)
        print(f"[ITEM_STATUS] requestedId={request_id}, result: {result}")
        return result
    
    def cancel_coupon(self, coupon_id):
        """즉시할인쿠폰 파기 (v1 API - PUT + action=expire)"""
        # v1 엔드포인트: PUT /coupons/{couponId}?action=expire
        path = f"/v2/providers/fms/apis/api/v1/vendors/{self.vendor_id}/coupons/{coupon_id}"
        query = "action=expire"
        
        result = self._request("PUT", path, query)
        print(f"[쿠폰 파기] couponId={coupon_id}, 결과: {result}")
        return result
    
    def get_coupon_request_status(self, request_id):
        """쿠폰 요청 상태 확인 (비동기 결과) - v1 API"""
        # 올바른 경로: /requested/{requestedId}
        path = f"/v2/providers/fms/apis/api/v1/vendors/{self.vendor_id}/requested/{request_id}"
        result = self._request("GET", path)
        print(f"[요청상태] requestedId={request_id}, 결과: {result}")
        return result
    
    def get_coupon_list(self, status="APPLIED"):
        """활성 쿠폰 목록 (간편 조회)"""
        return self.get_coupons(status)
    
    def get_coupons_by_vendor_item(self, vendor_item_id):
        """특정 상품(vendorItemId)에 연결된 쿠폰 조회"""
        # API: GET /v2/providers/fms/apis/api/v1/vendors/{vendorId}/coupons/items/{vendorItemId}
        path = f"/v2/providers/fms/apis/api/v1/vendors/{self.vendor_id}/coupons/items/{vendor_item_id}"
        result = self._request("GET", path)
        print(f"[ITEM_COUPONS] vendorItemId={vendor_item_id}, result: {result}")
        return result
    
    def cancel_coupons_for_item(self, vendor_item_id, fixed_keywords=None):
        """특정 상품에 연결된 모든 쿠폰 파기 (고정 쿠폰은 파기 불가 알림)
        
        Returns:
            dict: {
                'cancelled': [파기된 coupon_id 리스트],
                'blocked': [{'coupon_id':..., 'name':..., 'reason':...}]  # 고정 쿠폰으로 차단된 목록
            }
        """
        if fixed_keywords is None:
            fixed_keywords = ['2천원', '3천원', '5천원', '1만원', '피크하이트']
        
        cancelled = []
        blocked = []
        result = self.get_coupons_by_vendor_item(vendor_item_id)
        
        if not result.get('success'):
            print(f"[CANCEL_FOR_ITEM] Failed to get coupons for {vendor_item_id}")
            return {'cancelled': cancelled, 'blocked': blocked}
        
        # 응답 구조 파싱
        response_data = result.get('data', {})
        coupon_list = []
        
        if isinstance(response_data, dict):
            inner_data = response_data.get('data', {})
            if isinstance(inner_data, dict):
                content = inner_data.get('content', [])
                if isinstance(content, list):
                    coupon_list = content
                elif isinstance(content, dict):
                    coupon_list = [content]
        
        for coupon in coupon_list:
            coupon_id = coupon.get('couponId')
            promo_name = coupon.get('promotionName', '')
            status = coupon.get('status', '')
            
            if not coupon_id:
                continue
            
            # 이미 파기된 쿠폰은 스킵
            if status in ['EXPIRED', 'CANCELLED']:
                print(f"[CANCEL_FOR_ITEM] Already expired: {coupon_id} ({status})")
                continue
            
            # 고정 쿠폰 → 파기 불가, blocked 목록에 추가
            if any(kw in promo_name for kw in fixed_keywords):
                print(f"[CANCEL_FOR_ITEM] BLOCKED by fixed coupon: {promo_name} (ID: {coupon_id})")
                blocked.append({
                    'coupon_id': coupon_id,
                    'name': promo_name,
                    'reason': f"고정 쿠폰 '{promo_name}'에 묶여있음. WING에서 수동 해제 필요."
                })
                continue
            
            print(f"[CANCEL_FOR_ITEM] Cancelling: {promo_name} (ID: {coupon_id}, status: {status})")
            cancel_result = self.cancel_coupon(coupon_id)
            
            if cancel_result.get('success'):
                cancelled.append(coupon_id)
                print(f"[CANCEL_FOR_ITEM] Cancelled: {coupon_id}")
            else:
                print(f"[CANCEL_FOR_ITEM] Failed to cancel {coupon_id}: {cancel_result}")
        
        return {'cancelled': cancelled, 'blocked': blocked}


# ==================== 잔디 알림 ====================

def is_quiet_hours():
    """야간 시간 체크 (한국 시간 23:00 ~ 09:00)"""
    now = get_kst_now()
    hour = now.hour
    return hour >= 23 or hour < 9


def send_jandi_notification(title, body, color="blue"):
    """잔디 웹훅으로 알림 전송"""
    # 야간 모드: 한국 시간 23:00 ~ 09:00에는 알림 전송 안 함
    if is_quiet_hours():
        print(f"[야간 모드] 잔디 알림 스킵 (23:00~09:00): {title}")
        return True  # 성공으로 처리 (에러 아님)
    
    config = load_config()
    if not config:
        return False
    
    # 새 구조 / 구 구조 호환
    webhook_url = get_jandi_webhook(config)
    if not webhook_url:
        print("잔디 웹훅 URL이 설정되지 않음")
        return False
    
    color_map = {
        "green": "#2ECC71",
        "yellow": "#F1C40F",
        "red": "#E74C3C",
        "blue": "#3498DB"
    }
    
    payload = {
        "body": title,
        "connectColor": color_map.get(color, "#3498DB"),
        "connectInfo": [{
            "title": "상세",
            "description": body
        }]
    }
    
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/vnd.tosslab.jandi-v2+json"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status == 200
    except Exception as e:
        print(f"잔디 알림 실패: {e}")
        return False


def _convert_4byte_emoji(text):
    """4바이트 유니코드 이모지를 HTML 엔티티로 변환 (HTML body용)"""
    result = []
    for ch in text:
        if ord(ch) > 0xFFFF:
            result.append(f'&#{ord(ch)};')
        else:
            result.append(ch)
    return ''.join(result)


def _replace_4byte_emoji(text):
    """4바이트 이모지를 BMP 대체 문자로 변환 (이메일 제목/본문용)"""
    replacements = {
        '🏷️': '[가격]', '🏷': '[가격]',
        '🤖': '[AUTO]', '📊': '[통계]', '📈': '[상승]', '📉': '[하락]',
        '🔍': '[검색]', '👉': '>>', '💰': '[원]', '📌': '[!]', '🔔': '[알림]',
        '🎯': '[타겟]', '💡': '[TIP]', '⚡': '[번개]', '🚨': '[경고]', '🛒': '[장바구니]',
        '📦': '[상품]', '🎫': '[쿠폰]', '🧹': '[정리]',
    }
    for emoji, replacement in replacements.items():
        text = text.replace(emoji, replacement)
    # 나머지 4바이트 이모지 제거
    return ''.join(ch for ch in text if ord(ch) <= 0xFFFF)


def send_email_notification(subject, body_text, html_body=None):
    """Apps Script 웹 앱을 통한 이메일 알림 발송"""
    config = load_config()
    if not config:
        return False
    
    webhook_url = config.get('global_settings', {}).get('email_webhook_url', '')
    if not webhook_url:
        print("[EMAIL] email_webhook_url이 설정되지 않음")
        return False
    
    payload = {
        "subject": _replace_4byte_emoji(subject),
        "body": _replace_4byte_emoji(body_text),
    }
    if html_body:
        payload["html_body"] = _convert_4byte_emoji(html_body)
    
    try:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST"
        )
        # Apps Script는 302 리다이렉트를 하므로 직접 처리
        import urllib.request as url_req
        opener = url_req.build_opener(url_req.HTTPRedirectHandler)
        with opener.open(req, timeout=30) as response:
            result = response.read().decode('utf-8')
            print(f"[EMAIL] 발송 성공: {result[:100]}")
            return True
    except Exception as e:
        print(f"[EMAIL] 발송 실패: {e}")
        return False


def build_auto_check_email(groups_result, checked_at):
    """자동 체크 결과를 이메일 HTML로 변환"""
    subject = f"[가격관리] 자동 체크 결과 ({checked_at})"
    
    rows = ""
    for gk, gr in groups_result.items():
        channel = gr.get('channel', 'C')
        name = f"[{channel}] {gr.get('name', gk)}"
        auto = '🤖 ON' if gr.get('auto_mode') else '⏸️ OFF'
        crawl = gr.get('crawl', '-')
        changed = '📊 변동!' if gr.get('price_changed') else '변동 없음'
        
        if gr.get('applied') and not gr.get('partial_fail'):
            applied = f"✅ {gr.get('apply_detail', '성공')}"
            row_color = '#e8f5e9'
        elif gr.get('applied') and gr.get('partial_fail'):
            applied = f"⚠️ {gr.get('apply_detail', '부분 실패')}"
            row_color = '#fff3e0'
        elif gr.get('auto_mode'):
            applied = f"❌ {gr.get('apply_error', '실패')}"
            row_color = '#ffebee'
        else:
            applied = '— (auto OFF)'
            row_color = '#f5f5f5'
        
        rows += f"""<tr style="background:{row_color}">
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{name}</td>
            <td style="padding:8px;border:1px solid #ddd;text-align:center">{auto}</td>
            <td style="padding:8px;border:1px solid #ddd">{crawl}</td>
            <td style="padding:8px;border:1px solid #ddd">{changed}</td>
            <td style="padding:8px;border:1px solid #ddd">{applied}</td>
        </tr>"""
    
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
        <div style="background:#1a1a2e;color:white;padding:20px;border-radius:10px 10px 0 0">
            <h2 style="margin:0">🏷️ 가격관리 자동 체크 결과</h2>
            <p style="margin:5px 0 0;opacity:0.8">{checked_at}</p>
        </div>
        <div style="padding:20px;background:white;border:1px solid #ddd">
            <table style="width:100%;border-collapse:collapse">
                <tr style="background:#1a1a2e;color:white">
                    <th style="padding:10px;text-align:left">그룹</th>
                    <th style="padding:10px;text-align:center">모드</th>
                    <th style="padding:10px">크롤링</th>
                    <th style="padding:10px">가격변동</th>
                    <th style="padding:10px">쿠폰발행</th>
                </tr>
                {rows}
            </table>
        </div>
        <div style="padding:15px;background:#f8f9fa;border-radius:0 0 10px 10px;border:1px solid #ddd;border-top:0;font-size:12px;color:#666">
            <p>이 메일은 가격관리 시스템에서 자동 발송되었습니다.</p>
            <p><a href="https://coupang-price-manager-221865276835.asia-northeast3.run.app">관리 대시보드 바로가기</a></p>
        </div>
    </div>"""
    
    # 텍스트 버전
    text = f"가격관리 자동 체크 결과 ({checked_at})\n\n"
    for gk, gr in groups_result.items():
        channel = gr.get('channel', 'C')
        name = f"[{channel}] {gr.get('name', gk)}"
        text += f"{name}: 크롤링={gr.get('crawl','-')}, 변동={'있음' if gr.get('price_changed') else '없음'}, "
        text += f"쿠폰={'발행' if gr.get('applied') else 'OFF/실패'}\n"
    
    return subject, text, html


# ==================== 경쟁사 크롤링 ====================

def crawl_coupang_price(url):
    """쿠팡 상품 페이지에서 가격 크롤링 (ScraperAPI 경유)
    
    Returns:
        dict: {success: bool, price: int, name: str, error: str}
    """
    config = load_config()
    scraper_api_key = config.get('global_settings', {}).get('scraper_api_key', '') if config else ''
    
    if not scraper_api_key:
        return {"success": False, "error": "ScraperAPI 키가 설정되지 않음. config > global_settings > scraper_api_key"}
    
    try:
        import requests as http_req
        from urllib.parse import urlparse, urlunparse
        
        # URL 정리: query 파라미터 제거 (ScraperAPI가 쿠팡 URL에 params 있으면 실패)
        parsed = urlparse(url)
        clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
        print(f"[CRAWL] URL: {clean_url}")
        
        # ScraperAPI 호출 (1크레딧)
        response = None
        
        try:
            response = http_req.get(
                "https://api.scraperapi.com",
                params={
                    "api_key": scraper_api_key,
                    "url": clean_url,
                    "country_code": "kr"
                },
                timeout=90
            )
        except Exception as req_err:
            print(f"[CRAWL] Request failed: {req_err}")
        
        if not response or response.status_code != 200:
            return {"success": False, "error": f"ScraperAPI 응답: {response.status_code if response else 'no response'}", "url": url}
        
        if len(response.text) < 1000:
            return {"success": False, "error": "페이지 내용이 너무 짧음 (차단 가능성)", "url": url}
        
        # 가격 추출 - final-price-amount 클래스 (쿠팡 2026년 구조)
        price = None
        name = ""
        
        # 방법 1: final-price-amount 클래스 (최우선)
        match = re.search(r'final-price-amount[^>]*>(\d{1,3}(?:,\d{3})+)원', response.text)
        if match:
            price = int(match.group(1).replace(',', ''))
        
        # 방법 2: prod-sale-price (구 구조)
        if not price:
            match = re.search(r'prod-sale-price[^>]*total-price[^>]*>(\d{1,3}(?:,\d{3})+)', response.text)
            if match:
                price = int(match.group(1).replace(',', ''))
        
        # 방법 3: 정규식 가격 패턴 (첫 번째 만원 이상 가격)
        if not price:
            all_prices = re.findall(r'(\d{1,3}(?:,\d{3})+)원', response.text[:50000])
            for p in all_prices:
                val = int(p.replace(',', ''))
                if 5000 < val < 500000:
                    price = val
                    break
        
        # 상품명 추출
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', response.text)
        if title_match:
            name = title_match.group(1).strip()
            # " - 쿠팡!" 제거
            name = re.sub(r'\s*[-|]\s*쿠팡!?\s*$', '', name)
        
        if price:
            return {
                "success": True,
                "price": price,
                "name": name,
                "url": url
            }
        else:
            return {
                "success": False,
                "error": "가격을 찾을 수 없습니다",
                "url": url
            }
    
    except Exception as e:
        return {"success": False, "error": f"크롤링 오류: {str(e)}", "url": url}


def crawl_competitor_prices(product_group):
    """제품 그룹의 모든 경쟁사 가격 크롤링
    
    Args:
        product_group: 제품 그룹 설정 dict
        
    Returns:
        list: 각 경쟁사의 크롤링 결과
    """
    results = []
    competitors = product_group.get('competitors', [])
    
    for competitor in competitors:
        url = competitor.get('url')
        if not url:
            continue
        
        result = crawl_coupang_price(url)
        result['competitor_id'] = competitor.get('id', '')
        result['competitor_name'] = competitor.get('name', '')
        
        if result['success']:
            # 가격 업데이트
            competitor['last_price'] = result['price']
            competitor['last_checked'] = format_kst_datetime()
        
        results.append(result)
        
        # 연속 요청 방지 (1초 대기)
        time_module.sleep(1)
    
    return results


# ==================== 헬퍼 함수 (새 구조 지원) ====================

def get_active_product_group(config):
    """현재 활성 제품 그룹 반환"""
    active_group_key = config.get('active_group', '')
    product_groups = config.get('product_groups', {})
    
    if active_group_key and active_group_key in product_groups:
        group = product_groups[active_group_key]
        group['_key'] = active_group_key
        return group
    
    # 첫 번째 그룹 반환
    if product_groups:
        first_key = list(product_groups.keys())[0]
        group = product_groups[first_key]
        group['_key'] = first_key
        return group
    
    return None


def get_jandi_webhook(config):
    """잔디 웹훅 URL 반환 (새/구 구조 호환)"""
    # 새 구조
    if 'global_settings' in config:
        return config['global_settings'].get('jandi_webhook')
    # 구 구조
    if 'settings' in config:
        return config['settings'].get('jandi_webhook')
    return None


def get_contract_id(config):
    """계약서 ID 반환 (새/구 구조 호환)"""
    # 새 구조
    if 'global_settings' in config:
        return config['global_settings'].get('contract_id')
    # 구 구조
    if 'coupon_settings' in config:
        return config['coupon_settings'].get('contract_id')
    return None


# ==================== 인증 라우트 ====================

LOGIN_PAGE_HTML = '''<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>로그인 - 가격 경쟁 관리</title>
    <script src="https://accounts.google.com/gsi/client" async defer></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-container {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 40px 50px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            text-align: center;
            max-width: 420px;
        }
        .logo { font-size: 48px; margin-bottom: 10px; }
        h1 { 
            color: #1a1a2e; 
            font-size: 24px; 
            margin-bottom: 10px;
        }
        .subtitle {
            color: #666;
            font-size: 14px;
            margin-bottom: 30px;
        }
        .google-btn-container {
            display: flex;
            justify-content: center;
            margin: 30px 0;
        }
        .allowed-emails {
            background: #f8f9fa;
            border-radius: 10px;
            padding: 15px;
            margin-top: 20px;
            text-align: left;
        }
        .allowed-emails h3 {
            color: #333;
            font-size: 12px;
            margin-bottom: 8px;
        }
        .allowed-emails ul {
            list-style: none;
            font-size: 12px;
            color: #666;
        }
        .allowed-emails li {
            padding: 3px 0;
        }
        .error-msg {
            background: #fee;
            color: #c00;
            padding: 10px;
            border-radius: 8px;
            margin-top: 15px;
            font-size: 13px;
            display: none;
        }
        .footer {
            margin-top: 30px;
            font-size: 11px;
            color: #999;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">🛒</div>
        <h1>가격 경쟁 관리</h1>
        <p class="subtitle">BonScience / 테라바이오텍코리아</p>
        
        <div class="google-btn-container">
            <div id="g_id_onload"
                 data-client_id="''' + GOOGLE_CLIENT_ID + '''"
                 data-callback="handleCredentialResponse"
                 data-auto_prompt="false">
            </div>
            <div class="g_id_signin"
                 data-type="standard"
                 data-size="large"
                 data-theme="outline"
                 data-text="sign_in_with"
                 data-shape="rectangular"
                 data-logo_alignment="left">
            </div>
        </div>
        
        <div id="error-msg" class="error-msg"></div>
        
        <div class="allowed-emails">
            <h3>🔐 허용된 계정</h3>
            <ul>
                ''' + ''.join([f'<li>• {email}</li>' for email in ALLOWED_EMAILS]) + '''
            </ul>
        </div>
        
        <div class="footer">
            v26 | © 2026 BonScience
        </div>
    </div>
    
    <script>
        function handleCredentialResponse(response) {
            // Google ID 토큰을 서버로 전송
            fetch('/auth/google/callback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ credential: response.credential })
            })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    window.location.href = '/';
                } else {
                    document.getElementById('error-msg').style.display = 'block';
                    document.getElementById('error-msg').textContent = data.error || '로그인 실패';
                }
            })
            .catch(err => {
                document.getElementById('error-msg').style.display = 'block';
                document.getElementById('error-msg').textContent = '로그인 처리 중 오류가 발생했습니다.';
            });
        }
    </script>
</body>
</html>'''


@app.route('/login')
def login_page():
    """로그인 페이지"""
    if is_authenticated():
        return redirect('/')
    return LOGIN_PAGE_HTML


@app.route('/auth/google/callback', methods=['POST'])
def google_auth_callback():
    """Google OAuth 콜백 - ID 토큰 검증"""
    if not HAS_GOOGLE_AUTH:
        return jsonify({'success': False, 'error': 'Google Auth 라이브러리가 설치되지 않았습니다.'}), 500
    
    data = request.json
    credential = data.get('credential')
    
    if not credential:
        return jsonify({'success': False, 'error': '인증 정보가 없습니다.'}), 400
    
    try:
        # Google ID 토큰 검증
        idinfo = id_token.verify_oauth2_token(
            credential, 
            google_requests.Request(), 
            GOOGLE_CLIENT_ID
        )
        
        email = idinfo.get('email', '').lower()
        name = idinfo.get('name', '')
        
        # 이메일 허용 목록 확인
        if email not in [e.lower() for e in ALLOWED_EMAILS]:
            log_action("AUTH_DENIED", f"접근 거부: {email}")
            return jsonify({
                'success': False, 
                'error': f'접근 권한이 없습니다: {email}'
            }), 403
        
        # 세션에 사용자 정보 저장
        session['authenticated'] = True
        session['email'] = email
        session['name'] = name
        session['login_time'] = get_kst_now().isoformat()
        
        log_action("AUTH_LOGIN", f"로그인 성공: {email} ({name})")
        
        return jsonify({
            'success': True,
            'email': email,
            'name': name
        })
        
    except ValueError as e:
        log_action("AUTH_ERROR", f"토큰 검증 실패: {str(e)}")
        return jsonify({'success': False, 'error': '인증 토큰 검증 실패'}), 401
    except Exception as e:
        log_action("AUTH_ERROR", f"인증 오류: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/auth/logout', methods=['POST'])
def logout():
    """로그아웃"""
    email = session.get('email', 'unknown')
    session.clear()
    log_action("AUTH_LOGOUT", f"로그아웃: {email}")
    return jsonify({'success': True})


@app.route('/auth/status')
def auth_status():
    """인증 상태 확인"""
    if is_authenticated():
        return jsonify({
            'authenticated': True,
            'email': session.get('email'),
            'name': session.get('name'),
            'login_time': session.get('login_time')
        })
    return jsonify({'authenticated': False})


@app.route('/api/allowed-emails', methods=['GET'])
@login_required
def get_allowed_emails():
    """허용된 이메일 목록 조회"""
    return jsonify({
        'emails': ALLOWED_EMAILS,
        'count': len(ALLOWED_EMAILS)
    })


# ==================== 라우트: 정적 파일 ====================

@app.route('/')
@login_required
def index():
    """메인 대시보드"""
    return send_from_directory('.', 'index.html')


@app.route('/<path:filename>')
@login_required
def static_files(filename):
    """정적 파일 서빙"""
    # 로그인 관련 파일은 인증 없이 접근 허용
    if filename in ['favicon.ico']:
        return send_from_directory('.', filename)
    return send_from_directory('.', filename)


# ==================== API 라우트 ====================

@app.route('/api/config', methods=['GET'])
def get_config():
    """설정 조회"""
    config = load_config()
    if not config:
        return jsonify({"error": "Config not found"}), 404
    # 버전 정보 추가
    config['_version'] = APP_VERSION
    config['_build_date'] = BUILD_DATE
    return jsonify(config)


@app.route('/api/version', methods=['GET'])
def get_version():
    """버전 정보 조회"""
    return jsonify({
        "version": APP_VERSION,
        "build_date": BUILD_DATE,
        "gcs_enabled": HAS_GCS,
        "gcs_bucket": GCS_BUCKET_NAME if HAS_GCS else None
    })


@app.route('/api/config/download', methods=['GET'])
def download_config():
    """설정 파일 다운로드 (파일로 저장용)
    
    사용법:
    - 브라우저: /api/config/download 접속 → config.json 다운로드
    - curl: curl -o config.json https://서버URL/api/config/download
    """
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    response = make_response(json.dumps(config, ensure_ascii=False, indent=2))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Content-Disposition'] = 'attachment; filename=config.json'
    return response


@app.route('/api/config/upload', methods=['POST'])
def upload_config():
    """설정 파일 업로드 (전체 덮어쓰기)
    
    ⚠️ 주의: 기존 설정이 완전히 덮어씌워집니다!
    사용 전 /api/config/download로 백업하세요.
    """
    try:
        # JSON 직접 전송 또는 파일 업로드 모두 지원
        if request.is_json:
            new_config = request.json
        elif 'file' in request.files:
            file = request.files['file']
            new_config = json.loads(file.read().decode('utf-8'))
        else:
            return jsonify({"error": "JSON 데이터 또는 파일이 필요합니다"}), 400
        
        # 필수 키 검증
        required_keys = ['api', 'settings', 'products']
        missing = [k for k in required_keys if k not in new_config]
        if missing:
            return jsonify({"error": f"필수 키 누락: {missing}"}), 400
        
        # 기존 config 백업 (메모리에)
        old_config = load_config()
        
        # 새 config 저장
        save_config(new_config)
        log_action("CONFIG_UPLOAD", "설정 파일 전체 업로드", {
            "old_settings": old_config.get('settings') if old_config else None,
            "new_settings": new_config.get('settings')
        })
        
        return jsonify({
            "success": True,
            "message": "설정 파일이 업로드되었습니다",
            "uploaded_at": format_kst_datetime()
        })
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON 파싱 실패: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": f"업로드 실패: {str(e)}"}), 500


@app.route('/api/config/sync-check', methods=['GET'])
def config_sync_check():
    """설정 파일 동기화 체크용 해시값 반환
    
    배포 전 로컬 config와 서버 config가 동일한지 확인할 때 사용
    """
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    # 설정 내용의 해시값 계산
    config_str = json.dumps(config, sort_keys=True, ensure_ascii=False)
    config_hash = hashlib.md5(config_str.encode()).hexdigest()
    
    return jsonify({
        "hash": config_hash,
        "settings_summary": {
            "price_gap": config['settings'].get('price_gap'),
            "price_direction": config['settings'].get('price_direction'),
            "discount_hours": config['settings'].get('discount_hours'),
            "auto_mode": config['settings'].get('auto_mode'),
            "contract_id": config.get('coupon_settings', {}).get('contract_id')
        },
        "last_check": format_kst_datetime()
    })


@app.route('/api/config', methods=['POST'])
def update_config():
    """설정 업데이트 (새 구조 + 구 구조 호환)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    
    # 새 구조: global_settings 업데이트
    if 'global_settings' in data:
        if 'global_settings' not in config:
            config['global_settings'] = {}
        config['global_settings'].update(data['global_settings'])
    
    # 새 구조: product_groups 업데이트
    if 'product_groups' in data:
        if 'product_groups' not in config:
            config['product_groups'] = {}
        for key, value in data['product_groups'].items():
            if key in config['product_groups']:
                config['product_groups'][key].update(value)
            else:
                config['product_groups'][key] = value
    
    # 새 구조: active_group 업데이트
    if 'active_group' in data:
        config['active_group'] = data['active_group']
    
    # 구 구조 호환: settings 업데이트
    if 'settings' in data:
        if 'settings' not in config:
            config['settings'] = {}
        config['settings'].update(data['settings'])
    
    # 구 구조 호환: products 업데이트
    if 'products' in data:
        if 'products' not in config:
            config['products'] = {}
        for key, value in data['products'].items():
            if key in config['products']:
                config['products'][key].update(value)
    
    save_config(config)
    log_action("CONFIG_UPDATE", "설정 변경됨", data)
    return jsonify({"success": True})


# ==================== 제품 그룹 관리 API ====================

@app.route('/api/product-groups', methods=['GET'])
def get_product_groups():
    """모든 제품 그룹 목록 조회"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    active = config.get('active_group', '')
    
    return jsonify({
        "groups": groups,
        "active_group": active
    })


@app.route('/api/product-groups/<group_key>', methods=['GET'])
def get_product_group(group_key):
    """특정 제품 그룹 조회"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    return jsonify({
        "key": group_key,
        "group": groups[group_key]
    })


@app.route('/api/product-groups', methods=['POST'])
def create_product_group():
    """새 제품 그룹 생성"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    group_key = data.get('key')
    group_data = data.get('group')
    
    if not group_key or not group_data:
        return jsonify({"error": "key와 group 데이터가 필요합니다"}), 400
    
    # 키 중복 체크
    if 'product_groups' not in config:
        config['product_groups'] = {}
    
    if group_key in config['product_groups']:
        return jsonify({"error": f"이미 존재하는 제품 그룹입니다: {group_key}"}), 400
    
    # 기본값 설정
    default_group = {
        "name": group_data.get('name', '새 제품'),
        "enabled": True,
        "auto_mode": False,
        "coupon_name": group_data.get('coupon_name', f"본사이언스 {group_data.get('name', '새 제품')} 할인쿠폰"),
        "price_gap": 1000,
        "price_direction": "lower",
        "discount_hours": 4,
        "check_interval_minutes": 240,
        "pack3_extra_discount": 3,
        "pack6_extra_discount": 7,
        "products": group_data.get('products', {
            "1bottle": {"name": "1개입", "vendor_item_id": 0, "original_price": 0, "current_price": 0, "min_price": 0, "max_price": 0},
            "3bottle": {"name": "3개입", "vendor_item_id": 0, "original_price": 0, "current_price": 0, "min_price": 0, "max_price": 0},
            "6bottle": {"name": "6개입", "vendor_item_id": 0, "original_price": 0, "current_price": 0, "min_price": 0, "max_price": 0}
        }),
        "competitors": group_data.get('competitors', [])
    }
    
    # 전달된 데이터로 덮어쓰기
    default_group.update(group_data)
    
    config['product_groups'][group_key] = default_group
    save_config(config)
    
    log_action("GROUP_CREATE", f"제품 그룹 생성: {group_key}", default_group)
    
    return jsonify({
        "success": True,
        "key": group_key,
        "group": default_group
    })


@app.route('/api/product-groups/<group_key>', methods=['PUT'])
def update_product_group(group_key):
    """제품 그룹 수정"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    data = request.json
    
    # Deep merge: products 안의 개별 필드만 업데이트 (기존 vendor_item_id 등 보존)
    existing = groups[group_key]
    for key, value in data.items():
        if key == 'products' and isinstance(value, dict):
            if 'products' not in existing:
                existing['products'] = {}
            for pk, pv in value.items():
                if isinstance(pv, dict) and pk in existing['products']:
                    existing['products'][pk].update(pv)
                else:
                    existing['products'][pk] = pv
        elif key == 'competitors' and isinstance(value, list):
            # competitors는 리스트이므로 그대로 교체
            existing[key] = value
        else:
            existing[key] = value
    
    save_config(config)
    
    log_action("GROUP_UPDATE", f"제품 그룹 수정: {group_key}", data)
    
    return jsonify({
        "success": True,
        "key": group_key,
        "group": groups[group_key]
    })


@app.route('/api/product-groups/<group_key>', methods=['DELETE'])
def delete_product_group(group_key):
    """제품 그룹 삭제"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    deleted = groups.pop(group_key)
    
    # 활성 그룹이 삭제되면 다른 그룹으로 변경
    if config.get('active_group') == group_key:
        if groups:
            config['active_group'] = list(groups.keys())[0]
        else:
            config['active_group'] = ''
    
    save_config(config)
    
    log_action("GROUP_DELETE", f"제품 그룹 삭제: {group_key}", deleted)
    
    return jsonify({
        "success": True,
        "deleted_key": group_key,
        "active_group": config.get('active_group', '')
    })


@app.route('/api/product-groups/<group_key>/activate', methods=['POST'])
def activate_product_group(group_key):
    """제품 그룹 활성화 (현재 작업 대상으로 설정)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    config['active_group'] = group_key
    save_config(config)
    
    log_action("GROUP_ACTIVATE", f"활성 그룹 변경: {group_key}")
    
    return jsonify({
        "success": True,
        "active_group": group_key
    })


# ==================== 경쟁사 관리 API ====================

@app.route('/api/product-groups/<group_key>/competitors', methods=['GET'])
def get_competitors(group_key):
    """제품 그룹의 경쟁사 목록 조회"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    competitors = groups[group_key].get('competitors', [])
    return jsonify({"competitors": competitors})


@app.route('/api/product-groups/<group_key>/competitors', methods=['POST'])
def add_competitor(group_key):
    """경쟁사 추가 (최대 3개)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    data = request.json
    url = data.get('url')
    name = data.get('name', '')
    
    if not url:
        return jsonify({"error": "URL이 필요합니다"}), 400
    
    competitors = groups[group_key].get('competitors', [])
    
    if len(competitors) >= 3:
        return jsonify({"error": "경쟁사는 최대 3개까지만 추가할 수 있습니다"}), 400
    
    # URL에서 vendorItemId 추출 시도
    vendor_item_id = ""
    vid_match = re.search(r'vendorItemId=(\d+)', url)
    if vid_match:
        vendor_item_id = vid_match.group(1)
    
    # 고유 ID 생성
    comp_id = f"comp_{len(competitors) + 1}_{int(time_module.time())}"
    
    new_competitor = {
        "id": comp_id,
        "name": name,
        "url": url,
        "vendor_item_id": vendor_item_id,
        "last_price": 0,
        "last_checked": ""
    }
    
    competitors.append(new_competitor)
    groups[group_key]['competitors'] = competitors
    save_config(config)
    
    log_action("COMPETITOR_ADD", f"경쟁사 추가: {name} ({group_key})")
    
    return jsonify({
        "success": True,
        "competitor": new_competitor
    })


@app.route('/api/product-groups/<group_key>/competitors/<comp_id>', methods=['DELETE'])
def delete_competitor(group_key, comp_id):
    """경쟁사 삭제"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    competitors = groups[group_key].get('competitors', [])
    new_competitors = [c for c in competitors if c.get('id') != comp_id]
    
    if len(new_competitors) == len(competitors):
        return jsonify({"error": f"경쟁사를 찾을 수 없습니다: {comp_id}"}), 404
    
    groups[group_key]['competitors'] = new_competitors
    save_config(config)
    
    log_action("COMPETITOR_DELETE", f"경쟁사 삭제: {comp_id} ({group_key})")
    
    return jsonify({"success": True})


@app.route('/api/product-groups/<group_key>/competitors/bulk', methods=['POST'])
def save_competitors_bulk(group_key):
    """경쟁사 일괄 저장 (전체 교체)"""
    config = load_config()
    if not config:
        return jsonify({"error": "Config not found"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"Group not found: {group_key}"}), 404
    
    data = request.get_json() or {}
    competitors_data = data.get('competitors', [])
    
    print(f"[BULK SAVE] group={group_key}, input={competitors_data}")
    
    # 새 경쟁사 목록 생성
    new_competitors = []
    for i, comp in enumerate(competitors_data[:3]):  # 최대 3개
        url = comp.get('url', '').strip()
        price = comp.get('last_price')
        name = comp.get('name', '').strip()
        
        # URL이나 가격 중 하나라도 있으면 저장
        if not url and not price:
            continue
            
        comp_id = f"comp_{i + 1}_{int(time_module.time())}"
        new_comp = {
            "id": comp_id,
            "order": comp.get('order', i + 1),
            "name": name or f"Competitor {i + 1}",
            "url": url,
            "last_price": price,
            "last_checked": format_kst_datetime() if price else ""
        }
        new_competitors.append(new_comp)
    
    # 순서대로 정렬
    new_competitors.sort(key=lambda x: x.get('order', 0))
    
    groups[group_key]['competitors'] = new_competitors
    save_config(config)
    
    print(f"[BULK SAVE] saved={new_competitors}")
    log_action("COMPETITOR_BULK_SAVE", f"Competitors saved: {len(new_competitors)} ({group_key})")
    
    return jsonify({
        "success": True,
        "count": len(new_competitors),
        "competitors": new_competitors
    })


@app.route('/api/product-groups/<group_key>/crawl-competitors', methods=['POST'])
def crawl_competitors(group_key):
    """경쟁사 가격 크롤링"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    group = groups[group_key]
    
    # 크롤링 전 기존 가격 저장
    old_prices = {}
    for comp in group.get('competitors', []):
        old_prices[comp.get('id', '')] = comp.get('last_price', 0)
    
    results = crawl_competitor_prices(group)
    
    # config에 업데이트된 가격 저장
    save_config(config)
    
    success_count = sum(1 for r in results if r['success'])
    
    # 가격 변동 감지
    price_changes = []
    for r in results:
        if r['success']:
            comp_id = r.get('competitor_id', '')
            old_price = old_prices.get(comp_id, 0)
            new_price = r.get('price', 0)
            if old_price > 0 and new_price != old_price:
                change_pct = round((new_price - old_price) / old_price * 100, 1)
                price_changes.append({
                    'name': r.get('competitor_name', ''),
                    'old': old_price,
                    'new': new_price,
                    'change': change_pct
                })
    
    # 가격 변동 시 잔디 알림
    if price_changes:
        group_name = group.get('name', group_key)
        body = "\n".join([
            f"{'📈' if c['change'] > 0 else '📉'} {c['name']}: {c['old']:,}원 → {c['new']:,}원 ({c['change']:+.1f}%)"
            for c in price_changes
        ])
        
        if group.get('auto_mode'):
            body += "\n\n🤖 auto_mode ON → 자동 가격 재조정 진행합니다."
        
        send_jandi_notification(
            f"🔍 [{group_name}] 경쟁사 가격 변동 감지!",
            body,
            "yellow"
        )
    
    log_action("CRAWL", f"경쟁사 크롤링: {success_count}/{len(results)}개 성공 ({group_key})" + 
               (f", 가격 변동: {len(price_changes)}건" if price_changes else ""))
    
    return jsonify({
        "success": True,
        "results": results,
        "price_changes": price_changes,
        "crawled_at": format_kst_datetime()
    })


@app.route('/api/product-groups/<group_key>/competitors/<comp_id>/price', methods=['POST'])
def update_competitor_price(group_key, comp_id):
    """경쟁사 가격 수동 입력"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    data = request.json
    price = data.get('price')
    
    if not price:
        return jsonify({"error": "price가 필요합니다"}), 400
    
    competitors = groups[group_key].get('competitors', [])
    updated = False
    
    for comp in competitors:
        if comp.get('id') == comp_id:
            comp['last_price'] = int(price)
            comp['last_checked'] = format_kst_datetime()
            updated = True
            break
    
    if not updated:
        return jsonify({"error": f"경쟁사를 찾을 수 없습니다: {comp_id}"}), 404
    
    save_config(config)
    
    log_action("COMPETITOR_PRICE", f"경쟁사 가격 수동 입력: {price}원 ({comp_id})")
    
    return jsonify({
        "success": True,
        "price": int(price),
        "updated_at": format_kst_datetime()
    })


@app.route('/api/product-groups/<group_key>/sync-prices', methods=['POST'])
def sync_group_prices(group_key):
    """제품 그룹의 쿠팡 가격 동기화"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    group = groups[group_key]
    products = group.get('products', {})
    api = CoupangAPI(config)
    results = []
    
    for product_key, product in products.items():
        vendor_item_id = product.get('vendor_item_id')
        if not vendor_item_id:
            continue
        
        price_result = api.get_inventory(vendor_item_id)
        
        if price_result.get('success'):
            data = price_result.get('data', {})
            
            original_price = None
            sale_price = None
            
            if isinstance(data, dict):
                inner_data = data.get('data', data)
                if isinstance(inner_data, dict):
                    sale_price = inner_data.get('salePrice')
            
            # salePrice = 쿠팡에 설정된 실제 판매가 (쿠폰 적용 전 가격)
            if sale_price:
                products[product_key]['original_price'] = int(sale_price)
                products[product_key]['current_price'] = int(sale_price)
            
            results.append({
                "product": product.get('name', product_key),
                "vendor_item_id": vendor_item_id,
                "original_price": sale_price,
                "current_price": sale_price,
                "success": True
            })
        else:
            results.append({
                "product": product.get('name', product_key),
                "vendor_item_id": vendor_item_id,
                "error": price_result.get('error'),
                "success": False
            })
    
    save_config(config)
    log_action("SYNC_PRICES", f"가격 동기화: {group_key}", results)
    
    return jsonify({
        "success": True,
        "results": results,
        "synced_at": format_kst_datetime()
    })


# ==================== 쿠폰 정리 ====================

def cleanup_group_coupons(api, group_name, coupon_name, fixed_keywords=None):
    """해당 그룹의 기존 쿠폰을 전부 파기 (고정 쿠폰 + 보호 쿠폰 제외)
    
    쿠폰 이름에 coupon_name이 포함된 것만 찾아서 파기.
    고정 쿠폰(여러 상품 범용)과 보호 대상(증량판 등)은 파기하지 않음.
    """
    if fixed_keywords is None:
        fixed_keywords = ['2천원', '3천원', '5천원', '1만원']
    
    # 보호 키워드 - 이것이 포함되면 절대 파기하지 않음
    protected_keywords = ['증량판', '증량']
    
    cancelled = []
    blocked = []
    
    coupons_result = api.get_coupons("APPLIED")
    if not coupons_result.get('success'):
        return {'cancelled': cancelled, 'blocked': blocked}
    
    coupon_data = coupons_result.get('data', {})
    if isinstance(coupon_data, dict):
        inner = coupon_data.get('data', {})
        if isinstance(inner, dict):
            coupon_list = inner.get('content', [])
        else:
            coupon_list = []
    else:
        coupon_list = []
    
    for coupon in coupon_list:
        coupon_id = coupon.get('couponId')
        promo_name = coupon.get('promotionName', '')
        status = coupon.get('status', '')
        
        if not coupon_id or status in ['EXPIRED', 'CANCELLED']:
            continue
        
        # 보호 대상 쿠폰은 절대 건드리지 않음 (증량판 등)
        if any(pk in promo_name for pk in protected_keywords):
            print(f"[CLEANUP] PROTECTED (증량판 등): {promo_name} (ID: {coupon_id})")
            continue
        
        # 이 그룹의 쿠폰인지 확인: coupon_name 전체가 promo_name에 포함되어야 매칭
        is_group_coupon = False
        if coupon_name and coupon_name in promo_name:
            is_group_coupon = True
        
        if not is_group_coupon:
            continue
        
        # 고정 쿠폰 판별: 고정 키워드만 있고 coupon_name이 없으면 → 고정 (파기 불가)
        has_fixed_kw = any(fk in promo_name for fk in fixed_keywords)
        if has_fixed_kw:
            print(f"[CLEANUP] BLOCKED (fixed): {promo_name} (ID: {coupon_id})")
            blocked.append({'coupon_id': coupon_id, 'name': promo_name})
            continue
        
        # 파기
        print(f"[CLEANUP] Cancelling: {promo_name} (ID: {coupon_id})")
        cancel_result = api.cancel_coupon(coupon_id)
        if cancel_result.get('success'):
            cancelled.append({'coupon_id': coupon_id, 'name': promo_name})
        else:
            print(f"[CLEANUP] Failed to cancel {coupon_id}: {cancel_result}")
    
    return {'cancelled': cancelled, 'blocked': blocked}


@app.route('/api/product-groups/<group_key>/cleanup-coupons', methods=['POST'])
def cleanup_coupons_api(group_key):
    """특정 그룹의 기존 쿠폰 전부 정리 (수동 호출용)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    group = groups[group_key]
    group_name = group.get('name', '')
    coupon_name = group.get('coupon_name', f"{group_name} 할인쿠폰")
    fixed_keywords = ['2천원', '3천원', '5천원', '1만원']
    
    api = CoupangAPI(config)
    result = cleanup_group_coupons(api, group_name, coupon_name, fixed_keywords)
    
    cancelled = result.get('cancelled', [])
    blocked = result.get('blocked', [])
    
    log_action("CLEANUP_COUPONS", f"쿠폰 정리: {group_key} ({len(cancelled)}개 파기, {len(blocked)}개 차단)", result)
    
    if cancelled:
        body = "\n".join([f"🗑️ {c['name']} (ID: {c['coupon_id']})" for c in cancelled])
        if blocked:
            body += f"\n\n🔒 파기 불가 (고정 쿠폰): {len(blocked)}개"
        send_jandi_notification(f"🧹 {group_name} 쿠폰 정리 완료", body, "blue")
    
    return jsonify({
        "success": True,
        "cancelled": cancelled,
        "blocked": blocked,
        "cancelled_count": len(cancelled),
        "blocked_count": len(blocked)
    })


@app.route('/api/product-groups/<group_key>/apply-prices', methods=['POST'])
def apply_group_prices(group_key):
    """제품 그룹의 1+3+6병 전체 쿠폰 발행 (기존 쿠폰 파기 포함)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    group = groups[group_key]
    products = group.get('products', {})
    competitors = group.get('competitors', [])
    group_name = group.get('name', '')
    
    # 경쟁사 가격 확인
    competitor_price = competitors[0].get('last_price', 0) if competitors else 0
    if not competitor_price:
        return jsonify({"error": "경쟁사 가격이 없습니다. 먼저 경쟁사 가격을 입력하세요."}), 400
    
    price_gap = group.get('price_gap', 1000)
    price_direction = group.get('price_direction', 'lower')
    discount_hours = group.get('discount_hours', 4)
    contract_id = config.get('global_settings', {}).get('contract_id') or config.get('coupon_settings', {}).get('contract_id')
    coupon_name = group.get('coupon_name', f"{group_name} 할인쿠폰")
    
    if not contract_id:
        return jsonify({"error": "계약서 ID가 설정되지 않았습니다"}), 400
    
    api = CoupangAPI(config)
    
    # ==================== 고정 쿠폰 키워드 (파기하면 안 되는 것들) ====================
    fixed_coupon_keywords = ['2천원', '3천원', '5천원', '1만원']
    
    # ==================== 1단계: 해당 그룹의 기존 쿠폰 전부 파기 ====================
    cancelled_coupons = []
    blocked_coupons = []
    
    print(f"[APPLY] Step 1: Cleanup existing coupons for group '{group_name}'")
    cleanup = cleanup_group_coupons(api, group_name, coupon_name, fixed_coupon_keywords)
    cancelled_coupons = cleanup.get('cancelled', [])
    blocked_coupons = cleanup.get('blocked', [])
    
    if cancelled_coupons:
        print(f"[APPLY] Cleaned up {len(cancelled_coupons)} old coupons")
        time_module.sleep(2)  # 파기 처리 대기
    
    if blocked_coupons:
        print(f"[APPLY] {len(blocked_coupons)} coupons blocked by fixed coupons")
    
    # ==================== 2단계: 새 쿠폰 발행 ====================
    results = []
    
    # 1bottle, 3bottle, 6bottle
    product_configs = [
        ('1bottle', 1, 0),
        ('3bottle', 3, group.get('pack3_extra_discount', 0)),
        ('6bottle', 6, group.get('pack6_extra_discount', 0))
    ]
    
    for product_key, multiplier, extra_discount in product_configs:
        product = products.get(product_key)
        if not product or not product.get('vendor_item_id'):
            continue
        
        vendor_item_id = product['vendor_item_id']
        config_original_price = product.get('original_price', 0)
        min_price = product.get('min_price', 0)
        max_price = product.get('max_price', 999999)
        
        # ★★★ 핵심: 쿠팡 WING API로 실제 판매가 조회 ★★★
        inventory = api.get_inventory(vendor_item_id)
        actual_sale_price = 0
        
        if inventory.get('success'):
            inv_data = inventory.get('data', {})
            if isinstance(inv_data, dict):
                actual_sale_price = inv_data.get('salePrice', 0)
                if not actual_sale_price:
                    # 다른 구조 대응
                    items = inv_data.get('data', [])
                    if isinstance(items, list) and items:
                        actual_sale_price = items[0].get('salePrice', 0)
                    elif isinstance(items, dict):
                        actual_sale_price = items.get('salePrice', 0)
        
        if not actual_sale_price:
            # API 조회 실패 시 config 값 사용 (경고 포함)
            actual_sale_price = config_original_price
            print(f"[APPLY] WARNING: Could not get actual price for {product_key}, using config: {config_original_price}")
        
        print(f"[APPLY] {product_key}: config_original={config_original_price}, actual_sale={actual_sale_price}")
        
        if not actual_sale_price:
            results.append({
                "product": product.get('name', product_key),
                "success": False,
                "error": "가격 정보를 가져올 수 없음"
            })
            continue
        
        # 목표 가격 계산
        if price_direction == 'lower':
            base_price = (competitor_price - price_gap) * multiplier
        else:
            base_price = (competitor_price + price_gap) * multiplier
        
        if extra_discount > 0:
            target_price = round(base_price * (1 - extra_discount / 100) / 10) * 10
        else:
            target_price = round(base_price / 10) * 10
        
        # 안전장치 적용
        target_price = max(min_price, min(max_price, target_price))
        
        # ★★★ 할인금액 = 실제 쿠팡 판매가 - 목표가격 (정가 아닌 판매가 기준!) ★★★
        discount_amount = actual_sale_price - target_price
        
        if discount_amount <= 0:
            # 이미 목표가보다 싸면 쿠폰 불필요
            results.append({
                "product": product.get('name', product_key),
                "success": False,
                "error": f"쿠폰 불필요 (현재 판매가 {actual_sale_price:,}원 ≤ 목표가 {target_price:,}원)"
            })
            continue
        
        # 안전 검증: 할인 후 가격이 min_price 밑으로 내려가면 차단
        final_price = actual_sale_price - discount_amount
        if final_price < min_price:
            results.append({
                "product": product.get('name', product_key),
                "success": False,
                "error": f"최저가 위반! (판매가 {actual_sale_price:,} - 할인 {discount_amount:,} = {final_price:,} < 최저가 {min_price:,})"
            })
            continue
        
        # 쿠폰 발행
        print(f"[COUPON] Creating: {product_key}, discount={discount_amount}, vendor_item_id={vendor_item_id}")
        
        try:
            coupon_result = api.create_instant_coupon(
                vendor_item_ids=[vendor_item_id],
                discount_amount=discount_amount,
                contract_id=contract_id,
                hours=discount_hours,
                title=f"{coupon_name} {multiplier}병 {discount_amount:,}원"
            )
            
            print(f"[COUPON] Result for {product_key}: {coupon_result}")
            
            if coupon_result.get('success'):
                coupon_id = coupon_result.get('coupon_id')
                items_added = coupon_result.get('items_added', False)
                expected_final = actual_sale_price - discount_amount
                
                products[product_key]['current_price'] = target_price
                results.append({
                    "product": product.get('name', product_key),
                    "vendor_item_id": vendor_item_id,
                    "actual_sale_price": actual_sale_price,
                    "discount_amount": discount_amount,
                    "target_price": target_price,
                    "expected_final_price": expected_final,
                    "coupon_id": coupon_id,
                    "items_added": items_added,
                    "success": items_added,  # 상품 연결 실패하면 실패 처리!
                    "error": "" if items_added else f"쿠폰 생성됨(ID:{coupon_id}) but 상품 연결 실패 - 쿠팡에서 할인 미적용"
                })
            else:
                error_msg = coupon_result.get('error', 'Unknown error')
                error_detail = coupon_result.get('error_detail', '')
                results.append({
                    "product": product.get('name', product_key),
                    "success": False,
                    "error": f"{error_msg} {error_detail}".strip()
                })
        except Exception as e:
            print(f"[COUPON] Exception for {product_key}: {str(e)}")
            results.append({
                "product": product.get('name', product_key),
                "success": False,
                "error": f"Exception: {str(e)}"
            })
    
    save_config(config)
    
    # 상품이 하나도 처리되지 않은 경우 → 명확한 실패
    if not results:
        error_msg = f"처리된 상품이 없습니다. vendor_item_id 설정을 확인하세요."
        log_action("APPLY_PRICES", f"가격 적용 실패: {group_key} - {error_msg}")
        send_jandi_notification(
            f"🚨 {group_name} 쿠폰 발행 실패",
            f"처리된 상품 0개 — vendor_item_id가 설정되지 않았거나 상품 데이터가 없습니다.\n\n관리자 확인 필요",
            "red"
        )
        return jsonify({
            "success": False,
            "error": error_msg,
            "results": [],
            "cancelled_coupons": cancelled_coupons,
            "blocked_coupons": blocked_coupons,
            "applied_at": format_kst_datetime()
        })
    
    # 결과 요약
    success_count = sum(1 for r in results if r.get('success'))
    fail_count = sum(1 for r in results if not r.get('success'))
    blocked_count = sum(1 for r in results if r.get('blocked_by'))
    
    # 실제 성공 여부 판단: 1개 이상 성공해야 true
    overall_success = success_count > 0
    
    log_action("APPLY_PRICES", f"가격 적용: {group_key} ({success_count}/{len(results)}개 성공, {fail_count}개 실패, {blocked_count}개 차단)", results)
    
    # 잔디 알림
    if success_count > 0:
        body = "\n".join([
            f"✅ {r['product']}: 판매가 {r.get('actual_sale_price', 0):,} → 할인 {r.get('discount_amount', 0):,} → 최종 {r.get('expected_final_price', 0):,}원"
            for r in results if r.get('success')
        ])
        if blocked_count > 0:
            body += f"\n\n🔒 고정 쿠폰 차단: {blocked_count}개 (수동 해제 필요)"
        if cancelled_coupons:
            body += f"\n🗑️ 기존 쿠폰 {len(cancelled_coupons)}개 파기됨"
        send_jandi_notification(
            f"🎫 {group_name} 쿠폰 발행",
            body,
            "green" if success_count == len(results) else "yellow"
        )
    
    # 실패가 있으면 잔디 경고 알림
    if fail_count > 0:
        fail_body = "\n".join([
            f"❌ {r['product']}: {r.get('error', '알 수 없는 오류')}"
            for r in results if not r.get('success')
        ])
        send_jandi_notification(
            f"⚠️ {group_name} 쿠폰 발행 실패 ({fail_count}건)",
            fail_body,
            "red"
        )
    
    return jsonify({
        "success": overall_success,
        "results": results,
        "cancelled_coupons": cancelled_coupons,
        "blocked_coupons": blocked_coupons,
        "applied_at": format_kst_datetime()
    })


@app.route('/api/debug/apply-prices/<group_key>', methods=['POST'])
def debug_apply_prices(group_key):
    """디버그용 - 가격 적용 단계별 테스트"""
    import traceback
    steps = []
    
    try:
        # Step 1: Config load
        config = load_config()
        steps.append({"step": 1, "name": "load_config", "ok": config is not None})
        
        if not config:
            return jsonify({"steps": steps, "error": "Config not found"}), 404
        
        # Step 2: Group check
        groups = config.get('product_groups', {})
        group = groups.get(group_key)
        steps.append({"step": 2, "name": "get_group", "ok": group is not None, "group_name": group.get('name') if group else None})
        
        if not group:
            return jsonify({"steps": steps, "error": f"Group not found: {group_key}"}), 404
        
        # Step 3: Competitors
        competitors = group.get('competitors', [])
        competitor_price = competitors[0].get('last_price', 0) if competitors else 0
        steps.append({"step": 3, "name": "competitors", "count": len(competitors), "price": competitor_price})
        
        # Step 4: Contract ID
        contract_id = config.get('global_settings', {}).get('contract_id')
        steps.append({"step": 4, "name": "contract_id", "ok": bool(contract_id), "value": contract_id})
        
        # Step 5: CoupangAPI init
        api = CoupangAPI(config)
        steps.append({"step": 5, "name": "CoupangAPI", "ok": True})
        
        # Step 6: Get coupons
        coupons_result = api.get_coupons("APPLIED")
        steps.append({"step": 6, "name": "get_coupons", "ok": coupons_result.get('success', False)})
        
        # Step 7: Products
        products = group.get('products', {})
        product_info = {}
        for pk, pv in products.items():
            product_info[pk] = {
                "vendor_item_id": pv.get('vendor_item_id'),
                "original_price": pv.get('original_price')
            }
        steps.append({"step": 7, "name": "products", "data": product_info})
        
        return jsonify({"success": True, "steps": steps})
        
    except Exception as e:
        steps.append({"step": "ERROR", "exception": str(e), "traceback": traceback.format_exc()})
        return jsonify({"success": False, "steps": steps, "error": str(e)}), 500


@app.route('/api/debug/test-add-items/<coupon_id>/<vendor_item_id>', methods=['POST'])
def debug_add_items(coupon_id, vendor_item_id):
    """디버그용 - 특정 쿠폰에 상품 추가 테스트"""
    import traceback
    try:
        config = load_config()
        api = CoupangAPI(config)
        
        print(f"[DEBUG] Testing add_coupon_items: coupon={coupon_id}, item={vendor_item_id}")
        result = api.add_coupon_items(int(coupon_id), [int(vendor_item_id)])
        print(f"[DEBUG] Result: {result}")
        
        return jsonify({
            "success": True,
            "coupon_id": coupon_id,
            "vendor_item_id": vendor_item_id,
            "result": result
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/api/debug/check-request/<request_id>', methods=['GET'])
def debug_check_request(request_id):
    """디버그용 - 요청상태 확인 API"""
    try:
        config = load_config()
        api = CoupangAPI(config)
        
        result = api.get_coupon_request_status(request_id)
        return jsonify({
            "success": True,
            "request_id": request_id,
            "result": result
        })
    except Exception as e:
        import traceback
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/api/product-groups/<group_key>/update-original-prices', methods=['POST'])
def update_original_prices(group_key):
    """그룹 상품의 쿠팡 정가(취소선 가격) 변경"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"그룹 없음: {group_key}"}), 404
    
    data = request.json or {}
    # {"1bottle": 90000, "3bottle": 270000, "6bottle": 540000}
    prices = data.get('prices', {})
    
    if not prices:
        return jsonify({"error": "prices를 지정해주세요. 예: {\"1bottle\": 90000}"}), 400
    
    api = CoupangAPI(config)
    group = groups[group_key]
    products = group.get('products', {})
    results = []
    
    for product_key, new_price in prices.items():
        product = products.get(product_key)
        if not product:
            results.append({"product": product_key, "success": False, "error": "상품 없음"})
            continue
        
        vid = product.get('vendor_item_id')
        if not vid:
            results.append({"product": product_key, "success": False, "error": "vendor_item_id 없음"})
            continue
        
        old_price = product.get('original_price', 0)
        result = api.update_original_price(vid, int(new_price))
        
        if result.get('success'):
            product['original_price'] = int(new_price)
            results.append({
                "product": product.get('name', product_key),
                "vendor_item_id": vid,
                "old_original_price": old_price,
                "new_original_price": int(new_price),
                "success": True
            })
        else:
            results.append({
                "product": product.get('name', product_key),
                "success": False,
                "error": result.get('error', ''),
                "body": result.get('body', '')
            })
    
    save_config(config)
    log_action("UPDATE_ORIGINAL_PRICE", f"정가 변경: {group_key}", results)
    
    return jsonify({"success": True, "results": results})


@app.route('/api/test', methods=['GET'])
def test_connection():
    """API 연결 테스트 - 상세 결과 반환"""
    config = load_config()
    if not config:
        return jsonify({"success": False, "error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    
    # 새 구조 (product_groups) 우선
    groups = config.get('product_groups', {})
    active_key = config.get('active_group', '')
    
    vendor_item_id = None
    product_name = None
    
    if groups and active_key and active_key in groups:
        products = groups[active_key].get('products', {})
        if '1bottle' in products:
            vendor_item_id = products['1bottle'].get('vendor_item_id')
            product_name = f"{groups[active_key].get('name', '')} 1개입"
    
    # 기존 구조 폴백
    if not vendor_item_id:
        old_products = config.get('products', {})
        for key, prod in old_products.items():
            if prod.get('vendor_item_id'):
                vendor_item_id = prod['vendor_item_id']
                product_name = prod.get('name', key)
                break
    
    if not vendor_item_id:
        return jsonify({"success": False, "error": "테스트할 상품이 없습니다"}), 404
    
    # 쿠팡 API 테스트
    result = api.get_inventory(vendor_item_id)
    
    if result.get('success'):
        data = result.get('data', {})
        inner = data.get('data', data) if isinstance(data, dict) else {}
        
        return jsonify({
            "success": True,
            "message": "쿠팡 Wing API 연결 성공!",
            "vendor_id": config.get('api', {}).get('vendor_id') or config.get('vendor_id'),
            "test_product": product_name,
            "vendor_item_id": vendor_item_id,
            "api_response": {
                "original_price": inner.get('originalPrice'),
                "sale_price": inner.get('salePrice'),
                "quantity": inner.get('quantity'),
                "status": inner.get('statusName') or inner.get('status')
            }
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get('error', 'API 호출 실패'),
            "details": result
        })


@app.route('/api/debug/raw-inventory/<int:vendor_item_id>', methods=['GET'])
def debug_raw_inventory(vendor_item_id):
    """디버그: 쿠팡 WING API 원본 응답 확인 (인증 불필요)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    api = CoupangAPI(config)
    result = api.get_inventory(vendor_item_id)
    return jsonify({"vendor_item_id": vendor_item_id, "raw_response": result})


@app.route('/api/test-jandi', methods=['POST'])
def test_jandi():
    """잔디 알림 테스트"""
    success = send_jandi_notification(
        "🔔 [테스트 알림]",
        f"가격 경쟁 관리 시스템 알림 테스트\n\n⏰ {format_kst_datetime()}",
        "blue"
    )
    return jsonify({"success": success})


# ==================== 계약서 API ====================

@app.route('/api/contracts', methods=['GET'])
def get_contracts():
    """계약서 목록 조회 - contractId 확인용"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    result = api.get_contracts()
    return jsonify(result)


@app.route('/api/budget', methods=['GET'])
def get_budget():
    """쿠폰 예산 현황 조회"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    result = api.get_budget()
    return jsonify(result)


# ==================== 즉시할인쿠폰 API ====================

@app.route('/api/instant-coupon/create', methods=['POST'])
def create_instant_coupon():
    """
    즉시할인쿠폰 생성
    
    Request Body:
    {
        "vendor_item_ids": [92195127705],  // 상품 옵션ID 배열
        "discount_amount": 16000,           // 할인 금액
        "contract_id": "계약서ID",          // 필수!
        "hours": 5,                         // 유효 시간 (선택)
        "end_date": "2026-03-31 23:59:00", // 종료일 (선택)
        "title": "프라임 NMN 할인"          // 쿠폰명 (선택)
    }
    """
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    vendor_item_ids = data.get('vendor_item_ids')
    discount_amount = data.get('discount_amount')
    contract_id = data.get('contract_id') or config.get('coupon_settings', {}).get('contract_id')
    hours = data.get('hours', config['settings'].get('discount_hours', 5))
    end_date = data.get('end_date')
    title = data.get('title')
    
    if not vendor_item_ids:
        return jsonify({"error": "상품을 지정해주세요 (vendor_item_ids)"}), 400
    if not discount_amount:
        return jsonify({"error": "할인 금액을 지정해주세요 (discount_amount)"}), 400
    if not contract_id:
        return jsonify({"error": "계약서 ID가 필요합니다. WING에서 예산 설정 후 /api/contracts 조회하세요"}), 400
    
    api = CoupangAPI(config)
    
    result = api.create_instant_coupon(
        vendor_item_ids=vendor_item_ids,
        discount_amount=discount_amount,
        contract_id=contract_id,
        hours=hours if not end_date else None,
        end_date=end_date,
        title=title
    )
    
    if result.get('success'):
        log_action("COUPON_CREATE", f"즉시할인쿠폰 생성 요청: {discount_amount:,}원 할인", result)
        
        # 비동기 처리이므로 requestId 반환됨
        request_id = result.get('data', {}).get('requestedId')
        if request_id:
            result['note'] = "비동기 처리입니다. /api/instant-coupon/status/{requestId}로 결과를 확인하세요."
    else:
        log_action("COUPON_ERROR", f"쿠폰 생성 실패: {result.get('error')}", result)
    
    return jsonify(result)


@app.route('/api/instant-coupon/status/<request_id>', methods=['GET'])
def get_coupon_status_by_request(request_id):
    """쿠폰 요청 상태 확인 (비동기 결과)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    result = api.get_coupon_request_status(request_id)
    return jsonify(result)


@app.route('/api/instant-coupon/cancel', methods=['POST'])
def cancel_instant_coupon():
    """즉시할인쿠폰 파기 (중지)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    coupon_id = data.get('coupon_id')
    
    if not coupon_id:
        return jsonify({"error": "쿠폰 ID를 지정해주세요"}), 400
    
    api = CoupangAPI(config)
    result = api.cancel_coupon(coupon_id)
    
    if result.get('success'):
        log_action("COUPON_CANCEL", f"쿠폰 파기: {coupon_id}", result)
    
    return jsonify(result)


@app.route('/api/instant-coupon/add-items', methods=['POST'])
def add_coupon_items():
    """기존 쿠폰에 상품 추가"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    coupon_id = data.get('coupon_id')
    vendor_item_ids = data.get('vendor_item_ids')
    
    if not coupon_id:
        return jsonify({"error": "쿠폰 ID를 지정해주세요"}), 400
    if not vendor_item_ids:
        return jsonify({"error": "상품을 지정해주세요"}), 400
    
    api = CoupangAPI(config)
    result = api.add_coupon_items(coupon_id, vendor_item_ids)
    
    if result.get('success'):
        log_action("COUPON_ADD_ITEMS", f"쿠폰 {coupon_id}에 상품 추가", result)
    
    return jsonify(result)


@app.route('/api/save-contract-id', methods=['POST'])
def save_contract_id():
    """계약서 ID 저장 (설정에 기본값으로)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    contract_id = data.get('contract_id')
    
    if not contract_id:
        return jsonify({"error": "계약서 ID를 지정해주세요"}), 400
    
    if 'coupon_settings' not in config:
        config['coupon_settings'] = {}
    
    config['coupon_settings']['contract_id'] = contract_id
    save_config(config)
    
    log_action("CONFIG_UPDATE", f"계약서 ID 저장: {contract_id}")
    return jsonify({"success": True, "contract_id": contract_id})


@app.route('/api/products', methods=['GET'])
def get_products():
    """상품 목록 조회 (실시간 가격 포함)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    products_data = {}
    
    for key, product in config['products'].items():
        if not product.get('enabled'):
            continue
        
        result = api.get_inventory(product['vendor_item_id'])
        if result['success'] and result.get('data'):
            data = result['data']
            products_data[key] = {
                **product,
                'api_data': {
                    'salePrice': data.get('salePrice'),
                    'originalPrice': data.get('originalPrice'),
                    'discountedPrice': data.get('discountedPrice'),
                    'usedSalePrice': data.get('usedSalePrice'),
                }
            }
        else:
            products_data[key] = {**product, 'api_data': None, 'api_error': result.get('error')}
    
    return jsonify(products_data)


@app.route('/api/competitor', methods=['POST'])
def update_competitor_price_legacy():
    """경쟁사 가격 업데이트 (기존 구조용)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    price = data.get('price')
    
    if not price:
        return jsonify({"error": "가격을 입력해주세요"}), 400
    
    config['competitors']['rokit_america']['last_price'] = int(price)
    config['competitors']['rokit_america']['last_checked'] = get_kst_now().isoformat()
    save_config(config)
    
    record_competitor_price(int(price))
    log_action("COMPETITOR_UPDATE", f"경쟁사 가격 업데이트: {price:,}원")
    
    # 오토모드 시 자동 가격 적용
    if config['settings'].get('auto_mode'):
        apply_result = auto_apply_prices(config, int(price))
        return jsonify({"success": True, "auto_applied": True, "result": apply_result})
    
    return jsonify({"success": True, "auto_applied": False})


def auto_apply_prices(config, competitor_price):
    """오토모드: 자동 가격 적용"""
    api = CoupangAPI(config)
    price_gap = config['settings'].get('price_gap', 300)
    price_method = config['settings'].get('price_method', 'price')
    hours = config['settings'].get('discount_hours', 5)
    price_direction = config['settings'].get('price_direction', 'lower')  # 'lower' or 'higher'
    contract_id = config.get('coupon_settings', {}).get('contract_id')
    
    results = []
    products = [
        ('prime_nmn_1bottle_60', 1, 0),
        ('prime_nmn_3bottle_60', 3, config['settings'].get('pack3_extra_discount', 1)),
        ('prime_nmn_6bottle_60', 6, config['settings'].get('pack6_extra_discount', 2)),
    ]
    
    for product_key, multiplier, extra_discount in products:
        product = config['products'].get(product_key)
        if not product or not product.get('enabled'):
            continue
        
        # 목표 가격 계산 (가격 방향에 따라)
        if price_direction == 'lower':
            base_price_1 = competitor_price - price_gap
        else:
            base_price_1 = competitor_price + price_gap
        
        base_price = base_price_1 * multiplier
        
        # 3병/6병 추가 할인 적용
        if extra_discount > 0:
            target_price = round(base_price * (1 - extra_discount / 100) / 10) * 10
        else:
            target_price = round(base_price / 10) * 10
        
        # 안전장치
        target_price = max(target_price, product['min_price'])
        target_price = min(target_price, product['max_price'])
        
        old_price = product['current_price']
        
        if price_method == 'coupon':
            # 즉시할인쿠폰 방식 (정가 고정, 쿠폰으로 할인)
            if not contract_id:
                results.append({
                    "product": product['name'],
                    "error": "계약서 ID가 설정되지 않음. /api/contracts 조회 후 /api/save-contract-id로 저장하세요.",
                    "success": False
                })
                continue
            
            discount_amount = product['original_price'] - target_price
            result = api.create_instant_coupon(
                vendor_item_ids=[product['vendor_item_id']],
                discount_amount=discount_amount,
                contract_id=contract_id,
                hours=hours,
                title=f"{product['name']} {discount_amount:,}원 할인"
            )
        else:
            # 직접 가격 변경 방식 (2중 할인 위험!)
            result = api.update_price(product['vendor_item_id'], target_price)
        
        if result.get('success'):
            # 쿠폰 방식일 때 상품 연결 성공 여부 추가 체크
            items_added = result.get('items_added', True)  # 쿠폰 아닌 경우 기본 True
            if price_method == 'coupon':
                items_added = result.get('items_added', False)
            
            config['products'][product_key]['current_price'] = target_price
            record_price_change(product_key, old_price, target_price, price_method)
            results.append({
                "product": product['name'],
                "old_price": old_price,
                "new_price": target_price,
                "discount_amount": product['original_price'] - target_price if price_method == 'coupon' else 0,
                "success": True,
                "items_added": items_added,
                "coupon_id": result.get('coupon_id')
            })
        else:
            results.append({
                "product": product['name'],
                "error": result.get('error'),
                "body": result.get('body'),
                "success": False
            })
    
    save_config(config)
    
    # 잔디 알림
    success_count = sum(1 for r in results if r['success'])
    items_failed_count = sum(1 for r in results if r['success'] and not r.get('items_added', True))
    
    jandi_msg = f"""🤖 오토모드 가격 자동 조정

📊 경쟁사 가격: {competitor_price:,}원
⚡ 조정 방식: {'쿠폰' if price_method == 'coupon' else '직접 가격변경'}

"""
    for r in results:
        if r['success']:
            if r.get('items_added', True):
                jandi_msg += f"✅ {r['product']}: {r['old_price']:,}원 → {r['new_price']:,}원\n"
            else:
                jandi_msg += f"⚠️ {r['product']}: {r['old_price']:,}원 → {r['new_price']:,}원 (상품 미연결!)\n"
        else:
            jandi_msg += f"❌ {r['product']}: 실패 ({r.get('error', '알 수 없음')})\n"
    
    # 상품 연결 실패 건 있으면 경고 추가
    if items_failed_count > 0:
        jandi_msg += f"\n⚠️ **확인 조치 필요!**\n"
        jandi_msg += f"상품 연결 실패: {items_failed_count}건\n"
        jandi_msg += "쿠팡 WING에서 쿠폰에 상품이 연결되었는지 확인하세요."
        
        send_jandi_notification(
            f"⚠️ [오토모드] {success_count}/{len(results)}개 적용 - 확인 필요",
            jandi_msg,
            "yellow"
        )
    else:
        send_jandi_notification(
            f"🤖 [오토모드] {success_count}/{len(results)}개 적용 완료",
            jandi_msg,
            "green" if success_count == len(results) else "yellow"
        )
    
    log_action("AUTO_APPLY", f"오토모드 가격 적용: {success_count}/{len(results)}개 성공", results)
    
    return results


@app.route('/api/apply-price', methods=['POST'])
def apply_price():
    """가격 적용 (수동)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    target_price = data.get('target_price') or data.get('price')  # 둘 다 허용
    product_key = data.get('product_key', 'prime_nmn_1bottle_60')
    
    # 쿠폰명: 요청에서 먼저, 없으면 config에서, 그래도 없으면 기본값
    coupon_name = data.get('coupon_name') or config['settings'].get('coupon_name') or '본사이언스 프라임 NMN 할인쿠폰'
    
    if not target_price:
        return jsonify({"error": "목표 가격을 지정해주세요"}), 400
    
    product = config['products'].get(product_key)
    if not product:
        return jsonify({"error": "상품을 찾을 수 없습니다"}), 404
    
    target_price = round(int(target_price) / 10) * 10
    
    if target_price < product['min_price']:
        return jsonify({"error": f"최저가({product['min_price']:,}원) 미만입니다"}), 400
    if target_price > product['max_price']:
        return jsonify({"error": f"최고가({product['max_price']:,}원) 초과입니다"}), 400
    
    api = CoupangAPI(config)
    method = data.get('method') or config['settings'].get('price_method', 'coupon')
    hours = data.get('hours') or config['settings'].get('discount_hours', 5)
    contract_id = config.get('coupon_settings', {}).get('contract_id')
    old_price = product['current_price']
    
    if method == 'coupon':
        if not contract_id:
            return jsonify({"error": "계약서 ID가 설정되지 않았습니다. /api/contracts 조회 후 설정하세요."}), 400
        
        # 상품별 구분자 (쿠폰명에 붙임)
        product_tags = {
            'prime_nmn_1bottle_60': '[1병]',
            'prime_nmn_3bottle_60': '[3병]',
            'prime_nmn_6bottle_60': '[6병]',
        }
        product_tag = product_tags.get(product_key, '')
        
        # 상품별 할인금액 범위 (동일 상품 쿠폰 식별용)
        product_discount_ranges = {
            'prime_nmn_1bottle_60': (10000, 25000),   # 1병: 약 1만~2.5만원 할인
            'prime_nmn_3bottle_60': (30000, 50000),   # 3병: 약 3만~5만원 할인
            'prime_nmn_6bottle_60': (40000, 70000),   # 6병: 약 4만~7만원 할인
        }
        discount_range = product_discount_ranges.get(product_key, (0, 0))
        
        # 고정 쿠폰 키워드 (파기하면 안 되는 것들)
        fixed_coupon_keywords = ['2천원', '3천원', '5천원', '1만원', '피크하이트']
        
        # 기존 쿠폰 찾아서 파기
        coupons_result = api.get_coupons("APPLIED")
        cancelled_coupons = []
        
        if coupons_result.get('success'):
            coupon_data = coupons_result.get('data', {})
            if isinstance(coupon_data, dict):
                inner_data = coupon_data.get('data', {})
                if isinstance(inner_data, dict):
                    coupon_list = inner_data.get('content', [])
                else:
                    coupon_list = []
            else:
                coupon_list = []
            
            for coupon in coupon_list:
                promo_name = coupon.get('promotionName', '')
                coupon_id = coupon.get('couponId')
                discount = coupon.get('discount', 0)
                
                # 고정 쿠폰은 제외
                if any(kw in promo_name for kw in fixed_coupon_keywords):
                    continue
                
                # NMN 관련 쿠폰인지 확인
                is_nmn_coupon = 'NMN' in promo_name or 'nmn' in promo_name.lower() or '프라임' in promo_name
                
                if not is_nmn_coupon:
                    continue
                
                # 해당 상품 쿠폰인지 확인
                should_cancel = False
                
                # 방법 1: 구분자로 매칭
                if product_tag and product_tag in promo_name:
                    should_cancel = True
                
                # 방법 2: 할인금액 범위로 매칭 (구분자 없는 기존 쿠폰)
                if not should_cancel and discount_range[0] > 0:
                    if discount_range[0] <= discount <= discount_range[1]:
                        # 구분자가 없는 쿠폰만 (다른 상품 구분자가 없어야 함)
                        other_tags = [t for k, t in product_tags.items() if k != product_key]
                        if not any(t in promo_name for t in other_tags):
                            should_cancel = True
                
                if should_cancel and coupon_id:
                    cancel_result = api.cancel_coupon(coupon_id)
                    if cancel_result.get('success'):
                        cancelled_coupons.append(f"{promo_name} (ID: {coupon_id}, {int(discount):,}원)")
                        log_action("COUPON_CANCEL", f"기존 쿠폰 파기: {promo_name} (ID: {coupon_id})")
                    else:
                        log_action("COUPON_CANCEL_FAIL", f"쿠폰 파기 실패: {coupon_id}", cancel_result)
        
        # 새 쿠폰 발행 - 쿠폰명 끝에 상품 구분자 자동 추가
        discount_amount = product['original_price'] - target_price
        final_coupon_name = f"{coupon_name} {product_tag}".strip()
        
        result = api.create_instant_coupon(
            vendor_item_ids=[product['vendor_item_id']],
            discount_amount=discount_amount,
            contract_id=contract_id,
            hours=hours,
            title=final_coupon_name
        )
        
        # 파기된 쿠폰 정보 추가
        if cancelled_coupons:
            result['cancelled_coupons'] = cancelled_coupons
        
        # 실제 사용된 쿠폰명 저장
        result['coupon_name_used'] = final_coupon_name
    else:
        result = api.update_price(product['vendor_item_id'], target_price)
    
    if result.get('success'):
        config['products'][product_key]['current_price'] = target_price
        save_config(config)
        record_price_change(product_key, old_price, target_price, method)
        
        # 실제 사용된 쿠폰명 (구분자 포함)
        actual_coupon_name = result.get('coupon_name_used', coupon_name)
        log_action("PRICE_APPLY", f"{product['name']} 가격 적용: {old_price:,}원 → {target_price:,}원 (쿠폰명: {actual_coupon_name})")
        
        # 잔디 알림 전송
        discount_amount = product['original_price'] - target_price
        discount_rate = round((discount_amount / product['original_price']) * 100, 1)
        method_text = "🎫 즉시할인쿠폰" if method == 'coupon' else "💰 직접가격변경"
        
        jandi_msg = f"""📦 {product['name']}

{method_text}
🏷️ 쿠폰명: {actual_coupon_name}

💵 정가: {product['original_price']:,}원
🎫 할인: {discount_amount:,}원 ({discount_rate}%)
💰 판매가: {target_price:,}원

📊 이전가: {old_price:,}원 → {target_price:,}원"""

        if method == 'coupon':
            jandi_msg += f"\n⏱️ 유효시간: {hours}시간"
            
            # 파기된 기존 쿠폰 정보
            if result.get('cancelled_coupons'):
                jandi_msg += f"\n\n🗑️ 기존 쿠폰 파기: {len(result['cancelled_coupons'])}개"
                for c in result['cancelled_coupons'][:3]:  # 최대 3개만 표시
                    jandi_msg += f"\n  • {c}"
            
            # 상품 연결 실패 체크
            if result.get('coupon_id') and not result.get('items_added'):
                jandi_msg += f"\n\n⚠️ **상품 연결 실패!**"
                jandi_msg += f"\n쿠폰 ID: {result.get('coupon_id')}"
                jandi_msg += f"\n👉 확인 조치 필요"
                send_jandi_notification("⚠️ [가격 적용 - 확인 필요]", jandi_msg, "yellow")
                log_action("COUPON_ITEM_FAIL", f"쿠폰 상품 연결 실패: couponId={result.get('coupon_id')}", result)
                return jsonify(result)
        
        send_jandi_notification("✅ [가격 적용 완료]", jandi_msg, "green")
    
    return jsonify(result)


@app.route('/api/update-our-price', methods=['POST'])
def update_our_price():
    """우리 상품 판매가 직접 수정"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    product_key = data.get('product_key')
    new_price = data.get('price')
    
    if not product_key or not new_price:
        return jsonify({"error": "상품과 가격을 지정해주세요"}), 400
    
    product = config['products'].get(product_key)
    if not product:
        return jsonify({"error": "상품을 찾을 수 없습니다"}), 404
    
    new_price = round(int(new_price) / 10) * 10
    
    if new_price < product['min_price']:
        return jsonify({"error": f"최저가({product['min_price']:,}원) 미만입니다"}), 400
    if new_price > product['max_price']:
        return jsonify({"error": f"최고가({product['max_price']:,}원) 초과입니다"}), 400
    
    api = CoupangAPI(config)
    method = config['settings'].get('price_method', 'price')
    hours = config['settings'].get('discount_hours', 5)
    contract_id = config.get('coupon_settings', {}).get('contract_id')
    old_price = product['current_price']
    
    if method == 'coupon':
        if not contract_id:
            return jsonify({"error": "계약서 ID가 설정되지 않았습니다. /api/contracts 조회 후 설정하세요."}), 400
        
        discount_amount = product['original_price'] - new_price
        result = api.create_instant_coupon(
            vendor_item_ids=[product['vendor_item_id']],
            discount_amount=discount_amount,
            contract_id=contract_id,
            hours=hours,
            title=f"{product['name']} {discount_amount:,}원 할인"
        )
    else:
        result = api.update_price(product['vendor_item_id'], new_price)
    
    if result.get('success'):
        config['products'][product_key]['current_price'] = new_price
        save_config(config)
        record_price_change(product_key, old_price, new_price, method)
        log_action("PRICE_CHANGE", f"{product['name']} 가격 변경: {old_price:,}원 → {new_price:,}원")
        
        competitor_price = config['competitors']['rokit_america'].get('last_price', 0)
        price_diff = competitor_price - new_price
        send_jandi_notification(
            "🔔 [가격 변경 알림]",
            f"📦 상품: {product['name']}\n💰 {old_price:,}원 → {new_price:,}원\n🎯 경쟁사: {competitor_price:,}원\n{'✅ 우리가 ' + str(price_diff) + '원 저렴!' if price_diff > 0 else '⚠️ 경쟁사가 더 저렴'}",
            "green" if price_diff > 0 else "yellow"
        )
    
    return jsonify(result)


@app.route('/api/auto-mode', methods=['POST'])
def toggle_auto_mode():
    """오토모드 토글"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    auto_mode = not config['settings'].get('auto_mode', False)
    config['settings']['auto_mode'] = auto_mode
    save_config(config)
    
    status = "활성화" if auto_mode else "비활성화"
    log_action("AUTO_MODE", f"오토모드 {status}")
    
    if auto_mode:
        send_jandi_notification(
            "🤖 [오토모드 활성화]",
            "경쟁사 가격 입력 시 자동으로 가격이 조정됩니다.\n\n⏰ 4시간마다 가격 체크 알림이 전송됩니다.",
            "green"
        )
    
    return jsonify({"success": True, "auto_mode": auto_mode})


@app.route('/api/save-safety-limits', methods=['POST'])
def save_safety_limits():
    """안전장치 가격 범위 저장"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    data = request.json
    product_key = data.get('product_key')
    min_price = data.get('min_price')
    max_price = data.get('max_price')
    
    if not product_key or not min_price or not max_price:
        return jsonify({"error": "product_key, min_price, max_price가 필요합니다"}), 400
    
    if product_key not in config['products']:
        return jsonify({"error": f"상품을 찾을 수 없습니다: {product_key}"}), 404
    
    if min_price >= max_price:
        return jsonify({"error": "최소가가 최대가보다 작아야 합니다"}), 400
    
    old_min = config['products'][product_key].get('min_price')
    old_max = config['products'][product_key].get('max_price')
    
    config['products'][product_key]['min_price'] = int(min_price)
    config['products'][product_key]['max_price'] = int(max_price)
    save_config(config)
    
    product_name = config['products'][product_key].get('name', product_key)
    log_action("SAFETY_LIMITS", f"{product_name} 안전장치 변경: {old_min:,}~{old_max:,} → {min_price:,}~{max_price:,}")
    
    return jsonify({
        "success": True,
        "product_key": product_key,
        "product_name": product_name,
        "min_price": min_price,
        "max_price": max_price
    })


@app.route('/api/sync-prices', methods=['POST'])
def sync_prices():
    """쿠팡 API에서 실제 가격 동기화
    
    original_price(정가)와 current_price(현재 판매가)를 
    쿠팡 API에서 가져와서 config에 업데이트
    """
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    results = []
    
    for product_key, product in config['products'].items():
        if not product.get('enabled'):
            continue
        
        vendor_item_id = product.get('vendor_item_id')
        if not vendor_item_id:
            continue
        
        # 쿠팡 API에서 가격 조회
        price_result = api.get_inventory(vendor_item_id)
        
        if price_result.get('success'):
            data = price_result.get('data', {})
            
            # 응답 구조 파싱 (여러 형태 대응)
            original_price = None
            sale_price = None
            
            if isinstance(data, dict):
                # data.data 구조일 수 있음
                inner_data = data.get('data', data)
                if isinstance(inner_data, dict):
                    original_price = inner_data.get('originalPrice') or inner_data.get('supplyPrice')
                    sale_price = inner_data.get('salePrice') or inner_data.get('originalPrice')
            
            if original_price:
                old_original = product.get('original_price')
                config['products'][product_key]['original_price'] = int(original_price)
                
            if sale_price:
                old_current = product.get('current_price')
                config['products'][product_key]['current_price'] = int(sale_price)
            
            results.append({
                "product": product.get('name'),
                "vendor_item_id": vendor_item_id,
                "original_price": original_price,
                "current_price": sale_price,
                "success": True
            })
        else:
            results.append({
                "product": product.get('name'),
                "vendor_item_id": vendor_item_id,
                "error": price_result.get('error'),
                "success": False
            })
    
    save_config(config)
    log_action("SYNC_PRICES", f"가격 동기화 완료: {len([r for r in results if r['success']])}개 성공")
    
    return jsonify({
        "success": True,
        "results": results,
        "synced_at": format_kst_datetime()
    })


@app.route('/api/reset-coupon-usage', methods=['POST'])
def reset_coupon_usage():
    """쿠폰 사용량 리셋"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    old_used = config['settings'].get('coupon_used', 0)
    config['settings']['coupon_used'] = 0
    save_config(config)
    
    log_action("COUPON_RESET", f"쿠폰 사용량 리셋: {old_used:,}원 → 0원")
    return jsonify({"success": True, "old_used": old_used})


@app.route('/api/coupons', methods=['GET'])
def get_coupons():
    """쿠폰 목록"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    result = api.get_coupons()
    return jsonify(result)


@app.route('/api/coupon-status', methods=['GET'])
def get_coupon_status():
    """쿠폰 상태 조회"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    
    applied_result = api.get_coupons("APPLIED")
    used_result = api.get_coupons("USED_OUT")
    
    vendor_item_ids = {
        str(p['vendor_item_id']): key 
        for key, p in config['products'].items() 
        if p.get('enabled')
    }
    
    coupon_status = {}
    
    if applied_result.get('success') and applied_result.get('data'):
        coupons = applied_result['data']
        if isinstance(coupons, list):
            for coupon in coupons:
                vendor_item_id = str(coupon.get('vendorItemId'))
                if vendor_item_id in vendor_item_ids:
                    product_key = vendor_item_ids[vendor_item_id]
                    coupon_status[product_key] = {
                        'status': 'active',
                        'coupon_id': coupon.get('couponId'),
                        'discount_value': coupon.get('discountValue'),
                        'end_date': coupon.get('endDate'),
                        'remaining_count': coupon.get('remainingCount'),
                        'raw': coupon
                    }
    
    for product_key in vendor_item_ids.values():
        if product_key not in coupon_status:
            coupon_status[product_key] = {'status': 'none'}
    
    return jsonify(coupon_status)


@app.route('/api/cleanup-nmn-coupons', methods=['POST'])
def cleanup_nmn_coupons():
    """중복된 NMN 쿠폰 정리 - 가장 최신 쿠폰만 남기고 나머지 파기"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    api = CoupangAPI(config)
    coupons_result = api.get_coupons("APPLIED")
    
    if not coupons_result.get('success'):
        return jsonify({"error": "쿠폰 목록 조회 실패", "detail": coupons_result}), 500
    
    coupon_data = coupons_result.get('data', {})
    if isinstance(coupon_data, dict):
        inner_data = coupon_data.get('data', {})
        if isinstance(inner_data, dict):
            coupon_list = inner_data.get('content', [])
        else:
            coupon_list = []
    else:
        coupon_list = []
    
    # NMN 관련 쿠폰만 필터링 (기존 고정 쿠폰 제외)
    nmn_coupons = []
    fixed_coupon_names = ['2천원', '3천원', '5천원', '1만원', '피크하이트']  # 고정 쿠폰 키워드
    
    for coupon in coupon_list:
        promo_name = coupon.get('promotionName', '')
        # NMN 관련이고, 고정 쿠폰이 아닌 것만
        if 'NMN' in promo_name or 'nmn' in promo_name.lower():
            if not any(fixed in promo_name for fixed in fixed_coupon_names):
                nmn_coupons.append(coupon)
    
    if not nmn_coupons:
        return jsonify({"success": True, "message": "정리할 NMN 쿠폰이 없습니다", "cancelled": 0})
    
    # 상품별로 그룹화 (구분자 기준)
    groups = {'[1병]': [], '[3병]': [], '[6병]': [], 'unknown': []}
    
    for coupon in nmn_coupons:
        promo_name = coupon.get('promotionName', '')
        if '[1병]' in promo_name:
            groups['[1병]'].append(coupon)
        elif '[3병]' in promo_name:
            groups['[3병]'].append(coupon)
        elif '[6병]' in promo_name:
            groups['[6병]'].append(coupon)
        else:
            groups['unknown'].append(coupon)
    
    cancelled = []
    kept = []
    
    # 각 그룹에서 가장 최신(couponId가 큰 것)만 남기고 나머지 파기
    for group_name, group_coupons in groups.items():
        if len(group_coupons) <= 1:
            if group_coupons:
                kept.append(f"{group_coupons[0].get('promotionName')} (ID: {group_coupons[0].get('couponId')})")
            continue
        
        # couponId 기준 정렬 (내림차순 - 최신이 앞으로)
        sorted_coupons = sorted(group_coupons, key=lambda x: x.get('couponId', 0), reverse=True)
        
        # 첫 번째(최신)는 유지
        kept.append(f"{sorted_coupons[0].get('promotionName')} (ID: {sorted_coupons[0].get('couponId')})")
        
        # 나머지는 파기
        for coupon in sorted_coupons[1:]:
            coupon_id = coupon.get('couponId')
            promo_name = coupon.get('promotionName', '')
            cancel_result = api.cancel_coupon(coupon_id)
            if cancel_result.get('success'):
                cancelled.append(f"{promo_name} (ID: {coupon_id})")
                log_action("COUPON_CLEANUP", f"중복 쿠폰 파기: {promo_name} (ID: {coupon_id})")
    
    # unknown 그룹(구분자 없는 기존 쿠폰)은 전부 파기
    for coupon in groups['unknown']:
        coupon_id = coupon.get('couponId')
        promo_name = coupon.get('promotionName', '')
        cancel_result = api.cancel_coupon(coupon_id)
        if cancel_result.get('success'):
            cancelled.append(f"{promo_name} (ID: {coupon_id})")
            log_action("COUPON_CLEANUP", f"구분자 없는 쿠폰 파기: {promo_name} (ID: {coupon_id})")
    
    return jsonify({
        "success": True,
        "cancelled_count": len(cancelled),
        "cancelled": cancelled,
        "kept": kept
    })


@app.route('/api/logs', methods=['GET'])
def get_logs():
    """액션 로그"""
    return jsonify(action_logs[-50:])


@app.route('/api/price-history', methods=['GET'])
def get_price_history():
    """가격 변동 이력"""
    history = load_price_history()
    return jsonify(history)


@app.route('/api/sales-chart', methods=['GET'])
def get_sales_chart():
    """BigQuery에서 판매 차트 데이터"""
    client = get_bigquery_client()
    if not client:
        return jsonify({"error": "BigQuery 연결 실패", "data": []})
    
    days = request.args.get('days', 90, type=int)
    
    query = f"""
    SELECT 
        DATE(order_date) as date,
        product_code,
        SUM(quantity) as total_quantity,
        COUNT(DISTINCT order_id) as order_count
    FROM `novatra-test.release.total_sales`
    WHERE 
        channel = '[쿠팡]'
        AND product_code IN ('BS_NMN60 X1', 'BS_NMN60 X3', 'BS_NMN60 X6', 'BS_NMN72 X6')
        AND order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
    GROUP BY date, product_code
    ORDER BY date
    """
    
    try:
        results = client.query(query).result()
        data = []
        for row in results:
            data.append({
                'date': row.date.isoformat(),
                'product_code': row.product_code,
                'total_quantity': row.total_quantity,
                'order_count': row.order_count
            })
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"error": str(e), "data": []})


@app.route('/api/weekly-summary', methods=['GET'])
def get_weekly_summary():
    """주간 요약"""
    client = get_bigquery_client()
    if not client:
        return jsonify({"error": "BigQuery 연결 실패"})
    
    query = """
    WITH weekly AS (
        SELECT 
            DATE_TRUNC(DATE(order_date), WEEK(MONDAY)) as week_start,
            SUM(quantity) as total_quantity,
            COUNT(DISTINCT order_id) as order_count,
            SUM(total_price) as total_revenue
        FROM `novatra-test.release.total_sales`
        WHERE 
            channel = '[쿠팡]'
            AND product_code IN ('BS_NMN60 X1', 'BS_NMN60 X3', 'BS_NMN60 X6')
            AND order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 8 WEEK)
        GROUP BY week_start
        ORDER BY week_start DESC
        LIMIT 2
    )
    SELECT * FROM weekly
    """
    
    try:
        results = list(client.query(query).result())
        if len(results) >= 2:
            this_week = results[0]
            last_week = results[1]
            return jsonify({
                "this_week": {
                    "quantity": this_week.total_quantity,
                    "orders": this_week.order_count,
                    "revenue": float(this_week.total_revenue) if this_week.total_revenue else 0
                },
                "last_week": {
                    "quantity": last_week.total_quantity,
                    "orders": last_week.order_count,
                    "revenue": float(last_week.total_revenue) if last_week.total_revenue else 0
                }
            })
        return jsonify({"error": "데이터 부족"})
    except Exception as e:
        return jsonify({"error": str(e)})


# ==================== AI 인사이트 (마진 밸런스 분석) ====================

def get_today_kst():
    """오늘 날짜 (KST 기준) 문자열 반환"""
    return get_kst_now().strftime('%Y-%m-%d')


def can_generate_insight(config, group_key):
    """해당 제품 그룹이 오늘 AI 인사이트를 생성할 수 있는지 확인"""
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return False, "제품 그룹을 찾을 수 없습니다"
    
    group = groups[group_key]
    ai_insight = group.get('ai_insight', {})
    last_date = ai_insight.get('last_date', '')
    today = get_today_kst()
    
    if last_date == today:
        return False, f"오늘({today}) 이미 분석을 완료했습니다. 다음 분석: 내일 00:00"
    
    return True, "분석 가능"


def calculate_price_elasticity(sales_data):
    """가격-판매량 데이터로 가격 탄력성 계산
    
    가격 탄력성 = (판매량 변화율) / (가격 변화율)
    """
    if len(sales_data) < 2:
        return -1.5  # 기본값
    
    # 간단한 회귀 분석 대용
    prices = [d['price'] for d in sales_data]
    quantities = [d['quantity'] for d in sales_data]
    
    avg_price = sum(prices) / len(prices)
    avg_qty = sum(quantities) / len(quantities)
    
    if avg_price == 0 or avg_qty == 0:
        return -1.5
    
    # 가격 변화에 따른 판매량 변화 추정
    price_changes = []
    qty_changes = []
    
    for i in range(1, len(sales_data)):
        if sales_data[i-1]['price'] > 0 and sales_data[i-1]['quantity'] > 0:
            price_change = (sales_data[i]['price'] - sales_data[i-1]['price']) / sales_data[i-1]['price']
            qty_change = (sales_data[i]['quantity'] - sales_data[i-1]['quantity']) / sales_data[i-1]['quantity']
            if price_change != 0:
                price_changes.append(price_change)
                qty_changes.append(qty_change)
    
    if not price_changes:
        return -1.5
    
    # 평균 탄력성
    elasticities = [q/p if p != 0 else 0 for p, q in zip(price_changes, qty_changes)]
    avg_elasticity = sum(elasticities) / len(elasticities) if elasticities else -1.5
    
    # 일반적으로 음수 (가격 오르면 판매량 감소)
    return round(avg_elasticity, 2)


def simulate_prices(current_price, cost, elasticity, current_qty, price_range):
    """다양한 가격대별 예상 판매량 및 수익 시뮬레이션"""
    simulations = []
    
    for price in price_range:
        if price <= 0:
            continue
        
        # 가격 변화율
        price_change_pct = (price - current_price) / current_price if current_price > 0 else 0
        
        # 예상 판매량 (탄력성 기반)
        qty_change_pct = elasticity * price_change_pct
        expected_qty = max(1, round(current_qty * (1 + qty_change_pct)))
        
        # 마진 계산
        margin = price - cost
        margin_rate = (margin / price * 100) if price > 0 else 0
        
        # 일 수익
        daily_profit = margin * expected_qty
        
        # 현재 대비 변화
        current_profit = (current_price - cost) * current_qty
        profit_change_pct = ((daily_profit - current_profit) / current_profit * 100) if current_profit > 0 else 0
        
        simulations.append({
            'price': price,
            'margin': margin,
            'margin_rate': round(margin_rate, 1),
            'expected_qty': expected_qty,
            'daily_profit': round(daily_profit),
            'profit_change_pct': round(profit_change_pct, 1)
        })
    
    return simulations


def find_optimal_price(simulations, min_margin_rate=35):
    """최적 가격 찾기 (마진율 유지하면서 수익 최대화)"""
    valid = [s for s in simulations if s['margin_rate'] >= min_margin_rate]
    if not valid:
        valid = simulations
    
    # 일 수익 최대화
    optimal = max(valid, key=lambda x: x['daily_profit'])
    return optimal


@app.route('/api/product-groups/<group_key>/ai-insight', methods=['GET'])
def get_ai_insight(group_key):
    """AI 인사이트 조회 (캐시된 결과 반환)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    group = groups[group_key]
    ai_insight = group.get('ai_insight', {})
    
    can_generate, message = can_generate_insight(config, group_key)
    
    return jsonify({
        "group_key": group_key,
        "group_name": group.get('name', ''),
        "can_generate": can_generate,
        "message": message,
        "insight": ai_insight.get('result', None),
        "last_generated": ai_insight.get('last_date', ''),
        "last_time": ai_insight.get('last_time', '')
    })


@app.route('/api/product-groups/<group_key>/ai-insight', methods=['POST'])
def generate_ai_insight(group_key):
    """AI 인사이트 생성 (제품별 하루 1회 제한)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다"}), 404
    
    groups = config.get('product_groups', {})
    if group_key not in groups:
        return jsonify({"error": f"제품 그룹을 찾을 수 없습니다: {group_key}"}), 404
    
    # 하루 1회 제한 체크
    can_generate, message = can_generate_insight(config, group_key)
    if not can_generate:
        return jsonify({
            "error": message,
            "can_generate": False,
            "insight": groups[group_key].get('ai_insight', {}).get('result', None)
        }), 429  # Too Many Requests
    
    group = groups[group_key]
    products = group.get('products', {})
    
    # 1병 기준 데이터 추출
    product_1 = products.get('1bottle', {})
    current_price = product_1.get('current_price', 0) or product_1.get('original_price', 0)
    original_price = product_1.get('original_price', 0)
    min_price = product_1.get('min_price', 0)
    max_price = product_1.get('max_price', 0)
    
    if current_price == 0:
        return jsonify({"error": "가격 정보가 없습니다. 먼저 쿠팡 가격을 동기화해주세요."}), 400
    
    # 원가 추정 (정가의 50~55% 가정, 실제로는 config에서 받아야 함)
    cost = group.get('estimated_cost', int(original_price * 0.5)) if original_price > 0 else int(current_price * 0.55)
    
    # BigQuery에서 판매 데이터 조회 시도
    sales_data = []
    avg_daily_qty = 10  # 기본값
    
    if HAS_BIGQUERY:
        try:
            client = get_bigquery_client()
            if client:
                # 최근 30일 일별 판매 데이터 조회
                # TODO: 실제 product_code 매핑 필요
                query = f"""
                    SELECT 
                        DATE(order_date) as sale_date,
                        AVG(price) as avg_price,
                        SUM(quantity) as total_qty
                    FROM `novatra-test.release.total_sales`
                    WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
                    GROUP BY sale_date
                    ORDER BY sale_date
                """
                # 실제 쿼리 실행은 product_code 매핑 후
                # results = client.query(query).result()
                pass
        except Exception as e:
            print(f"BigQuery 조회 실패: {e}")
    
    # 기본 분석 (BigQuery 없이도 동작)
    if not sales_data:
        # 샘플 데이터로 시뮬레이션
        sales_data = [
            {'price': current_price, 'quantity': avg_daily_qty}
        ]
    
    # 가격 탄력성 계산 (데이터 부족 시 기본값 사용)
    elasticity = calculate_price_elasticity(sales_data) if len(sales_data) > 1 else -1.8
    
    # 가격 시뮬레이션
    price_step = 500 if current_price < 50000 else 1000
    price_range = list(range(
        max(min_price, int(current_price * 0.8)),
        min(max_price, int(current_price * 1.2)) + price_step,
        price_step
    ))
    
    # 현재 가격 포함
    if current_price not in price_range:
        price_range.append(current_price)
        price_range.sort()
    
    simulations = simulate_prices(current_price, cost, elasticity, avg_daily_qty, price_range)
    
    # 최적 가격 찾기
    optimal = find_optimal_price(simulations, min_margin_rate=35)
    
    # 현재 상태 계산
    current_margin = current_price - cost
    current_margin_rate = (current_margin / current_price * 100) if current_price > 0 else 0
    current_daily_profit = current_margin * avg_daily_qty
    
    # 결과 구성
    result = {
        "current_state": {
            "price": current_price,
            "cost": cost,
            "margin": current_margin,
            "margin_rate": round(current_margin_rate, 1),
            "avg_daily_qty": avg_daily_qty,
            "daily_profit": current_daily_profit
        },
        "elasticity": elasticity,
        "simulations": simulations,
        "optimal": {
            "price": optimal['price'],
            "margin_rate": optimal['margin_rate'],
            "expected_qty": optimal['expected_qty'],
            "daily_profit": optimal['daily_profit'],
            "profit_change_pct": optimal['profit_change_pct'],
            "monthly_extra_profit": round((optimal['daily_profit'] - current_daily_profit) * 30)
        },
        "recommendation": f"최적 가격 {optimal['price']:,}원으로 변경 시, 마진율 {optimal['margin_rate']}% 유지하면서 월 수익 약 {abs(round((optimal['daily_profit'] - current_daily_profit) * 30 / 10000))}만원 {'증가' if optimal['profit_change_pct'] > 0 else '감소'} 예상",
        "warning": f"{int(cost / 0.65):,}원 이하로 내리면 마진율 35% 미만으로 수익성 악화" if cost > 0 else None
    }
    
    # 결과 저장 (캐시)
    if 'ai_insight' not in groups[group_key]:
        groups[group_key]['ai_insight'] = {}
    
    groups[group_key]['ai_insight']['result'] = result
    groups[group_key]['ai_insight']['last_date'] = get_today_kst()
    groups[group_key]['ai_insight']['last_time'] = format_kst_datetime()
    
    save_config(config)
    
    log_action("AI_INSIGHT", f"AI 인사이트 생성: {group_key}", {
        "optimal_price": optimal['price'],
        "profit_change": optimal['profit_change_pct']
    })
    
    return jsonify({
        "success": True,
        "group_key": group_key,
        "group_name": group.get('name', ''),
        "insight": result,
        "generated_at": format_kst_datetime(),
        "next_available": "내일 00:00"
    })


# ==================== 전체 자동화 ====================

_auto_check_lock = threading.Lock()

@app.route('/api/auto-check-all', methods=['GET', 'POST'])
def auto_check_all():
    """전체 자동화 엔드포인트 (Cloud Scheduler 2시간마다 호출)
    
    각 그룹별로:
    1. 경쟁사 가격 크롤링 (ScraperAPI)
    2. 가격 변동 감지 → 잔디 알림
    3. auto_mode ON인 그룹 → 쿠폰 자동 재발행
    
    Cloud Scheduler 설정:
    - URL: https://coupang-price-manager-xxx.run.app/api/auto-check-all
    - Method: POST
    - Frequency: 0 */2 9-22 * * (09~22시, 2시간마다)
    """
    if not _auto_check_lock.acquire(blocking=False):
        return jsonify({"message": "이미 실행 중입니다", "executed": False, "skipped": True})
    
    try:
        return _do_auto_check_all()
    finally:
        _auto_check_lock.release()


def _do_auto_check_all():
    """실제 자동 체크 로직 (락 내부에서 실행)"""
    config = load_config()
    if not config:
        return jsonify({"error": "설정 파일이 없습니다", "executed": False}), 404
    
    groups = config.get('product_groups', {})
    all_results = {}
    
    for group_key, group in groups.items():
        if not group.get('enabled', True):
            continue
        
        group_name = group.get('name', group_key)
        group_result = {
            'name': group_name,
            'channel': group.get('channel', 'C'),
            'auto_mode': group.get('auto_mode', False),
            'crawl': None,
            'price_changed': False,
            'applied': False,
        }
        
        # 1단계: 경쟁사 크롤링
        competitors = group.get('competitors', [])
        if not competitors:
            group_result['crawl'] = 'no competitors'
            all_results[group_key] = group_result
            continue
        
        old_prices = {c.get('id', ''): c.get('last_price', 0) for c in competitors}
        
        crawl_results = crawl_competitor_prices(group)
        save_config(config)  # 크롤링 결과 저장
        
        success_count = sum(1 for r in crawl_results if r['success'])
        group_result['crawl'] = f"{success_count}/{len(crawl_results)} success"
        
        # 가격 변동 확인
        for r in crawl_results:
            if r['success']:
                comp_id = r.get('competitor_id', '')
                old_price = old_prices.get(comp_id, 0)
                new_price = r.get('price', 0)
                if old_price > 0 and new_price != old_price:
                    group_result['price_changed'] = True
                    change_pct = round((new_price - old_price) / old_price * 100, 1)
                    
                    send_jandi_notification(
                        f"🔍 [{group_name}] 경쟁사 가격 변동!",
                        f"{r.get('competitor_name','')}: {old_price:,}원 → {new_price:,}원 ({change_pct:+.1f}%)",
                        "yellow"
                    )
        
        # 2단계: auto_mode ON이면 쿠폰 재발행
        if group.get('auto_mode'):
            try:
                with app.test_request_context():
                    apply_response = apply_group_prices(group_key)
                    # Flask Response에서 JSON 추출
                    if hasattr(apply_response, 'get_json'):
                        apply_data = apply_response.get_json()
                    else:
                        apply_data = apply_response
                    
                    apply_success = apply_data.get('success', False) if isinstance(apply_data, dict) else False
                    
                    if apply_success:
                        results_data = apply_data.get('results', [])
                        ok = sum(1 for r in results_data if r.get('success'))
                        fail = sum(1 for r in results_data if not r.get('success'))
                        total = len(results_data)
                        
                        if total == 0:
                            # 상품 0개 처리 = 실질적 실패
                            group_result['applied'] = False
                            group_result['apply_error'] = '상품 데이터 없음 (vendor_item_id 확인 필요)'
                        elif fail > 0:
                            # 부분 실패
                            group_result['applied'] = True
                            group_result['partial_fail'] = True
                            group_result['apply_detail'] = f"{ok}/{total} coupons issued ({fail} failed)"
                        else:
                            group_result['applied'] = True
                            group_result['apply_detail'] = f"{ok}/{total} coupons issued"
                    else:
                        group_result['applied'] = False
                        group_result['apply_error'] = apply_data.get('error', '발행 실패') if isinstance(apply_data, dict) else '발행 실패'
                    
            except Exception as e:
                group_result['applied'] = False
                group_result['apply_error'] = str(e)
                log_action("AUTO_CHECK_ERROR", f"자동 재발행 실패 ({group_key}): {str(e)}")
        
        all_results[group_key] = group_result
    
    log_action("AUTO_CHECK_ALL", f"전체 자동 체크 완료: {len(all_results)}개 그룹", all_results)
    
    checked_at = format_kst_datetime()
    
    # 이메일: 15분 후 쿠팡 쿠폰 적용 여부 검증 후 발송 (백그라운드)
    _start_delayed_verification(all_results, checked_at, config)
    
    return jsonify({
        "success": True,
        "groups": all_results,
        "checked_at": checked_at,
        "email": "15분 후 검증 완료 시 발송 예정"
    })


COUPON_VERIFY_DELAY_SECONDS = 15 * 60  # 15분


def _start_delayed_verification(all_results, checked_at, config):
    """백그라운드 스레드로 15분 후 쿠폰 적용 검증 + 이메일 발송"""
    
    # GCS에 검증 대기 상태 저장 (컨테이너 재시작 안전장치)
    try:
        pending = {
            'all_results': all_results,
            'checked_at': checked_at,
            'verify_after': format_kst_datetime(offset_minutes=15),
            'status': 'pending'
        }
        save_to_gcs(pending, 'pending_verification.json')
        print("[VERIFY] 검증 대기 상태 GCS 저장 완료")
    except Exception as e:
        print(f"[VERIFY] GCS 저장 실패 (계속 진행): {e}")
    
    def _delayed_verify():
        import time as _time
        
        delay = COUPON_VERIFY_DELAY_SECONDS
        print(f"[VERIFY] {delay//60}분 후 쿠폰 적용 검증 예정...")
        _time.sleep(delay)
        
        print("[VERIFY] 쿠폰 적용 검증 시작")
        
        try:
            with app.app_context():
                verified_results = _verify_coupon_application(all_results, config)
                verified_at = format_kst_datetime()
                
                # 검증 실패 시 잔디 긴급 알림
                for gk, gr in verified_results.items():
                    if gr.get('verify_ok') == False:
                        details = '\n'.join(gr.get('verify_details', []))
                        send_jandi_notification(
                            f"🚨 [{gr.get('name', gk)}] 쿠폰 적용 실패 감지!",
                            f"15분 검증 결과 쿠팡에서 쿠폰이 활성화되지 않았습니다.\n\n{details}\n\n수동 확인 필요",
                            "red"
                        )
                
                # 검증 결과로 이메일 발송
                subject, text, html = build_verified_email(verified_results, checked_at, verified_at)
                send_email_notification(subject, text, html)
                
                log_action("VERIFY_COMPLETE", f"쿠폰 검증 완료 + 이메일 발송", verified_results)
                
                # GCS 검증 대기 상태 삭제
                try:
                    save_to_gcs({'status': 'completed', 'verified_at': verified_at}, 'pending_verification.json')
                except:
                    pass
        except Exception as e:
            print(f"[VERIFY] 검증 실패: {e}")
            # 검증 실패해도 발행 결과 기반으로 이메일 발송 (검증 실패 표시)
            try:
                for gk in all_results:
                    all_results[gk]['verify_status'] = f'검증 실패: {str(e)[:50]}'
                    all_results[gk]['verify_ok'] = False
                subject, text, html = build_verified_email(all_results, checked_at, format_kst_datetime())
                send_email_notification(subject, text, html)
            except Exception as e2:
                print(f"[VERIFY] 이메일 발송도 실패: {e2}")
                send_jandi_notification("🚨 [긴급] 쿠폰 검증 + 이메일 발송 실패", f"오류: {str(e)}\n이메일 오류: {str(e2)}", "red")
    
    t = threading.Thread(target=_delayed_verify, daemon=True)
    t.start()
    print(f"[VERIFY] 검증 스레드 시작됨 ({COUPON_VERIFY_DELAY_SECONDS//60}분 후 실행)")


def _verify_coupon_application(all_results, config):
    """쿠팡 API로 각 그룹별 쿠폰 활성 여부 검증 (쿠폰명 매칭 방식)"""
    api = CoupangAPI(config)
    
    # 현재 활성 쿠폰 전체 조회
    coupons_response = api.get_coupons("APPLIED")
    active_coupons = []
    if coupons_response.get('success'):
        data = coupons_response.get('data', {})
        if isinstance(data, dict):
            inner = data.get('data', data)
            if isinstance(inner, dict):
                active_coupons = inner.get('content', [])
            elif isinstance(inner, list):
                active_coupons = inner
        elif isinstance(data, list):
            active_coupons = data
    
    print(f"[VERIFY] 활성 쿠폰 {len(active_coupons)}개 조회됨")
    
    groups = config.get('product_groups', {})
    
    for group_key, gr in all_results.items():
        if not gr.get('applied'):
            gr['verify_status'] = '발행 안 함'
            gr['verify_ok'] = None
            continue
        
        group_config = groups.get(group_key, {})
        coupon_name = group_config.get('coupon_name', group_config.get('name', ''))
        products = group_config.get('products', {})
        
        verified = 0
        failed = 0
        details = []
        
        bottle_labels = {'1bottle': '1병', '3bottle': '3병', '6bottle': '6병'}
        
        for pk in ['1bottle', '3bottle', '6bottle']:
            product = products.get(pk)
            if not product or not product.get('vendor_item_id'):
                continue
            
            label = bottle_labels.get(pk, pk)
            
            # 쿠폰명에 그룹명+병수가 포함된 활성 쿠폰 찾기
            found = None
            for c in active_coupons:
                c_name = c.get('promotionName', c.get('title', ''))
                c_status = c.get('status', c.get('couponStatus', ''))
                
                if c_status == 'APPLIED' and coupon_name in c_name and label in c_name:
                    found = c
                    break
            
            if found:
                discount = found.get('discount', found.get('discountValue', 0))
                verified += 1
                details.append(f"✅ {product.get('name', pk)}: 쿠폰 활성 (할인 {int(discount):,}원, ID:{found.get('couponId')})")
            else:
                failed += 1
                details.append(f"❌ {product.get('name', pk)}: 쿠폰 미적용 ('{coupon_name} {label}' 패턴 미발견)")
        
        total = verified + failed
        if total == 0:
            gr['verify_status'] = '검증 대상 없음'
            gr['verify_ok'] = None
        elif failed == 0:
            gr['verify_status'] = f'✅ {verified}/{total} 쿠폰 확인'
            gr['verify_ok'] = True
        else:
            gr['verify_status'] = f'❌ {failed}/{total} 미적용'
            gr['verify_ok'] = False
        
        gr['verify_details'] = details
        print(f"[VERIFY] {group_key}: {gr['verify_status']} {details}")
    
    return all_results


def build_verified_email(groups_result, issued_at, verified_at):
    """검증 완료 후 이메일 HTML 생성"""
    
    # 전체 상태 판단
    any_fail = any(gr.get('verify_ok') == False for gr in groups_result.values())
    all_ok = all(gr.get('verify_ok') in (True, None) for gr in groups_result.values())
    
    if any_fail:
        subject = f"🚨 [가격관리] 쿠폰 적용 실패 감지 ({verified_at})"
        header_bg = '#b71c1c'
        header_text = '⚠️ 쿠폰 적용 실패 감지'
    else:
        subject = f"[가격관리] 자동 체크 결과 ({verified_at})"
        header_bg = '#1a1a2e'
        header_text = '🏷️ 가격관리 자동 체크 결과'
    
    rows = ""
    for gk, gr in groups_result.items():
        channel = gr.get('channel', 'C')
        name = f"[{channel}] {gr.get('name', gk)}"
        auto = '🤖 ON' if gr.get('auto_mode') else '⏸️ OFF'
        crawl = gr.get('crawl', '-')
        changed = '📊 변동!' if gr.get('price_changed') else '변동 없음'
        
        # 검증 결과 기반 상태 표시
        verify_ok = gr.get('verify_ok')
        if verify_ok is True:
            applied = f"✅ {gr.get('verify_status', '확인됨')}"
            row_color = '#e8f5e9'
        elif verify_ok is False:
            applied = f"❌ {gr.get('verify_status', '실패')}"
            row_color = '#ffebee'
        elif gr.get('auto_mode'):
            applied = gr.get('verify_status', gr.get('apply_error', '확인 불가'))
            row_color = '#fff3e0'
        else:
            applied = '— (auto OFF)'
            row_color = '#f5f5f5'
        
        rows += f"""<tr style="background:{row_color}">
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{name}</td>
            <td style="padding:8px;border:1px solid #ddd;text-align:center">{auto}</td>
            <td style="padding:8px;border:1px solid #ddd">{crawl}</td>
            <td style="padding:8px;border:1px solid #ddd">{changed}</td>
            <td style="padding:8px;border:1px solid #ddd">{applied}</td>
        </tr>"""
    
    # 실패 상세 정보
    fail_details_html = ""
    for gk, gr in groups_result.items():
        if gr.get('verify_ok') == False and gr.get('verify_details'):
            fail_details_html += f"<div style='margin:10px 0;padding:10px;background:#fff3e0;border-left:4px solid #f44336;border-radius:4px'>"
            fail_details_html += f"<b>{gr.get('name', gk)}</b><br>"
            fail_details_html += "<br>".join(gr['verify_details'])
            fail_details_html += "</div>"
    
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
        <div style="background:{header_bg};color:white;padding:20px;border-radius:10px 10px 0 0">
            <h2 style="margin:0">{header_text}</h2>
            <p style="margin:5px 0 0;opacity:0.8">발행: {issued_at} | 검증: {verified_at}</p>
        </div>
        <div style="padding:20px;background:white;border:1px solid #ddd">
            <table style="width:100%;border-collapse:collapse">
                <tr style="background:#1a1a2e;color:white">
                    <th style="padding:10px;text-align:left">그룹</th>
                    <th style="padding:10px;text-align:center">모드</th>
                    <th style="padding:10px">크롤링</th>
                    <th style="padding:10px">가격변동</th>
                    <th style="padding:10px">쿠폰 검증</th>
                </tr>
                {rows}
            </table>
            {fail_details_html}
        </div>
        <div style="padding:15px;background:#f8f9fa;border-radius:0 0 10px 10px;border:1px solid #ddd;border-top:0;font-size:12px;color:#666">
            <p>이 메일은 가격관리 시스템에서 자동 발송되었습니다. (쿠폰 발행 15분 후 검증 완료)</p>
            <p><a href="https://coupang-price-manager-221865276835.asia-northeast3.run.app">관리 대시보드 바로가기</a></p>
        </div>
    </div>"""
    
    # 텍스트 버전
    text = f"가격관리 자동 체크 결과 (발행: {issued_at}, 검증: {verified_at})\n\n"
    for gk, gr in groups_result.items():
        channel = gr.get('channel', 'C')
        name = f"[{channel}] {gr.get('name', gk)}"
        text += f"{name}: {gr.get('verify_status', '확인 불가')}\n"
    
    return subject, text, html


# ==================== 스케줄러 ====================

@app.route('/api/scheduled-check', methods=['GET', 'POST'])
def api_scheduled_check():
    """Cloud Scheduler가 호출하는 자동 체크 엔드포인트
    
    → auto_check_all()로 위임 (product_groups 기반 신규 로직)
    
    Cloud Scheduler 설정:
    - URL: https://your-cloud-run-url.run.app/api/scheduled-check
    - HTTP Method: POST
    - Frequency: 0 */2 9-22 * * (09~22시, 2시간마다)
    """
    return auto_check_all()


def scheduled_check_reminder():
    """스케줄러: 가격 체크 알림 + 쿠폰 자동 갱신"""
    config = load_config()
    if not config:
        return
    
    auto_mode = config['settings'].get('auto_mode', False)
    if not auto_mode:
        return
    
    price_method = config['settings'].get('price_method', 'coupon')
    competitor = config['competitors'].get('rokit_america', {})
    last_price = competitor.get('last_price', 0)
    
    if price_method == 'coupon' and last_price > 0:
        log_action("SCHEDULER", "쿠폰 상태 확인 시작")
        check_and_renew_coupons(config, last_price)
    else:
        send_jandi_notification(
            "⏰ [가격 체크 알림]",
            f"""정기 가격 체크 시간입니다!

🔍 마지막 확인 경쟁사 가격: {last_price:,}원

👉 경쟁사 가격 확인하기:
https://www.coupang.com/vp/products/7830558357

가격이 변동되었다면 시스템에 입력해주세요.""",
            "yellow"
        )
        log_action("SCHEDULER", "정기 가격 체크 알림 전송")


def check_and_renew_coupons(config, competitor_price):
    """쿠폰 상태 확인 후 만료/없으면 자동 재발행"""
    api = CoupangAPI(config)
    price_gap = config['settings'].get('price_gap', 300)
    hours = config['settings'].get('discount_hours', 5)
    contract_id = config.get('coupon_settings', {}).get('contract_id')
    
    if not contract_id:
        log_action("SCHEDULER", "계약서 ID가 설정되지 않아 쿠폰 갱신 스킵")
        send_jandi_notification(
            "⚠️ [쿠폰 갱신 실패]",
            "계약서 ID(contractId)가 설정되지 않았습니다.\n\n/api/contracts 조회 후 /api/save-contract-id로 저장하세요.",
            "yellow"
        )
        return
    
    coupons_result = api.get_coupon_list()
    active_coupons = {}
    
    if coupons_result.get('success') and coupons_result.get('data'):
        # 쿠폰 목록에서 vendorItemId로 활성 쿠폰 매핑
        data = coupons_result.get('data', {})
        if isinstance(data, dict):
            coupon_list = data.get('content', [])
        elif isinstance(data, list):
            coupon_list = data
        else:
            coupon_list = []
        
        for coupon in coupon_list:
            vendor_item_id = coupon.get('vendorItemId')
            if vendor_item_id:
                active_coupons[str(vendor_item_id)] = coupon
    
    renewed = []
    skipped = []
    
    products = [
        ('prime_nmn_1bottle_60', '1개입', 1, 0),
        ('prime_nmn_3bottle_60', '3개입', 3, config['settings'].get('pack3_extra_discount', 1)),
        ('prime_nmn_6bottle_60', '6개입', 6, config['settings'].get('pack6_extra_discount', 2)),
    ]
    
    price_direction = config['settings'].get('price_direction', 'lower')
    
    for product_key, label, multiplier, extra_discount in products:
        product = config['products'].get(product_key)
        if not product or not product.get('enabled'):
            continue
        
        vendor_item_id = str(product['vendor_item_id'])
        
        if vendor_item_id in active_coupons:
            skipped.append(f"{label}: 쿠폰 활성 중 ⏳")
            continue
        
        # 가격 방향에 따라 기준가 계산
        if price_direction == 'lower':
            base_price_1 = competitor_price - price_gap
        else:
            base_price_1 = competitor_price + price_gap
        
        base_price = base_price_1 * multiplier
        
        if extra_discount > 0:
            target_price = round(base_price * (1 - extra_discount / 100) / 10) * 10
        else:
            target_price = round(base_price / 10) * 10
        
        target_price = max(target_price, product['min_price'])
        target_price = min(target_price, product['max_price'])
        
        discount_amount = product['original_price'] - target_price
        result = api.create_instant_coupon(
            vendor_item_ids=[product['vendor_item_id']],
            discount_amount=discount_amount,
            contract_id=contract_id,
            hours=hours,
            title=f"{product['name']} {discount_amount:,}원 할인"
        )
        
        if result.get('success'):
            items_added = result.get('items_added', False)
            config['products'][product_key]['current_price'] = target_price
            
            if items_added:
                renewed.append(f"{label}: {target_price:,}원 ({discount_amount:,}원 할인) ✅")
            else:
                renewed.append(f"{label}: {target_price:,}원 ({discount_amount:,}원 할인) ⚠️ 상품 미연결")
            
            current_used = config['settings'].get('coupon_used', 0)
            config['settings']['coupon_used'] = current_used + discount_amount
        else:
            error_msg = result.get('error', '알 수 없는 오류')
            renewed.append(f"{label}: 발행 실패 ❌ ({error_msg})")
    
    save_config(config)
    
    if renewed:
        budget = config['settings'].get('coupon_budget', 500000)
        used = config['settings'].get('coupon_used', 0)
        
        # 상품 연결 실패 건 체크
        items_failed = sum(1 for r in renewed if '상품 미연결' in r)
        
        jandi_msg = f"""⏰ 스케줄러 실행 (쿠폰 갱신)

📊 기준 경쟁사 가격: {competitor_price:,}원
⏱️ 쿠폰 유효 시간: {hours}시간

🔄 재발행:
{chr(10).join(renewed)}

⏳ 유지 (활성 중):
{chr(10).join(skipped) if skipped else '없음'}

💳 쿠폰 예산: {used:,}원 / {budget:,}원 사용"""
        
        # 상품 연결 실패 건 있으면 경고 추가
        if items_failed > 0:
            jandi_msg += f"\n\n⚠️ **확인 조치 필요!**\n상품 연결 실패: {items_failed}건\n쿠팡 WING에서 쿠폰에 상품이 연결되었는지 확인하세요."
            send_jandi_notification("⚠️ [쿠폰 자동 갱신 - 확인 필요]", jandi_msg, "yellow")
        else:
            send_jandi_notification("🔄 [쿠폰 자동 갱신]", jandi_msg, "green")
        
        log_action("SCHEDULER", f"쿠폰 자동 재발행: {len(renewed)}개" + (f", 상품 연결 실패: {items_failed}건" if items_failed > 0 else ""))
    else:
        send_jandi_notification(
            "✅ [쿠폰 상태 정상]",
            f"모든 쿠폰이 활성 상태입니다.\n\n{chr(10).join(skipped)}",
            "blue"
        )
        log_action("SCHEDULER", "쿠폰 모두 활성 상태, 재발행 불필요")


# ==================== 서버 시작 시 자동 스케줄러 실행 ====================

def _startup_scheduled_check():
    """서버 시작 후 자동으로 스케줄러 실행 (배포 후 공백 방지)"""
    import time as _time
    _time.sleep(15)  # 서버 완전 기동 대기
    
    try:
        print("[STARTUP] 서버 시작 - 자동 스케줄러 실행 시작")
        with app.app_context():
            with app.test_request_context():
                result = api_scheduled_check()
                if hasattr(result, 'get_json'):
                    data = result.get_json()
                else:
                    data = result
                success = data.get('success', False) if isinstance(data, dict) else False
                print(f"[STARTUP] 자동 스케줄러 결과: {'성공' if success else '실패'}")
    except Exception as e:
        print(f"[STARTUP] 자동 스케줄러 실행 실패: {e}")
        # 실패 시 잔디 알림
        try:
            send_jandi_notification(
                "⚠️ [서버 시작] 자동 스케줄러 실패",
                f"서버 시작 후 자동 스케줄러 실행이 실패했습니다.\n오류: {str(e)[:200]}\n\n수동 확인 필요: /api/scheduled-check",
                "red"
            )
        except:
            pass

# Cloud Run 환경에서만 시작 시 자동 실행 (로컬 개발 제외)
if os.environ.get('K_SERVICE'):
    _startup_thread = threading.Thread(target=_startup_scheduled_check, daemon=True)
    _startup_thread.start()
    print("[STARTUP] 자동 스케줄러 백그라운드 스레드 시작됨 (15초 후 실행)")


# ==================== 메인 ====================

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Coupang Price Manager Server")
    print("=" * 60)
    
    try:
        config = load_config()
        if config:
            print(f"   Vendor ID: {config['api']['vendor_id']}")
            print(f"   Check Interval: {config['settings'].get('check_interval', 4)}h")
            print(f"   Discount Hours: {config['settings'].get('discount_hours', 5)}h")
            print(f"   Auto Mode: {'ON' if config['settings'].get('auto_mode', False) else 'OFF'}")
        else:
            print("   WARNING: config.json not found, using defaults")
    except Exception as e:
        print(f"   WARNING: Config load error: {e}")
        config = None
    
    # Cloud Run 환경 감지 - 스케줄러 비활성화
    # Cloud Run에서는 Cloud Scheduler + Cloud Tasks 사용 권장
    is_cloud_run = os.environ.get('K_SERVICE') is not None
    
    if is_cloud_run:
        print("   Running on Cloud Run - Scheduler disabled")
    else:
        print("   Running locally - Scheduler disabled (use Cloud Scheduler for production)")
    
    # Cloud Run 포트
    port = int(os.environ.get('PORT', 8080))
    
    print(f"\n   Server starting on port {port}...")
    print("=" * 60 + "\n")
    
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
