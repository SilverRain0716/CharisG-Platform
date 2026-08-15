"""Daily USD/KRW refresh — called by systemd timer."""
import sys

from backend.purchase.services.exchange_rate_service import update_and_store

if __name__ == "__main__":
    try:
        result = update_and_store()
        print(
            f"[fx-refresh] rate={result['rate']} updated_at={result['updated_at']}",
            flush=True,
        )
        sys.exit(0)
    except Exception as e:
        print(f"[fx-refresh] FAILED: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
