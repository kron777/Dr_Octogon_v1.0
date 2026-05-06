"""
octagon_copy_trader.py — copy-trading lane for Octagon.

Architecture
────────────
  select_wallets()        Fetches Polymarket leaderboard (ALL-time PnL), enriches each
                          candidate with per-wallet activity stats, and returns the top
                          CONFIG.copy_trade_max_wallets wallets that pass quality filters.

  run_copy_watcher()      Long-running async loop that:
                            • Refreshes the wallet list every COPY_TRADE_WALLET_REFRESH_S
                            • Polls each wallet's /activity every COPY_TRADE_POLL_INTERVAL_S
                            • Emits CopySignal to an asyncio.Queue on new BUY events

  maybe_copy_execute()    Executor path for copy signals. Applies risk caps and writes
                          a paper trade with copy_source set for P&L attribution.

  read_hud_data()         Returns dict consumed by HUD server for ARM 10 panel.

Wallet quality filters (all must pass)
───────────────────────────────────────
  win_rate         > COPY_TRADE_MIN_WIN_RATE (default 0.70)
    Computed as: REDEEM events / (REDEEM + inferred_loss events) from activity page.
    "Inferred loss" = BUY events whose conditionId never appears in a REDEEM.
    KNOWN LIMITATION: Only the most recent 200 activity events are sampled; wallets
    with very high activity may have win_rate underestimated.
  trade_count      > COPY_TRADE_MIN_RESOLVED (default 100)
    Approximated as BUY TRADE event count from activity (200-event page).
    Drops wash-traders who only MERGE/SPLIT.
  last_active      within 30 days
    Most recent TRADE timestamp from activity.
  median_size      > COPY_TRADE_MIN_MEDIAN_USD (default $10)
    Median of usdcSize across BUY TRADE events. Drops airdrop farmers (~25% of volume).

Copy signal
───────────
  Emitted only for NEW BUY TRADE events (side=BUY) seen since last poll.
  SELL trades are skipped — whale exit intent is opaque.

Risk caps (enforced in maybe_copy_execute)
──────────────────────────────────────────
  per-trade:    min(5% of bankroll, MAX_STAKE)
  daily cap:    5% of starting bankroll → halt ALL copying for the day
  drawdown:     any wallet at -15% over tracked period → dropped from active list
"""

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
import structlog

from octagon.octagon_config import CONFIG
from octagon.octagon_models import Trade

log = structlog.get_logger(__name__)

_LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
_ACTIVITY_URL = "https://data-api.polymarket.com/activity"
_HEADERS = {"User-Agent": "octagon/1.0 (+prediction-research)"}

_ACTIVITY_PAGE = 200     # events per wallet fetch
_ACTIVITY_DAYS = 30      # recency window
_MAX_FETCH_WALLETS = 50  # candidates pulled from leaderboard before filtering


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class WalletStats:
    proxy_wallet: str
    user_name: str
    pnl: float               # all-time USD PnL from leaderboard
    vol: float               # all-time volume from leaderboard
    win_rate: float          # estimated from activity sample
    trade_count: int         # BUY events in activity sample
    median_size_usd: float   # median BUY size from activity sample
    last_active_ts: int      # unix timestamp of most recent trade
    pnl_30d: float           # running copy P&L tracked by this module (starts 0)


@dataclass
class CopySignal:
    wallet: str
    market_id: str          # Polymarket conditionId (used as market_id)
    side: str               # always "BUY" (SELL signals are dropped)
    whale_size: float       # usdcSize from the activity event
    whale_price: float      # price from the activity event
    condition_id: str
    title: str
    timestamp: int          # unix timestamp


# ── Shared mutable state (module-level, guarded by asyncio single-thread) ────

_active_wallets: list[WalletStats] = []
_last_wallet_refresh: float = 0.0
_last_seen_ts: dict[str, int] = {}          # wallet → last activity timestamp
_copy_events: list[dict] = []               # ring buffer of last 20 signal events
_wallet_refresh_lock = asyncio.Lock()


# ── Leaderboard fetch ─────────────────────────────────────────────────────────

