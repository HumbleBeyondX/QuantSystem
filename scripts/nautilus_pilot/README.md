# NautilusTrader Pilot — ETH Momentum Sniper

Port of the validated ETH momentum strategy (`scripts/backtest_btc_sniper.py`,
class `BTCSniperBacktest`) to **NautilusTrader 1.230.0**, run on
`data/sample/ETH_USDT_4h.csv` (13,669 4h bars, 2020-01-01 → 2026-03-28).

Params (validated combo run): initial_capital=100,000, leverage=50x,
risk_per_trade=5%, max_margin_pct=30%, initial stop 3.5×ATR (min 0.5% price),
trailing 3.0×ATR from peak activated after 2.0×ATR profit, max_hold=180 bars,
cooldown=3, pause 18 bars after 5 consecutive losses, min_score=2, vol_min=1.2,
Donchian(10) breakout + EMA(21) pullback entries, taker fee 0.0005/side.
Both long and short entries (the reference code path takes shorts — 94 of 213
trades in the rerun are short, so the port keeps them; `long_only` is a config
flag).

## Files

- `load_data.py` — CSV → `BarDataWrangler` 4-HOUR-LAST bars + the
  `ETHUSDT-PERP.SIM` `CryptoPerpetual` (taker fee 0.0005).
- `eth_sniper_nautilus.py` — `EthSniperStrategy` (Nautilus `Strategy`
  subclass). All indicators re-implemented incrementally; exits managed with a
  resting `STOP_MARKET` order modified bar-by-bar, plus market orders for time
  exits and through-market cases.
- `check_indicators.py` — parity check of the incremental indicators vs the
  reference pandas `compute_signals()`. **PASS: every signal matches to
  ~1e-12 (machine precision) on the full ETH series.**
- `run_reference.py` — reruns the reference engine on this exact CSV with the
  combo params, plus a variant with the 95%-of-margin loss cap removed.
- `run_backtest.py` — Nautilus `BacktestEngine` (low-level API), SIM MARGIN
  venue, USDT base, default_leverage=50. Runs two variants in subprocesses.

## How to run

```powershell
$py = "E:\Duke\QuantTrade\QuantSystem\.venv-nautilus\Scripts\python.exe"
cd E:\Duke\QuantTrade\QuantSystem\scripts\nautilus_pilot
& $py check_indicators.py   # indicator parity (PASS)
& $py run_reference.py      # reference engine, with + without loss cap
& $py run_backtest.py       # Nautilus, pure_stop + liq_cap variants
```

Outputs: `reference_results.json`, `nautilus_results.json`.

## Results (daily-resampled equity, Sharpe annualized sqrt(365))

| Metric | REF (quoted combo run) | REF rerun (this CSV) | REF, loss cap removed | **NAUTILUS pure stop** | NAUTILUS w/ liq-cap emulation |
|---|---|---|---|---|---|
| Sharpe | 2.03 | 2.24 | 0.42 | **0.48** | 0.54 |
| CAGR | 82.7% | 96.4% | 8.9% | **11.5%** | 14.3% |
| Max DD | -12.1% | -12.1% | -56.3% | **-53.4%** | -47.4% |
| Trades | ~260 | 213 | 213 | **221** | 696 |
| Win rate | ~55% | 50.2% | 50.2% | **47.1%** | 18.3% |
| Final equity ($100k) | — | $6,744,233 | $170,206 | **$197,469** | $230,836 |
| Entry mix (brk/pull) | — | 93 / 120 | 93 / 120 | **92 / 130** | 239 / 458 |
| Exits (trail/stop/time) | — | 98 / 99 / 15+1 | 98 / 95 / 19+1 | **93 / 110 / 18** | 127 / 568 / 1 |

## Read on the gap — the headline result is a loss-cap artifact

The port is signal-faithful: indicators match to machine precision, and the
trade count / entry mix / exit mix of the Nautilus run track the reference
closely (221 vs 213 trades, 92/130 vs 93/120 breakout/pullback).

The performance gap is **not** fill semantics. It is the reference engine's
`_close()` loss cap:

