#!/usr/bin/env python3
"""
Real Trade Balance Update Cron Job

This cron job updates balance for real trading sessions every N seconds:
1) Fetch all running real trade sessions from Go API (/api/v1/execute/real-sessions/running)
2) For each session:
   - Fetch exchange credentials via decode API (/api/v1/execute/decode-secret)
   - Call the exchange API to get account balance (get_account_balance)
   - Calculate USDT balance from account portfolio
   - Update balance via Go API (/api/v1/execute/real-sessions/update-balance)
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

# Use same execution service base URL as other modules
GOLANG_API_BASE_URL = os.environ.get("GOLANG_API_URL", "http://localhost:8083")
USDT = "USDT"


def to_float(v: Any) -> float:
    """Convert value to float safely."""
    try:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        return float(str(v))
    except Exception:
        return 0.0


def fetch_running_real_sessions(page: int = 1, limit: int = 10000) -> list:
    """Fetch all running real trade sessions."""
    endpoint = f"/api/v1/execute/real-sessions/running?page={page}&limit={limit}"
    resp = make_golang_api_call(method="GET", endpoint=endpoint, data=None, base_url=GOLANG_API_BASE_URL)
    if not resp or "data" not in resp:
        logger_access.info(f"No real sessions data returned: {resp}")
        return []
    
    return resp.get("data", [])


def decode_api_secret(session_key: str, api_secret_encrypted: str) -> Optional[Dict[str, str]]:
    """
    Decode encrypted API secret and get exchange details.
    
    Returns dict with: exchange, api_key, api_secret, passphrase
    """
    endpoint = "/api/v1/execute/decode-secret"
    payload = {
        "session_key": session_key,
        "secret_key": api_secret_encrypted
    }
    
    resp = make_golang_api_call(
        method="POST",
        endpoint=endpoint,
        data=payload,
        base_url=GOLANG_API_BASE_URL
    )
    
    if not resp or not resp.get("success"):
        logger_access.info(f"Failed to decode secret for {session_key}: {resp}")
        return None
    
    return {
        "exchange": resp.get("exchange", "binance").lower(),
        "api_key": resp.get("api_key", ""),
        "api_secret": resp.get("api_secret", ""),
        "passphrase": resp.get("passphrase", ""),
    }


def get_usdt_balance_from_exchange(exchange: str, api_key: str, api_secret: str, passphrase: str = "") -> Optional[float]:
    """
    Get total balance from exchange account.
    
    Calculates total balance by:
    1. Getting all account holdings
    2. Getting price for each asset in USDT
    3. Converting all holdings to USDT value
    
    Returns total portfolio value in USDT.
    """
    try:
        acc_info = {
            "api_key": api_key,
            "secret_key": api_secret,
            "passphrase": passphrase,
        }
        
        # Get the exchange client
        client = get_client_exchange(
            exchange_name=exchange,
            acc_info=acc_info,
            symbol="BTC",
            quote=USDT,
            session_key=""
        )
        
        if not client:
            logger_error.error(f"Failed to create client for exchange: {exchange}")
            return None
        
        # Get account balance
        account_balance = client.get_account_balance()
        if not account_balance or "data" not in account_balance:
            logger_error.error(f"Failed to get account balance from {exchange}")
            return None
        
        balances = account_balance.get("data", {})
        total_value_usd = 0.0
        
        # Calculate portfolio value by converting all assets to USDT
        for asset_symbol, asset_info in balances.items():
            amount = to_float(asset_info.get("total", 0.0))
            
            if amount > 0:
                try:
                    if asset_symbol == "USDT":
                        # USDT price is always 1.0
                        price = 1.0
                    else:
                        # Get price for this asset in USDT
                        try:
                            ticker_data = client.get_ticker(asset_symbol, USDT)
                            price = to_float(ticker_data.get("last", 0.0)) if ticker_data else 0.0
                        except Exception as ticker_error:
                            logger_access.info(f"Could not get ticker for {asset_symbol}_USDT: {ticker_error}")
                            # Try with a temporary client if ticker fails
                            try:
                                temp_client = get_client_exchange(
                                    exchange_name=exchange,
                                    acc_info=acc_info,
                                    symbol=asset_symbol,
                                    quote=USDT,
                                    session_key=""
                                )
                                price_data = temp_client.get_price()
                                price = to_float(price_data.get("price", 0.0)) if price_data else 0.0
                            except Exception as price_error:
                                logger_access.info(f"Could not get price for {asset_symbol}: {price_error}")
                                price = 0.0
                    
                    # Calculate USD value for this asset
                    usd_value = amount * price
                    total_value_usd += usd_value
                    
                    logger_access.info(f"{asset_symbol}: amount={amount:.8f}, price=${price:.8f}, value=${usd_value:.2f}")
                    
                except Exception as e:
                    logger_error.error(f"Error processing {asset_symbol}: {e}")
                    # Skip this asset and continue
                    continue
        
        logger_access.info(f"Total portfolio value from {exchange}: ${total_value_usd:.2f} USDT")
        
        return total_value_usd
        
    except Exception as e:
        logger_error.error(f"Error getting balance from {exchange}: {e}")
        return None


def update_real_session_balance(session_key: str, current_balance: float, pnl: Optional[float] = None) -> bool:
    """
    Update real trade session balance in database and Redis.
    
    Returns True if successful.
    """
    endpoint = "/api/v1/execute/real-sessions/update-balance"
    payload = {
        "session_key": session_key,
        "current_balance": current_balance,
    }
    
    if pnl is not None:
        payload["total_pnl"] = pnl
    
    resp = make_golang_api_call(
        method="POST",
        endpoint=endpoint,
        data=payload,
        base_url=GOLANG_API_BASE_URL
    )
    
    if resp and resp.get("success"):
        logger_access.info(f"✓ Updated balance for {session_key}: {current_balance:.2f}")
        return True
    else:
        logger_error.error(f"✗ Failed to update balance for {session_key}: {resp}")
        return False


def update_session_balance(session: Dict[str, Any]) -> bool:
    """
    Update balance for a single real trade session.
    
    Process:
    1. Get session key and encrypted secret
    2. Decode the secret to get exchange details
    3. Call exchange API to get current balance
    4. Update database and Redis with new balance
    """
    try:
        session_key = session.get("session_key") or session.get("SessionKey")
        if not session_key:
            logger_error.error("Session key not found in session data")
            return False
        
        # Get encrypted API secret
        api_secret_encrypted = session.get("api_secret") or session.get("APISecret")
        if not api_secret_encrypted:
            logger_error.error(f"No encrypted API secret for {session_key}")
            return False
        
        logger_access.info(f"Processing real trade session: {session_key}")
        
        # Decode the secret and get exchange details
        decoded_creds = decode_api_secret(session_key, api_secret_encrypted)
        if not decoded_creds:
            logger_error.error(f"Failed to decode credentials for {session_key}")
            return False
        
        exchange = decoded_creds["exchange"]
        api_key = decoded_creds["api_key"]
        api_secret = decoded_creds["api_secret"]
        passphrase = decoded_creds.get("passphrase", "")
        
        logger_access.info(f"Exchange: {exchange}, Account: {api_key[:10]}...")
        
        # Get current balance from exchange
        current_balance = get_usdt_balance_from_exchange(
            exchange=exchange,
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase
        )
        print('current_balance: ',current_balance)
        if current_balance is None:
            logger_error.error(f"Could not fetch balance from {exchange} for {session_key}")
            return False
        
        # Get initial balance for PnL calculation
        initial_balance = to_float(session.get("initial_balance") or session.get("InitialBalance"))
        pnl = current_balance - initial_balance if initial_balance > 0 else 0.0
        
        logger_access.info(
            f"Session {session_key}: current_balance={current_balance:.2f}, "
            f"initial_balance={initial_balance:.2f}, pnl={pnl:.2f}"
        )
        
        # Update balance in database and Redis
        if update_real_session_balance(session_key, current_balance, pnl):
            return True
        else:
            logger_error.error(f"Failed to update balance for {session_key}")
            return False
            
    except Exception as e:
        logger_error.error(f"Error processing session {session_key}: {e}")
        return False


def run_once():
    """Run one iteration of real trade balance updates for all running sessions."""
    sessions = fetch_running_real_sessions(page=1, limit=500)
    if not sessions:
        logger_access.info("No running real trade sessions found")
        return
    print('sessions:: ',sessions)
    logger_access.info(f"Found {len(sessions)} running real trade sessions")
    
    success_count = 0
    for session in sessions:
        try:
            if update_session_balance(session):
                success_count += 1
        except Exception as e:
            logger_error.error(f"Error processing session: {e}")
            continue
    
    logger_access.info(f"Updated {success_count}/{len(sessions)} sessions successfully")


def main():
    interval_sec = int(os.environ.get("UPDATE_REAL_BALANCE_INTERVAL_SEC", "30"))  # default 3 minutes
    logger_access.info(
        f"Starting REAL TRADE update-balance cron loop, interval={interval_sec}s, "
        f"base_url={GOLANG_API_BASE_URL}"
    )
    
    while True:
        try:
            run_once()
        except Exception as e:
            logger_error.error(f"update-balance-real-trade main loop error: {e}")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()