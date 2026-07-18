#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
be_floor_producer.py — 동적 원가바닥(BE floor) 생산자 (클로비 전용, BQ 권한 필요)

목적:
  라이브 서비스(server.py)가 매 사이클 BQ를 치지 않도록, 클로비(BQ 권한 보유)가
  각 config 그룹의 손익분기(BE) 원가바닥을 계산해 GCS config.json에 심는다.
  서비스는 그 값을 하한(effective_floor = max(min_price, be_floor))으로 읽기만 한다.

BE 공식 (그룹×병수 조합별):
  BE = ceil( (product_pricing.sales_cost_krw × 병수 + product_shipping.shipping_krw[병수]) / (1 - 0.117) / 100 ) × 100
  - 0.117 = 쿠팡 판매수수료 (수수료 차감 후에도 원가+배송을 회수해야 손익분기)
  - shipping_krw = product_shipping (sku, quantity=병수, product_type='single') 실측.
    해당 병수 미등록 시 최대 병수의 shipping 사용(fallback), 그래도 없으면 1병 shipping.
  - 그룹의 be_floor = 각 병수별 BE 중, 실제 config에 존재하는 병수만 dict로 기록(선택)
    + 그룹 대표값(1병 기준 or 최소 병수) 는 서비스가 product_key별로 매칭.

  ※ 서비스(server.py) 소비 형태: 그룹 config에 be_floor_map = {"1bottle": N, "3bottle": M, ...}
    형태로 병수(bottle key)별 BE floor 를 심고, be_floor_updated(ISO ts) 를 함께 기록.

경로 예시의 백슬래시는 문서용(raw). 코드에는 영향 없음.

이번 단계(2026-07-13): DRY-RUN 전용.
  - GCS config 는 READ 만. WRITE 금지(--apply 주어도 이번엔 안전차단 필요 시 사용).
  - 기본 실행 = 계산표 출력 + 감사 대조. write 안 함.

매핑(2026-07-13 감사 확정, 이름 기반):
  config 그룹 키 → product_pricing.sku

실행:
  # dry-run (기본, write 안 함) — 클로비/로컬 어디서나 검증용
  set GOOGLE_APPLICATION_CREDENTIALS=C:\...\sa-key.json
  python be_floor_producer.py

  # 실주입은 이번 단계 금지. 승인 후 main() 의 [SAFETY] 차단 라인을 제거하고:
  #   python be_floor_producer.py --apply --allow-write

클로비 일일 스케줄(등록은 이번 단계 안 함 — 승인 후):
  schtasks /Create /TN "BE_FloorProducer" /SC DAILY /ST 11:30 ^
    /TR "cmd /c set GOOGLE_APPLICATION_CREDENTIALS=C:\path\sa-key.json && python C:\Dev\coupang-price-manager\be_floor_producer.py --apply --allow-write"
  # 11:30 = 자동 가격조정 사이클 이전. be_floor 를 최신화한 뒤 서비스가 읽도록.
  # server.py BE_FLOOR_MAX_AGE_HOURS=48 이므로, 하루라도 스킵되면 이틀차까진 유효,
  # 이틀 초과 결손 시 서비스가 HOLD(자동조정 정지) 로 안전하게 fail.
