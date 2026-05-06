"""
octagon_executor.py — trade execution gatekeeper.

Hard constraints (applied in order, all must pass before any order is placed):
  1. STOP file  ~/Desktop/octagon/STOP  — instant kill switch (checked at entry + post-sleep)
  2. unciteable=True  — never trade, no exceptions
  3. Execution edge >= tier.min_edge_to_trade on the chosen side
  4. Quarter-Kelly stake sizing: stake = (edge / (1-price)) * tier.kelly_fraction * bankroll
  5. tier.max_stake_usd cap
  6. tier.daily_loss_cap_usd — abort if at cap, trim stake if partial room left
  7. First-trade confirmation window (60s) — loud warning, sleep, recheck STOP
  8. LIVE_TRADING_ENABLED  — False by default (paper mode); live path raises NotImplementedError

Paper mode: trade is sized, validated, and logged exactly as if real — only the API call is skipped.
Live mode: NOT YET IMPLEMENTED.

Tier selection: get_effective_tier(bankroll) reads freq_lever.json on each decision — no restart needed.

Entry point:
    await executor.maybe_execute(prediction, market, ledger)
"""

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog

import octagon.octagon_hype_detector as hype_detector
import octagon.octagon_copy_trader as copy_trader
from octagon.octagon_config import CONFIG, get_effective_tier
from octagon.octagon_ledger import OctagonLedger
from octagon.octagon_models import MarketSnapshot, Prediction, Trade

log = structlog.get_logger(__name__)

_STOP_PATH = Path.home() / "Desktop" / "octagon" / "STOP"
_FIRST_TRADE_SLEEP_S = 60


