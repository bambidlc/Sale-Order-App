#!/usr/bin/env python3
"""
Simple File Watcher for debugging
"""

import os
import time
import subprocess
import sys


def get_csv_files():
    """Get all CSV files in the watch folder"""
    watch_folder = "to_be_processed"
    if not os.path.exists(watch_folder):
        return []
    
    csv_files = []
    for filename in os.listdir(watch_folder):
        if filename.lower().endswith('.csv'):
            file_path = os.path.join(watch_folder, filename)
            csv_files.append(file_path)
    
    return csv_files


def run_converter():
    """Run the sales order converter script"""
    try:
        print("Running sales order converter...")
        result = subprocess.run([sys.executable, "sales_order_converter.py"], 
                              capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            print("✅ Converter completed successfully")
            if result.stdout.strip():
                print("Output:", result.stdout.strip())
        else:
            print("❌ Converter failed with error:")
            print("Error:", result.stderr.strip())
            
    except Exception as e:
        print(f"❌ Error running converter: {str(e)}")


def main():
    """Main function"""
    print("=== Simple File Watcher ===")
    
    # Check for existing files
    csv_files = get_csv_files()
    print(f"Found {len(csv_files)} CSV files:")
    for file_path in csv_files:
        print(f"  - {os.path.basename(file_path)}")
    
    if csv_files:
        print("\nProcessing files...")
        run_converter()
        print("Done!")
    else:
        print("No CSV files to process.")


if __name__ == "__main__":
    main() 