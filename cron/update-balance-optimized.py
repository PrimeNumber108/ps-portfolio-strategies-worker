#!/usr/bin/env python3
"""
Optimized cron job to update paper sessions' current balance every 3 minutes.

OPTIMIZED FLOW (much faster):
1) Fetch running paper sessions from Go API (/api/v1/execute/paper-sessions?status=running)
2) For each session_key:
   - Fetch current balance + recent orders (last 3 minutes) from new API endpoint
     (/api/v1/execute/paper/session-balance-recent?session_key=...&minutes=3)
   - Calculate balance change from recent orders only (not all orders)
   - Update balance via POST to Go API (/api/v1/execute/paper/update-balance)

This is much more efficient than the old approach which fetched ALL orders for each session.
"""

import os
import sys
import time
import json
from typing import Dict, Any, Optional, List

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from utils.golang_auth import make_golang_api_call
from exchange_api_spot.user import get_client_exchange
from logger import logger_access, logger_error, logger_database

# Fee helpers
try:
    from exchange_api_spot.paper_trade.compute_bl import compute_trade_fee, split_symbol
except Exception:
    # Fallback in case of path differences
    from exchange_api_spot.paper_trade.compute_bl import compute_trade_fee, split_symbol

# Use same execution service base URL as other modules
GOLANG_API_BASE_URL = os.environ.get("GOLANG_API_URL", "http://localhost:8083")

USDT = "USDT"

# Env toggle: subtract paper order fees from computed balance (default: on)
APPLY_PAPER_ORDER_FEES_IN_BALANCE = os.environ.get("APPLY_PAPER_ORDER_FEES_IN_BALANCE", "1") not in ("0", "false", "False")


