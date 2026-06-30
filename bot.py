# Sliv 3 - bot.py
# Pair: uSOL/cbBTC | Aerodrome Slipstream (routerType=0) vs PancakeSwap V3 (routerType=1)
# Flash loan: Morpho Blue (0% fee, handled inside Sliv3.sol)
# Detection: Base Flashblocks WSS (~200ms) with HTTP polling fallback (5s)

import asyncio
import json
import time
import os
import requests
import brotli
import websockets
from web3 import Web3
from datetime import datetime

# ---------------------------------------------------------------------------
# RPC / WS endpoints
# ---------------------------------------------------------------------------
RPC_URL          = "https://base-rpc.publicnode.com"
RPC_EXEC_URL     = "https://base-rpc.publicnode.com"
FLASHBLOCKS_WS   = "wss://mainnet.flashblocks.base.org/ws"

EXEC_RPCS = [
    "https://base-rpc.publicnode.com",
    "https://mainnet.base.org",
    "https://base.drpc.org",
]

# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------
PRIVATE_KEY      = os.environ.get("PRIVATE_KEY")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CONTRACT_ADDR    = "0xB2bCc18b0a4897338c84fFF7303b6d4FcD3A0126"

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
USOL  = "0x311935Cd80B76769bF2ecC9D8Ab7635b2139cf82"   # token0 on both pools
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"    # token1 on both pools, borrow token

# ---------------------------------------------------------------------------
# Pools / routers
# ---------------------------------------------------------------------------
AERO_POOL   = "0xccfa472815563ff9eb2de95c7b2be1ccf91f7f31"
CAKE_POOL   = "0x8Df6dd38D718bD726374521c2DcFE90Eb9CB7d43"

AERO_ROUTER = "0xBE6D8f0d05cC4be24d5167a3eF062215bE6d18a5"
CAKE_ROUTER = "0x678Aa4bF4E210cf2166753e054d5b7c31cc7fa86"

ROUTER_TYPE_AERO = 0
ROUTER_TYPE_CAKE = 1

AERO_TICK_SPACING = 10     # int24
CAKE_FEE_PARAM    = 500    # passed through ABI as int24

CBBTC_DECIMAL_ADJ = 10**10  # uSOL 18 decimals, cbBTC 8 decimals -> 10 decimal diff

# ---------------------------------------------------------------------------
# Strategy params
# ---------------------------------------------------------------------------
SUMMARY_INTERVAL  = 3600
COOLDOWN_SECONDS  = 12
MIN_PROFIT_USD    = 0.10
HTTP_FALLBACK_SLEEP = 5

USOL_TIERS = [
    (0.30, 1000),
    (0.15, 500),
]

# cbBTC price fetched live on startup (not hardcoded)
CBBTC_PRICE_USD = None

