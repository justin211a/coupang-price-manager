# 쿠팡 가격 경쟁 관리 v32

## 주요 변경사항 (v31 → v32)

### 1. 제품 그룹 기반 구조
- 기존: 단일 제품 설정
- 변경: 여러 제품 그룹 관리 (NMN, 레스베라트롤 등)
- 각 그룹별 독립적인 설정 (오토모드, 쿠폰명, 할인율, 경쟁사 등)

### 2. 1+3+6병 통합 관리
- 한 화면에서 1병/3병/6병 동시 확인
- 전체 적용 버튼 하나로 1+3+6병 쿠폰 일괄 발행

### 3. 경쟁사 관리
- 제품당 최대 3개 경쟁사 URL 등록
- 자동 크롤링 + 수동 입력 지원
- 크롤링 실패 시 수동 입력 폴백

### 4. AI 마진 밸런스 분석
- 제품별 하루 1회 AI 분석
- 가격 시뮬레이션 테이블
- 최적 가격 추천

## 설치 및 실행

### 로컬 테스트
```bash
pip install -r requirements.txt
python server.py
```
http://localhost:8080 접속

### Cloud Run 배포
```bash
gcloud run deploy coupang-price-manager --source . --region asia-northeast3 --allow-unauthenticated
```

## API 엔드포인트 (새로 추가됨)

### 제품 그룹
- GET/POST `/api/product-groups` - 목록/생성
- GET/PUT/DELETE `/api/product-groups/<key>` - 조회/수정/삭제
- POST `/api/product-groups/<key>/activate` - 활성 그룹 변경

### 경쟁사
- GET/POST `/api/product-groups/<key>/competitors` - 조회/추가
- DELETE `/api/product-groups/<key>/competitors/<id>` - 삭제
- POST `/api/product-groups/<key>/crawl-competitors` - 크롤링
- POST `/api/product-groups/<key>/competitors/<id>/price` - 수동 입력

### 가격
- POST `/api/product-groups/<key>/sync-prices` - 쿠팡 가격 동기화
- POST `/api/product-groups/<key>/apply-prices` - 쿠폰 일괄 발행

### AI 인사이트
- GET `/api/product-groups/<key>/ai-insight` - 조회 (캐시)
- POST `/api/product-groups/<key>/ai-insight` - 생성 (하루 1회)

## config.json 구조

```json
{
  "api": { "access_key": "...", "secret_key": "...", "vendor_id": "..." },
  "global_settings": { "jandi_webhook": "...", "contract_id": "..." },
  "product_groups": {
    "prime_nmn": {
      "name": "PRIME NMN 60정",
      "auto_mode": false,
      "coupon_name": "...",
      "price_gap": 1100,
      "price_direction": "lower",
      "discount_hours": 4,
      "pack3_extra_discount": 3,
      "pack6_extra_discount": 7,
      "estimated_cost": 25000,
      "products": {
        "1bottle": { "vendor_item_id": 92195127705, ... },
        "3bottle": { ... },
        "6bottle": { ... }
      },
      "competitors": [ { "id": "...", "url": "...", "last_price": 52720 } ],
      "ai_insight": { "last_date": "2026-03-11", "result": { ... } }
    }
  },
  "active_group": "prime_nmn"
}
```

## 파일 구조

```
v32-refactor/
├── server.py        # Flask 서버 (3200+ lines)
├── index.html       # 프론트엔드 UI
├── config.json      # 설정 파일
├── requirements.txt # Python 의존성
├── Dockerfile       # Cloud Run용
└── deploy.bat       # Windows 배포 스크립트
```

## 주의사항

1. **쿠팡 쿠폰 적용 지연**: 쿠폰 발행 후 실제 적용까지 최대 30분 소요
2. **AI 분석 하루 1회**: 비용 절감을 위해 제품당 하루 1회로 제한
3. **크롤링 실패 가능**: 쿠팡 페이지 구조 변경 시 수동 입력 필요

## 버전

- v32 (2026-03-11): 제품 그룹 구조, AI 인사이트
- v31: 기본 기능 안정화
- v30: 초기 버전
# Auto-deploy test 1773281951
