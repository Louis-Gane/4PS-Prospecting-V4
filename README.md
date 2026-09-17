# Companies House turnover/employee extraction pipeline

Run in order:

1. **`1_bulk_xbrl.py`** — Pulls structured data straight from Companies
   House's free bulk Accounts Data Product instead of the per-document
   API. Covers accounts filed electronically (iXBRL/XBRL), roughly 60%
   of all filings. Outputs `structured_results.csv` (resolved) and
   `needs_pdf_extraction.csv` (everything left over).
   - `pip install stream-read-xbrl pandas`
   - Edit `TARGET_COMPANY_NUMBERS` / `load_target_companies()` to point
     at your actual company list.
   - Check `df.columns.tolist()` on a real pull — exact XBRL tag names
     vary a bit by taxonomy (full accounts vs FRS 105 micro-entity vs
     charity SORP), so the two field names in `extract_fields()` may
     need adjusting.

2. **`2_fetch_pdfs.py`** — For companies not resolved in step 1, hits the
   Companies House API to find and download the latest accounts filing
   document, and classifies each as text-layer vs scanned so step 3
   knows which path to use.
   - `pip install requests pdfplumber pandas`
   - Requires `CH_API_KEY` env var (your existing API key).
   - Respects the 600 requests / 5 min rate limit with a small sleep —
     tune `time.sleep()` if you're running a lot of companies.

3. **`3_extract_from_pdfs.py`** — Extracts turnover and employee figures
   from the downloaded PDFs, escalating through three tiers:
   - Tier A: text-layer extraction + regex/keyword search (cheap)
   - Tier B: OCR fallback for scanned PDFs (slower, needs
     `poppler-utils` + `tesseract-ocr` installed at OS level)
   - Tier C: LLM extraction for anything the regex still can't pin down
     reliably (needs `ANTHROPIC_API_KEY` env var)
   - `pip install pdfplumber pytesseract pdf2image pandas anthropic`

4. **`4_merge_results.py`** — Combines everything into `final_results.csv`
   with a `source` column so you can see which figures came from
   ground-truth XBRL data vs OCR vs LLM inference, and spot-check
   accordingly.

## Setup in Codespaces

1. Add secrets via repo **Settings → Secrets and variables → Codespaces**:
   `CH_API_KEY` and (optionally, for tier C) `ANTHROPIC_API_KEY`. Restart
   the Codespace after adding them — they're injected as env vars.
2. The included `.devcontainer/devcontainer.json` installs
   `poppler-utils` and `tesseract-ocr` and runs `pip install -r
   requirements.txt` automatically on Codespace creation.
3. Put `CH_turnover_request_list_v2.csv` in the repo root (same folder
   as the scripts) before running stage 1.

## Known limitations to budget for

- **Small/micro-entity exemptions**: many small companies are legally
  exempt from disclosing turnover (P&L) in their filed accounts. No
  amount of parsing recovers a number that was never filed — treat
  `null` as a valid, expected outcome for a chunk of your list, not a
  pipeline failure. In the current target list, ~16% (472 of 2,923)
  are in categories (micro-entity, small, abridged, exemption filings)
  that typically don't disclose turnover at all — these are flagged
  automatically as `likely_turnover_disclosed = False` in stage 1, and
  stage 4 separates "expected nulls" from "genuine gaps worth
  investigating" in `genuine_gaps_for_review.csv`.
- **Future period ends**: some rows have an `Accounts_LastMadeUp` date
  that hasn't happened yet — no filing will exist for those until after
  that date passes. Stage 1 prints a count of these on load.
- **Employee numbers** are often in free-text directors' reports rather
  than a clean tagged field, even within XBRL — expect this to have
  lower structured-data coverage than turnover.
- **Tier C (LLM) costs**: only runs on the subset that reaches it, but
  if that subset is large, batch it and consider truncating input
  further or chunking very long accounts documents.
- **Rate limits**: both the CH REST API and the document API have their
  own rate limits — check current limits in the developer docs before
  scaling this up, they do change.
