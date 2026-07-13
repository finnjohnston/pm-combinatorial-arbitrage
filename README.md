# Kalshi Combinatorial Arbitrage Engine

In a mutually-exclusive Kalshi event, exactly one outcome resolves to YES and pays $1. If the prices of all outcomes sum to less than $1.00, buying every one locks in a guaranteed profit at settlement (with a symmetric sell-side case when prices sum above $1.00). This engine scans Kalshi's full market universe in real time, streams order books for the liquid subset, and detects, sizes, and executes these multi-leg trades whenever there is an edge. It does not place real orders on Kalshi. Execution is a high-fidelity simulation against live market data, modeling real order book depth, fees, collateral, and capital constraints.

## Findings

Apparent combinatorial arbitrage on Kalshi is overwhelmingly illusory. Crossed books that display a riskless edge appear constantly, but the vast majority are phantom liquidity — quotes that vanish within seconds and never survive a short persistence check. Attempting to execute against them systematically loses money: fills come back one-sided, leaving directional exposure at market-implied odds rather than a hedged position. The engine's instrumentation measures this directly by tracking how many displayed opportunities survive re-evaluation before dispatch.

A small durable remainder is real and capturable. It concentrates in attention gaps — moments when a market's book goes untended because participants are watching the underlying event itself rather than their quotes. These opportunities persist long enough to trade, fill with full hedging, and settle at the value locked in at execution.

Two layers of the result should be read differently. The prevalence and persistence of displayed crossings are are properties of the live market that hold regardless of whether the engine trades. The profit-and-loss claims are conditional on the fill model — they follow from a deliberately conservative simulation of participation, latency, and book erosion, not from live order placement.

## Setup

Requires Python 3.11+ and Kalshi API credentials (an API key ID and its RSA private key).

```bash
pip install -r requirements.txt

export KALSHI_KEY_ID=<your-key-id>
export KALSHI_PRIVATE_KEY=/path/to/kalshi-private-key.pem
```

Run the engine with a starting bankroll (USD):

```bash
caffeinate -i python main.py --capital 10000
```

Logs stream to the console. While the engine runs, get a point-in-time report of capital, open positions, and the day's settled trades:

```bash
python snapshot.py
```
