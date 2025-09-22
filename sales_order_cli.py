import argparse
import os
from src.converter import parse_rows, convert_rows, write_template_csv


def main():
    parser = argparse.ArgumentParser(description="Sales Order Converter CLI")
    parser.add_argument("input", help="Input CSV file path or a folder to process all .csv files")
    parser.add_argument("-o", "--output", help="Output folder", default="processed")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    def process_one(path: str):
        rows = parse_rows(path)
        out_rows = convert_rows(rows)
        base = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(args.output, f"{base}_converted.csv")
        write_template_csv(out_rows, out_path)
        print(f"Converted: {path} -> {out_path}")

    if os.path.isdir(args.input):
        for name in os.listdir(args.input):
            if name.lower().endswith('.csv'):
                process_one(os.path.join(args.input, name))
    else:
        process_one(args.input)


if __name__ == "__main__":
    main()
