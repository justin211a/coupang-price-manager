#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""보호 쿠폰 가드 단위 테스트 (2026-07-18)

라이브 쿠팡 API 호출 없음 — FakeAPI 스텁으로 로직만 검증.
실행: PYTHONUTF8=1 python test_protected_coupons.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import server


class FakeAPI:
    """cleanup_group_coupons / _is_protected_coupon 검증용 스텁.
    get_coupons/cancel_coupon 만 구현. 실제 네트워크 호출 없음."""
    def __init__(self, coupons):
        self._coupons = coupons
        self.cancelled = []
    def get_coupons(self, status):
        return {"success": True, "data": {"data": {"content": self._coupons}}}
    def cancel_coupon(self, coupon_id):
        self.cancelled.append(coupon_id)
        return {"success": True}
    # 실 메서드 재사용
    _is_protected_coupon = server.CoupangAPI._is_protected_coupon


PASS = 0
FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def test_matches_protected_pattern():
    print("[matches_protected_pattern]")
    check("특가 부분일치", server.matches_protected_pattern("알파CD 특가쿠폰(수동)", ["특가"]))
    check("멀티패턴 일치", server.matches_protected_pattern("알파CD 특가", ["없음", "특가"]))
    check("미일치=False", not server.matches_protected_pattern("멜라더블 할인쿠폰", ["특가"]))
    check("빈 패턴=False", not server.matches_protected_pattern("특가쿠폰", []))
    check("None 패턴=False", not server.matches_protected_pattern("특가쿠폰", None))
    check("None 이름=False", not server.matches_protected_pattern(None, ["특가"]))
    check("빈 문자열 패턴 무시", not server.matches_protected_pattern("어떤쿠폰", ["", None]))


def test_cleanup_skips_protected():
    print("[cleanup_group_coupons — 보호 패턴 스킵]")
    coupons = [
        {"couponId": 1, "promotionName": "알파CD 할인쿠폰 1병 4,900원", "status": "APPLIED"},
        {"couponId": 2, "promotionName": "알파CD 특가쿠폰 수동", "status": "APPLIED"},
        {"couponId": 3, "promotionName": "알파CD 2천원 할인쿠폰", "status": "APPLIED"},
    ]
    api = FakeAPI(coupons)
    # coupon_name="알파CD" 매칭, protected=["특가"]
    res = server.cleanup_group_coupons(api, "알파CD", "알파CD", ["2천원"], ["특가"])
    cancelled_ids = [c["coupon_id"] for c in res["cancelled"]]
    protected_ids = [c["coupon_id"] for c in res.get("protected", [])]
    blocked_ids = [c["coupon_id"] for c in res["blocked"]]
    check("일반 자동쿠폰(1) 파기됨", 1 in cancelled_ids)
    check("특가쿠폰(2) 파기 안 됨", 2 not in cancelled_ids)
    check("특가쿠폰(2) protected 목록", 2 in protected_ids)
    check("고정쿠폰(3) blocked", 3 in blocked_ids)
    check("cancel_coupon 은 1만 호출", api.cancelled == [1])


def test_cleanup_no_patterns_backcompat():
    print("[cleanup_group_coupons — 패턴 없으면 기존 동작]")
    coupons = [
        {"couponId": 10, "promotionName": "멜라더블 할인쿠폰 3병", "status": "APPLIED"},
    ]
    api = FakeAPI(coupons)
    res = server.cleanup_group_coupons(api, "멜라더블", "멜라더블", ["2천원"], None)
    check("패턴 None 이면 정상 파기", 10 in [c["coupon_id"] for c in res["cancelled"]])
    check("protected 빈 목록", res.get("protected") == [])


def test_is_protected_coupon():
    print("[_is_protected_coupon — CIR08 방어]")
    coupons = [
        {"couponId": 55, "promotionName": "알파CD 특가쿠폰(수동)", "status": "APPLIED"},
        {"couponId": 56, "promotionName": "멜라더블 할인쿠폰", "status": "APPLIED"},
    ]
    api = FakeAPI(coupons)
    check("특가 충돌쿠폰=보호", api._is_protected_coupon(55, ["특가"]) is True)
    check("비특가 충돌쿠폰=비보호", api._is_protected_coupon(56, ["특가"]) is False)
    check("패턴 없으면 비보호", api._is_protected_coupon(55, None) is False)
    check("미존재 ID=비보호", api._is_protected_coupon(999, ["특가"]) is False)


def test_floor_guard_sanity():
    print("[floor guard — 회귀 확인]")
    ef = server.compute_effective_floor(10000, 9000, 500, 3)
    check("effective_floor = max(min, be+margin*n)", ef == max(10000, 9000 + 500 * 3))
    check("check_floor_guard 통과", server.check_floor_guard(11000, ef) is True)
    check("check_floor_guard 차단", server.check_floor_guard(9000, ef) is False)
    check("None 가드", server.compute_effective_floor(None, None, None, None) == 0)


if __name__ == "__main__":
    test_matches_protected_pattern()
    test_cleanup_skips_protected()
    test_cleanup_no_patterns_backcompat()
    test_is_protected_coupon()
    test_floor_guard_sanity()
    print(f"\n=== 결과: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
