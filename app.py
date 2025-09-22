from fastapi import FastAPI, UploadFile, File, Response
from fastapi.responses import StreamingResponse, HTMLResponse
import io
import os
from src.converter import parse_rows, convert_rows, TEMPLATE_HEADERS
import csv

app = FastAPI(title="Sales Order Converter API")

@app.get("/upload", response_class=HTMLResponse)
async def upload_page():
    return """
<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Sales Order Converter</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 40px; }
      .card { max-width: 520px; padding: 24px; border: 1px solid #ddd; border-radius: 8px; }
      h1 { margin-top: 0; font-size: 20px; }
      input[type=file] { margin: 12px 0; }
      button { padding: 8px 14px; cursor: pointer; }
      .hint { color: #666; font-size: 12px; }
    </style>
  </head>
  <body>
    <div class=\"card\">
      <h1>Upload CSV to Convert</h1>
      <form action=\"/convert\" method=\"post\" enctype=\"multipart/form-data\">
        <input type=\"file\" name=\"file\" accept=\".csv\" required />
        <div class=\"hint\">Any .csv filename is supported. Comma or semicolon delimiters are detected automatically.</div>
        <br/>
        <button type=\"submit\">Convert and Download</button>
      </form>
    </div>
  </body>
</html>
"""

@app.post("/convert")
async def convert_file(file: UploadFile = File(...)):
    # Save upload temporarily
    contents = await file.read()
    temp_path = os.path.join("to_be_processed", file.filename)
    os.makedirs("to_be_processed", exist_ok=True)
    with open(temp_path, 'wb') as f:
        f.write(contents)

    try:
        rows = parse_rows(temp_path)
        out_rows = convert_rows(rows)
        # Stream CSV back
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=TEMPLATE_HEADERS)
        writer.writeheader()
        writer.writerows(out_rows)
        buf.seek(0)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={
            "Content-Disposition": f"attachment; filename={os.path.splitext(file.filename)[0]}_converted.csv"
        })
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

@app.get("/")
async def root():
    return {"status": "ok"}