# ---------------------------------------------------------------------------
# ABIs
# ---------------------------------------------------------------------------
AERO_SLOT0_ABI = [
    {"inputs":[],"name":"slot0","outputs":[
        {"name":"sqrtPriceX96","type":"uint160"},
        {"name":"tick","type":"int24"},
        {"name":"observationIndex","type":"uint16"},
        {"name":"observationCardinality","type":"uint16"},
        {"name":"observationCardinalityNext","type":"uint16"},
        {"name":"unlocked","type":"bool"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token1","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}
]

CAKE_V3_ABI = [
    {"inputs":[],"name":"slot0","outputs":[
        {"name":"sqrtPriceX96","type":"uint160"},
        {"name":"tick","type":"int24"},
        {"name":"observationIndex","type":"uint16"},
        {"name":"observationCardinality","type":"uint16"},
        {"name":"observationCardinalityNext","type":"uint16"},
        {"name":"feeProtocol","type":"uint32"},
        {"name":"unlocked","type":"bool"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token1","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}
]

CONTRACT_ABI = [
    {
        "inputs": [
            {"name": "borrowAmount", "type": "uint256"},
            {
                "name": "p", "type": "tuple",
                "components": [
                    {"name": "routerA", "type": "address"},
                    {"name": "routerTypeA", "type": "uint8"},
                    {"name": "paramA", "type": "int24"},
                    {"name": "routerB", "type": "address"},
                    {"name": "routerTypeB", "type": "uint8"},
                    {"name": "paramB", "type": "int24"},
                    {"name": "borrowToken", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "minProfit", "type": "uint256"}
                ]
            }
        ],
        "name": "executeArb",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {"inputs":[{"name":"token","type":"address"}],"name":"getBalance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]

# ---------------------------------------------------------------------------
# Web3 setup
# ---------------------------------------------------------------------------
w3_read  = Web3(Web3.HTTPProvider(RPC_URL))
w3_exec  = Web3(Web3.HTTPProvider(RPC_EXEC_URL))
account  = w3_exec.eth.account.from_key(PRIVATE_KEY)
contract = w3_exec.eth.contract(
    address=Web3.to_checksum_address(CONTRACT_ADDR), abi=CONTRACT_ABI)

aero_pool = w3_read.eth.contract(
    address=Web3.to_checksum_address(AERO_POOL), abi=AERO_SLOT0_ABI)
cake_pool = w3_read.eth.contract(
    address=Web3.to_checksum_address(CAKE_POOL), abi=CAKE_V3_ABI)

last_fire     = 0
usol_spreads  = []
last_summary  = time.time()
revert_count  = 0
fire_count    = 0

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                      timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ---------------------------------------------------------------------------
# Live cbBTC price on startup
# ---------------------------------------------------------------------------
def fetch_cbbtc_price_usd():
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "coinbase-wrapped-btc", "vs_currencies": "usd"},
            timeout=10
        )
        price = resp.json()["coinbase-wrapped-btc"]["usd"]
        print(f"Fetched live cbBTC price: ${price}")
        return float(price)
    except Exception as e:
        print(f"Price fetch failed, falling back to bitcoin price: {e}")
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=10
            )
            price = resp.json()["bitcoin"]["usd"]
            print(f"Fetched BTC price as cbBTC proxy: ${price}")
            return float(price)
        except Exception as e2:
            print(f"Fallback price fetch also failed: {e2}")
            return 100000.0  # last-resort hardcoded fallback

# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def get_price(pool_contract, base_token, decimal_adjustment=1):
    """Returns price of token0 in terms of token1, adjusted for decimals.
    base_token = the token we treat as the reference (token1 = cbBTC here)."""
    try:
        slot0          = pool_contract.functions.slot0().call()
        token0         = pool_contract.functions.token0().call().lower()
        sqrt_price_x96 = slot0[0]
        price_ratio    = (sqrt_price_x96 / (2**96)) ** 2 * decimal_adjustment
        return price_ratio if token0 == base_token.lower() else 1 / price_ratio
    except Exception as e:
        print(f"Price error: {e}")
        return None

def calculate_spread(price_a, price_b):
    if not price_a or not price_b:
        return 0, False
    spread   = abs(price_a - price_b) / min(price_a, price_b) * 100
    buy_on_a = price_a < price_b
    return spread, buy_on_a

def get_tier(spread, tiers):
    for min_spread, loan_amt in tiers:
        if spread >= min_spread:
            return min_spread, loan_amt
    return None, None

def to_wei_cbbtc(amount_usd, price_usd):
    # cbBTC has 8 decimals
    return int((amount_usd / price_usd) * 1e8)

def min_profit_wei_cbbtc(price_usd):
    return int((MIN_PROFIT_USD / price_usd) * 1e8)

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def fire_arb(buy_on_a, loan_usd, spread, tier):
    global revert_count, fire_count
    try:
        loan_wei_amt = to_wei_cbbtc(loan_usd, CBBTC_PRICE_USD)
        min_profit   = min_profit_wei_cbbtc(CBBTC_PRICE_USD)

        # buy_on_a = True means: buy uSOL cheap on Aero, sell expensive on Cake
        if buy_on_a:
            rA, rtA, pA = AERO_ROUTER, ROUTER_TYPE_AERO, AERO_TICK_SPACING
            rB, rtB, pB = CAKE_ROUTER, ROUTER_TYPE_CAKE, CAKE_FEE_PARAM
        else:
            rA, rtA, pA = CAKE_ROUTER, ROUTER_TYPE_CAKE, CAKE_FEE_PARAM
            rB, rtB, pB = AERO_ROUTER, ROUTER_TYPE_AERO, AERO_TICK_SPACING

        params = (
            Web3.to_checksum_address(rA), rtA, pA,
            Web3.to_checksum_address(rB), rtB, pB,
            Web3.to_checksum_address(CBBTC),
            Web3.to_checksum_address(USOL),
            min_profit
        )

        msg = (f"uSOL/cbBTC TIER {tier}% | "
               f"Spread: {spread:.3f}% | Loan: ${loan_usd}")
        print(msg)
        send_telegram(msg)

        data = contract.encode_abi("executeArb", args=[loan_wei_amt, params])

        tx_hash = None
        for rpc_url in EXEC_RPCS:
            try:
                w3_try    = Web3(Web3.HTTPProvider(
                    rpc_url, request_kwargs={"timeout": 8}))
                nonce     = w3_try.eth.get_transaction_count(
                    account.address, 'pending')
                gas_price = int(w3_try.eth.gas_price * 1.2)
                tx = {
                    "type": 2,
                    "chainId": 8453,
                    "to": Web3.to_checksum_address(CONTRACT_ADDR),
                    "value": 0,
                    "gas": 3000000,
                    "maxFeePerGas": gas_price,
                    "maxPriorityFeePerGas": gas_price,
                    "nonce": nonce,
                    "data": data,
                }
                signed  = account.sign_transaction(tx)
                tx_hash = w3_try.eth.send_raw_transaction(
                    signed.raw_transaction)
                print(f"Submitted via {rpc_url}")
                break
            except Exception as rpc_err:
                print(f"RPC {rpc_url} failed: {rpc_err}")
                continue

        if not tx_hash:
            raise Exception("All RPCs failed")

        print(f"uSOL/cbBTC tx sent: {tx_hash.hex()}")
        send_telegram(f"uSOL/cbBTC tx: {tx_hash.hex()}")

        receipt = w3_exec.eth.wait_for_transaction_receipt(
            tx_hash, timeout=60)
        fire_count += 1
        if receipt.status == 1:
            msg = f"SUCCESS uSOL/cbBTC! Tier {tier}%, spread {spread:.3f}%"
        else:
            revert_count += 1
            msg = (f"REVERTED uSOL/cbBTC - Tier {tier}%, spread {spread:.3f}%, "
                   f"loan ${loan_usd} (likely 'Not profitable' - slippage check)")
        print(msg)
        send_telegram(msg)

    except Exception as e:
        err = f"uSOL/cbBTC error: {e}"
        print(err)
        send_telegram(f"WARNING {err}")

def send_hourly_summary():
    if not usol_spreads:
        return
    msg = (
        f"Hourly - uSOL/cbBTC (Sliv 3)\n"
        f"Aero 0.03% vs Cake 0.05% | Morpho 0% | BE ~0.12-0.15%\n"
        f"Max:  {max(usol_spreads):.3f}%\n"
        f"Min:  {min(usol_spreads):.3f}%\n"
        f"Avg:  {sum(usol_spreads)/len(usol_spreads):.3f}%\n"
        f"Above {USOL_TIERS[1][0]}%: "
        f"{sum(1 for s in usol_spreads if s >= USOL_TIERS[1][0])}\n"
        f"Above {USOL_TIERS[0][0]}%: "
        f"{sum(1 for s in usol_spreads if s >= USOL_TIERS[0][0])}\n"
        f"Total checks: {len(usol_spreads)}\n"
        f"Fires: {fire_count} | Reverts: {revert_count}\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    send_telegram(msg)

def check_and_maybe_fire():
    global last_fire

    price_aero = get_price(aero_pool, CBBTC, CBBTC_DECIMAL_ADJ)
    price_cake = get_price(cake_pool, CBBTC, CBBTC_DECIMAL_ADJ)

    if not price_aero or not price_cake:
        print("uSOL/cbBTC | Could not fetch prices")
        return

    spread, buy_on_a = calculate_spread(price_aero, price_cake)
    direction = "Buy Aero Sell Cake" if buy_on_a else "Buy Cake Sell Aero"
    print(f"uSOL/cbBTC | Aero: {price_aero:.10f} | Cake: {price_cake:.10f} | "
          f"Spread: {spread:.3f}% | {direction}")

    usol_spreads.append(spread)

    tier, loan_usd = get_tier(spread, USOL_TIERS)
    if tier is None:
        return

    if time.time() - last_fire < COOLDOWN_SECONDS:
        print("uSOL/cbBTC | Cooldown active")
        return

    if spread >= 0.3:
        time.sleep(0.3)
        price_aero2 = get_price(aero_pool, CBBTC, CBBTC_DECIMAL_ADJ)
        price_cake2 = get_price(cake_pool, CBBTC, CBBTC_DECIMAL_ADJ)
        if price_aero2 and price_cake2:
            spread2, buy_on_a2 = calculate_spread(price_aero2, price_cake2)
            if abs(spread2 - spread) > spread * 0.5:
                print(f"uSOL/cbBTC | Spread unstable "
                      f"({spread:.3f}% vs {spread2:.3f}%), skipping")
                return
            spread, buy_on_a = spread2, buy_on_a2
            tier2, loan_usd2 = get_tier(spread, USOL_TIERS)
            if tier2 is None:
                return
            tier, loan_usd = tier2, loan_usd2

    fire_arb(buy_on_a, loan_usd, spread, tier)
    last_fire = time.time()

def on_new_block():
    global last_summary

    check_and_maybe_fire()

    if time.time() - last_summary >= SUMMARY_INTERVAL:
        send_hourly_summary()
        usol_spreads.clear()
        last_summary = time.time()

# ---------------------------------------------------------------------------
# Flashblocks listener (Brotli-compressed binary feed, ~200ms sub-blocks)
# ---------------------------------------------------------------------------
async def run_flashblocks():
    print("Connecting to Flashblocks WSS...")
    async with websockets.connect(FLASHBLOCKS_WS) as ws:
        print("Flashblocks connected (no subscription call needed)")
        send_telegram(
            "Sliv 3 LIVE\n"
            "uSOL/cbBTC: Aero 0.03% vs Cake 0.05%\n"
            "Flash loan: Morpho Blue (0% fee)\n"
            "Detection: Flashblocks ~200ms\n"
            f"Tiers: {USOL_TIERS[1][0]}%/${USOL_TIERS[1][1]} | "
            f"{USOL_TIERS[0][0]}%/${USOL_TIERS[0][1]}\n"
            "Double-check: enabled >=0.3%"
        )
        async for message in ws:
            try:
                if isinstance(message, bytes):
                    data = json.loads(brotli.decompress(message))
                else:
                    data = json.loads(message)
                on_new_block()
            except Exception as e:
                print(f"Flashblocks message error: {e}")

async def run_flashblocks_with_reconnect():
    backoff = 5
    while True:
        try:
            await run_flashblocks()
        except Exception as e:
            print(f"Flashblocks error: {e} - reconnecting in {backoff}s")
            await asyncio.sleep(backoff)

def run_http_fallback():
    print("HTTP polling fallback (5s)")
    send_telegram("WARNING Sliv 3 running on HTTP fallback (5s polling)")
    while True:
        try:
            on_new_block()
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(HTTP_FALLBACK_SLEEP)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global CBBTC_PRICE_USD

    print("Sliv 3 - uSOL/cbBTC - Flashblocks Mode")
    print("Aero (routerType=0, tickSpacing=10) vs Cake (routerType=1, fee=500)")
    print(f"Tiers: {USOL_TIERS[1][0]}%/${USOL_TIERS[1][1]} | "
          f"{USOL_TIERS[0][0]}%/${USOL_TIERS[0][1]}")
    print(f"Contract: {CONTRACT_ADDR}")
    print("Flash loan: Morpho Blue (0% fee)")

    CBBTC_PRICE_USD = fetch_cbbtc_price_usd()

    try:
        asyncio.run(run_flashblocks_with_reconnect())
    except KeyboardInterrupt:
        print("Shutting down")
    except Exception as e:
        print(f"Flashblocks fatal error: {e} - falling back to HTTP")
        send_telegram(f"WARNING Flashblocks fatal: {e} - HTTP fallback")
        run_http_fallback()

if __name__ == "__main__":
    main()
