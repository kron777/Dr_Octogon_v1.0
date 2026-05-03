"""
python -m octagon.report — daily eyeball summary.

Prints:
  • Totals: markets, predictions, resolutions, unciteable count
  • Category breakdown
  • Rolling 7-day stats (avg confidence, clarity, abs edge)
  • 5 oldest unresolved markets
  • Predictions awaiting resolution
"""

from octagon.octagon_ledger import OctagonLedger
from octagon.octagon_main import setup_logging
from octagon.octagon_config import CONFIG


def main() -> None:
    setup_logging(CONFIG.log_path)
    ledger = OctagonLedger()
    s = ledger.summary()

    print("=" * 60)
    print("DOCTOR_OCTAGON — ledger summary")
    print("=" * 60)

    print(f"\nMarkets seen:     {s['total_markets']}")
    print(f"Predictions:      {s['total_predictions']}")
    print(f"  unciteable:     {s['unciteable']}")
    print(f"  last 24h:       {s['predictions_24h']}")
    print(f"Resolutions:      {s['total_resolutions']}")

    if s.get("avg_confidence_7d") is not None:
        print(f"\n7-day stats:")
        print(f"  avg confidence:  {s['avg_confidence_7d']:.3f}")
        print(f"  avg clarity:     {s['avg_clarity_7d']:.3f}")
        print(f"  avg |edge|:      {s['avg_abs_edge_7d']:.3f}")

    if s["by_category"]:
        print("\nBy category:")
        for cat, n in sorted(s["by_category"].items(), key=lambda x: -x[1]):
            print(f"  {cat:<20} {n}")

    if s["oldest_unresolved"]:
        print("\nOldest unresolved markets:")
        for mid, question, resolves_at in s["oldest_unresolved"]:
            print(f"  [{resolves_at}] {question[:60]}  ({mid[:12]}...)")

    print()


if __name__ == "__main__":
    main()
