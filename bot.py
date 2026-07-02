# Sliv 3 — Anti-Sliv Update (bot.py)
# Pair: uSOL/cbBTC | Aerodrome Slipstream vs PancakeSwap V3
# Flash loan: Morpho Blue (0% fee, handled inside Sliv3_1.sol)
# Detection: Base Flashblocks WSS (~200ms) with HTTP polling fallback
#
# Architecture:
#   Python detects gross spread >= 0.15% via slot0 HTTP reads, fires tx.
#   Contract handles ALL arb intelligence on-chain:
#   slot0 reads, direction, loan sizing, Quoter simulation, flash loan, swap.
#   Clean exit (~$0.001) if not profitable. No expensive reverts.

import asyncio
import json
import time
import os
import requests
import brotli
import websockets
from web3 import Web3
from datetime import datetime

RPC_URL        = "https://base-rpc.publicnode.com"
RPC_EXEC_URL   = "https://base-rpc.publicnode.com"
FLASHBLOCKS_WS = "wss://mainnet.flashblocks.base.org/ws"

EXEC_RPCS = [
    "https://base-rpc.publicnode.com",
    "https://mainnet.base.org",
    "https://base.drpc.org",
]

PRIVATE_KEY      = os.environ.get("PRIVATE_KEY")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CONTRACT_ADDR    = "0x89c0847958b535513fb8C697De3FbBd155C3C709"

USOL  = "0x311935Cd80B76769bF2ecC9D8Ab7635b2139cf82"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"

AERO_POOL = "0xCcfA472815563ff9eB2de95C7b2bE1Ccf91f7F31"
CAKE_POOL = "0x8Df6dd38D718bD726374521c2DcFE90Eb9CB7d43"

CBBTC_DECIMAL_ADJ      = 10**10
GROSS_SPREAD_THRESHOLD = 0.15
MIN_PROFIT_USD         = 0.10
COOLDOWN_SECONDS       = 12
SUMMARY_INTERVAL       = 3600
HTTP_FALLBACK_SLEEP    = 5

CBBTC_PRICE_USD = None

AERO_SLOT0_ABI = [
    {"inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"},
        {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"},
        {"name": "unlocked", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"}
]

CAKE_SLOT0_ABI = [
    {"inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"},
        {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"},
        {"name": "feeProtocol", "type": "uint32"},
        {"name": "unlocked", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"}
]

CONTRACT_ABI = [
    {
        "inputs": [
            {"name": "cbbtcPriceCents", "type": "uint256"},
            {"name": "minProfitWei", "type": "uint256"}
        ],
        "name": "executeArb",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "getBalance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "buyOnAero",   "type": "bool"},
            {"indexed": False, "name": "borrowAmount", "type": "uint256"},
            {"indexed": False, "name": "finalAmount",  "type": "uint256"},
            {"indexed": False, "name": "profit",       "type": "uint256"}
        ],
        "name": "ArbExecuted",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [{"indexed": False, "name": "reason", "type": "string"}],
        "name": "CleanExit",
        "type": "event"
    }
]

ARB_EXECUTED_TOPIC = Web3.keccak(
    text="ArbExecuted(bool,uint256,uint256,uint256)"
).hex()

w3_read  = Web3(Web3.HTTPProvider(RPC_URL))
w3_exec  = Web3(Web3.HTTPProvider(RPC_EXEC_URL))
account  = w3_exec.eth.account.from_key(PRIVATE_KEY)
contract = w3_exec.eth.contract(
    address=Web3.to_checksum_address(CONTRACT_ADDR), abi=CONTRACT_ABI)

aero_pool = w3_read.eth.contract(
    address=Web3.to_checksum_address(AERO_POOL), abi=AERO_SLOT0_ABI)
cake_pool = w3_read.eth.contract(
    address=Web3.to_checksum_address(CAKE_POOL), abi=CAKE_SLOT0_ABI)

last_fire    = 0
usol_spreads = []
last_summary = time.time()
fire_count   = 0
arb_count    = 0
clean_count  = 0


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                      timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def fetch_cbbtc_price_usd():
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "coinbase-wrapped-btc", "vs_currencies": "usd"},
            timeout=10
        )
        price = float(resp.json()["coinbase-wrapped-btc"]["usd"])
        print(f"cbBTC price: ${price:,.2f}")
        return price
    except Exception as e:
        print(f"cbBTC price fetch failed, trying BTC: {e}")
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=10
            )
            price = float(resp.json()["bitcoin"]["usd"])
            print(f"BTC price (cbBTC proxy): ${price:,.2f}")
            return price
        except Exception as e2:
            print(f"Fallback price fetch failed: {e2}")
            return 100000.0


def get_price(pool_contract, base_token, decimal_adjustment=1):
    try:
        slot0          = pool_contract.functions.slot0().call()
        token0         = pool_contract.functions.token0().call().lower()
        sqrt_price_x96 = slot0[0]
        price_ratio    = (sqrt_price_x96 / (2**96)) ** 2 * decimal_adjustment
        return price_ratio if token0 == base_token.lower() else 1 / price_ratio
    except Exception as e:
        print(f"Price read error: {e}")
        return None


def calculate_spread(price_a, price_b):
    if not price_a or not price_b:
        return 0
    return abs(price_a - price_b) / min(price_a, price_b) * 100


