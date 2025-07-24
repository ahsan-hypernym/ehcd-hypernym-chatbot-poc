import os
import pandas as pd
import docx
from PyPDF2 import PdfReader

INPUT_DIR = "/mnt/data/EHCD_Data"
OUTPUT_DIR = "/mnt/data/CSV_Output"

os.makedirs(OUTPUT_DIR, exist_ok=True)

def extract_pdf_text(file_path):
    reader = PdfReader(file_path)
    return "\n".join([page.extract_text() or '' for page in reader.pages])

def extract_docx_text(file_path):
    doc = docx.Document(file_path)
    return "\n".join([para.text for para in doc.paragraphs])

def extract_xlsx_text(file_path):
    dfs = pd.read_excel(file_path, sheet_name=None)
    combined_text = []
    for name, df in dfs.items():
        combined_text.append(f"--- Sheet: {name} ---\n{df.to_string(index=False)}")
    return "\n\n".join(combined_text)

def save_as_csv(file_name, text):
    csv_path = os.path.join(OUTPUT_DIR, file_name + ".csv")
    df = pd.DataFrame([{"text": text}])
    df.to_csv(csv_path, index=False)

for file in os.listdir(INPUT_DIR):
    file_path = os.path.join(INPUT_DIR, file)
    file_name, ext = os.path.splitext(file)

    try:
        if ext.lower() == ".pdf":
            text = extract_pdf_text(file_path)
        elif ext.lower() == ".docx":
            text = extract_docx_text(file_path)
        elif ext.lower() in [".xls", ".xlsx"]:
            text = extract_xlsx_text(file_path)
        else:
            print(f"Skipped unsupported file: {file}")
            continue

        save_as_csv(file_name, text)
        print(f"Saved: {file_name}.csv")
    except Exception as e:
        print(f"Error processing {file}: {e}")