def to_float(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        return float(str(v))
    except Exception:
        return 0.0


def fetch_running_sessions(page: int = 1, limit: int = 10000) -> list:
    """Fetch running paper sessions (manager role to get all)."""
    endpoint = f"/api/v1/execute/paper-sessions?status=running&role=1&page={page}&limit={limit}"
    resp = make_golang_api_call(method="GET", endpoint=endpoint, data=None, base_url=GOLANG_API_BASE_URL)
    if not resp or "data" not in resp:
        logger_access.info(f"No sessions data returned: {resp}")
        return []
    return resp.get("data", [])


def fetch_session_balance_with_recent_orders(session_key: str, minutes: int = 3) -> Optional[Dict[str, Any]]:
    """
    Fetch session balance and recent orders from the new optimized API endpoint.
    Returns dict with: current_balance, exchange, recent_orders, etc.
    """
    endpoint = f"/api/v1/execute/paper/session-balance-recent?session_key={session_key}&minutes={minutes}"
    resp = make_golang_api_call(method="GET", endpoint=endpoint, data=None, base_url=GOLANG_API_BASE_URL)
    print('resp: ',resp)
    if not resp or not resp.get("success"):
        logger_access.info(f"Failed to fetch session balance for {session_key}: {resp}")
        return None
    return resp


def get_price_quote_in_usdt(exchange: str, base: str, quote: str = USDT) -> Optional[float]:
    """Get latest price for base/USDT via exchange client in PAPER_MODE.
    Returns None if unavailable.
    """
    try:
        # Dummy credentials are fine; PAPER_MODE clients don't need real keys
        acc_info = {"api_key": "paper", "secret_key": "paper", "session_key": ""}
        client = get_client_exchange(exchange_name=exchange, acc_info=acc_info, symbol=base, quote=quote, session_key="")
        if not client:
            return None
        price_data = None
        # Prefer get_price if available
        if hasattr(client, "get_price"):
            price_data = client.get_price(base, quote)
        if price_data and isinstance(price_data, dict):
            pr = price_data.get("price") or price_data.get("last") or price_data.get("lastPr")
            return to_float(pr) if pr is not None else None
        # Fallback to get_ticker
        if hasattr(client, "get_ticker"):
            ticker = client.get_ticker(base, quote)
            if ticker and isinstance(ticker, dict):
                pr = ticker.get("last") or ticker.get("lastPr")
                return to_float(pr) if pr is not None else None
    except Exception as e:
        logger_error.error(f"Price fetch error for {base}/{quote} on {exchange}: {e}")
    return None


def compute_balance_change_from_orders(exchange: str, orders: List[Dict[str, Any]]) -> float:
    """
    Calculate the net balance change (in USDT) from recent orders.
    
    For each order:
    - BUY: subtract (price * quantity + fee) from balance
    - SELL: add (price * quantity - fee) to balance
    
    Returns the net change in USDT.
    """
    balance_change = 0.0
    
    for o in orders:
        try:
            symbol = o.get("symbol") or o.get("Symbol") or ""
            side = (o.get("side") or o.get("Side") or "").upper()
            price = to_float(o.get("avg_price") or o.get("avgPrice") or o.get("price") or o.get("Price") or 0.0)
            qty = to_float(o.get("filled_quantity") or o.get("fillQuantity") or o.get("quantity") or 0.0)
            fee_raw = to_float(o.get("fee") or o.get("Fee") or 0.0)
            
            if not symbol or not side or price <= 0 or qty <= 0:
                continue
            
            # Determine base/quote
            try:
                base, quote = split_symbol(symbol)
            except Exception:
                # Default to USDT quote if cannot split
                base, quote = symbol, USDT
            
            # Calculate order value in quote currency
            order_value_quote = price * qty
            
            # Determine fee in quote currency
            fee_in_quote = fee_raw
            if fee_in_quote <= 0 and APPLY_PAPER_ORDER_FEES_IN_BALANCE:
                fee_in_quote = compute_trade_fee(side=side, price=price, quantity=qty, fee_rate=0.0, return_currency="quote")
            
            # Convert to USDT if quote != USDT
            if quote == USDT:
                order_value_usdt = order_value_quote
                fee_usdt = fee_in_quote
            else:
                q_price_usdt = get_price_quote_in_usdt(exchange, quote, USDT) or 0.0
                order_value_usdt = order_value_quote * q_price_usdt if q_price_usdt > 0 else 0.0
                fee_usdt = fee_in_quote * q_price_usdt if q_price_usdt > 0 else 0.0
            
            # Apply balance change based on side
            if side == "BUY":
                # Buying costs money: subtract order value and fee
                balance_change -= (order_value_usdt + fee_usdt)
            elif side == "SELL":
                # Selling adds money: add order value minus fee
                balance_change += (order_value_usdt - fee_usdt)
                
        except Exception as e:
            logger_error.error(f"Error computing balance change for order {o}: {e}")
            continue
    
    return balance_change


def update_session_balance_optimized(session_key: str, minutes: int = 3) -> None:
    """
    Optimized balance update:
    1. Fetch current balance and recent orders from API
    2. Calculate balance change from recent orders only
    3. Update balance
    """

    session_data = fetch_session_balance_with_recent_orders(session_key, minutes)
    if not session_data:
        logger_access.info(f"Could not fetch session data for {session_key}")
        return
    
    current_balance = to_float(session_data.get("current_balance", 0.0))
    exchange = session_data.get("exchange", "binance").lower()
    recent_orders = session_data.get("recent_orders", [])
    orders_count = session_data.get("orders_count", 0)
    
    logger_access.info(f"Session {session_key}: current_balance={current_balance}, recent_orders={orders_count}")
    
    # If no recent orders, no need to update
    if orders_count == 0:
        logger_access.info(f"No recent orders for {session_key}, skipping update")
        return
    
    # Calculate balance change from recent orders
    balance_change = compute_balance_change_from_orders(exchange, recent_orders)
    
    # Calculate new balance
    new_balance = current_balance + balance_change
    
    # Ensure balance doesn't go negative
    new_balance = max(new_balance, 0.0)
    
    logger_access.info(
        f"Session {session_key}: balance_change={balance_change:.2f}, "
        f"old_balance={current_balance:.2f}, new_balance={new_balance:.2f}"
    )
    
    # Update balance via API
    payload = {
        "session_key": session_key,
        "current_balance": float(new_balance),
        # Note: We're not updating current_tokens_value here since we're only tracking USDT balance changes
        # If you need to track individual token balances, you'll need to fetch and update them separately
    }
    
    resp = make_golang_api_call(
        method="POST",
        endpoint="/api/v1/execute/paper/update-balance",
        data=payload,
        base_url=GOLANG_API_BASE_URL,
    )
    
    if resp and resp.get("success"):
        logger_access.info(
            f"✓ Updated balance for {session_key}: {current_balance:.2f} → {new_balance:.2f} "
            f"(change: {balance_change:+.2f}, orders: {orders_count})"
        )
    else:
        logger_error.error(f"✗ Failed to update balance for {session_key}: {resp}")


def run_once():
    """Run one iteration of balance updates for all running sessions."""
    sessions = fetch_running_sessions(page=1, limit=500)
    if not sessions:
        logger_access.info("No running paper sessions found")
        return
    
    logger_access.info(f"Found {len(sessions)} running paper sessions")
    
    # Get interval from env to determine how many minutes of orders to fetch
    interval_sec = int(os.environ.get("UPDATE_BALANCE_INTERVAL_SEC", "180"))
    minutes = max(int(interval_sec / 60) + 1, 3)  # Add 1 minute buffer, minimum 3 minutes
    
    for s in sessions:
        try:
            session_key = s.get("session_key") or s.get("SessionKey")
            if not session_key:
                continue
            update_session_balance_optimized(session_key, minutes)
        except Exception as e:
            logger_error.error(f"Error processing session {s}: {e}")
            continue


def main():
    interval_sec = int(os.environ.get("UPDATE_BALANCE_INTERVAL_SEC", "180"))  # default 3 minutes
    logger_access.info(
        f"Starting OPTIMIZED update-balance cron loop, interval={interval_sec}s, "
        f"base_url={GOLANG_API_BASE_URL}, apply_fees={APPLY_PAPER_ORDER_FEES_IN_BALANCE}"
    )
    logger_access.info("This version only fetches recent orders (last N minutes) instead of all orders")
    
    while True:
        try:
            run_once()
        except Exception as e:
            logger_error.error(f"update-balance main loop error: {e}")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()