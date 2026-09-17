"""
Stage 4: Combine stage 1 (structured XBRL) and stage 3 (PDF-derived)
results into a single output, tagging the source of each figure so you
can audit confidence later -- structured XBRL data is ground truth,
PDF-derived data (especially tier B/C) should be spot-checked.
"""

import pandas as pd

structured = pd.read_csv("structured_results.csv").rename(
    columns={
        "companies_house_registered_number": "CH_Number",
        "turnover_gross_operating_revenue": "turnover",
        "average_number_employees_during_period": "employees",
    }
)
structured["source"] = "xbrl_bulk_data"

pdf_derived = pd.read_csv("pdf_extraction_results.csv").rename(columns={"company_number": "CH_Number"})
pdf_derived["source"] = pdf_derived["method"]
pdf_derived = pdf_derived[["CH_Number", "turnover", "employees", "source"]]

combined = pd.concat([
    structured[["CH_Number", "turnover", "employees", "source"]],
    pdf_derived,
])
combined["CH_Number"] = combined["CH_Number"].astype(str).str.zfill(8)

# Join back onto the original request list so Priority_Tier, Group_Name,
# Accounts_Category etc. are carried through into the final deliverable.
target_list = pd.read_csv("CH_turnover_request_list_v2.csv", dtype={"CH_Number": str})
target_list["CH_Number"] = target_list["CH_Number"].str.zfill(8)

final = target_list.merge(combined, on="CH_Number", how="left")
final.to_csv("final_results.csv", index=False)

print(final["source"].value_counts(dropna=False))
print(f"\nOverall coverage: {final['turnover'].notna().sum()} / {len(final)} companies with turnover data")

# Companies with no turnover AND in a category unlikely to disclose it --
# these are expected nulls, not extraction failures. Worth reporting
# separately so you're not chasing figures that were never filed.
expected_nulls = final[final["turnover"].isna() & ~final["likely_turnover_disclosed"]]
print(f"Of the missing rows, {len(expected_nulls)} are in categories that typically "
      f"don't disclose turnover at all (expected, not a pipeline failure)")

genuine_gaps = final[final["turnover"].isna() & final["likely_turnover_disclosed"]]
genuine_gaps.to_csv("genuine_gaps_for_review.csv", index=False)
print(f"{len(genuine_gaps)} companies have no turnover despite being in a category that "
      f"should disclose it -> genuine_gaps_for_review.csv (worth investigating manually)")
