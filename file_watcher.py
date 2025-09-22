#!/usr/bin/env python3
"""
File Watcher for Sales Order Converter
Monitors the to_be_processed folder and automatically processes new CSV files
"""

import os
import time
import subprocess
import sys
from datetime import datetime


class SalesOrderFileWatcher:
    def __init__(self):
        self.watch_folder = "to_be_processed"
        self.converter_script = "sales_order_converter.py"
        self.processed_files = set()
        self.check_interval = 2  # Check every 2 seconds
        
    def get_csv_files(self):
        """Get all CSV files in the watch folder"""
        if not os.path.exists(self.watch_folder):
            return set()
        
        csv_files = set()
        for filename in os.listdir(self.watch_folder):
            if filename.lower().endswith('.csv'):
                file_path = os.path.join(self.watch_folder, filename)
                csv_files.add(file_path)
        
        return csv_files
    
    def run_converter(self):
        """Run the sales order converter script"""
        try:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"[{current_time}] Running sales order converter...")
            result = subprocess.run([sys.executable, self.converter_script], 
                                  capture_output=True, text=True, timeout=60)
            
            current_time = datetime.now().strftime('%H:%M:%S')
            if result.returncode == 0:
                print(f"[{current_time}] Converter completed successfully")
                if result.stdout.strip():
                    print("Output:", result.stdout.strip())
            else:
                print(f"[{current_time}] Converter failed with error:")
                print("Error:", result.stderr.strip())
                
        except subprocess.TimeoutExpired:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"[{current_time}] Converter timed out")
        except Exception as e:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"[{current_time}] Error running converter: {str(e)}")
    
    def watch_folder(self):
        """Main watching loop"""
        print("=" * 50)
        print("Sales Order File Watcher Started")
        print(f"Watching folder: {os.path.abspath(self.watch_folder)}")
        print(f"Converter script: {self.converter_script}")
        print(f"Check interval: {self.check_interval} seconds")
        print("=" * 50)
        current_time = datetime.now().strftime('%H:%M:%S')
        print(f"[{current_time}] Monitoring for new CSV files...")
        print("Press Ctrl+C to stop watching")
        print()
        
        # Check for existing files and process them
        existing_files = self.get_csv_files()
        if existing_files:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"[{current_time}] Found {len(existing_files)} existing CSV files")
            for file_path in sorted(existing_files):
                print(f"    - {os.path.basename(file_path)}")
            
            print()
            print("Processing existing files...")
            self.run_converter()
            print()
            
            # Update processed files after running converter
            self.processed_files = self.get_csv_files()
        else:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"[{current_time}] No existing CSV files found")
            self.processed_files = set()
        
        try:
            while True:
                current_files = self.get_csv_files()
                new_files = current_files - self.processed_files
                
                if new_files:
                    current_time = datetime.now().strftime('%H:%M:%S')
                    print(f"[{current_time}] NEW: Detected {len(new_files)} new CSV file(s):")
                    for file_path in sorted(new_files):
                        print(f"    - {os.path.basename(file_path)}")
                    
                    print()
                    self.run_converter()
                    print()
                    
                    # Update the processed files list
                    self.processed_files = current_files
                
                time.sleep(self.check_interval)
                
        except KeyboardInterrupt:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"\n[{current_time}] File watcher stopped by user")
        except Exception as e:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"\n[{current_time}] Unexpected error: {str(e)}")
    
    def check_requirements(self):
        """Check if required files exist"""
        if not os.path.exists(self.converter_script):
            print(f"Error: Converter script '{self.converter_script}' not found!")
            return False
        
        if not os.path.exists(self.watch_folder):
            print(f"Creating watch folder: {self.watch_folder}")
            os.makedirs(self.watch_folder)
        
        return True


def main():
    """Main function"""
    watcher = SalesOrderFileWatcher()
    
    if not watcher.check_requirements():
        print("Please ensure all required files exist and try again.")
        return
    
    try:
        watcher.watch_folder()
    except Exception as e:
        print(f"Fatal error: {str(e)}")


if __name__ == "__main__":
    main() 