```python
# Cap loss at margin (liquidation)
if net_pnl < -self._margin * 0.95:
    net_pnl = -self._margin * 0.95
    reason = "liquidation"
```

At 50x leverage, margin = notional/50 ≈ a ~2% adverse move, while the initial
stop sits 3.5×ATR away (usually 3–7%). So **every stop-loss (99 of 213 closes)
triggers the cap**: the position is held all the way down to the 3.5×ATR stop
(keeping every trade that dips 2–3 ATR and recovers), but when it does stop
out, only ~2–4.75% of equity is charged instead of the full 5% risked. That is
a free option no real venue offers — a real 50x position would have been
force-liquidated at the ~2% mark, converting many of the reference's
"recovered" winners into full-margin losses.

Decomposition on identical data and identical signals:

- Reference **with** cap: +6,644% (Sharpe 2.24)
- Reference **without** cap (only change): +70% (Sharpe 0.42)
- Nautilus honest port: +97% (Sharpe 0.48) — consistent with the no-cap
  reference; the residual comes from fill/fee minutiae listed below.
- Nautilus "liq-cap emulation" (stop tightened to the 0.95×margin-loss price):
  +131%, but 696 trades at 18% win rate — tightening the stop changes the
  path, so it cannot reproduce the hold-wide-stop-pay-small-loss combination.

**Implication:** the validated Sharpe 2.03 / CAGR 82.7% for this sleeve rests
on the loss-cap accounting artifact, not on entry/exit edge. Under honest
margin accounting the strategy is roughly flat-to-modest with a >50% drawdown
at these settings. (Per the house Sharpe ≥ 1.5 gate, this configuration would
not pass on an honest engine.) Worth re-examining the combo results before
scaling live capital.

## Remaining (minor) engine differences

1. **Stop fills**: Nautilus L1 bar execution fills triggered STOP_MARKET
   orders at the trigger price — same convention as the reference. Matched.
2. **Trailing lag**: the reference trails intrabar (uses the current bar's
   high to set the trail and the same bar's low to trigger it). Nautilus
   modifies the resting stop after the bar closes → one-bar lag. When the
   close is already through the desired trigger, the port closes at market
   (close price) that bar, like the reference would have.
3. **Trail deactivation**: the reference re-evaluates trail activation from
   the current close each bar (it can deactivate). Ported by modifying the
   resting trigger down when deactivated; a bar that both deactivates and
   breaches can differ.
4. **Entry fills**: both fill at the signal bar's close; Nautilus charges the
   taker fee identically (0.0005 × notional per side).
5. **PnL accounting**: reference nets only the exit fee into trade PnL (entry
   fee hits cash separately); Nautilus position PnL nets both fees — shifts a
   few near-zero trades between win/loss and slightly alters the
   consecutive-loss pause path (221 vs 213 trades).
6. **Qty rounding**: Nautilus enforces size_increment 0.001 ETH; the reference
   uses unrounded float quantities. Negligible.
7. **No forced liquidation** either way: Nautilus's SIM venue does not
   force-liquidate margin breaches by default, and the reference only caps the
   booked loss. Neither engine models real 50x liquidation mechanics; a real
   venue would be *harsher* than both.

## Nautilus pain points hit (1.230.0, Windows)

- **One engine per process**: the Rust logging layer panics
  (`Failed to initialize logging: attempted to set a logger after the logging
  system was already initialized`) when a second `BacktestEngine` is created
  in the same process, even after `dispose()`. Worked around by running each
  variant via subprocess.
- **Cython signatures are opaque** (`(*args, **kwargs)`): had to introspect
  docstrings / .pyx sources for `CryptoPerpetual` and the matching engine;
  web docs lag the installed version, so package introspection was the ground
  truth.
- **pandas 3.x deprecation warnings** (`Timestamp.utcnow`) emitted from inside
  `engine.run()` — cosmetic.
- **Fill-semantics archaeology**: whether stops fill at trigger or at the
  breaching tick under L1 bar execution is not documented prominently; found
  `is_trigger_price_fill` in `backtest/engine.pyx` (fills at trigger). This
  mattered for parity.
- Performance was a non-issue: 13.7k bars with per-bar order modifications
  runs in a few seconds per variant.
