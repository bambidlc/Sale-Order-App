import argparse
from pathlib import Path

from src.odoo_sync import OdooApiError, OdooSalesOrderSyncService, SyncConfigurationError
from src.outlook_fetcher import (
    OutlookApiError,
    OutlookConfigurationError,
    OutlookFetcherService,
    OutlookNoMessagesError,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drain Shop order reports into Odoo and delete each source only after success."
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=100,
        help="Maximum successful Outlook reports to process in one scheduled run.",
    )
    args = parser.parse_args()
    if args.max_messages < 1:
        parser.error("--max-messages must be at least 1")

    base_dir = Path(__file__).resolve().parent
    try:
        fetcher = OutlookFetcherService(base_dir=base_dir)
        sync_service = OdooSalesOrderSyncService(base_dir=base_dir)

        fetcher.retry_pending_cleanup()
        processed = 0
        while processed < args.max_messages:
            try:
                fetch_summary = fetcher.fetch_latest_report()
            except OutlookNoMessagesError:
                print(f"Order automation complete: processed={processed}; Outlook folder is clear.")
                return 0

            report_path = fetch_summary["download_path"]
            summary = sync_service.sync_file(report_path, dry_run=False, force=False)
            if summary.get("failed_orders", 0):
                print(
                    "Order automation stopped: "
                    f"report={Path(report_path).name} failed_orders={summary['failed_orders']}; "
                    "source email and file were preserved."
                )
                return 1
            if summary.get("status") not in {"success", "already_synced"}:
                print(
                    "Order automation stopped on a non-success result: "
                    f"status={summary.get('status')}; source email and file were preserved."
                )
                return 1

            fetcher.complete_processing(fetch_summary)
            processed += 1
            print(
                f"Processed Shop order report {processed}/{args.max_messages}: "
                f"{Path(report_path).name}; Outlook message permanently deleted."
            )

        print(f"Order automation batch complete: processed={processed}.")
        return 0
    except (
        FileNotFoundError,
        SyncConfigurationError,
        OdooApiError,
        OutlookConfigurationError,
        OutlookApiError,
        ValueError,
    ) as exc:
        print(f"Order automation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
