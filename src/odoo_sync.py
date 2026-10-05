import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

import requests
from .network_resilience import READ_METHODS, NetworkRequestError, atomic_write_json, request_with_retry

from src.cron_report_parser import OrderLineDraft, SalesOrderDraft, build_sales_order_drafts, parse_rows


class SyncConfigurationError(RuntimeError):
    pass


class OdooApiError(RuntimeError):
    pass


class OdooAmbiguousWriteError(OdooApiError):
    """A lost response may hide a committed write; discard cached misses."""
    pass


@dataclass(slots=True, frozen=True)
class SyncSettings:
    base_dir: Path
    download_path: Path
    state_file: Path
    log_file: Path
    allowed_extensions: tuple[str, ...]
    odoo_base_url: str
    odoo_database: str
    odoo_api_token: str


@dataclass(slots=True, frozen=True)
class ReportFile:
    path: Path
    modified_at: str
    size_bytes: int
    fingerprint: str


@dataclass(slots=True)
class ProductResolution:
    product_id: int
    uom_id: int | None
    display_name: str


@dataclass(slots=True)
class SyncOrderResult:
    order_name: str
    source_document: str
    customer_number: str
    customer_name: str
    salesperson_name: str
    line_count: int
    status: str
    message: str
    sale_order_id: int | None = None
    partner_id: int | None = None
    created_partner_id: int | None = None
    created_product_skus: list[str] = field(default_factory=list)
    planned_actions: list[str] = field(default_factory=list)
    unmatched_skus: list[str] = field(default_factory=list)


def _short_path(path_value: str) -> str:
    try:
        return Path(path_value).name
    except Exception:
        return path_value


def _format_actions(actions: list[str]) -> str:
    return ", ".join(actions)


def _format_order_summary_line(order: dict[str, Any]) -> str:
    parts = [
        f"[{order.get('status', '').upper()}]",
        str(order.get("order_name") or order.get("source_document") or "unknown"),
        f"cust={order.get('customer_number') or '-'}",
        f"lines={order.get('line_count') or 0}",
    ]
    if order.get("sale_order_id"):
        parts.append(f"sale_order={order['sale_order_id']}")
    if order.get("partner_id"):
        parts.append(f"partner={order['partner_id']}")
    if order.get("created_partner_id"):
        parts.append(f"new_partner={order['created_partner_id']}")
    if order.get("created_product_skus"):
        parts.append(f"new_products={','.join(order['created_product_skus'])}")
    if order.get("planned_actions"):
        parts.append(f"actions={_format_actions(order['planned_actions'])}")
    if order.get("unmatched_skus"):
        parts.append(f"unmatched={','.join(order['unmatched_skus'])}")
    return " | ".join(parts)


def format_preview_summary(preview: dict[str, Any]) -> str:
    lines = [
        "Preview",
        f"  Report : {_short_path(str(preview.get('report_path', '')))}",
        f"  Orders : {preview.get('order_count', 0)}",
    ]
    orders = preview.get("orders", [])
    if orders:
        lines.append("")
        lines.append("First Orders")
        for order in orders[:10]:
            lines.append(
                "  "
                + " | ".join(
                    [
                        str(order.get("order_name") or order.get("source_document") or "unknown"),
                        f"store={order.get('store_number') or '-'}",
                        f"cust={order.get('customer_number') or '-'}",
                        f"lines={order.get('line_count') or 0}",
                        f"date={order.get('x_studio_fecha') or order.get('order_date_odoo') or '-'}",
                    ]
                )
            )
        if len(orders) > 10:
            lines.append(f"  ... {len(orders) - 10} more")
    return "\n".join(lines)


