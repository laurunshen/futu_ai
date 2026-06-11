from __future__ import annotations

from typing import Any

from .futu_client import FutuPaperClient
from .models import OrderIntent
from .portfolios import (
    apply_order_to_portfolio,
    record_futu_sync_order,
    update_futu_sync_order,
)


PENDING_SYNC_STATUSES = {"submitted", "futu_submitted", "partial", "partially_applied", "local_apply_failed"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number != number:
            return default
        return number
    except (TypeError, ValueError):
        return default


def _order_fill_summary(sync_result: dict[str, Any]) -> tuple[float, float]:
    deals = [deal for deal in sync_result.get("deals") or [] if isinstance(deal, dict)]
    deal_qty = sum(_num(deal.get("qty")) for deal in deals)
    if deal_qty > 0:
        deal_value = sum(_num(deal.get("qty")) * _num(deal.get("price")) for deal in deals)
        return round(deal_qty, 4), round(deal_value / deal_qty, 4) if deal_value > 0 else 0.0

    order = sync_result.get("order") if isinstance(sync_result.get("order"), dict) else {}
    dealt_qty = _num(order.get("dealt_qty"))
    dealt_avg_price = _num(order.get("dealt_avg_price"))
    return round(dealt_qty, 4), round(dealt_avg_price, 4)


def _sync_order_payload(
    *,
    portfolio_id: str,
    order_payload: dict[str, Any],
    sync_result: dict[str, Any],
    decision_id: str,
    source: str,
    status: str,
    message: str,
    applied_qty: float = 0.0,
) -> dict[str, Any]:
    intent = OrderIntent.from_dict(order_payload)
    order = sync_result.get("order") if isinstance(sync_result.get("order"), dict) else {}
    dealt_qty, dealt_avg_price = _order_fill_summary(sync_result)
    return {
        "portfolio_id": portfolio_id,
        "decision_id": decision_id,
        "source": source,
        "order_id": str(sync_result.get("order_id") or order.get("order_id") or ""),
        "code": intent.code,
        "side": intent.side,
        "qty": intent.qty,
        "price": intent.price,
        "dealt_qty": dealt_qty,
        "dealt_avg_price": dealt_avg_price,
        "applied_qty": applied_qty,
        "status": status,
        "message": message,
        "futu_order": order,
        "futu_deals": list(sync_result.get("deals") or []),
        "order_payload": dict(order_payload),
    }


def apply_order_with_optional_futu_sync(
    *,
    client: FutuPaperClient,
    portfolio: dict[str, Any],
    portfolio_id: str,
    order_payload: dict[str, Any],
    source: str,
    decision_id: str,
    reason: str,
    fx_to_hkd: dict[str, Any] | None = None,
    fx_source: str = "",
    fx_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not portfolio.get("futu_sync_enabled"):
        return apply_order_to_portfolio(
            portfolio_id,
            order_payload,
            source=source,
            decision_id=decision_id,
            reason=reason,
            fx_to_hkd=fx_to_hkd,
            fx_source=fx_source,
            fx_status=fx_status,
        )

    intent = OrderIntent.from_dict(order_payload)
    remark = f"FPAI_{str(decision_id or '')[:18]}" if decision_id else "FPAI_SYNC"
    sync_result = client.place_paper_order_with_status(intent, remark=remark)
    if not sync_result.get("ok"):
        return {
            "ok": False,
            "status": "futu_submit_failed",
            "mode": "futu_sync",
            "portfolio_id": portfolio_id,
            "decision_id": decision_id,
            "message": str(sync_result.get("data") or sync_result.get("error") or "Futu paper order failed"),
            "futu_sync": sync_result,
        }

    dealt_qty, dealt_avg_price = _order_fill_summary(sync_result)
    if dealt_qty <= 0 or dealt_avg_price <= 0:
        sync_payload = _sync_order_payload(
            portfolio_id=portfolio_id,
            order_payload=order_payload,
            sync_result=sync_result,
            decision_id=decision_id,
            source=source,
            status="futu_submitted",
            message="Futu paper order submitted; waiting for actual fill.",
        )
        record_futu_sync_order(portfolio_id, sync_payload)
        return {
            "ok": True,
            "status": "futu_submitted",
            "mode": "futu_sync",
            "portfolio_id": portfolio_id,
            "decision_id": decision_id,
            "message": sync_payload["message"],
            "futu_sync": sync_payload,
        }

    actual_order = dict(order_payload)
    actual_order["qty"] = dealt_qty
    actual_order["price"] = dealt_avg_price
    try:
        local_application = apply_order_to_portfolio(
            portfolio_id,
            actual_order,
            source="futu_sync",
            decision_id=decision_id,
            reason=reason,
            fx_to_hkd=fx_to_hkd,
            fx_source=fx_source,
            fx_status=fx_status,
        )
    except Exception as exc:
        sync_payload = _sync_order_payload(
            portfolio_id=portfolio_id,
            order_payload=order_payload,
            sync_result=sync_result,
            decision_id=decision_id,
            source=source,
            status="local_apply_failed",
            message=f"Futu paper fill was received, but local portfolio apply failed: {exc}",
            applied_qty=0.0,
        )
        record_futu_sync_order(portfolio_id, sync_payload)
        return {
            "ok": False,
            "status": "local_apply_failed",
            "mode": "futu_sync",
            "portfolio_id": portfolio_id,
            "decision_id": decision_id,
            "message": sync_payload["message"],
            "futu_sync": sync_payload,
        }
    status = "applied" if dealt_qty + 1e-9 >= intent.qty else "partially_applied"
    sync_payload = _sync_order_payload(
        portfolio_id=portfolio_id,
        order_payload=order_payload,
        sync_result=sync_result,
        decision_id=decision_id,
        source=source,
        status=status,
        message="Futu paper fill applied to local portfolio.",
        applied_qty=dealt_qty,
    )
    record_futu_sync_order(portfolio_id, sync_payload)
    return {
        **local_application,
        "status": status,
        "mode": "futu_sync",
        "message": sync_payload["message"],
        "futu_sync": sync_payload,
    }


def refresh_futu_sync_orders(
    *,
    client: FutuPaperClient,
    portfolio: dict[str, Any],
    fx_to_hkd: dict[str, Any] | None = None,
    fx_source: str = "",
    fx_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not portfolio.get("futu_sync_enabled"):
        return []

    results: list[dict[str, Any]] = []
    portfolio_id = str(portfolio.get("id") or "")
    for sync_order in portfolio.get("futu_sync_orders") or []:
        if not isinstance(sync_order, dict):
            continue
        if str(sync_order.get("status") or "").lower() not in PENDING_SYNC_STATUSES:
            continue
        order_id = str(sync_order.get("order_id") or "")
        code = str(sync_order.get("code") or "")
        if not order_id or not code:
            continue
        intent = OrderIntent.from_dict(sync_order.get("order_payload") or {})
        status_payload = client.order_status(intent.market, order_id, code)
        deals_payload = client.deals(intent.market, code, order_id)
        sync_result = {
            "ok": bool(status_payload.get("ok") or deals_payload.get("ok")),
            "order_id": order_id,
            "order": ((status_payload.get("data") or [{}])[0] if status_payload.get("ok") else sync_order.get("futu_order", {})),
            "deals": deals_payload.get("data") or sync_order.get("futu_deals", []),
        }
        dealt_qty, dealt_avg_price = _order_fill_summary(sync_result)
        applied_qty = _num(sync_order.get("applied_qty"))
        incremental_qty = round(dealt_qty - applied_qty, 4)
        if incremental_qty <= 0 or dealt_avg_price <= 0:
            update_futu_sync_order(
                portfolio_id,
                order_id,
                {
                    "dealt_qty": dealt_qty,
                    "dealt_avg_price": dealt_avg_price,
                    "futu_order": sync_result.get("order") or {},
                    "futu_deals": sync_result.get("deals") or [],
                },
            )
            results.append({"order_id": order_id, "status": "no_new_fill", "dealt_qty": dealt_qty})
            continue

        actual_order = dict(sync_order.get("order_payload") or {})
        actual_order["qty"] = incremental_qty
        actual_order["price"] = dealt_avg_price
        decision_id = str(sync_order.get("decision_id") or "")
        local_decision_id = decision_id if applied_qty <= 0 else f"{decision_id}:{order_id}:{dealt_qty:g}"
        try:
            local_application = apply_order_to_portfolio(
                portfolio_id,
                actual_order,
                source="futu_sync",
                decision_id=local_decision_id,
                reason=str(sync_order.get("message") or "Futu paper fill sync"),
                fx_to_hkd=fx_to_hkd,
                fx_source=fx_source,
                fx_status=fx_status,
            )
        except Exception as exc:
            failure_sync = {
                **sync_order,
                "status": "local_apply_failed",
                "message": f"Futu paper fill was received, but local portfolio apply failed: {exc}",
                "dealt_qty": dealt_qty,
                "dealt_avg_price": dealt_avg_price,
                "futu_order": sync_result.get("order") or {},
                "futu_deals": sync_result.get("deals") or [],
            }
            update_futu_sync_order(
                portfolio_id,
                order_id,
                failure_sync,
            )
            results.append(
                {
                    "order_id": order_id,
                    "decision_id": decision_id,
                    "status": "local_apply_failed",
                    "error": str(exc),
                    "application": {
                        "ok": False,
                        "status": "local_apply_failed",
                        "mode": "futu_sync",
                        "portfolio_id": portfolio_id,
                        "decision_id": decision_id,
                        "message": failure_sync["message"],
                        "futu_sync": failure_sync,
                    },
                }
            )
            continue
        next_status = "applied" if dealt_qty + 1e-9 >= _num(sync_order.get("qty")) else "partially_applied"
        updated_sync = {
            **sync_order,
            "status": next_status,
            "message": "Futu paper fill applied to local portfolio.",
            "dealt_qty": dealt_qty,
            "dealt_avg_price": dealt_avg_price,
            "applied_qty": dealt_qty,
            "futu_order": sync_result.get("order") or {},
            "futu_deals": sync_result.get("deals") or [],
        }
        update_futu_sync_order(portfolio_id, order_id, updated_sync)
        application_payload = {
            **local_application,
            "decision_id": decision_id,
            "status": next_status,
            "mode": "futu_sync",
            "message": updated_sync["message"],
            "futu_sync": updated_sync,
        }
        results.append(
            {
                "order_id": order_id,
                "decision_id": decision_id,
                "status": next_status,
                "application": application_payload,
            }
        )
    return results
