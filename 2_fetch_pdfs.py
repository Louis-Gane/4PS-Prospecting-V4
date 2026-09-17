"""
Stage 2: For companies not covered by the bulk XBRL data (stage 1),
fetch their latest accounts filing document via the Companies House
API and classify it (text-layer PDF vs scanned/image PDF) so stage 3
knows which extraction path to use.

Docs: https://developer.company-information.service.gov.uk/
Auth: HTTP Basic auth, API key as username, blank password.

Install:
    pip install requests pdfplumber pandas --break-system-packages
"""

import os
import time
import requests
import pandas as pd
from pathlib import Path

API_KEY = os.environ["CH_API_KEY"]  # set this in your environment, don't hardcode
BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"

OUTPUT_DIR = Path("pdfs")
OUTPUT_DIR.mkdir(exist_ok=True)

SESSION = requests.Session()
SESSION.auth = (API_KEY, "")


def get_latest_accounts_filing(company_number: str) -> dict | None:
    """Find the most recent 'accounts' filing history item for a company."""
    url = f"{BASE}/company/{company_number}/filing-history"
    resp = SESSION.get(url, params={"category": "accounts", "items_per_page": 1})
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return items[0] if items else None


def download_filing_document(company_number: str, filing_item: dict) -> Path | None:
    """
    Download the actual document behind a filing history item.
    filing_item['links']['document_metadata'] points at the document API,
    which then gives you a content URL to fetch the actual PDF bytes from.
    """
    doc_meta_url = filing_item.get("links", {}).get("document_metadata")
    if not doc_meta_url:
        return None

    meta_resp = SESSION.get(doc_meta_url)
    meta_resp.raise_for_status()
    meta = meta_resp.json()

    # Request the PDF representation explicitly
    content_url = f"{doc_meta_url}/content"
    pdf_resp = SESSION.get(content_url, headers={"Accept": "application/pdf"})
    pdf_resp.raise_for_status()

    out_path = OUTPUT_DIR / f"{company_number}.pdf"
    out_path.write_bytes(pdf_resp.content)
    return out_path


def classify_pdf(pdf_path: Path) -> str:
    """
    Cheap classification: does the PDF have a text layer we can extract,
    or is it scanned/image-only (needs OCR)?
    """
    import pdfplumber

    try:
        with pdfplumber.open(pdf_path) as pdf:
            sample_text = "".join((p.extract_text() or "") for p in pdf.pages[:3])
        return "text" if len(sample_text.strip()) > 50 else "scanned"
    except Exception as e:
        return f"error: {e}"


def process_company(company_number: str) -> dict:
    result = {"company_number": company_number, "status": None, "pdf_path": None, "pdf_type": None}
    try:
        filing = get_latest_accounts_filing(company_number)
        if not filing:
            result["status"] = "no_accounts_filing_found"
            return result

        pdf_path = download_filing_document(company_number, filing)
        if not pdf_path:
            result["status"] = "no_document_link"
            return result

        result["pdf_path"] = str(pdf_path)
        result["pdf_type"] = classify_pdf(pdf_path)
        result["status"] = "downloaded"
    except requests.HTTPError as e:
        result["status"] = f"http_error: {e.response.status_code}"
    except Exception as e:
        result["status"] = f"error: {e}"
    return result


if __name__ == "__main__":
    needs_pdf = pd.read_csv("needs_pdf_extraction.csv", dtype={"CH_Number": str})
    targets = needs_pdf["CH_Number"].str.zfill(8)

    results = []
    for i, company_number in enumerate(targets):
        results.append(process_company(company_number))
        # Companies House API is rate limited (600 requests / 5 min per key)
        time.sleep(0.5)
        if i % 50 == 0:
            print(f"{i}/{len(targets)} processed")

    pd.DataFrame(results).to_csv("pdf_fetch_results.csv", index=False)
    print("Done. See pdf_fetch_results.csv for status and pdf_type per company.")
