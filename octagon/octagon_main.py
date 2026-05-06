"""
Main loop orchestrator.

  python -m octagon.main           — run continuously
  python -m octagon.main --once    — single cycle, then exit (smoke test / cron)
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys

import structlog

from octagon.octagon_config import CONFIG
from octagon.octagon_ledger import OctagonLedger
from octagon.octagon_models import MarketSnapshot
import octagon.octagon_scanner as scanner
import octagon.octagon_triage as triage
import octagon.octagon_research as research
import octagon.octagon_resolution_watcher as watcher
import octagon.octagon_executor as executor


def setup_logging(log_path: str) -> None:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10_000_000, backupCount=5
    )
    console_handler = logging.StreamHandler(sys.stderr)

    formatter = logging.Formatter("%(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger(__name__)


async def run_cycle(ledger: OctagonLedger) -> None:
    log.info("cycle.start")

    markets: list[MarketSnapshot] = await scanner.scan()

    recent = ledger.recent_predictions()
    candidates = triage.filter(markets, recent)

    # Log all snapshots regardless of triage outcome
    for m in markets:
        try:
            ledger.log_market_snapshot(m)
        except Exception as exc:
            log.warning("cycle.snapshot_log_failed", market_id=m.market_id, error=str(exc))

    log.info("cycle.candidates", n=len(candidates))

    for i, market in enumerate(candidates):
        if i > 0 and CONFIG.research_spacing_seconds > 0:
            await asyncio.sleep(CONFIG.research_spacing_seconds)
        prediction = None
        try:
            prediction = await research.evaluate(market)
            ledger.log_prediction(prediction)
        except Exception as exc:
            log.error(
                "cycle.research_failed",
                market_id=market.market_id,
                error=str(exc),
            )
        if prediction is not None:
            try:
                await executor.maybe_execute(prediction, market, ledger)
            except Exception as exc:
                log.error(
                    "cycle.executor_failed",
                    market_id=market.market_id,
                    error=str(exc),
                    exc_info=True,
                )

    log.info("cycle.done")


async def _main_loop(ledger: OctagonLedger, stop_event: asyncio.Event) -> None:
    async def watcher_loop() -> None:
        while not stop_event.is_set():
            try:
                await watcher.run(ledger)
            except Exception as exc:
                log.error("watcher.error", error=str(exc))
            await asyncio.sleep(3600)

    watcher_task = asyncio.create_task(watcher_loop())

    copy_task = None
    if CONFIG.copy_trade_enabled:
        log.info("copy_lane.starting")
        copy_task = asyncio.create_task(executor.run_copy_lane(ledger))

    try:
        while not stop_event.is_set():
            try:
                await run_cycle(ledger)
            except Exception as exc:
                log.error("main_loop.cycle_error", error=str(exc))
            await asyncio.sleep(CONFIG.loop_interval_seconds)
    finally:
        watcher_task.cancel()
        if copy_task:
            copy_task.cancel()
        _flush_wal(ledger.db_path)


def _flush_wal(db_path: str) -> None:
    try:
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    except Exception as exc:
        log.warning("shutdown.wal_flush_failed", error=str(exc))


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="Doctor_Octagon Phase 0")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan-triage-research-log cycle then exit",
    )
    args = parser.parse_args()

    setup_logging(CONFIG.log_path)
    log.info("octagon.start", mode="once" if args.once else "loop")

    ledger = OctagonLedger()

    if args.once:
        asyncio.run(run_cycle(ledger))
        log.info("octagon.once_done")
        sys.exit(0)

    stop_event = asyncio.Event()

    def _handle_signal(*_) -> None:
        log.info("octagon.shutdown_signal")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    asyncio.run(_main_loop(ledger, stop_event))
    log.info("octagon.stopped")