async def maybe_execute(
    prediction: Prediction,
    market: MarketSnapshot,
    ledger: OctagonLedger,
) -> None:
    """
    Main entry point. Evaluates all constraints and either logs a paper/live
    trade or returns silently with a structured log explaining why not.
    """
    # ── 1. STOP file ─────────────────────────────────────────────────────────
    if _STOP_PATH.exists():
        log.info("executor.stop_file", market_id=market.market_id)
        return

    # ── 2. Unciteable block ──────────────────────────────────────────────────
    if prediction.unciteable:
        log.debug("executor.skip_unciteable", market_id=market.market_id)
        return

    # ── Resolve active tier (reads freq_lever.json fresh on each decision) ───
    bankroll = _effective_bankroll(ledger)
    try:
        tier, mode = get_effective_tier(bankroll)
    except Exception:
        tier, mode = CONFIG.active_tier(bankroll), "auto"

    log.info("executor.tier_active", tier=tier.name, mode=mode, market_id=market.market_id)

    # ── 3. Execution edge check ──────────────────────────────────────────────
    # YES side: buy YES tokens at ask_yes; edge = p_yes - ask_yes
    # NO side:  buy NO tokens at (1 - bid_yes); edge = bid_yes - p_yes
    yes_exec_edge = prediction.p_yes - market.ask_yes
    no_exec_edge = market.bid_yes - prediction.p_yes

    if yes_exec_edge >= no_exec_edge and yes_exec_edge >= tier.min_edge_to_trade:
        side: str = "YES"
        entry_price = market.ask_yes
        raw_kelly = yes_exec_edge / (1.0 - entry_price)
        exec_edge = yes_exec_edge
    elif no_exec_edge >= tier.min_edge_to_trade:
        side = "NO"
        entry_price = 1.0 - market.bid_yes  # NO token ask price
        raw_kelly = no_exec_edge / entry_price
        exec_edge = no_exec_edge
    else:
        log.debug(
            "executor.edge_too_small",
            market_id=market.market_id,
            yes_exec_edge=round(yes_exec_edge, 4),
            no_exec_edge=round(no_exec_edge, 4),
            threshold=tier.min_edge_to_trade,
            tier=tier.name,
        )
        return

    # ── 3b. Implausible-edge filter ───────────────────────────────────────────
    if exec_edge > tier.max_edge_to_trade:
        log.warning(
            "executor.edge_implausible",
            market_id=market.market_id,
            side=side,
            edge=round(exec_edge, 4),
            cap=tier.max_edge_to_trade,
            p_yes=round(prediction.p_yes, 4),
            market_price=round(entry_price, 4),
            tier=tier.name,
            question=market.question[:80],
        )
        return

    # ── 3c. Hype-fade gate (only active when HYPE_FADE_ENABLED=true) ─────────
    if CONFIG.hype_fade_enabled:
        hype = await hype_detector.analyze(market, prediction, ledger)

        # In hype-fade mode only NO bets on high-hype markets are allowed
        if side != "NO":
            log.debug(
                "executor.hype_fade_yes_blocked",
                market_id=market.market_id,
                side=side,
                hype_score=round(hype.hype_score, 3),
            )
            hype_detector.record_rejected(market.market_id)
            return

        if hype.hype_score < CONFIG.hype_fade_min_score:
            log.debug(
                "executor.hype_fade_score_too_low",
                market_id=market.market_id,
                hype_score=round(hype.hype_score, 3),
                threshold=CONFIG.hype_fade_min_score,
            )
            hype_detector.record_rejected(market.market_id)
            return

        if hype.whale_signal in {"fresh-yes", "insider-suspect"}:
            log.warning(
                "executor.hype_fade_whale_block",
                market_id=market.market_id,
                whale_signal=hype.whale_signal,
                hype_score=round(hype.hype_score, 3),
            )
            hype_detector.record_rejected(market.market_id)
            return

        if exec_edge < CONFIG.hype_fade_min_edge:
            log.debug(
                "executor.hype_fade_edge_too_small",
                market_id=market.market_id,
                exec_edge=round(exec_edge, 4),
                threshold=CONFIG.hype_fade_min_edge,
            )
            hype_detector.record_rejected(market.market_id)
            return

        placed = hype_detector.placed_today()
        if placed >= CONFIG.hype_fade_daily_cap:
            log.info(
                "executor.hype_fade_daily_cap",
                market_id=market.market_id,
                placed_today=placed,
                cap=CONFIG.hype_fade_daily_cap,
            )
            return

    # ── 4. Kelly stake sizing ─────────────────────────────────────────────────
    raw_stake = raw_kelly * tier.kelly_fraction * bankroll

    # ── 5. Hard cap ───────────────────────────────────────────────────────────
    stake = min(raw_stake, tier.max_stake_usd)
    if stake <= 0.0:
        return

    # ── 6. Daily loss cap ─────────────────────────────────────────────────────
    today_loss = ledger.today_paper_loss_usd()
    remaining_cap = tier.daily_loss_cap_usd - today_loss
    if remaining_cap <= 0.0:
        log.warning(
            "executor.daily_loss_cap_reached",
            today_loss=round(today_loss, 4),
            cap=tier.daily_loss_cap_usd,
            tier=tier.name,
            market_id=market.market_id,
        )
        return
    stake = min(stake, remaining_cap)

    # ── 7. First-trade confirmation window ────────────────────────────────────
    if ledger.total_trades() == 0:
        log.warning(
            "executor.first_trade_window",
            market_id=market.market_id,
            side=side,
            stake_usd=round(stake, 4),
            entry_price=round(entry_price, 4),
            exec_edge=round(exec_edge, 4),
            tier=tier.name,
            question=market.question[:80],
            msg="First trade ever — sleeping 60s. Write STOP file now to abort.",
        )
        await asyncio.sleep(_FIRST_TRADE_SLEEP_S)
        if _STOP_PATH.exists():
            log.info("executor.first_trade_aborted_by_stop", market_id=market.market_id)
            return

    # ── 8. Paper vs live ──────────────────────────────────────────────────────
    if CONFIG.live_trading_enabled:
        raise NotImplementedError(
            "Live trading not yet implemented. "
            "Set LIVE_TRADING_ENABLED=false to continue in paper mode."
        )

    # Paper trade — log identically to a live trade, API call omitted
    log.info(
        "executor.paper_trade",
        market_id=market.market_id,
        side=side,
        stake_usd=round(stake, 4),
        entry_price=round(entry_price, 4),
        exec_edge=round(exec_edge, 4),
        p_yes=round(prediction.p_yes, 4),
        confidence=round(prediction.confidence, 4),
        tier=tier.name,
        question=market.question[:80],
    )

    trade = Trade(
        trade_id=str(uuid.uuid4()),
        prediction_id=prediction.prediction_id,
        market_id=market.market_id,
        side=side,
        entry_price=entry_price,
        size_usd=stake,
        entered_at=datetime.utcnow(),
    )
    ledger.log_trade(trade, paper=True)

    if CONFIG.hype_fade_enabled:
        hype_detector.record_placed(market.market_id)


def _effective_bankroll(ledger: OctagonLedger) -> float:
    return ledger.current_bankroll()


async def execute_copy_signal(
    signal: "copy_trader.CopySignal",
    ledger: OctagonLedger,
) -> None:
    """
    Entry point for copy-trade signals from run_copy_watcher().
    Delegates to copy_trader.maybe_copy_execute() which owns all risk caps.
    Only active when COPY_TRADE_ENABLED=true.
    """
    if not CONFIG.copy_trade_enabled:
        return
    bankroll = _effective_bankroll(ledger)
    await copy_trader.maybe_copy_execute(signal, ledger, bankroll)


async def run_copy_lane(ledger: OctagonLedger) -> None:
    """
    Long-running coroutine that owns the copy-trading signal queue.
    Call this from octagon.main alongside the existing scan loop when
    COPY_TRADE_ENABLED=true.
    """
    signal_queue: asyncio.Queue = asyncio.Queue()
    watcher = asyncio.create_task(copy_trader.run_copy_watcher(signal_queue))

    try:
        while True:
            signal = await signal_queue.get()
            await execute_copy_signal(signal, ledger)
            signal_queue.task_done()
    except asyncio.CancelledError:
        watcher.cancel()
        raise
