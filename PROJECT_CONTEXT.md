# C사 가격관리 시스템 — 프로젝트 컨텍스트

> **이 문서는 코워크(Cowork)가 프로젝트를 이해하고 작업을 이어갈 수 있도록 작성되었습니다.**
> v33.9 | 2026-03-13 기준

---

## 1. 프로젝트 개요

C사(쿠팡)에서 판매하는 본사이언스(BonScience) 제품의 가격을 경쟁사 대비 자동으로 관리하는 시스템입니다.

**핵심 동작:** 4시간마다 → 경쟁사 가격 크롤링 → 즉시할인쿠폰 자동 발행 → 상품 연결 → 잔디+이메일 결과 발송

**관리 중인 제품:**
- 레스베라트롤 (1병/3병/6병) — 경쟁사: 닥터스베스트 트랜스 레스베라트롤
- PRIME NMN 60정 (1병/3병/6병) — 경쟁사: 로킷아메리카 NMN

---

## 2. 아키텍처

```
GitHub (justin211a/coupang-price-manager, private)
    │ push
    ▼
GitHub Actions → 자동 Cloud Run 배포
    │
    ▼
Cloud Run (asia-northeast3)
├── Flask 서버 (server.py ~4,000줄)
├── React SPA (index.html)
├── VPC Connector + Cloud NAT → 고정 IP: 34.22.68.152
└── GCS Bucket (coupang-price-manager-config) → config.json 저장
    │
    ├── 쿠팡 WING API (HMAC 인증) — 쿠폰 생성/파기/상품연결/가격조회
    ├── ScraperAPI — 쿠팡 봇 차단 우회 크롤링
    ├── Cloud Scheduler — 4시간마다 /api/auto-check-all 호출
    ├── JANDI 웹훅 — 실시간 알림
    └── Apps Script 웹 앱 — 이메일 발송 (5명)
```

---

## 3. 접속 정보

| 항목 | 값 |
|------|-----|
| 관리 대시보드 | https://coupang-price-manager-221865276835.asia-northeast3.run.app |
| GitHub Repo | justin211a/coupang-price-manager (private) |
| GitHub PAT | [GITHUB_PAT - 대표님에게 문의] |
| WING Access Key | b2b6748d-9588-47de-954f-2cef119d1240 |
| WING Secret Key | [SECRET_KEY - 대표님에게 문의] |
| Vendor ID | A00479438 |
| Contract ID | 173582 |
| 고정 IP | 34.22.68.152 (쿠팡 WING에 등록됨) |
| GCS Bucket | coupang-price-manager-config |
| GCP Project | novatra-test |
| ScraperAPI Key | [SCRAPER_API_KEY - 대표님에게 문의] |
| 잔디 웹훅 | https://wh.jandi.com/connect-api/webhook/18891904/0afe7bcc161e91ead2a9297c2d242ad0 |
| 이메일 웹훅 (Apps Script) | https://script.google.com/macros/s/AKfycbwKFSLRLF8SP6h1sAu7lW2M_YW06hdd9E0ddqmLl6DjWujqzANuFbvka7Z52MHfdjKQgw/exec |
| 허용 로그인 | justin@terabiotech.com, shjung4196@gmail.com |
| 이메일 수신자 | jun, andrew, reina, justin, mj.jeong @terabiotech.com |

---

## 4. 제품 그룹 설정 (config.json 기준)

### 4.1 레스베라트롤

| 설정 | 값 |
|------|-----|
| auto_mode | ON |
| 경쟁사 | 닥터스베스트 트랜스 레스베라트롤 |
| 경쟁사 URL | https://www.coupang.com/vp/products/10217488?vendorItemId=88949153035 |
| price_gap | 1,300원 (경쟁사보다 낮게) |
| price_direction | lower |
| discount_hours | 4.25 (4시간 15분) |
| pack3_extra_discount | 3% |
| pack6_extra_discount | 7% |
| coupon_name | "본사이언스 레스베라트롤" |

| 옵션 | vendorItemId | 정가 | min_price | max_price |
|------|------------|------|-----------|-----------|
| 1병 | 92628641615 | 55,000 | 20,000 | 54,000 |
| 3병 | 92628641625 | 165,000 | 50,000 | 164,000 |
| 6병 | 92628641605 | 330,000 | 100,000 | 329,000 |

### 4.2 PRIME NMN 60정

