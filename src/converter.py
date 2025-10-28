import csv
import os
import re
from datetime import datetime
from typing import List, Dict


TEMPLATE_HEADERS = [
    "name",
    "partner_id",
    "user_id",
    "Cust #",
    "Salesperson",
    "activity_ids",
    "order_line/name",
    "order_line/product_uom_qty",
    "order_line/price_unit",
    "order_line/product_id",
    "order_line/product_template_id/name",
    "order_line/product_template_id",
]

_SALESPERSON_MAP: Dict[str, str] | None = None


def _load_salesperson_map(file_path: str = "Sales Person List.csv") -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not os.path.exists(file_path):
        return mapping
    try:
        with open(file_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter=",")
            for row in reader:
                code = (row.get("Code") or "").strip().upper()
                name = (row.get("Name") or "").strip()
                if code and name:
                    mapping[code] = name
    except UnicodeDecodeError:
        with open(file_path, "r", encoding="latin-1", newline="") as f:
            reader = csv.DictReader(f, delimiter=",")
            for row in reader:
                code = (row.get("Code") or "").strip().upper()
                name = (row.get("Name") or "").strip()
                if code and name:
                    mapping[code] = name
    return mapping


def _get_salesperson_map() -> Dict[str, str]:
    global _SALESPERSON_MAP
    if _SALESPERSON_MAP is None:
        _SALESPERSON_MAP = _load_salesperson_map()
    return _SALESPERSON_MAP


def detect_delimiter(file_path: str) -> str:
    try:
        with open(file_path, 'r', encoding='utf-8', newline='') as f:
            first = f.readline()
            return ';' if first.count(';') > first.count(',') else ','
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='latin-1', newline='') as f:
            first = f.readline()
            return ';' if first.count(';') > first.count(',') else ','


def parse_rows(file_path: str) -> List[Dict]:
    delim = detect_delimiter(file_path)
    rows: List[Dict] = []
    try:
        with open(file_path, 'r', encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f, delimiter=delim):
                rows.append(row)
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='latin-1', newline='') as f:
            for row in csv.DictReader(f, delimiter=delim):
                rows.append(row)
    return rows


def convert_rows(epicor_rows: List[Dict]) -> List[Dict]:
    def get_val(row: Dict, keys: List[str]) -> str:
        for k in keys:
            if k in row and row[k] is not None:
                return str(row.get(k, '')).strip()
        return ''

    header_keys = list(epicor_rows[0].keys()) if epicor_rows else []
    doc_keys = ['Doc #', 'DOC #', 'Doc#', 'DOC#', 'Doc No', 'DOC NO', 'Doc']
    customer_keys = ['Customer Name', 'CUSTOMER NAME', 'Customer', 'CLIENTE', 'Cust #']
    sku_keys = ['SKU', 'Sku', 'sku', 'Item', 'ITEM']
    desc_keys = ['Description', 'DESCRIPTION', 'description', 'DESCRIPCION', 'DESCRIPCIÓN']
    qty_keys = ['Qty', 'QTY', 'Quantity']
    price_keys = ['Price', 'PRICE', 'Unit Price', 'UnitPrice']
    salesperson_keys = ['Salesperson', 'SALESPERSON', 'Sales Person', 'Sales_Person', 'Salesperson']
    cust_keys = ['Cust #']

    doc_key = next((k for k in header_keys if k in doc_keys), None)
    customer_key = next((k for k in header_keys if k in customer_keys), None)
    cust_key = next((k for k in header_keys if k in cust_keys), None)

    sp_map = _get_salesperson_map()

    template_rows: List[Dict] = []

    if doc_key:
        current_doc = None
        for row in epicor_rows:
            sku = get_val(row, sku_keys)
            if not sku:
                continue
            description = get_val(row, desc_keys)
            qty = get_val(row, qty_keys)
            price = get_val(row, price_keys)
            doc_number = get_val(row, [doc_key])
            customer_name = get_val(row, [customer_key]) if customer_key else ''
            salesperson_code = get_val(row, salesperson_keys).upper()
            salesperson_name = sp_map.get(salesperson_code) if salesperson_code else ''
            cust_value = get_val(row, [cust_key]) if cust_key else ''

            try:
                quantity = float(qty.replace(',', '')) if qty else 0.0
            except Exception:
                quantity = 0.0
            try:
                unit_price = float(price.replace(',', '')) if price else 0.0
            except Exception:
                unit_price = 0.0

            row_out: Dict = {
                'name': '',
                'partner_id': '',
                'user_id': '',
                'Cust #': '',
                'Salesperson': '',
                'activity_ids': '',
            }

            if doc_number and doc_number != current_doc and doc_number != '0':
                current_doc = doc_number
                order_name = f"O{re.sub(r'\s+', '', doc_number)}"
                row_out['name'] = order_name
                row_out['partner_id'] = customer_name or 'Default User'
                # user_id from salesperson mapping; fallback to previous default
                row_out['user_id'] = salesperson_name or 'Jabes Omar De La Cruz'
                row_out['Cust #'] = cust_value or customer_name or 'Default User'
                row_out['Salesperson'] = salesperson_name or 'Jabes Omar De La Cruz'

            product_template_id = f"[{sku}] {description}"
            row_out.update({
                'order_line/name': product_template_id,
                'order_line/product_uom_qty': f"{quantity:.2f}",
                'order_line/price_unit': f"{unit_price:.2f}",
                'order_line/product_id': product_template_id,
                'order_line/product_template_id/name': description,
                'order_line/product_template_id': product_template_id,
            })
            template_rows.append(row_out)
        return template_rows

    # single-order fallback
    quotation_number = f"QO{datetime.now().strftime('%m%d%H%M')}"
    first = True
    for row in epicor_rows:
        sku = get_val(row, sku_keys)
        if not sku:
            continue
        description = get_val(row, desc_keys)
        qty = get_val(row, qty_keys)
        price = get_val(row, price_keys)
        try:
            quantity = float(qty.replace(',', '')) if qty else 0.0
        except Exception:
            quantity = 0.0
        try:
            unit_price = float(price.replace(',', '')) if price else 0.0
        except Exception:
            unit_price = 0.0
        row_out: Dict = {
            'name': '',
            'partner_id': '',
            'user_id': '',
            'Cust #': '',
            'Salesperson': '',
            'activity_ids': '',
        }
        if first:
            row_out['name'] = quotation_number
            row_out['partner_id'] = 'Default User'
            # No salesperson in single-order; keep default
            row_out['user_id'] = 'Jabes Omar De La Cruz'
            row_out['Cust #'] = 'Default User'
            row_out['Salesperson'] = 'Jabes Omar De La Cruz'
            first = False
        product_template_id = f"[{sku}] {description}"
        row_out.update({
            'order_line/name': product_template_id,
            'order_line/product_uom_qty': f"{quantity:.2f}",
            'order_line/price_unit': f"{unit_price:.2f}",
            'order_line/product_id': product_template_id,
            'order_line/product_template_id/name': description,
            'order_line/product_template_id': product_template_id,
        })
        template_rows.append(row_out)
    return template_rows


def write_template_csv(rows: List[Dict], output_path: str) -> None:
    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=TEMPLATE_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
