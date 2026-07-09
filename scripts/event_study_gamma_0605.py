#!/usr/bin/env python3
"""Event study: what would Gamma Scalp have made/lost on the 2026-06-05 vol spike?

Scenario: strategy opens an ATM 5-DTE SPY straddle at 6-04 close, holds through
the 6-05 −2.6% / VIX+40% move. We price both SHORT-vol (its primary mode) and
LONG-vol (its disabled buy-vol mode), with continuous delta-hedge, and decompose
PnL into gamma / theta / vega / hedge / costs — same as the live strategy logs.

Reuses the strategy's own Black-Scholes engine so the math matches exactly.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from strategies.options.gamma_scalp_strategy import (
    bs_call, bs_put, bs_delta_call, bs_delta_put,
    bs_gamma, bs_theta_straddle, bs_vega,
)

# ── Market inputs ──
S0 = 757.09          # SPY close 6-04
S1 = 737.41          # SPY close 6-05
S1_low = 735.53      # SPY intraday low 6-05
K = 757.0            # ATM strike
r = 0.0362           # risk-free (from strategy state)
iv0 = 0.1540         # IV at entry = VIX 15.40 / 100
iv1 = 0.2157         # IV next day = VIX 21.57 / 100
T0 = 5 / 365.0       # 5 DTE at entry
T1 = 4 / 365.0       # 4 DTE next day

# ── Strategy config ──
N_CONTRACTS = 30
MULT = 100
COMMISSION = 0.65
OPT_SPREAD = 0.005
HEDGE_THRESHOLD = 0.06
EQUITY_SPREAD = 0.01

def fmt(x): return f"${x:>+12,.2f}"

print("=" * 70)
print("EVENT STUDY — Gamma Scalp on 2026-06-05 vol spike")
print("=" * 70)
print(f"  SPY: {S0:.2f} → {S1:.2f}  ({(S1/S0-1)*100:+.2f}%, intraday low {S1_low:.2f})")
print(f"  IV:  {iv0:.4f} → {iv1:.4f}  (VIX {iv0*100:.1f} → {iv1*100:.1f}, {(iv1/iv0-1)*100:+.0f}%)")
print(f"  Straddle: K={K}, {N_CONTRACTS} contracts × {MULT} mult")
print()

# ── Entry pricing (6-04 close) ──
call0 = bs_call(S0, K, T0, r, iv0)
put0 = bs_put(S0, K, T0, r, iv0)
straddle0 = call0 + put0
delta0 = bs_delta_call(S0, K, T0, r, iv0) + bs_delta_put(S0, K, T0, r, iv0)
gamma0 = bs_gamma(S0, K, T0, r, iv0)
theta0 = bs_theta_straddle(S0, K, T0, r, iv0)
vega0 = bs_vega(S0, K, T0, r, iv0)

notional_straddle = straddle0 * N_CONTRACTS * MULT
print(f"Entry straddle value: ${straddle0:.2f}/share  (call ${call0:.2f} + put ${put0:.2f})")
print(f"  Position notional:  ${notional_straddle:,.0f}")
print(f"  Greeks/share: delta={delta0:+.4f}  gamma={gamma0:.5f}  "
      f"theta={theta0:+.3f}/day  vega={vega0:+.3f}/1%vol")
print()

# Entry hedge: straddle ATM delta ≈ 0, so initial hedge ≈ 0 shares
hedge_shares0 = -delta0 * N_CONTRACTS * MULT  # neutralize
print(f"  Initial delta-hedge: {hedge_shares0:+.1f} SPY shares (≈0 ATM)")
print()

# ── Exit pricing (6-05 close) ──
call1 = bs_call(S1, K, T1, r, iv1)
put1 = bs_put(S1, K, T1, r, iv1)
straddle1 = call1 + put1

# ── Option PnL (per share) ──
opt_pnl_per_share = straddle1 - straddle0
opt_pnl_total = opt_pnl_per_share * N_CONTRACTS * MULT

# ── Greek decomposition (Taylor expansion) ──
dS = S1 - S0
dSigma = (iv1 - iv0) * 100  # in "1% vol" units for vega
dT = 1  # 1 day

gamma_pnl = 0.5 * gamma0 * (dS ** 2) * N_CONTRACTS * MULT
theta_pnl = theta0 * dT * N_CONTRACTS * MULT
vega_pnl = vega0 * dSigma * N_CONTRACTS * MULT
delta_pnl_unhedged = delta0 * dS * N_CONTRACTS * MULT

# ── Delta hedge: how many shares re-traded (for cost only) ──
# A delta-hedged book strips out the directional (delta·ΔS) component. What
# remains for the option holder is gamma + theta + vega. The hedge itself is
# the mechanism that REALIZES the gamma term — it is NOT a separate additive
# PnL (adding it would double-count the delta drift that gamma already nets).
delta1 = bs_delta_call(S1, K, T1, r, iv1) + bs_delta_put(S1, K, T1, r, iv1)
hedge_shares1 = -delta1 * N_CONTRACTS * MULT
hedge_shares_traded = abs(hedge_shares1 - hedge_shares0)

# ── Transaction costs ──
entry_commission = N_CONTRACTS * 2 * COMMISSION  # call + put
entry_spread = straddle0 * OPT_SPREAD * N_CONTRACTS * MULT
hedge_cost = hedge_shares_traded * EQUITY_SPREAD
total_costs = entry_commission + entry_spread + hedge_cost

# Sanity check: delta-hedged option reprice should ≈ gamma+theta+vega
hedged_reprice_check = gamma_pnl + theta_pnl + vega_pnl

print("─" * 70)
print("GREEK DECOMPOSITION (delta-hedged book — gamma scalp P&L attribution)")
print("─" * 70)
print(f"  Gamma PnL  (0.5·Γ·ΔS²):     {fmt(gamma_pnl)}   ← realized move payoff (BIG)")
print(f"  Theta PnL  (Θ·1day):        {fmt(theta_pnl)}   ← time decay (cost of being long)")
print(f"  Vega PnL   (ν·ΔIV):         {fmt(vega_pnl)}   ← IV +6.2 vol pts (BIG)")
print(f"  Txn costs:                  {fmt(-total_costs)}   ({hedge_shares_traded:.0f} hedge shares)")
print(f"  {'':28}{'─'*14}")
print(f"  Net (delta-hedged long):    {fmt(hedged_reprice_check - total_costs)}")
print()

# ── Scenario: LONG vol (buy straddle, delta-hedged) ──
long_vol_pnl = gamma_pnl + theta_pnl + vega_pnl - total_costs
# ── Scenario: SHORT vol (sell straddle) — greeks flip sign, costs still paid ──
short_vol_pnl = -(gamma_pnl + theta_pnl + vega_pnl) - total_costs

print("=" * 70)
print("BOTTOM LINE — what each positioning would have made today")
print("=" * 70)
print(f"  ▶ LONG vol  (buy straddle, the DISABLED mode):  {fmt(long_vol_pnl)}")
print(f"  ▶ SHORT vol (sell straddle, the PRIMARY mode):  {fmt(short_vol_pnl)}")
print(f"  ▶ FLAT      (what actually happened):           {fmt(0)}")
print()
print(f"  Pure option MTM (long straddle, no hedge):      {fmt(opt_pnl_total)}")
print(f"  Account equity base: $100,254")
print(f"    Long-vol  return on equity: {long_vol_pnl/100254*100:+.2f}%")
print(f"    Short-vol return on equity: {short_vol_pnl/100254*100:+.2f}%")
print("=" * 70)
