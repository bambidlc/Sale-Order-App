#!/usr/bin/env python3
"""
Sales Order Converter
Converts Epicor sales order CSV files to Sales Order Template format
"""

import csv
import os
import shutil
import re
from datetime import datetime
from typing import List, Dict, Tuple, Optional


class SalesOrderConverter:
    def __init__(self):
        self.input_folder = "to_be_processed"
        self.output_folder = "processed"
        self.template_headers = [
            "name",
            "partner_id", 
            "user_id",
            "activity_ids",
            "order_line/name",
            "order_line/product_uom_qty",
            "order_line/price_unit",
            "order_line/product_id",
            "order_line/product_template_id/name",
            "order_line/product_template_id"
        ]
    
    def extract_metadata_from_epicor(self, rows: List[Dict]) -> Tuple[str, str, str]:
        """
        Extract sales order name, client name, and salesman from Epicor file header
        """
        sales_order_name = ""
        client_name = ""
        salesman_name = ""
        
        # Look through the first 10 rows for metadata
        for i, row in enumerate(rows[:10]):
            description = row.get('Description', '').strip()
            
            if not description:
                continue
                
            # Look for project/order name (typically in first few lines)
            if i == 0 and description and not sales_order_name:
                sales_order_name = description
            
            # Look for client name (often contains company keywords)
            if any(keyword in description.upper() for keyword in ['PROY', 'PROJECT', 'COMPANY', 'CORP', 'LTD', 'INC']):
                if not client_name:
                    client_name = description
            
            # Look for contact person (often has @ symbol or phone patterns)
            if '@' in description or re.search(r'\+?\d{2,3}[\s\-\(\)]*\d', description):
                if not salesman_name:
                    # Extract name from email or contact line
                    if '@' in description:
                        salesman_name = description.split('@')[0].strip()
                    else:
                        salesman_name = description
        
        # Fallbacks
        if not sales_order_name:
            sales_order_name = f"SO_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if not client_name:
            client_name = "Unknown Client"
        if not salesman_name:
            salesman_name = "Unknown Salesman"
            
        return sales_order_name, client_name, salesman_name
    
    def detect_delimiter(self, file_path: str) -> str:
        """
        Detect the delimiter used in the CSV file
        """
        try:
            with open(file_path, 'r', encoding='utf-8', newline='') as file:
                first_line = file.readline()
                if first_line.count(';') > first_line.count(','):
                    return ';'
                else:
                    return ','
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='latin-1', newline='') as file:
                first_line = file.readline()
                if first_line.count(';') > first_line.count(','):
                    return ';'
                else:
                    return ','
    
    def parse_epicor_csv(self, file_path: str) -> List[Dict]:
        """
        Parse Epicor CSV file with auto-detected delimiter
        """
        delimiter = self.detect_delimiter(file_path)
        print(f"  Using delimiter: '{delimiter}'")
        
        rows = []
        try:
            with open(file_path, 'r', encoding='utf-8', newline='') as file:
                reader = csv.DictReader(file, delimiter=delimiter)
                for row in reader:
                    rows.append(row)
        except UnicodeDecodeError:
            # Try with different encoding
            with open(file_path, 'r', encoding='latin-1', newline='') as file:
                reader = csv.DictReader(file, delimiter=delimiter)
                for row in reader:
                    rows.append(row)
        
        return rows
    
    def convert_to_template_format(self, epicor_rows: List[Dict], sales_order_name: str, 
                                 client_name: str, salesman_name: str) -> List[Dict]:
        """
        Convert Epicor rows to Sales Order Template format
        - If a Doc # column exists, group by Doc # and start each order with
          name = 'O' + Doc#, partner_id = Customer Name, user_id fixed
        - Lines without SKU are ignored (treated as notes)
        """
        template_rows = []

        # Helpers to read values by possible header variants
        def get_val(row: Dict, keys: List[str]) -> str:
            for key in keys:
                if key in row and row[key] is not None:
                    return str(row.get(key, '')).strip()
            return ""

        # Identify available columns
        header_keys = list(epicor_rows[0].keys()) if epicor_rows else []
        doc_keys = ['Doc #', 'DOC #', 'Doc#', 'DOC#', 'Doc No', 'DOC NO', 'Doc']
        customer_keys = ['Customer Name', 'CUSTOMER NAME', 'Customer', 'CLIENTE']
        sku_keys = ['SKU', 'Sku', 'sku', 'Item', 'ITEM']
        desc_keys = ['Description', 'DESCRIPTION', 'description', 'DESCRIPCION', 'DESCRIPCIÓN']
        qty_keys = ['Qty', 'QTY', 'Quantity']
        price_keys = ['Price', 'PRICE', 'Unit Price', 'UnitPrice']

        doc_key = next((k for k in header_keys if k in doc_keys), None)
        customer_key = next((k for k in header_keys if k in customer_keys), None)

        if doc_key:
            # Group by Doc #, output first product line per doc with name/partner/user
            current_doc = None
            for row in epicor_rows:
                sku = get_val(row, sku_keys)
                if not sku:
                    continue

                description = get_val(row, desc_keys)
                qty = get_val(row, qty_keys)
                price = get_val(row, price_keys)
                doc_number = get_val(row, [doc_key])
                cust_name = get_val(row, [customer_key]) if customer_key else ""

                try:
                    quantity = float(qty.replace(',', '')) if qty else 0.00
                except Exception:
                    quantity = 0.00

                try:
                    unit_price = float(price.replace(',', '')) if price else 0.00
                except Exception:
                    unit_price = 0.00

                template_row = {
                    "name": "",
                    "partner_id": "",
                    "user_id": "",
                    "activity_ids": "",
                }

                if doc_number and doc_number != current_doc and doc_number != '0':
                    current_doc = doc_number
                    order_name = f"O{re.sub(r'\s+', '', doc_number)}"
                    partner = cust_name if cust_name else "Default User"
                    template_row.update({
                        "name": order_name,
                        "partner_id": partner,
                        "user_id": "Jabes Omar De La Cruz",
                    })

                product_template_id = f"[{sku}] {description}"
                template_row.update({
                    "order_line/name": product_template_id,
                    "order_line/product_uom_qty": f"{quantity:.2f}",
                    "order_line/price_unit": f"{unit_price:.2f}",
                    "order_line/product_id": product_template_id,
                    "order_line/product_template_id/name": description,
                    "order_line/product_template_id": product_template_id
                })

                template_rows.append(template_row)

            return template_rows

        # Single-order fallback (no Doc #)
        quotation_number = f"QO{datetime.now().strftime('%m%d%H%M')}"
        first_row = True
        for row in epicor_rows:
            sku = get_val(row, sku_keys)
            if not sku:
                continue

            description = get_val(row, desc_keys)
            qty = get_val(row, qty_keys)
            price = get_val(row, price_keys)

            try:
                quantity = float(qty.replace(',', '')) if qty else 0.00
            except Exception:
                quantity = 0.00

            try:
                unit_price = float(price.replace(',', '')) if price else 0.00
            except Exception:
                unit_price = 0.00

            template_row = {
                "name": "",
                "partner_id": "",
                "user_id": "",
                "activity_ids": "",
            }

            if first_row:
                template_row.update({
                    "name": quotation_number,
                    "partner_id": "Default User",
                    "user_id": "Jabes Omar De La Cruz",
                })
                first_row = False

            product_template_id = f"[{sku}] {description}"
            template_row.update({
                "order_line/name": product_template_id,
                "order_line/product_uom_qty": f"{quantity:.2f}",
                "order_line/price_unit": f"{unit_price:.2f}",
                "order_line/product_id": product_template_id,
                "order_line/product_template_id/name": description,
                "order_line/product_template_id": product_template_id
            })

            template_rows.append(template_row)

        return template_rows
    
    def write_template_csv(self, template_rows: List[Dict], output_path: str):
        """
        Write template format CSV file
        """
        with open(output_path, 'w', encoding='utf-8', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=self.template_headers)
            writer.writeheader()
            writer.writerows(template_rows)
    
    def process_file(self, input_file_path: str) -> str:
        """
        Process a single Epicor CSV file
        """
        print(f"Processing: {input_file_path}")
        
        # Parse Epicor CSV
        epicor_rows = self.parse_epicor_csv(input_file_path)
        
        # Extract metadata
        sales_order_name, client_name, salesman_name = self.extract_metadata_from_epicor(epicor_rows)
        
        print(f"  Sales Order: {sales_order_name}")
        print(f"  Client: {client_name}")
        print(f"  Salesman: {salesman_name}")
        
        # Convert to template format
        template_rows = self.convert_to_template_format(
            epicor_rows, sales_order_name, client_name, salesman_name
        )
        
        # Generate output filename
        base_name = os.path.splitext(os.path.basename(input_file_path))[0]
        output_filename = f"{base_name}_converted.csv"
        output_path = os.path.join(self.output_folder, output_filename)
        
        # Write template CSV
        self.write_template_csv(template_rows, output_path)
        
        print(f"  Converted to: {output_path}")
        print(f"  Total lines processed: {len(template_rows)}")
        
        return output_path
    
    def process_all_files(self):
        """
        Process all *.csv files in the input folder
        """
        if not os.path.exists(self.input_folder):
            print(f"Input folder '{self.input_folder}' does not exist!")
            return
        
        if not os.path.exists(self.output_folder):
            os.makedirs(self.output_folder)
        
        # Find all *.csv files
        csv_files = []
        for filename in os.listdir(self.input_folder):
            if filename.endswith('.csv'):
                csv_files.append(os.path.join(self.input_folder, filename))
        
        if not csv_files:
            print(f"No *.csv files found in '{self.input_folder}'")
            return
        
        print(f"Found {len(csv_files)} CSV files to process")
        
        processed_files = []
        for file_path in csv_files:
            try:
                output_path = self.process_file(file_path)
                processed_files.append((file_path, output_path))
                print("Successfully processed\n")
            except Exception as e:
                print(f"Error processing {file_path}: {str(e)}\n")
        
        print(f"\nProcessing complete!")
        print(f"Successfully processed {len(processed_files)} files")
        
        # Move processed files to processed folder
        for input_file, output_file in processed_files:
            try:
                processed_input_path = os.path.join(self.output_folder, os.path.basename(input_file))
                shutil.move(input_file, processed_input_path)
                print(f"Moved {input_file} to {processed_input_path}")
            except Exception as e:
                print(f"Could not move {input_file}: {str(e)}")


def main():
    """
    Main function to run the converter
    """
    print("=== Sales Order Converter ===")
    print("Converting Epicor sales orders to Sales Order Template format")
    print()
    
    converter = SalesOrderConverter()
    converter.process_all_files()


if __name__ == "__main__":
    main() 