def fire_arb(spread):
    global fire_count, arb_count, clean_count
    try:
        cbbtc_price_cents = int(CBBTC_PRICE_USD * 100)
        min_profit_wei    = int((MIN_PROFIT_USD / CBBTC_PRICE_USD) * 1e8)

        msg = (f"uSOL/cbBTC FIRING | Spread: {spread:.3f}% | "
               f"cbBTC: ${CBBTC_PRICE_USD:,.0f}")
        print(msg)
        send_telegram(msg)

        data = contract.encode_abi(
            "executeArb", args=[cbbtc_price_cents, min_profit_wei])

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
                print(f"Tx submitted via {rpc_url}: {tx_hash.hex()}")
                break
            except Exception as rpc_err:
                print(f"RPC {rpc_url} failed: {rpc_err}")
                continue

        if not tx_hash:
            raise Exception("All RPCs failed")

        send_telegram(f"uSOL/cbBTC tx: {tx_hash.hex()}")

        receipt = w3_exec.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        fire_count += 1

        if receipt.status == 1:
            arb_happened = any(
                log["topics"] and
                log["topics"][0].hex() == ARB_EXECUTED_TOPIC
                for log in receipt.logs
                if log["topics"]
            )

            if arb_happened:
                arb_count += 1
                for log in receipt.logs:
                    if log["topics"] and log["topics"][0].hex() == ARB_EXECUTED_TOPIC:
                        profit_wei = int(log["data"].hex()[-64:], 16)
                        profit_usd = (profit_wei / 1e8) * CBBTC_PRICE_USD
                        msg = (f"SUCCESS uSOL/cbBTC! "
                               f"Spread: {spread:.3f}% | "
                               f"Profit: ${profit_usd:.4f}")
                        print(msg)
                        send_telegram(msg)
                        break
            else:
                clean_count += 1
                print(f"Clean exit | Spread: {spread:.3f}% | Contract returned cleanly")
        else:
            msg = f"REVERTED uSOL/cbBTC | Spread: {spread:.3f}%"
            print(msg)
            send_telegram(f"WARNING {msg}")

    except Exception as e:
        err = f"uSOL/cbBTC error: {e}"
        print(err)
        send_telegram(f"WARNING {err}")


def send_hourly_summary():
    if not usol_spreads:
        return
    msg = (
        f"Hourly - uSOL/cbBTC (Sliv 3)\n"
        f"Aero 0.03% vs Cake 0.05% | Morpho 0% | BE ~0.13%\n"
        f"Max:  {max(usol_spreads):.3f}%\n"
        f"Min:  {min(usol_spreads):.3f}%\n"
        f"Avg:  {sum(usol_spreads)/len(usol_spreads):.3f}%\n"
        f"Above {GROSS_SPREAD_THRESHOLD}%: "
        f"{sum(1 for s in usol_spreads if s >= GROSS_SPREAD_THRESHOLD)}\n"
        f"Total checks: {len(usol_spreads)}\n"
        f"Fires: {fire_count} | Arbs: {arb_count} | Clean exits: {clean_count}\n"
        f"cbBTC price: ${CBBTC_PRICE_USD:,.0f}\n"
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

    spread    = calculate_spread(price_aero, price_cake)
    direction = "Buy Aero" if price_aero < price_cake else "Buy Cake"
    print(f"uSOL/cbBTC | Aero: {price_aero:.10f} | Cake: {price_cake:.10f} | "
          f"Spread: {spread:.3f}% | {direction}")

    usol_spreads.append(spread)

    if spread < GROSS_SPREAD_THRESHOLD:
        return

    if time.time() - last_fire < COOLDOWN_SECONDS:
        print("uSOL/cbBTC | Cooldown active")
        return

    fire_arb(spread)
    last_fire = time.time()


def on_new_block():
    global last_summary, CBBTC_PRICE_USD

    check_and_maybe_fire()

    if time.time() - last_summary >= SUMMARY_INTERVAL:
        fresh_price = fetch_cbbtc_price_usd()
        if fresh_price:
            CBBTC_PRICE_USD = fresh_price
        send_hourly_summary()
        usol_spreads.clear()
        last_summary = time.time()


async def run_flashblocks():
    print("Connecting to Flashblocks WSS...")
    async with websockets.connect(FLASHBLOCKS_WS) as ws:
        print("Flashblocks connected (no subscription call needed)")
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


def main():
    global CBBTC_PRICE_USD

    print("Sliv 3 — Anti-Sliv Update")
    print(f"Pair: uSOL/cbBTC | Threshold: {GROSS_SPREAD_THRESHOLD}% | Loan: $300")
    print(f"Contract: {CONTRACT_ADDR}")
    print("Flash loan: Morpho Blue (0% fee)")
    print("On-chain: slot0 reads + Quoter simulation + arb execution")

    CBBTC_PRICE_USD = fetch_cbbtc_price_usd()

    send_telegram(
        "Sliv 3 LIVE (Anti-Sliv)\n"
        "uSOL/cbBTC: Aero 0.03% vs Cake 0.05%\n"
        "Flash loan: Morpho Blue (0% fee)\n"
        "Detection: Flashblocks ~200ms\n"
        f"Threshold: {GROSS_SPREAD_THRESHOLD}% gross | $300 loan\n"
        "Profitability: on-chain Quoter simulation\n"
        "Clean exit if unprofitable (~$0.001)"
    )

    try:
        asyncio.run(run_flashblocks_with_reconnect())
    except KeyboardInterrupt:
        print("Shutting down")
    except Exception as e:
        print(f"Flashblocks fatal: {e} - falling back to HTTP")
        send_telegram(f"WARNING Flashblocks fatal: {e} - HTTP fallback")
        run_http_fallback()


if __name__ == "__main__":
    main()