| 설정 | 값 |
|------|-----|
| auto_mode | ON |
| 경쟁사 | 로킷아메리카 NMN |
| price_gap | 1,500원 |
| discount_hours | 4.25 |
| coupon_name | "프라임 NMN 60정" |

| 옵션 | vendorItemId | 정가 | min_price | max_price |
|------|------------|------|-----------|-----------|
| 1병 | 92195127705 | 80,000 | 47,500 | 67,500 |
| 3병 | 92195127720 | 240,000 | 139,000 | 158,500 |
| 6병 | 92195127713 | 480,000 | 275,000 | 285,000 |

**주의:** "프라임 NMN 증량판" 쿠폰(72정 제품)은 별도 수동 관리. 자동 시스템이 절대 건드리지 않음. "증량판"/"증량" 키워드가 포함된 쿠폰은 보호 대상.

---

## 5. 자동 실행 흐름 (auto-check-all)

Cloud Scheduler가 4시간마다 `POST /api/auto-check-all`을 호출하면:

1. 각 제품 그룹 순회
2. **경쟁사 가격 크롤링** — ScraperAPI → 쿠팡 페이지에서 `final-price-amount` 클래스 파싱
3. 이전 가격과 비교 → 변동 시 잔디 알림
4. auto_mode ON인 그룹만 → **기존 쿠폰 전부 파기** (cleanup_group_coupons)
   - coupon_name이 포함된 쿠폰만 파기 (예: "프라임 NMN 60정"이 이름에 있는 쿠폰)
   - "증량판" 키워드 있으면 무조건 보호
   - "2천원"/"3천원"/"5천원"/"1만원" 고정 쿠폰도 보호
5. **WING API로 실제 쿠팡 판매가 조회** (get_inventory)
6. 목표가 계산: `(경쟁사가격 - price_gap) × 병수 × (1 - 추가할인%)`
7. **할인금액 = 실제판매가 - 목표가** (config 정가가 아닌 실제 판매가 기준!)
8. 안전장치: min_price ~ max_price 범위 제한
9. 쿠폰 생성 (v2 API) → 상품 연결 (v1 add_coupon_items)
10. [CIR08] 충돌 시 → 충돌 쿠폰 자동 파기 → 재시도
11. 결과 잔디 알림 + 이메일 발송 (5명)

### 쿠폰 유효시간
- 설정: 4시간 15분 (discount_hours: 4.25)
- Scheduler 주기: 4시간
- 15분 겹쳐서 공백 없음

### 쿠폰 이름 규칙
- 레스베라트롤: "본사이언스 레스베라트롤 {N}병 {할인금액:,}원"
- NMN 60정: "프라임 NMN 60정 {N}병 {할인금액:,}원"
- cleanup 시 coupon_name("본사이언스 레스베라트롤" 또는 "프라임 NMN 60정")이 포함된 것만 파기

---

## 6. 주요 API 엔드포인트

| 엔드포인트 | 설명 |
|-----------|------|
| `POST /api/auto-check-all` | 전체 자동화 (Cloud Scheduler가 호출) |
| `POST /api/product-groups/{key}/apply-prices` | 특정 그룹 수동 쿠폰 발행 |
| `POST /api/product-groups/{key}/cleanup-coupons` | 특정 그룹 기존 쿠폰 파기 |
| `POST /api/product-groups/{key}/crawl-competitors` | 경쟁사 가격 크롤링 |
| `POST /api/product-groups/{key}/sync-prices` | 실제 판매가 조회 |
| `POST /api/product-groups/{key}/update-original-prices` | 정가(취소선) 변경 |
| `GET /api/config` | 전체 설정 조회 |
| `POST /api/config` | 설정 변경 (병합 방식 — 주의: products 하위를 변경할 때 전체 products를 보내야 함) |
| `GET /api/coupons` | 활성 쿠폰 목록 (WING 조회) |
| `GET /api/logs` | 서버 로그 |
| `GET /api/version` | 서버 버전 |
| `POST /api/debug/test-add-items/{couponId}/{vendorItemId}` | 상품 연결 테스트 |

---

## 7. 배포 방법

```bash
# Claude가 직접 배포하는 워크플로우:
# 1. server.py 또는 index.html 수정
# 2. GitHub push (PAT 사용)
git remote set-url origin https://[GITHUB_PAT - 대표님에게 문의]@github.com/justin211a/coupang-price-manager.git
git add .
git commit -m "vXX.X: 변경 설명"
git push

# 3. GitHub Actions가 자동으로 Cloud Run 배포
# 4. 약 2분 후 /api/version으로 확인
```