async def _fetch_leaderboard(limit: int = _MAX_FETCH_WALLETS) -> list[dict]:
    """
    Fetch top-PnL wallets from Polymarket MONTH leaderboard.

    MONTH is used instead of ALL because the ALL-time leaderboard is dominated
    by one-time election bettors who haven't traded in 500+ days. MONTH ensures
    candidates are currently active, satisfying the 30-day recency filter without
    needing to paginate their full activity history.
    """
    async with httpx.AsyncClient(headers=_HEADERS, timeout=15) as client:
        resp = await client.get(
            _LEADERBOARD_URL,
            params={"timePeriod": "MONTH", "orderBy": "PNL", "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list) or len(data) == 0:
        raise RuntimeError(
            f"Leaderboard returned empty or malformed response: {str(data)[:200]}"
        )
    return data


async def _fetch_activity(wallet: str, limit: int = _ACTIVITY_PAGE) -> list[dict]:
    """Fetch recent activity events for a wallet."""
    async with httpx.AsyncClient(headers=_HEADERS, timeout=15) as client:
        resp = await client.get(
            _ACTIVITY_URL,
            params={"user": wallet, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json() or []


def _compute_stats(wallet: str, leaderboard_row: dict, activity: list[dict]) -> WalletStats:
    """
    Derive quality metrics from leaderboard row + activity sample.

    Win-rate method: track unique conditionIds. A conditionId with a REDEEM event
    is a WIN. A conditionId with BUY trades but no REDEEM is classified as LOSS
    (either still open or settled against us). Ratio = REDEEMs / (REDEEMs + losses).

    Limitation: only the most recent 200 activity events are sampled. High-frequency
    wallets will have trade_count underestimated and win_rate may be skewed.
    """
    buys = [e for e in activity if e.get("type") == "TRADE" and e.get("side") == "BUY"]
    redeems = [e for e in activity if e.get("type") == "REDEEM"]

    trade_count = len(buys)

    buy_conditions = {e["conditionId"] for e in buys if e.get("conditionId")}
    redeem_conditions = {e["conditionId"] for e in redeems if e.get("conditionId")}
    wins = len(buy_conditions & redeem_conditions)
    losses = len(buy_conditions - redeem_conditions)
    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0

    sizes = sorted(e.get("usdcSize", 0.0) for e in buys)
    median_size = sizes[len(sizes) // 2] if sizes else 0.0

    all_timestamps = [e.get("timestamp", 0) for e in activity if e.get("timestamp")]
    last_active_ts = max(all_timestamps) if all_timestamps else 0

    return WalletStats(
        proxy_wallet=wallet,
        user_name=leaderboard_row.get("userName", wallet[:10]),
        pnl=float(leaderboard_row.get("pnl", 0.0)),
        vol=float(leaderboard_row.get("vol", 0.0)),
        win_rate=round(win_rate, 4),
        trade_count=trade_count,
        median_size_usd=round(median_size, 2),
        last_active_ts=last_active_ts,
        pnl_30d=0.0,
    )


async def select_wallets() -> list[WalletStats]:
    """
    Fetch leaderboard, enrich each candidate with activity stats, apply filters.
    Returns top CONFIG.copy_trade_max_wallets wallets.

    Raises RuntimeError if leaderboard returns nothing — caller must surface, not swallow.
    """
    lb_rows = await _fetch_leaderboard(_MAX_FETCH_WALLETS)
    log.info("copy.leaderboard_fetched", count=len(lb_rows))

    cutoff_ts = int(time.time()) - (_ACTIVITY_DAYS * 86400)
    qualified: list[WalletStats] = []

    for row in lb_rows:
        wallet = row.get("proxyWallet", "")
        if not wallet:
            continue
        try:
            activity = await _fetch_activity(wallet)
        except Exception as exc:
            log.warning("copy.activity_fetch_failed", wallet=wallet[:12], error=str(exc)[:80])
            continue

        stats = _compute_stats(wallet, row, activity)

        # ── Apply quality filters ──────────────────────────────────────────────
        if stats.last_active_ts < cutoff_ts:
            log.debug("copy.filter_inactive", wallet=wallet[:12],
                      days_ago=round((time.time() - stats.last_active_ts) / 86400, 1))
            continue
        if stats.trade_count < CONFIG.copy_trade_min_resolved:
            log.debug("copy.filter_trade_count", wallet=wallet[:12],
                      count=stats.trade_count, min=CONFIG.copy_trade_min_resolved)
            continue
        if stats.win_rate < CONFIG.copy_trade_min_win_rate:
            log.debug("copy.filter_win_rate", wallet=wallet[:12],
                      wr=stats.win_rate, min=CONFIG.copy_trade_min_win_rate)
            continue
        if stats.median_size_usd < CONFIG.copy_trade_min_median_usd:
            log.debug("copy.filter_median_size", wallet=wallet[:12],
                      median=stats.median_size_usd, min=CONFIG.copy_trade_min_median_usd)
            continue

        log.info(
            "copy.wallet_qualified",
            wallet=wallet[:12],
            user=stats.user_name,
            win_rate=stats.win_rate,
            trades=stats.trade_count,
            median_usd=stats.median_size_usd,
            pnl=round(stats.pnl, 0),
        )
        qualified.append(stats)
        if len(qualified) >= CONFIG.copy_trade_max_wallets:
            break

    return qualified


# ── Position poller ───────────────────────────────────────────────────────────

async def _poll_wallet(wallet_stats: WalletStats, signal_queue: asyncio.Queue) -> None:
    """
    Fetch recent activity for one wallet. If new BUY TRADE events appeared
    since last poll, emit CopySignal for each.

    Updates _last_seen_ts[wallet] and _copy_events ring buffer.
    """
    wallet = wallet_stats.proxy_wallet
    try:
        activity = await _fetch_activity(wallet, limit=10)
    except Exception as exc:
        log.warning("copy.poll_failed", wallet=wallet[:12], error=str(exc)[:80])
        return

    prev_ts = _last_seen_ts.get(wallet, 0)
    new_buys = [
        e for e in activity
        if e.get("type") == "TRADE"
        and e.get("side") == "BUY"
        and e.get("timestamp", 0) > prev_ts
    ]

    if new_buys:
        latest_ts = max(e["timestamp"] for e in new_buys)
        _last_seen_ts[wallet] = latest_ts

        for event in new_buys:
            sig = CopySignal(
                wallet=wallet,
                market_id=event.get("conditionId", ""),
                side="BUY",
                whale_size=float(event.get("usdcSize", 0.0)),
                whale_price=float(event.get("price", 0.5)),
                condition_id=event.get("conditionId", ""),
                title=event.get("title", "")[:80],
                timestamp=event.get("timestamp", int(time.time())),
            )
            if not sig.market_id:
                continue
            log.info(
                "copy.signal_detected",
                wallet=wallet[:12],
                user=wallet_stats.user_name,
                market=sig.title[:50],
                side=sig.side,
                whale_size=round(sig.whale_size, 2),
                whale_price=sig.whale_price,
            )
            await signal_queue.put(sig)

            # update ring buffer for HUD
            _copy_events.insert(0, {
                "wallet": wallet[:12],
                "user": wallet_stats.user_name,
                "title": sig.title,
                "side": sig.side,
                "whale_size": round(sig.whale_size, 2),
                "whale_price": sig.whale_price,
                "timestamp": sig.timestamp,
                "status": "pending",
            })
            del _copy_events[20:]  # keep last 20
    else:
        # On first poll, seed the timestamp to avoid replaying history
        if wallet not in _last_seen_ts and activity:
            newest = max((e.get("timestamp", 0) for e in activity), default=0)
            _last_seen_ts[wallet] = newest
            log.debug("copy.poll_seeded", wallet=wallet[:12], ts=newest)


# ── Main watcher loop ─────────────────────────────────────────────────────────

async def run_copy_watcher(signal_queue: asyncio.Queue) -> None:
    """
    Long-running coroutine. Refreshes wallet list periodically, then polls
    each wallet on COPY_TRADE_POLL_INTERVAL_S. Runs until task is cancelled.
    """
    global _active_wallets, _last_wallet_refresh

    log.info("copy.watcher_started", poll_s=CONFIG.copy_trade_poll_interval_s)

    while True:
        now = time.time()

        # ── Refresh wallet list if stale ─────────────────────────────────────
        if now - _last_wallet_refresh >= CONFIG.copy_trade_wallet_refresh_s:
            async with _wallet_refresh_lock:
                try:
                    fresh = await select_wallets()
                    if not fresh:
                        log.warning(
                            "copy.no_wallets_after_filter",
                            msg="Leaderboard returned data but no wallet passed filters. "
                                "Check filter thresholds.",
                        )
                    else:
                        # Preserve pnl_30d for wallets carried over from previous list
                        prev_pnl = {w.proxy_wallet: w.pnl_30d for w in _active_wallets}
                        for w in fresh:
                            w.pnl_30d = prev_pnl.get(w.proxy_wallet, 0.0)
                        _active_wallets = fresh
                        _last_wallet_refresh = now
                        _save_hud_state()
                        log.info("copy.wallets_refreshed", count=len(_active_wallets),
                                 wallets=[w.user_name for w in _active_wallets])
                except RuntimeError as exc:
                    # Leaderboard malformed — surface and stop (don't fall back)
                    log.error("copy.leaderboard_failed", error=str(exc))
                    await asyncio.sleep(60)
                    continue
                except Exception as exc:
                    log.warning("copy.wallet_refresh_error", error=str(exc)[:120])

        # ── Drawdown check — drop wallets at -15% ─────────────────────────────
        threshold = CONFIG.starting_bankroll_usd * CONFIG.copy_trade_drawdown_stop_pct
        active = [w for w in _active_wallets if w.pnl_30d > -threshold]
        dropped = [w for w in _active_wallets if w.pnl_30d <= -threshold]
        for w in dropped:
            log.warning("copy.wallet_dropped_drawdown", wallet=w.proxy_wallet[:12],
                        user=w.user_name, pnl_30d=round(w.pnl_30d, 2))
        _active_wallets = active

        # ── Poll each active wallet ───────────────────────────────────────────
        if _active_wallets:
            await asyncio.gather(
                *[_poll_wallet(w, signal_queue) for w in _active_wallets],
                return_exceptions=True,
            )

        await asyncio.sleep(CONFIG.copy_trade_poll_interval_s)


# ── Executor integration ──────────────────────────────────────────────────────

async def maybe_copy_execute(
    signal: CopySignal,
    ledger,        # OctagonLedger — avoid circular import with TYPE_CHECKING
    bankroll: float,
) -> bool:
    """
    Risk-gate and execute a paper trade from a copy signal.

    Gates applied (no LLM forecaster — the whale is the forecaster):
      1. STOP file
      2. Daily copy-trade loss cap (5% of starting bankroll)
      3. Per-trade size cap (5% of current bankroll, capped at MAX_STAKE)
      4. Whale size must be > 0

    Returns True if trade was placed, False if gated out.
    """
    from pathlib import Path as _Path

    stop_path = _Path.home() / "Desktop" / "octagon" / "STOP"
    if stop_path.exists():
        log.info("copy.stop_file", market_id=signal.market_id)
        return False

    if signal.whale_size <= 0:
        log.debug("copy.skip_zero_size", market_id=signal.market_id)
        return False

    # Daily loss cap
    daily_loss = ledger.copy_trade_daily_loss_usd()
    daily_cap = CONFIG.starting_bankroll_usd * CONFIG.copy_trade_daily_loss_cap_pct
    if daily_loss >= daily_cap:
        log.warning(
            "copy.daily_cap_reached",
            daily_loss=round(daily_loss, 2),
            cap=round(daily_cap, 2),
            market_id=signal.market_id,
        )
        return False

    # Size: whale_size × ratio, capped at 5% bankroll and MAX_STAKE
    from octagon.octagon_config import CONFIG as _cfg
    tier_max = _cfg.max_stake_usd
    size_from_whale = signal.whale_size * _cfg.copy_trade_ratio
    size_from_bankroll = bankroll * 0.05
    stake = min(size_from_whale, size_from_bankroll, tier_max)
    stake = max(0.01, stake)

    # Clip to remaining daily cap room
    remaining = daily_cap - daily_loss
    stake = min(stake, remaining)
    if stake <= 0:
        return False

    trade_id = str(uuid.uuid4())
    # copy trades use a synthetic prediction_id; no LLM prediction exists
    synthetic_pred_id = f"copy-{signal.wallet[:8]}-{trade_id[:8]}"

    # Ensure market row exists (copy trade may reference a market not yet in DB)
    _ensure_market_row(ledger, signal)

    # Ensure prediction row exists (trades FK references predictions)
    _ensure_prediction_row(ledger, signal, synthetic_pred_id)

    trade = Trade(
        trade_id=trade_id,
        prediction_id=synthetic_pred_id,
        market_id=signal.condition_id,
        side=signal.side,
        entry_price=signal.whale_price,
        size_usd=round(stake, 4),
        entered_at=datetime.utcnow(),
    )

    ledger.log_copy_trade(trade, copy_source=signal.wallet, paper=True)

    # Update pnl_30d tracker (open trade = negative exposure; will resolve later)
    for w in _active_wallets:
        if w.proxy_wallet == signal.wallet:
            w.pnl_30d -= stake  # pessimistic until resolution
            break

    _save_hud_state()

    log.info(
        "copy.trade_placed",
        market_id=signal.market_id,
        side=signal.side,
        stake=round(stake, 4),
        whale_size=round(signal.whale_size, 2),
        copy_source=signal.wallet[:12],
        title=signal.title,
    )

    # Update the last copy event in ring buffer to "placed"
    for ev in _copy_events:
        if ev.get("wallet") == signal.wallet[:12] and ev.get("title") == signal.title:
            ev["status"] = "placed"
            ev["copy_size"] = round(stake, 4)
            break

    return True


def _ensure_market_row(ledger, signal: CopySignal) -> None:
    """Insert a minimal market row if not already present."""
    import sqlite3 as _sq
    con = _sq.connect(ledger.db_path)
    existing = con.execute(
        "SELECT 1 FROM markets WHERE market_id = ?", (signal.condition_id,)
    ).fetchone()
    if not existing:
        con.execute(
            """INSERT OR IGNORE INTO markets
               (market_id, question, category, resolution_criteria,
                resolves_at, first_seen_at, last_seen_at)
               VALUES (?, ?, 'Copy', '', '', ?, ?)""",
            (signal.condition_id, signal.title or signal.condition_id,
             datetime.utcnow().isoformat(), datetime.utcnow().isoformat()),
        )
        con.commit()
    con.close()


def _ensure_prediction_row(ledger, signal: CopySignal, pred_id: str) -> None:
    """Insert a minimal prediction row to satisfy the FK constraint."""
    import sqlite3 as _sq
    con = _sq.connect(ledger.db_path)
    con.execute(
        """INSERT OR IGNORE INTO predictions
           (prediction_id, market_id, p_yes, p_yes_raw, confidence,
            edge, market_price_at_prediction, resolution_clarity,
            unciteable, base_rate, base_rate_reference_class,
            edge_cases, predicted_at, ttl_seconds, reasoning_trace_path, model_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0.5, 'copy', '[]', ?, 3600, '', 'copy')""",
        (pred_id, signal.condition_id,
         signal.whale_price, signal.whale_price, 1.0,
         0.0, signal.whale_price, 1.0,
         datetime.utcnow().isoformat()),
    )
    con.commit()
    con.close()


# ── HUD data ──────────────────────────────────────────────────────────────────

def _save_hud_state() -> None:
    path = Path(CONFIG.copy_trade_hud_path)
    try:
        state = {
            "enabled": CONFIG.copy_trade_enabled,
            "active_wallets": [asdict(w) for w in _active_wallets],
            "last_events": _copy_events[:5],
            "updated_at": datetime.utcnow().isoformat(),
        }
        path.write_text(json.dumps(state, default=str))
    except Exception as exc:
        log.warning("copy.hud_save_failed", error=str(exc)[:80])


def read_hud_data() -> dict:
    """Return ARM 10 data for the HUD server."""
    path = Path(CONFIG.copy_trade_hud_path)
    try:
        if path.exists():
            data = json.loads(path.read_text())
            age_s = (datetime.utcnow() - datetime.fromisoformat(
                data.get("updated_at", "2000-01-01T00:00:00")
            )).total_seconds()
            data["stale"] = age_s > 120
            return data
    except Exception:
        pass
    return {
        "enabled": CONFIG.copy_trade_enabled,
        "active_wallets": [],
        "last_events": [],
        "stale": True,
        "updated_at": None,
    }
