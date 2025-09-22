#!/usr/bin/env python3
"""
Auto Converter for Sales Orders
Watches the to_be_processed folder and automatically processes new CSV files
"""

import os
import time
import subprocess
import sys


def get_csv_files():
    """Get all CSV files in the to_be_processed folder"""
    folder = "to_be_processed"
    if not os.path.exists(folder):
        return []
    
    files = []
    for filename in os.listdir(folder):
        if filename.lower().endswith('.csv'):
            files.append(os.path.join(folder, filename))
    return files


def run_converter():
    """Run the sales order converter"""
    try:
        print("Running converter...")
        result = subprocess.run([sys.executable, "sales_order_converter.py"], 
                              capture_output=True, text=True)
        
        if result.returncode == 0:
            print("Converter completed successfully!")
            return True
        else:
            print("Converter failed:")
            print(result.stderr)
            return False
    except Exception as e:
        print(f"Error running converter: {e}")
        return False


def watch_and_process():
    """Watch for new files and process them"""
    print("=== Auto Sales Order Converter ===")
    print("Watching for CSV files in to_be_processed folder...")
    print("Press Ctrl+C to stop")
    print()
    
    processed_files = set()
    
    try:
        while True:
            current_files = set(get_csv_files())
            new_files = current_files - processed_files
            
            if new_files:
                print(f"Found {len(new_files)} new file(s):")
                for file_path in sorted(new_files):
                    print(f"  - {os.path.basename(file_path)}")
                
                print()
                success = run_converter()
                
                if success:
                    print("Files processed successfully!")
                    processed_files = set(get_csv_files())  # Update after processing
                else:
                    print("Processing failed!")
                
                print("-" * 40)
            
            time.sleep(2)  # Check every 2 seconds
            
    except KeyboardInterrupt:
        print("\nAuto converter stopped.")


if __name__ == "__main__":
    watch_and_process() 