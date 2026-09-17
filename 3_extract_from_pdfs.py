"""
Stage 3: Extract turnover and average employee numbers from the PDFs
that stage 2 downloaded, escalating through three tiers:

  Tier A: text-layer PDF + keyword/regex search        (cheap, fast)
  Tier B: OCR (scanned PDFs) + same keyword/regex       (slower)
  Tier C: LLM extraction on the text                    (most robust to
                                                          layout variation,
                                                          used only where
                                                          A/B fail or are
                                                          ambiguous)

Install:
    pip install pdfplumber pytesseract pdf2image pandas --break-system-packages
Also needs poppler-utils and tesseract-ocr installed at the OS level:
    apt-get install -y poppler-utils tesseract-ocr
"""

import re
import json
import pandas as pd
from pathlib import Path

import pdfplumber

# ---- Tier A/B: keyword-based extraction --------------------------------

TURNOVER_PATTERNS = [
    r"turnover[^\d\n]{0,40}([\d,]+)",
    r"revenue[^\d\n]{0,40}([\d,]+)",
]

EMPLOYEE_PATTERNS = [
    r"average\s+(?:monthly\s+)?number\s+of\s+employees[^\d\n]{0,60}([\d,]+)",
    r"average\s+number\s+of\s+persons\s+employed[^\d\n]{0,60}([\d,]+)",
]


def clean_number(raw: str) -> int | None:
    try:
        return int(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def keyword_extract(text: str) -> dict:
    text_lower = text.lower()
    turnover = None
    for pat in TURNOVER_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            turnover = clean_number(m.group(1))
            if turnover is not None:
                break

    employees = None
    for pat in EMPLOYEE_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            employees = clean_number(m.group(1))
            if employees is not None:
                break

    return {"turnover": turnover, "employees": employees}


def extract_text_layer(pdf_path: Path) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def extract_via_ocr(pdf_path: Path) -> str:
    """Tier B fallback for scanned PDFs."""
    import pytesseract
    from pdf2image import convert_from_path

    pages = convert_from_path(pdf_path, dpi=200)
    return "\n".join(pytesseract.image_to_string(p) for p in pages)


# ---- Tier C: LLM extraction for anything Tier A/B couldn't resolve ------

def llm_extract(text: str, api_key_env: str = "ANTHROPIC_API_KEY") -> dict:
    """
    Falls back to Claude for messy/inconsistent layouts that regex can't
    reliably handle -- e.g. numbers split across lines, tables, or
    non-standard phrasing ("staff numbers averaged X during the year").

    Truncates to keep the request small: turnover and employee figures
    are almost always in the P&L, notes to the accounts, or directors'
    report, so the first ~15k characters is usually enough. If it isn't,
    widen this or chunk the doc.
    """
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    prompt = f"""Extract the following two figures from this UK company accounts
document text. Return ONLY valid JSON, no other text, no markdown fences:
{{"turnover": <number or null>, "employees": <integer or null>}}

- "turnover" is the company's total turnover/revenue for the reporting
  period, as a plain number (no currency symbol, no commas).
- "employees" is the average number of employees during the period.
- If a figure is genuinely not present in the text (e.g. a micro-entity
  filing that only includes a balance sheet), return null for it --
  do not guess or estimate.

Document text:
{text[:15000]}
"""
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json|```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"turnover": None, "employees": None}


# ---- Orchestration -------------------------------------------------------

def process_one(row: dict) -> dict:
    company_number = row["company_number"]
    pdf_path = Path(row["pdf_path"]) if pd.notna(row.get("pdf_path")) else None
    pdf_type = row.get("pdf_type")

    result = {"company_number": company_number, "turnover": None, "employees": None, "method": None}

    if pdf_path is None or not pdf_path.exists():
        result["method"] = "no_pdf"
        return result

    # Tier A
    if pdf_type == "text":
        text = extract_text_layer(pdf_path)
        found = keyword_extract(text)
        if found["turnover"] is not None and found["employees"] is not None:
            result.update(found, method="tier_a_keyword")
            return result
    else:
        text = None

    # Tier B (scanned PDFs, or Tier A left gaps -- OCR sometimes catches
    # text the layer parser missed due to unusual encoding)
    if text is None or (found["turnover"] is None or found["employees"] is None):
        try:
            ocr_text = extract_via_ocr(pdf_path)
            text = (text or "") + "\n" + ocr_text
            found = keyword_extract(text)
            if found["turnover"] is not None and found["employees"] is not None:
                result.update(found, method="tier_b_ocr_keyword")
                return result
        except Exception as e:
            result["method"] = f"ocr_failed: {e}"

    # Tier C: LLM cleanup for whatever regex still couldn't pin down
    try:
        llm_found = llm_extract(text or "")
        result["turnover"] = found.get("turnover") or llm_found.get("turnover")
        result["employees"] = found.get("employees") or llm_found.get("employees")
        result["method"] = "tier_c_llm"
    except Exception as e:
        result["method"] = f"llm_failed: {e}"

    return result


if __name__ == "__main__":
    fetch_results = pd.read_csv("pdf_fetch_results.csv")
    downloaded = fetch_results[fetch_results["status"] == "downloaded"]

    final_rows = [process_one(row) for _, row in downloaded.iterrows()]
    out = pd.DataFrame(final_rows)
    out.to_csv("pdf_extraction_results.csv", index=False)

    resolved = out["turnover"].notna().sum()
    print(f"Resolved turnover for {resolved}/{len(out)} PDF-only companies")
    print(out["method"].value_counts())