대표님이 수동 배포할 일은 없음. Claude(또는 코워크)가 push하면 자동 배포.

---

## 8. 쿠팡 WING API 핵심 정보

### 인증 방식
HMAC-SHA256 서명. `CEA algorithm=HmacSHA256, access-key={AK}, signed-date={datetime}, signature={sig}` 헤더.

### 주요 API
- 쿠폰 생성: `POST /v2/providers/seller_api/apis/api/v1/seller/instant-discount-coupons` (v2)
- 상품 연결: `POST /v2/providers/seller_api/apis/api/v1/seller/instant-discount-coupons/{couponId}/items` (v1)
- 쿠폰 파기: `PUT /v2/providers/seller_api/apis/api/v1/seller/instant-discount-coupons/{couponId}` + action=expire (v1)
- 쿠폰 목록: `GET /v2/providers/seller_api/apis/api/v1/seller/instant-discount-coupons` (v1)
- 재고/가격 조회: `GET /v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/inventories`
- 정가 변경: `PUT /v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/prices/{originalPrice}`

### [CIR08] 에러 대응
"The option has already been issued to another coupon (XXXXXXXX)" → 충돌 쿠폰 ID 추출 → _is_fixed_coupon 판별 → 자동 쿠폰이면 파기 후 재시도, 고정 쿠폰이면 잔디 알림 (수동 해제 필요)

---

## 9. 쿠팡 빨간 할인 딱지 관련

- 쿠팡은 상품별 "제안가(추천 할인가)"를 내부적으로 산출
- WING > 할인쿠폰 관리에서 확인 가능
- 최종 할인가가 제안가 이하여야 빨간 할인 딱지가 표시됨
- 현재 NMN 3병 max_price=158,500원, 6병 max_price=285,000원은 쿠팡 제안가에 맞춘 값
- 쿠폰 적용 후 페이지 반영에 10~30분 소요 (정상)
- 정가를 너무 높게 설정하면 쿠팡이 할인 표시를 안 해줄 수 있음

---

## 10. config.json 변경 시 주의사항

`POST /api/config`는 **병합(merge)** 방식이지만, products 하위 키를 부분 변경하면 나머지 필드가 사라질 수 있음.

```
# ❌ 잘못된 방법 (vendor_item_id 등이 사라짐)
{"product_groups": {"prime_nmn": {"products": {"3bottle": {"max_price": 158500}}}}}

# ✅ 올바른 방법 (전체 products를 보냄)
{"product_groups": {"prime_nmn": {"products": {
  "1bottle": {"name": "1개입", "vendor_item_id": 92195127705, "original_price": 80000, "min_price": 47500, "max_price": 67500},
  "3bottle": {"name": "3개입", "vendor_item_id": 92195127720, "original_price": 240000, "min_price": 139000, "max_price": 158500},
  "6bottle": {"name": "6개입", "vendor_item_id": 92195127713, "original_price": 480000, "min_price": 275000, "max_price": 285000}
}}}}
```

---

## 11. 고정 쿠폰 (파기 불가, 수동 관리)

| 쿠폰 ID | 이름 | 만료일 |
|----------|------|--------|
| 89782867 | 본사이언스 2천원 할인쿠폰 | 2026-03-31 |
| 89783158 | 본사이언스 5천원 할인쿠폰 | 2026-03-31 |
| 89783187 | 피크하이트 5천원 할인쿠폰 | 2026-03-31 |
| 89783241 | 본사이언스 1만원 할인쿠폰 | 2026-03-31 |
| 89783363 | 본사이언스 3천원 할인쿠폰 | 2026-03-31 |
| 90195093 | 프라임 NMN 증량판 6병 쿠폰 | 2026-04-10 |

---

## 12. Cloud Scheduler 관리

```bash
# 현재 설정: 매 4시간 정각 (0시, 4시, 8시, 12시, 16시, 20시)
# GCP Console: https://console.cloud.google.com/cloudscheduler?project=novatra-test

# 일시정지
gcloud scheduler jobs pause coupang-auto-check --location=asia-northeast3 --project=novatra-test

# 재개
gcloud scheduler jobs resume coupang-auto-check --location=asia-northeast3 --project=novatra-test

# 수동 실행
gcloud scheduler jobs run coupang-auto-check --location=asia-northeast3 --project=novatra-test

# 시간 변경
gcloud scheduler jobs update http coupang-auto-check --location=asia-northeast3 --schedule="0 */4 * * *" --project=novatra-test

# 삭제
gcloud scheduler jobs delete coupang-auto-check --location=asia-northeast3 --project=novatra-test
```

