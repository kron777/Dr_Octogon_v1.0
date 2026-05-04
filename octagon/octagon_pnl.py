"""
octagon_pnl.py — P&L computation for paper (and eventually live) trades.

Prediction-market settlement:
  A YES token bought at price p pays $1.00 if the market resolves YES, $0 if NO.
  size_usd / entry_price = number of tokens purchased.
  Win:  payout = tokens * $1.00;  net P&L = payout - size_usd
  Loss: net P&L = -size_usd
  INVALID: treat as refund (P&L = 0).
"""

from octagon.octagon_models import Trade


def compute_paper_pnl(trade: Trade, outcome: str) -> float:
    """Return net P&L in USD for a closed paper trade."""
    if outcome == "INVALID":
        return 0.0
    win = (trade.side == "YES" and outcome == "YES") or \
          (trade.side == "NO"  and outcome == "NO")
    if win:
        tokens_purchased = trade.size_usd / trade.entry_price
        payout = tokens_purchased * 1.00
        return round(payout - trade.size_usd, 6)
    else:
        return -trade.size_usd