def format_sync_summary(
    summary: dict[str, Any],
    *,
    include_orders: bool = False,
    max_orders: int | None = None,
) -> str:
    lines = [
        "Sync Summary",
        f"  Status  : {summary.get('status', '-')}",
        f"  Mode    : {summary.get('mode', '-')}",
        f"  Report  : {_short_path(str(summary.get('report_path', '')))}",
    ]

    if summary.get("report_modified_at"):
        lines.append(f"  Updated : {summary['report_modified_at']}")
    if summary.get("total_orders") is not None:
        lines.append(f"  Orders  : {summary.get('total_orders', 0)}")
        lines.append(
            "  Result  : "
            + ", ".join(
                [
                    f"created={summary.get('created_orders', 0)}",
                    f"ready={summary.get('ready_orders', 0)}",
                    f"skipped={summary.get('skipped_existing_orders', 0)}",
                    f"failed={summary.get('failed_orders', 0)}",
                ]
            )
        )
    if summary.get("message"):
        lines.append(f"  Note    : {summary['message']}")

    orders = summary.get("orders", [])
    if not orders:
        return "\n".join(lines)

    if include_orders:
        selected_orders = orders
    else:
        selected_orders = [
            order
            for order in orders
            if order.get("status") == "failed"
            or order.get("planned_actions")
            or order.get("created_partner_id")
            or order.get("created_product_skus")
            or order.get("unmatched_skus")
        ]

    if max_orders is not None:
        selected_orders = selected_orders[:max_orders]

    if selected_orders:
        lines.append("")
        lines.append("Orders")
        for order in selected_orders:
            lines.append(f"  {_format_order_summary_line(order)}")
    elif include_orders:
        lines.append("")
        lines.append("Orders")
        lines.append("  none")

    if max_orders is not None and len(orders) > len(selected_orders) and include_orders:
        lines.append(f"  ... {len(orders) - len(selected_orders)} more")

    return "\n".join(lines)


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _resolve_path(base_dir: Path, raw_value: str, fallback_name: str) -> Path:
    value = raw_value.strip() if raw_value else fallback_name
    path = Path(value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _parse_extensions(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return (".csv", ".xlsx", ".xls")

    parts = [part.strip() for chunk in raw_value.split(",") for part in chunk.split()]
    normalized = []
    for part in parts:
        if not part:
            continue
        normalized.append(part if part.startswith(".") else f".{part}")
    extensions = tuple(sorted({ext.lower() for ext in normalized}))
    return extensions or (".csv", ".xlsx", ".xls")


def load_sync_settings(base_dir: str | Path | None = None) -> SyncSettings:
    root = Path(base_dir or Path.cwd()).resolve()
    env_values = _parse_env_file(root / ".env")

    def get_value(key: str, *, required: bool = False, default: str = "") -> str:
        value = env_values.get(key) or default
        if required and not value:
            raise SyncConfigurationError(f"Missing required setting: {key}")
        return value

    return SyncSettings(
        base_dir=root,
        download_path=_resolve_path(root, get_value("DOWNLOAD_PATH", default="downloads"), "downloads"),
        state_file=_resolve_path(root, get_value("STATE_FILE", default=".sync_state.json"), ".sync_state.json"),
        log_file=_resolve_path(root, get_value("LOG_FILE", default="odoo_sync.log"), "odoo_sync.log"),
        allowed_extensions=_parse_extensions(get_value("ALLOWED_EXTENSIONS", default=".csv,.xlsx,.xls")),
        odoo_base_url=get_value("ODOO_BASE_URL", required=True).rstrip("/"),
        odoo_database=get_value("ODOO_DATABASE", required=True),
        odoo_api_token=get_value("ODOO_API_TOKEN", required=True),
    )


def _get_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("sale_order_app.odoo_sync")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_file.parent.mkdir(parents=True, exist_ok=True)
    target = str(log_file)
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == target:
            return logger

    handler = RotatingFileHandler(target, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_latest_report(settings: SyncSettings) -> ReportFile:
    if not settings.download_path.exists():
        raise FileNotFoundError(
            f"DOWNLOAD_PATH does not exist in .env: {settings.download_path}"
        )

    candidates = [
        path
        for path in settings.download_path.rglob("*")
        if path.is_file()
        and path.suffix.lower() in settings.allowed_extensions
        and not path.name.startswith("~$")
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No report files found in DOWNLOAD_PATH {settings.download_path} for extensions {settings.allowed_extensions}"
        )

    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    stat = latest.stat()
    return ReportFile(
        path=latest.resolve(),
        modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        size_bytes=stat.st_size,
        fingerprint=_file_fingerprint(latest),
    )


class SyncStateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def is_successfully_synced_report(self, report: ReportFile) -> bool:
        state = self.load().get("odoo_sync", {})
        return (
            state.get("fingerprint") == report.fingerprint
            and state.get("summary", {}).get("status") == "success"
        )

    def save_summary(self, report: ReportFile, summary: dict[str, Any]) -> None:
        state = self.load()
        state["odoo_sync"] = {
            "fingerprint": report.fingerprint,
            "path": str(report.path),
            "modified_at": report.modified_at,
            "synced_at": datetime.now().isoformat(),
            "summary": summary,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, state)


class OdooJson2Client:
    def __init__(self, settings: SyncSettings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.session = requests.Session()
        self.base_url = f"{settings.odoo_base_url}/json/2"
        self.headers = {
            "Authorization": f"bearer {settings.odoo_api_token}",
            "Content-Type": "application/json; charset=utf-8",
            "X-Odoo-Database": settings.odoo_database,
            "User-Agent": "sale-order-app/odoo-sync",
        }
        self._fields_cache: dict[str, dict[str, Any]] = {}

    def call(self, model: str, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{model}/{method}"
        try:
            return request_with_retry(
                self.session.post, url=url, headers=self.headers, json=payload or {},
                replay_safe=method in READ_METHODS, operation=f'Orders Odoo {model}/{method}',
                logger=self.logger, decode=lambda response: response.json() if response.content else None,
            )
        except NetworkRequestError as exc:
            error_class = OdooAmbiguousWriteError if exc.uncertain else OdooApiError
            raise error_class(str(exc)) from exc

    def validate_connection(self) -> dict[str, Any]:
        return self.call("res.users", "context_get", {})

    def fields_get(self, model: str) -> dict[str, Any]:
        cached = self._fields_cache.get(model)
        if cached is not None:
            return cached
        fields = self.call(model, "fields_get", {"attributes": ["type", "relation"]})
        self._fields_cache[model] = fields
        return fields

    def search_read(
        self,
        model: str,
        *,
        domain: list[Any],
        fields: list[str],
        limit: int | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"domain": domain, "fields": fields}
        if limit is not None:
            payload["limit"] = limit
        if order:
            payload["order"] = order
        return self.call(model, "search_read", payload)

    def create(self, model: str, values: dict[str, Any]) -> int:
        result = self.call(model, "create", {"vals_list": [values]})
        if isinstance(result, list):
            if not result:
                raise OdooApiError(f"Odoo create returned an empty result for {model}")
            return int(result[0])
        return int(result)

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        return self.call(model, "read", {"ids": ids, "fields": fields})

    def unlink(self, model: str, ids: list[int]) -> bool:
        return bool(self.call(model, "unlink", {"ids": ids}))


def _many2one_id(value: Any) -> int | None:
    if isinstance(value, list) and value:
        return int(value[0])
    if isinstance(value, tuple) and value:
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


class OdooSalesOrderSyncService:
    def __init__(self, base_dir: str | Path | None = None):
        self.settings = load_sync_settings(base_dir)
        self.logger = _get_logger(self.settings.log_file)
        self.state_store = SyncStateStore(self.settings.state_file)
        self.client = OdooJson2Client(self.settings, self.logger)
        self.partner_cache: dict[str, dict[str, Any] | None] = {}
        self.product_cache: dict[str, ProductResolution | None] = {}
        self.warehouse_cache: dict[str, int | None] = {}
        self.sale_order_fields: dict[str, Any] | None = None
        self.sale_order_line_fields: dict[str, Any] | None = None
        self.partner_fields: dict[str, Any] | None = None
        self.product_template_fields: dict[str, Any] | None = None
        self.default_uom_id: int | None = None

    def validate_connection(self) -> dict[str, Any]:
        return self.client.validate_connection()

    def _get_sale_order_fields(self) -> dict[str, Any]:
        if self.sale_order_fields is None:
            self.sale_order_fields = self.client.fields_get("sale.order")
        return self.sale_order_fields

    def _get_partner_fields(self) -> dict[str, Any]:
        if self.partner_fields is None:
            self.partner_fields = self.client.fields_get("res.partner")
        return self.partner_fields

    def _get_sale_order_line_fields(self) -> dict[str, Any]:
        if self.sale_order_line_fields is None:
            self.sale_order_line_fields = self.client.fields_get("sale.order.line")
        return self.sale_order_line_fields

    def _get_product_template_fields(self) -> dict[str, Any]:
        if self.product_template_fields is None:
            self.product_template_fields = self.client.fields_get("product.template")
        return self.product_template_fields

    def _get_default_uom_id(self) -> int:
        if self.default_uom_id is not None:
            return self.default_uom_id

        units = self.client.search_read("uom.uom", domain=[["name", "=", "Units"]], fields=["id", "name"], limit=1)
        if units:
            self.default_uom_id = int(units[0]["id"])
            return self.default_uom_id

        fallback = self.client.search_read("uom.uom", domain=[], fields=["id", "name"], limit=1, order="id asc")
        if not fallback:
            raise SyncConfigurationError("Unable to find a default unit of measure in Odoo.")
        self.default_uom_id = int(fallback[0]["id"])
        return self.default_uom_id

    def _warehouse_code_for_store(self, store_number: str) -> str:
        return "WHF" if store_number.strip() == "2" else "WH"

    def _resolve_warehouse_id(self, store_number: str) -> int | None:
        warehouse_code = self._warehouse_code_for_store(store_number)
        if warehouse_code in self.warehouse_cache:
            return self.warehouse_cache[warehouse_code]

        records = self.client.search_read(
            "stock.warehouse",
            domain=[["code", "=", warehouse_code]],
            fields=["id", "code", "name"],
            limit=2,
        )
        warehouse_id = int(records[0]["id"]) if len(records) == 1 else None
        self.warehouse_cache[warehouse_code] = warehouse_id
        return warehouse_id

    def preview_latest_report(self) -> dict[str, Any]:
        report = discover_latest_report(self.settings)
        return self.preview_file(report.path)

    def preview_file(self, file_path: str | Path) -> dict[str, Any]:
        path = Path(file_path).resolve()
        rows = parse_rows(str(path))
        drafts = build_sales_order_drafts(
            rows,
            resolve_local_partner_ids=False,
            resolve_local_product_names=False,
        )
        return {
            "report_path": str(path),
            "order_count": len(drafts),
            "orders": [
                {
                    "order_name": draft.order_name,
                    "source_document": draft.source_document,
                    "store_number": draft.store_number,
                    "customer_number": draft.customer_number,
                    "customer_name": draft.customer_name,
                    "phone_number": draft.phone_number,
                    "salesperson_name": draft.salesperson_name,
                    "line_count": len(draft.lines),
                    "order_date_odoo": draft.order_date_odoo,
                    "x_studio_fecha": draft.order_date_field_odoo,
                }
                for draft in drafts
            ],
        }

    def sync_latest_report(self, *, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
        report = discover_latest_report(self.settings)
        return self.sync_file(report.path, report=report, dry_run=dry_run, force=force)

    def sync_file(
        self,
        file_path: str | Path,
        *,
        report: ReportFile | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        path = Path(file_path).resolve()
        active_report = report or ReportFile(
            path=path,
            modified_at=datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            size_bytes=path.stat().st_size,
            fingerprint=_file_fingerprint(path),
        )

        if not dry_run and not force and self.state_store.is_successfully_synced_report(active_report):
            cached_state = self.state_store.load().get("odoo_sync", {})
            summary = {
                "status": "already_synced",
                "mode": "live",
                "message": "The newest report fingerprint matches the last successful sync. Use force=true to run it again.",
                "report_path": str(active_report.path),
                "report_modified_at": active_report.modified_at,
                "last_summary": cached_state.get("summary"),
            }
            self.logger.info("\n%s\n", format_sync_summary(summary))
            return summary

        rows = parse_rows(str(path))
        drafts = build_sales_order_drafts(
            rows,
            resolve_local_partner_ids=False,
            resolve_local_product_names=False,
        )
        self.logger.info(
            "\nSync Start\n  Mode    : %s\n  Report  : %s\n  Updated : %s\n  Orders  : %s\n",
            "dry_run" if dry_run else "live",
            active_report.path.name,
            active_report.modified_at,
            len(drafts),
        )

        results: list[SyncOrderResult] = []
        for draft in drafts:
            stop_on_uncertain_write = False
            try:
                result = self._sync_order(draft, dry_run=dry_run)
            except Exception as exc:
                result = SyncOrderResult(
                    order_name=draft.order_name,
                    source_document=draft.source_document,
                    customer_number=draft.customer_number,
                    customer_name=draft.customer_name,
                    salesperson_name=draft.salesperson_name,
                    line_count=len(draft.lines),
                    status="failed",
                    message=f"Unhandled exception: {exc}",
                )
                self.logger.exception(
                    "Order sync failed | order=%s | source_document=%s | customer=%s",
                    draft.order_name,
                    draft.source_document,
                    draft.customer_number,
                )
                stop_on_uncertain_write = isinstance(exc, OdooAmbiguousWriteError)
            results.append(result)
            self.logger.info(_format_order_summary_line(asdict(result)))
            if stop_on_uncertain_write:
                self.logger.error('Stopping this report after an uncertain write; next run will re-read Odoo with fresh caches.')
                break

        created_count = sum(1 for result in results if result.status == "created")
        skipped_count = sum(1 for result in results if result.status == "skipped_existing")
        failed_count = sum(1 for result in results if result.status == "failed")
        ready_count = sum(1 for result in results if result.status == "ready")

        summary = {
            "status": "dry_run" if dry_run else ("partial_success" if failed_count else "success"),
            "mode": "dry_run" if dry_run else "live",
            "report_path": str(active_report.path),
            "report_modified_at": active_report.modified_at,
            "report_size_bytes": active_report.size_bytes,
            "report_fingerprint": active_report.fingerprint,
            "total_orders": len(drafts),
            "created_orders": created_count,
            "ready_orders": ready_count,
            "skipped_existing_orders": skipped_count,
            "failed_orders": failed_count,
            "orders": [asdict(result) for result in results],
        }

        if not dry_run:
            self.state_store.save_summary(active_report, summary)

        self.logger.info("\n%s\n", format_sync_summary(summary, include_orders=False))

        return summary

    def _sync_order(self, draft: SalesOrderDraft, *, dry_run: bool) -> SyncOrderResult:
        result = SyncOrderResult(
            order_name=draft.order_name,
            source_document=draft.source_document,
            customer_number=draft.customer_number,
            customer_name=draft.customer_name,
            salesperson_name=draft.salesperson_name,
            line_count=len(draft.lines),
            status="failed",
            message="Unhandled sync state",
        )

        existing_order = self._find_existing_order(draft)
        if existing_order:
            result.status = "skipped_existing"
            result.sale_order_id = int(existing_order["id"])
            result.message = f"Order already exists in Odoo as {existing_order.get('name')}"
            return result

        unresolved_skus: list[str] = []
        line_payloads: list[dict[str, Any]] = []
        planned_actions: list[str] = []
        created_product_skus: list[str] = []
        planned_product_count = 0
        sale_order_line_fields = self._get_sale_order_line_fields()

        partner, partner_action = self._ensure_partner(draft, dry_run=dry_run)
        if partner_action:
            planned_actions.append(partner_action)

        if partner is None and not partner_action:
            result.message = "Customer could not be matched and the report row has no customer number to create one"
            result.planned_actions = planned_actions
            return result

        if partner is None and not dry_run:
            result.message = "Customer could not be matched or created in Odoo"
            result.planned_actions = planned_actions
            return result

        if partner is not None:
            result.partner_id = int(partner["id"])
            if partner_action and partner_action.startswith("created_partner"):
                result.created_partner_id = int(partner["id"])

        for line in draft.lines:
            product, product_action = self._ensure_product(line, dry_run=dry_run)
            if product_action:
                planned_actions.append(product_action)
                if product_action.startswith("create_product"):
                    planned_product_count += 1
                if product_action.startswith("created_product"):
                    created_product_skus.append(line.sku)

            if product is None and not dry_run:
                unresolved_skus.append(line.sku)
                continue

            if product is None:
                continue

            line_payload = {
                "order_id": 0,
                "product_id": product.product_id,
                "product_uom_qty": line.quantity,
                "price_unit": line.unit_price,
                "name": line.product_display,
            }
            if product.uom_id:
                if "product_uom_id" in sale_order_line_fields:
                    line_payload["product_uom_id"] = product.uom_id
                elif "product_uom" in sale_order_line_fields:
                    line_payload["product_uom"] = product.uom_id
            line_payloads.append(line_payload)

        if unresolved_skus:
            result.unmatched_skus = unresolved_skus
            result.planned_actions = planned_actions
            result.message = "One or more SKUs could not be matched or created in Odoo"
            return result

        if not line_payloads and not (dry_run and planned_product_count):
            result.message = "No valid product lines were found for the order"
            return result

        if dry_run:
            result.status = "ready"
            result.planned_actions = planned_actions
            result.message = "Validated against Odoo and ready to create"
            if planned_actions:
                result.message = f"{result.message}; planned actions: {', '.join(planned_actions)}"
            return result

        if partner is None:
            result.message = "Customer could not be matched or created in Odoo"
            result.planned_actions = planned_actions
            return result

        order_values = self._build_order_values(draft, int(partner["id"]))
        # Each JSON-2 call is a transaction. Create the header and every line
        # together so a Wi-Fi drop cannot leave a partly populated order that
        # the next report would mistake for an already completed import.
        order_values['order_line'] = [
            [0, 0, {key: value for key, value in payload.items() if key != 'order_id'}]
            for payload in line_payloads
        ]
        sale_order_id = self.client.create("sale.order", order_values)

        result.status = "created"
        result.sale_order_id = sale_order_id
        result.created_product_skus = created_product_skus
        result.planned_actions = planned_actions
        result.message = f"Created sale.order {sale_order_id} with {len(line_payloads)} lines"
        if planned_actions:
            result.message = f"{result.message}; actions: {', '.join(planned_actions)}"
        return result

    def _find_existing_order(self, draft: SalesOrderDraft) -> dict[str, Any] | None:
        sale_order_fields = self._get_sale_order_fields()
        candidate_domains = []
        if draft.source_document and "name" in sale_order_fields:
            for candidate_name in [
                draft.source_document,
                f"O{draft.source_document}",
                f"E{draft.source_document}",
            ]:
                candidate_domains.append([["name", "=", candidate_name]])
        elif draft.order_name and "name" in sale_order_fields:
            candidate_domains.append([["name", "=", draft.order_name]])
        if draft.source_document and "client_order_ref" in sale_order_fields:
            candidate_domains.append([["client_order_ref", "=", draft.source_document]])
        if draft.source_document and "origin" in sale_order_fields:
            candidate_domains.append([["origin", "=", draft.source_document]])

        for domain in candidate_domains:
            records = self.client.search_read(
                "sale.order",
                domain=domain,
                fields=["id", "name", "client_order_ref", "origin"],
                limit=1,
            )
            if records:
                return records[0]
        return None

    def _resolve_partner(self, customer_number: str) -> dict[str, Any] | None:
        normalized = customer_number.strip()
        if not normalized:
            return None

        if normalized in self.partner_cache:
            return self.partner_cache[normalized]

        partner_fields = self._get_partner_fields()
        if "x_studio_nmero_de_cuenta" not in partner_fields:
            raise SyncConfigurationError(
                "The res.partner model does not expose x_studio_nmero_de_cuenta in this Odoo database."
            )

        records = self.client.search_read(
            "res.partner",
            domain=[["x_studio_nmero_de_cuenta", "=", normalized]],
            fields=["id", "name", "x_studio_nmero_de_cuenta"],
            limit=2,
        )
        partner = records[0] if len(records) == 1 else None
        self.partner_cache[normalized] = partner
        return partner

    def _ensure_partner(self, draft: SalesOrderDraft, *, dry_run: bool) -> tuple[dict[str, Any] | None, str | None]:
        partner = self._resolve_partner(draft.customer_number)
        if partner is not None:
            return partner, None

        if not draft.customer_number:
            return None, None

        action = f"create_partner:{draft.customer_number}"
        if dry_run:
            return None, action

        partner = self._create_partner(draft)
        return partner, f"created_partner:{draft.customer_number}"

    def _create_partner(self, draft: SalesOrderDraft) -> dict[str, Any]:
        partner_fields = self._get_partner_fields()
        values: dict[str, Any] = {
            "name": draft.customer_name or draft.customer_number,
            "x_studio_nmero_de_cuenta": draft.customer_number,
        }
        if "phone" in partner_fields and draft.phone_number:
            values["phone"] = draft.phone_number
        if "customer_rank" in partner_fields:
            values["customer_rank"] = 1
        if "company_type" in partner_fields:
            values["company_type"] = self._detect_company_type(draft.customer_name)

        partner_id = self.client.create("res.partner", values)
        records = self.client.read(
            "res.partner",
            [partner_id],
            ["id", "name", "x_studio_nmero_de_cuenta", "phone"],
        )
        partner = records[0]
        self.partner_cache[draft.customer_number.strip()] = partner
        self.logger.info(
            "Created partner %s for customer number %s",
            partner_id,
            draft.customer_number,
        )
        return partner

    def _resolve_product(self, sku: str) -> ProductResolution | None:
        normalized = sku.strip()
        if not normalized:
            return None

        if normalized in self.product_cache:
            return self.product_cache[normalized]

        records = self.client.search_read(
            "product.product",
            domain=[["default_code", "=", normalized]],
            fields=["id", "display_name", "default_code", "uom_id"],
            limit=2,
        )
        if len(records) != 1:
            self.product_cache[normalized] = None
            return None

        product = ProductResolution(
            product_id=int(records[0]["id"]),
            uom_id=_many2one_id(records[0].get("uom_id")),
            display_name=str(records[0].get("display_name") or normalized),
        )
        self.product_cache[normalized] = product
        return product

    def _ensure_product(self, line: OrderLineDraft, *, dry_run: bool) -> tuple[ProductResolution | None, str | None]:
        product = self._resolve_product(line.sku)
        if product is not None:
            return product, None

        action = f"create_product:{line.sku}"
        if dry_run:
            return None, action

        product = self._create_product(line)
        return product, f"created_product:{line.sku}"

    def _create_product(self, line: OrderLineDraft) -> ProductResolution:
        template_fields = self._get_product_template_fields()
        values: dict[str, Any] = {
            "name": line.description or line.sku,
            "default_code": line.sku,
            "uom_id": self._get_default_uom_id(),
            "list_price": line.unit_price,
        }
        if "sale_ok" in template_fields:
            values["sale_ok"] = True
        if "purchase_ok" in template_fields:
            values["purchase_ok"] = True
        if "type" in template_fields:
            values["type"] = "consu"
        elif "detailed_type" in template_fields:
            values["detailed_type"] = "consu"

        template_id = self.client.create("product.template", values)
        records = self.client.read(
            "product.template",
            [template_id],
            ["id", "name", "default_code", "uom_id", "product_variant_id"],
        )
        if not records:
            raise OdooApiError(f"Odoo did not return the created product template {template_id}")

        variant_id = _many2one_id(records[0].get("product_variant_id"))
        if variant_id is None:
            raise OdooApiError(f"Created product template {template_id} has no product variant")

        product = ProductResolution(
            product_id=variant_id,
            uom_id=_many2one_id(records[0].get("uom_id")),
            display_name=f"[{line.sku}] {line.description or line.sku}",
        )
        self.product_cache[line.sku.strip()] = product
        self.logger.info("Created product template %s for SKU %s", template_id, line.sku)
        return product

    def _detect_company_type(self, customer_name: str) -> str:
        normalized = (customer_name or "").upper()
        company_tokens = [
            "INC",
            "LLC",
            "CORP",
            "CORPORATION",
            "COMPANY",
            "CONSTRUCTION",
            "GROUP",
            "ENTERPRISE",
            "INDUSTRIES",
            "HARDWARE",
            "LUMBER",
            "FERRETERIA",
        ]
        return "company" if any(token in normalized for token in company_tokens) else "person"

    def _build_order_values(self, draft: SalesOrderDraft, partner_id: int) -> dict[str, Any]:
        sale_order_fields = self._get_sale_order_fields()
        values: dict[str, Any] = {
            "partner_id": partner_id,
            "client_order_ref": draft.source_document or draft.order_name,
            "origin": draft.source_document or draft.order_name,
        }
        if "name" in sale_order_fields and draft.order_name:
            values["name"] = draft.order_name
        if draft.order_date_odoo and "date_order" in sale_order_fields:
            values["date_order"] = draft.order_date_odoo
        if draft.order_date_field_odoo and "x_studio_fecha" in sale_order_fields:
            values["x_studio_fecha"] = draft.order_date_field_odoo
        if "warehouse_id" in sale_order_fields:
            warehouse_id = self._resolve_warehouse_id(draft.store_number)
            if warehouse_id is not None:
                values["warehouse_id"] = warehouse_id
        self._apply_salesperson_field(values, draft.salesperson_name)
        return values

    def _apply_salesperson_field(self, values: dict[str, Any], salesperson_name: str) -> None:
        field_meta = self._get_sale_order_fields().get("x_studio_vendedor")
        if not field_meta or not salesperson_name:
            return

        field_type = field_meta.get("type")
        if field_type in {"char", "text", "html"}:
            values["x_studio_vendedor"] = salesperson_name
            return

        if field_type == "many2one":
            relation = field_meta.get("relation")
            if not relation:
                return
            records = self.client.search_read(
                relation,
                domain=[["name", "=", salesperson_name]],
                fields=["id", "name"],
                limit=2,
            )
            if len(records) == 1:
                values["x_studio_vendedor"] = int(records[0]["id"])