---

## 13. ScraperAPI 정보

- 무료 5,000건 (하루 6건 사용, 약 27개월분)
- render=false (JS 실행 안 함, 1요청 = 1크레딧)
- 대시보드: https://dashboard.scraperapi.com
- 쿠팡 가격 파싱: `final-price-amount` 클래스에서 추출

---

## 14. 트러블슈팅

### 쿠폰 상품 연결 실패 [CIR08]
- 원인: 상품이 이미 다른 쿠폰에 연결 (쿠팡 1상품 1쿠폰 규칙)
- 자동 해결: 충돌 쿠폰이 자동생성이면 파기 후 재시도
- 수동 필요: 고정 쿠폰(2천원 등)이면 WING에서 수동 해제

### 가격이 의도와 다르게 표시
- v33.4에서 수정: 쿠폰 할인은 config 정가가 아닌 **실제 판매가**에서 빠짐
- 항상 `get_inventory`로 실제 판매가를 조회한 후 할인금액 계산

### 크롤링 실패
- ScraperAPI 크레딧 소진 확인
- 쿠팡 페이지 구조 변경 시 파싱 로직 수정 필요

### 잔디 야간 모드
- 23:00 ~ 09:00 (KST)에는 잔디 알림 자동 스킵

---

## 15. 버전 히스토리 (v33 시리즈)

| 버전 | 날짜 | 변경 내용 |
|------|------|----------|
| v33.1 | 03-12 | add_coupon_items 복원 + [CIR08] 충돌 자동 해결 |
| v33.2 | 03-12 | 고정 쿠폰 판별 개선 |
| v33.3 | 03-12 | 그룹별 기존 쿠폰 자동 정리 + UI 버튼 |
| **v33.4** | 03-12 | **[CRITICAL] 실제 판매가 기준 할인 계산** |
| v33.5 | 03-12 | ScraperAPI 경쟁사 크롤링 + auto-check-all |
| v33.6 | 03-12 | 이메일 알림 + 파비콘 |
| v33.7 | 03-12 | 정가 변경 API |
| v33.8 | 03-12 | config 수정 |
| **v33.9** | 03-12 | 증량판 쿠폰 보호 + coupon_name 정확 매칭 + 병수 표시 |

---

## 16. 개발 원칙 (대표님과의 약속)

1. **Claude(또는 코워크)가 개발 책임자** — 코드 작성, 테스트, 버그 수정, 품질 보증, 배포
2. **대표님은 최종 의사결정자** — 방향 결정, 승인, 최종 검증
3. 확인할 사항이 있으면 **먼저 확인하고 승인받은 후 개발 시작** (토큰 낭비 방지)
4. **무조건 동의 금지** — 대안과 잠재적 문제점 제시 후 대표님이 결정
5. **논리적 오류가 있는 코드 전달 금지** — 대표님에게 디버깅 요청하지 않음
6. 배치 파일(.bat)에 한글 사용 금지
7. 가격/시세 추정치 사용 금지 — 실제 API 조회 필수
8. 대표님을 "대표님"으로 호칭 (사장님 X)

---

## 17. 미완료 작업 (로드맵)

### 단기
- [ ] PRIME NMN 120정 그룹 추가
- [ ] 디구 제품 그룹 추가
- [ ] Cloud Run 로그 자동 분석

### 중기
- [ ] 시간대별 자동 가격 조정 (예약 기능)
- [ ] 최적 가격 추천 AI (BigQuery 매출 데이터 기반)
- [ ] 매출 예측 분석

### 장기
- [ ] 네이버 스마트스토어 연동
- [ ] 11번가/Gmarket 연동
- [ ] 통합 가격 관리 대시보드 (전 채널)

### 기술 부채
- [ ] server.py 모듈 분리 (~4,000줄 단일 파일)
- [ ] 단위 테스트 추가
- [ ] config 병합 로직 개선 (부분 업데이트 시 데이터 손실 방지)

---

*이 문서는 Claude와 대표님의 협업으로 구축된 시스템의 전체 컨텍스트입니다. 새 세션이나 코워크에서 이 문서를 참조하면 즉시 작업을 이어갈 수 있습니다.*
