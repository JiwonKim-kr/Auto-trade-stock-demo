"""리컨실 순수 비교 테스트 — 기준선/일치/신규/소멸/수량 불일치 + 전송분 설명."""

from __future__ import annotations

from decimal import Decimal

from app.orders.reconcile import (
    DiscrepancyKind,
    reconcile,
    snapshot_from_holdings,
)
from app.toss.models import Holdings

D = Decimal


def test_baseline_when_no_previous():
    r = reconcile(None, {"005930": D("3")})
    assert r.baseline and r.status == "BASELINE" and r.ok


def test_ok_when_identical():
    prev = {"005930": D("3"), "AAPL": D("0.000271")}
    r = reconcile(prev, dict(prev))
    assert r.status == "OK" and r.ok


def test_new_symbol_detected():
    r = reconcile({"005930": D("3")}, {"005930": D("3"), "000660": D("1")})
    assert r.status == "MISMATCH"
    d = r.discrepancies[0]
    assert d.kind is DiscrepancyKind.NEW_SYMBOL and d.symbol == "000660"
    assert d.expected == D("0") and d.actual == D("1")


def test_missing_symbol_detected():
    r = reconcile({"005930": D("3"), "000660": D("1")}, {"005930": D("3")})
    d = r.discrepancies[0]
    assert d.kind is DiscrepancyKind.MISSING_SYMBOL and d.symbol == "000660"


def test_quantity_mismatch_detected():
    r = reconcile({"005930": D("3")}, {"005930": D("5")})
    d = r.discrepancies[0]
    assert d.kind is DiscrepancyKind.QUANTITY_MISMATCH
    assert d.expected == D("3") and d.actual == D("5")


# ── 전송(SUBMITTED) 순증감으로 설명되는 변화 = 정상 ───────────────────────────
def test_submitted_buy_explains_new_position():
    r = reconcile({}, {"005930": D("2")}, submitted_delta={"005930": D("2")})
    assert r.ok                                        # 시스템이 산 것 — 불일치 아님


def test_submitted_sell_explains_disappearance():
    r = reconcile({"005930": D("3")}, {}, submitted_delta={"005930": D("-3")})
    assert r.ok                                        # 시스템이 전량 판 것


def test_partial_fill_flagged_conservatively():
    # 5주 전송했는데 3주만 체결 → 기대 5 vs 실제 3 → 보수적 불일치(체결 API 연동 전 의도된 동작)
    r = reconcile({}, {"005930": D("3")}, submitted_delta={"005930": D("5")})
    d = r.discrepancies[0]
    assert d.kind is DiscrepancyKind.QUANTITY_MISMATCH and d.expected == D("5")
    assert "전송 순증감" in d.detail


def test_snapshot_from_holdings_maps_quantity():
    h = Holdings.model_validate({
        "totalPurchaseAmount": {"krw": "0"},
        "marketValue": {"amount": {"krw": "0"}},
        "profitLoss": {"amount": {"krw": "0"}, "rate": "0"},
        "items": [{"symbol": "005930", "name": "삼성전자", "currency": "KRW",
                   "quantity": "2.5", "lastPrice": "70000", "averagePurchasePrice": "68000",
                   "marketValue": {"purchaseAmount": "170000", "amount": "175000"},
                   "profitLoss": {"amount": "5000", "rate": "0.029"}}],
    })
    snaps = snapshot_from_holdings(h)
    assert snaps[0].symbol == "005930" and snaps[0].quantity == D("2.5")
    assert snaps[0].avg_price == D("68000") and snaps[0].currency == "KRW"
