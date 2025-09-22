# Sales Order Converter

This application converts Epicor sales order CSV files to the Sales Order Template format.

## Features

- Converts Epicor CSV files (semicolon-delimited) to Sales Order Template format (comma-delimited)
- Automatically extracts sales order name, client name, and salesman information from file headers
- Handles product lines with SKUs in the format `[SKU] Description`
- Treats lines with empty SKUs as notes with default values (0.00)
- Organizes files in `to_be_processed` and `processed` folders
- Moves processed files to prevent duplicate processing

## Roadmap / Next (web-ready)

- Package converter as reusable module under `src/`
- Provide CLI and FastAPI service for uploads
- CI via GitHub Actions, pinned `requirements.txt`

## Web API (FastAPI)

- Start API: `uvicorn app:app --reload`
- POST /convert with form-data file field `file` (CSV)
- Returns converted CSV download

## CLI

Convert a single file:

```bash
python sales_order_cli.py to_be_processed/ORDERS\ .csv
```

Convert a folder of CSVs:

```bash
python sales_order_cli.py to_be_processed -o processed
```

## GitHub Readiness

- `requirements.txt` pinned
- `src/` module for reuse (web + CLI + scripts)
- Add a `.gitignore` (Python, venv, OS files)
- Optional: GitHub Actions workflow to run lint/tests

## Usage

1. **Place Epicor CSV files in the `to_be_processed` folder**
   - Files can have any name (e.g., `trial 1.csv`, `order_12345.csv`, `expo6124.csv`)
   - Files can use either comma (`,`) or semicolon (`;`) delimiters (auto-detected)

2. **Run the converter**
   
   **Manual processing:**
   ```bash
   python sales_order_converter.py
   ```
   
   **Auto processing (recommended):**
   ```bash
   python auto_converter.py
   ```
   The auto converter watches the folder and automatically processes new files as they are added.

3. **Check results in the `processed` folder**
   - Converted files will be named `[original_name]_converted.csv`
   - Original files will be moved to the `processed` folder

## File Format

### Input (Epicor Format)
- Comma or semicolon-delimited CSV (auto-detected)
- Columns: `Ln#,SKU,Description,Qty,Price,Extension,...` or `Ln#;SKU;Description;Qty;Price;Extension;...`
- Header lines contain sales order metadata
- Empty SKU = note line (excluded from output)
- Filled SKU = product line

### Output (Sales Order Template Format)
- Comma-delimited CSV
- Columns: `id,name,partner_id,user_id,order_line/product_template_id,order_line/name,order_line/product_uom_qty,order_line/price_unit`
- First row contains sales order header with:
  - `id`: Auto-generated export ID
  - `name`: Auto-generated quotation number (QO + MMDDHHMI)
  - `partner_id`: "Default User"
  - `user_id`: "Jabes Omar De La Cruz"
- Product lines: `[SKU] Description` format
- Note lines: Description only with 0.00 values

## Example

**Input (any_file.csv):**
```
Ln#,SKU,Description,Qty,Price,Extension,...
1,,PROJECT NAME,,,,...
2,,CLIENT COMPANY,,,,...
3,,contact@email.com,,,,...
4,12345,Product Description,2.00,100.00,200.00,...
5,,Installation notes,,,,...
```
*Note: Semicolon delimiters (`;`) are also supported and auto-detected*

**Output (any_file_converted.csv):**
```
id,name,partner_id,user_id,order_line/product_template_id,order_line/name,order_line/product_uom_qty,order_line/price_unit
__export__.sale_order_20250718_120120,QO07181201,Default User,Jabes Omar De La Cruz,,NO SHIrlock,0.00,0.00
,,,,[12345] Product Description,[12345] Product Description,2.00,100.00
,,,,,Installation notes,0.00,0.00
```

## Requirements

- Python 3.6+
- No additional dependencies (uses built-in libraries)

## Folder Structure

```
Sales Order App/
├── sales_order_converter.py  # Manual converter
├── auto_converter.py         # Auto file watcher (recommended)
├── to_be_processed/          # Place any *.csv files here
├── processed/                # Converted files and moved originals
└── README.md
``` 