"""

import os
import sys
import json
import math
import argparse
from datetime import datetime, timezone, timedelta

# stdout UTF-8 (Windows 콘솔 대응)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---- 설정 상수 --------------------------------------------------------------
BQ_PROJECT = "novatra-test"
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET", "coupang-price-manager-config")
GCS_CONFIG_PATH = "config.json"
GCS_BACKUP_PREFIX = "backups/config"          # backups/config-YYYYMMDD-HHMMSS.json
COUPANG_FEE_RATE = 0.117                        # 쿠팡 판매수수료
PRICING_TABLE = f"{BQ_PROJECT}.warehouse.product_pricing"
SHIPPING_TABLE = f"{BQ_PROJECT}.warehouse.product_shipping"

KST = timezone(timedelta(hours=9))

# 그룹 키 → product_pricing.sku (감사 확정 매핑)
GROUP_SKU_MAP = {
    "prime_nmn": "PRN60",                 # PRIME NMN 60정
    "resveratrol": "PRR",                 # 레스베라트롤
    "prime_피세틴": "PRF",                 # 본사이언스 피세틴
    "프라임_스페르미딘": "PRS",             # 프라임 스페르미딘
    "프라임_베르베린": "PRBB",             # 프라임 베르베린
    "prime_nmn_tera": "PRN60",            # 프라임NMN(테라)
    "prime_berberine_tera": "PRBB",       # 프라임 베르베린(테라)
    "prime_fisetin_tera": "PRF",          # 프라임 피세틴(테라)
    "prime_resveratrol_tera": "PRR",      # 프라임 트랜스 레스베라트롤(테라)
    "prime_spermidine_tera": "PRS",       # 프라임 스페르미딘(테라)
    "prime_magnesium_tera": "PRMG",       # 프라임 마그네슘(테라)
    "prime_brain_tera": "PRB",            # 프라임브레인(테라)
    # ARTA 국내 3제품 (2026-07-18 섀도우 온보딩, enabled=false)
    "alphacd_arta": "KAPCD",              # 알파CD 14P (ARTA)
    "meladouble_arta": "KMLDB",           # 멜라더블 60캡슐 (ARTA)
    "calciumjelly_arta": "PRCJ",          # 프라임 키즈 칼슘젤리 (ARTA, 건기식)
}


# ---- 병수 추출 (server.py _get_multiplier 와 동일 규칙) ----------------------
import re
def get_multiplier(pk, pv):
    if isinstance(pv, dict) and pv.get("multiplier"):
        return int(pv["multiplier"])
    m = re.search(r"(\d+)", pk or "")
    return int(m.group(1)) if m else 1


# ---- BQ 조회 ----------------------------------------------------------------
def get_bq_client():
    from google.cloud import bigquery
    return bigquery.Client(project=BQ_PROJECT)


def fetch_pricing(client):
    """{sku: sales_cost_krw} — 영업원가. NULL 이면 제외(가드에서 처리)."""
    q = f"SELECT sku, sales_cost_krw, cogs_krw FROM `{PRICING_TABLE}`"
    out = {}
    for r in client.query(q).result():
        out[r["sku"]] = {
            "sales_cost_krw": r.get("sales_cost_krw"),
            "cogs_krw": r.get("cogs_krw"),
        }
    return out


def fetch_shipping(client):
    """{sku: {qty: shipping_krw}} + {sku: max_qty_shipping} — product_type='single'."""
    q = (
        f"SELECT sku, quantity, shipping_krw FROM `{SHIPPING_TABLE}` "
        f"WHERE product_type = 'single'"
    )
    ship = {}
    for r in client.query(q).result():
        sku = r["sku"]
        qty = r["quantity"]
        val = r.get("shipping_krw")
        if sku is None or qty is None or val is None:
            continue
        ship.setdefault(sku, {})[int(qty)] = int(val)
    return ship


def shipping_for(ship_map, sku, qty):
    """병수 qty 의 배송비. 정확 매칭 우선, 없으면 최대 qty(fallback), 그래도 없으면 None."""
    m = ship_map.get(sku)
    if not m:
        return None, "none"
    if qty in m:
        return m[qty], "exact"
    # fallback: 가장 큰 등록 qty 의 배송비 (보수적으로 최댓값 사용)
    max_q = max(m.keys())
    return m[max_q], f"fallback_q{max_q}"


# ---- BE 계산 ----------------------------------------------------------------
def compute_be(sales_cost_krw, bottles, shipping_krw):
    """BE = ceil((sales_cost×병수 + shipping) / (1-fee) / 100) * 100"""
    if sales_cost_krw is None:
        return None
    if shipping_krw is None:
        return None
    raw = (sales_cost_krw * bottles + shipping_krw) / (1.0 - COUPANG_FEE_RATE)
    return int(math.ceil(raw / 100.0) * 100)


# ---- GCS ----------------------------------------------------------------
def load_config_from_gcs():
    from google.cloud import storage
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(GCS_CONFIG_PATH)
    if not blob.exists():
        raise RuntimeError(f"config not found: gs://{GCS_BUCKET_NAME}/{GCS_CONFIG_PATH}")
    blob.reload()
    generation = blob.generation
    cfg = json.loads(blob.download_as_text())
    return cfg, generation


def write_config_to_gcs(cfg, expected_generation):
    """read-modify-write with generation guard + backup. (이번 단계에선 호출 금지)"""
    from google.cloud import storage
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET_NAME)
    # 1) backup 현재본
    ts = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    backup_blob = bucket.blob(f"{GCS_BACKUP_PREFIX}-{ts}.json")
    cur_blob = bucket.blob(GCS_CONFIG_PATH)
    cur_blob.reload()
    backup_blob.upload_from_string(cur_blob.download_as_text(), content_type="application/json")
    # 2) generation guard write
    payload = json.dumps(cfg, ensure_ascii=False, indent=2)
    cur_blob.upload_from_string(
        payload,
        content_type="application/json",
        if_generation_match=expected_generation,
    )
    return backup_blob.name


# ---- 메인 계산 로직 ----------------------------------------------------------
def build_be_table(cfg, pricing, shipping):
    """각 그룹×병수 BE 계산. returns (rows, group_floor_maps, warnings)."""
    rows = []
    group_floor_maps = {}   # {group_key: {bottle_key: be}}
    warnings = []
    groups = cfg.get("product_groups", {})
    for gk, g in groups.items():
        sku = GROUP_SKU_MAP.get(gk)
        floor_map = {}
        if sku is None:
            warnings.append(f"[MAP-MISS] 그룹 '{gk}' SKU 매핑 없음 → BE 계산 스킵")
            group_floor_maps[gk] = {}
            continue
        p = pricing.get(sku)
        sales_cost = p.get("sales_cost_krw") if p else None
        if sales_cost is None:
            warnings.append(f"[COST-NULL] 그룹 '{gk}' (sku={sku}) sales_cost_krw NULL → BE 계산 스킵")
            group_floor_maps[gk] = {}
            continue
        prods = g.get("products", {})
        for pk, pv in sorted(prods.items(), key=lambda x: get_multiplier(x[0], x[1])):
            bottles = get_multiplier(pk, pv)
            ship_val, ship_src = shipping_for(shipping, sku, bottles)
            if ship_val is None:
                warnings.append(f"[SHIP-MISS] 그룹 '{gk}' (sku={sku}) {pk}(x{bottles}) 배송비 없음 → 스킵")
                continue
            be = compute_be(sales_cost, bottles, ship_val)
            min_price = pv.get("min_price") or 0
            eff = max(min_price, be) if be is not None else min_price
            gap = (min_price - be) if be is not None else None
            rows.append({
                "group": gk, "sku": sku, "product_key": pk, "bottles": bottles,
                "sales_cost": sales_cost, "shipping": ship_val, "ship_src": ship_src,
                "be_floor": be, "min_price": min_price,
                "effective_floor": eff,
                "min_below_be": (min_price < be) if be is not None else None,
                "gap_min_minus_be": gap,
                "auto_mode": g.get("auto_mode"),
            })
            floor_map[pk] = be
        group_floor_maps[gk] = floor_map
    return rows, group_floor_maps, warnings


def print_table(rows):
    hdr = (f"{'group':22} {'sku':6} {'pkey':9} {'n':>2} "
           f"{'sales_cost':>10} {'ship':>6}({'src':<11}) {'BE_floor':>9} "
           f"{'min_price':>9} {'eff_floor':>9} {'min<BE?':>7} {'auto':>5}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        flag = "⚠YES" if r["min_below_be"] else "ok"
        print(f"{r['group']:22} {r['sku']:6} {r['product_key']:9} {r['bottles']:>2} "
              f"{r['sales_cost']:>10} {r['shipping']:>6}({r['ship_src']:<11}) {r['be_floor']:>9} "
              f"{r['min_price']:>9} {r['effective_floor']:>9} {flag:>7} {str(r['auto_mode']):>5}")


def audit_cross_check(rows):
    """감사 대조: 스페르미딘/베르베린/spermidine_tera 그룹 BE 재확인."""
    targets = {"프라임_스페르미딘", "프라임_베르베린", "prime_spermidine_tera"}
    print("\n=== 감사 대조 (스페르미딘/베르베린/spermidine_tera) ===")
    cnt = 0
    for r in rows:
        if r["group"] in targets:
            cnt += 1
            print(f"  {r['group']:22} {r['product_key']:9} x{r['bottles']} "
                  f"sales_cost={r['sales_cost']} ship={r['shipping']} → BE={r['be_floor']} "
                  f"(min={r['min_price']}, {'min<BE 위험' if r['min_below_be'] else 'min≥BE'})")
    print(f"  대상 행 수: {cnt}")
    return cnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="GCS config 에 be_floor 실주입 (이번 단계 금지 — 안전차단됨)")
    ap.add_argument("--allow-write", action="store_true",
                    help="--apply 와 함께 명시해야 실제 write. 이중 안전장치.")
    args = ap.parse_args()

    sa = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    print(f"[env] GOOGLE_APPLICATION_CREDENTIALS = {sa}")
    print(f"[env] GCS bucket = {GCS_BUCKET_NAME}, project = {BQ_PROJECT}")

    client = get_bq_client()
    pricing = fetch_pricing(client)
    shipping = fetch_shipping(client)
    print(f"[bq] product_pricing rows = {len(pricing)}, shipping skus = {len(shipping)}")

    cfg, generation = load_config_from_gcs()
    print(f"[gcs] config loaded, generation = {generation}, groups = {len(cfg.get('product_groups', {}))}")

    rows, floor_maps, warnings = build_be_table(cfg, pricing, shipping)

    print("\n=== BE FLOOR 계산표 (그룹×병수) ===")
    print_table(rows)

    if warnings:
        print("\n=== 경고 ===")
        for w in warnings:
            print("  " + w)

    audit_cross_check(rows)

    # 그룹별 be_floor_map 요약 (config 주입 예정 형태)
    now_iso = datetime.now(KST).isoformat()
    print("\n=== config 주입 예정 형태 (be_floor_map, be_floor_updated) ===")
    for gk, fm in floor_maps.items():
        if fm:
            print(f"  {gk}: be_floor_map={fm}  be_floor_updated={now_iso}")
        else:
            print(f"  {gk}: (스킵 — 매핑/원가/배송 결손)")

    if args.apply and args.allow_write:
        # 이번 단계에서는 호출되지 않아야 함. 명시적 안전차단.
        print("\n[SAFETY] --apply --allow-write 감지. 그러나 이번 단계는 write 금지 정책이라 중단.")
        print("[SAFETY] 실주입은 별도 승인 후, 이 안전차단 라인을 제거하고 실행.")
        return 2
    else:
        print("\n[DRY-RUN] GCS write 하지 않음. 계산표만 산출 완료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
