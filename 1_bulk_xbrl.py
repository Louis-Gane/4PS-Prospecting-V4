"""
Stage 1: Pull structured turnover / employee data from Companies House's
bulk Accounts Data Product, instead of parsing documents one at a time
via the API.

This covers accounts filed electronically (iXBRL/XBRL) -- roughly 60%
of all filings. It's a single bulk download + parse per day/month,
not one API call per company, so it's both more complete and much
faster than per-document scraping.

Install:
    pip install stream-read-xbrl pandas --break-system-packages

Docs: https://pypi.org/project/stream-read-xbrl/
Bulk data source: http://download.companieshouse.gov.uk/en_accountsdata.html
"""

import datetime
import pandas as pd
from stream_read_xbrl import stream_read_xbrl_daily, stream_read_xbrl_monthly


# ---- Config -----------------------------------------------------------

TARGET_LIST_CSV = "CH_turnover_request_list_v2.csv"

# Accounts categories that routinely omit the P&L (and therefore turnover)
# from the copy actually filed at Companies House, regardless of how good
# extraction is. Flagging these up front means a missing turnover for
# these companies gets logged as "not disclosed" rather than treated as
# a pipeline failure further down the pipe.
LIKELY_NO_TURNOVER_DISCLOSED = {
    "MICRO ENTITY",
    "SMALL",
    "UNAUDITED ABRIDGED",
    "AUDITED ABRIDGED",
    "TOTAL EXEMPTION SMALL",
    "TOTAL EXEMPTION FULL",
    "FILING EXEMPTION SUBSIDIARY",
}


def load_target_companies(csv_path: str = TARGET_LIST_CSV) -> pd.DataFrame:
    """
    Load the target list. Expects the columns actually present in
    CH_turnover_request_list_v2.csv: CH_Number, CH_Name, Accounts_Category,
    Accounts_LastMadeUp, plus grouping/priority metadata we carry through
    to the final output for reporting.
    """
    df = pd.read_csv(csv_path, dtype={"CH_Number": str})
    df["CH_Number"] = df["CH_Number"].str.zfill(8)
    df["Accounts_LastMadeUp"] = pd.to_datetime(df["Accounts_LastMadeUp"], dayfirst=True)
    df["likely_turnover_disclosed"] = ~df["Accounts_Category"].isin(LIKELY_NO_TURNOVER_DISCLOSED)

    # Some Accounts_LastMadeUp dates are in the future (period end hasn't
    # happened yet) -- those companies won't have a matching filing yet.
    today = pd.Timestamp(datetime.date.today())
    future_period = df["Accounts_LastMadeUp"] > today
    if future_period.any():
        print(f"Note: {future_period.sum()} companies have a future Accounts_LastMadeUp date "
              f"(period not yet ended) -- no filing will exist for them yet.")

    return df


TARGET_DF = load_target_companies()
TARGET_COMPANY_NUMBERS = set(TARGET_DF["CH_Number"])


# ---- Bulk pull ----------------------------------------------------------

def pull_monthly_xbrl(year: int, month: int) -> pd.DataFrame:
    """
    Streams one month of bulk XBRL data and returns a dataframe filtered
    to your target companies. stream_read_xbrl_monthly downloads and
    parses on the fly -- it does not load the whole month into memory
    at once, so this is safe to run even for large target lists.
    """
    rows = []
    with stream_read_xbrl_monthly(year, month) as (columns, xbrl_rows):
        col_index = {c: i for i, c in enumerate(columns)}
        for row in xbrl_rows:
            company_number = str(row[col_index["companies_house_registered_number"]]).zfill(8)
            if company_number in TARGET_COMPANY_NUMBERS:
                rows.append(row)

    return pd.DataFrame(rows, columns=columns)


def pull_daily_xbrl(date_str: str) -> pd.DataFrame:
    """Same idea but for a single day (date_str format: 'YYYY-MM-DD').
    Daily files are only retained for 60 days, so use this for recent
    catch-up and pull_monthly_xbrl for anything older."""
    rows = []
    with stream_read_xbrl_daily(date_str) as (columns, xbrl_rows):
        col_index = {c: i for i, c in enumerate(columns)}
        for row in xbrl_rows:
            company_number = str(row[col_index["companies_house_registered_number"]]).zfill(8)
            if company_number in TARGET_COMPANY_NUMBERS:
                rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def extract_fields(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pull out just the fields you care about. Column names below are the
    typical stream-read-xbrl output columns -- check
    `df.columns.tolist()` on your actual pull, since the exact set can
    vary by taxonomy version (full accounts vs FRS 105 micro-entity vs
    charity SORP etc).
    """
    wanted = [
        "companies_house_registered_number",
        "company_name",
        "balance_sheet_date",
        "turnover_gross_operating_revenue",
        "average_number_employees_during_period",
    ]
    available = [c for c in wanted if c in df.columns]
    missing = set(wanted) - set(available)
    if missing:
        print(f"Warning: columns not present in this pull: {missing}")
    return df[available]


def month_range(start: datetime.date, end: datetime.date):
    """Yield (year, month) tuples from start to end inclusive."""
    cur = datetime.date(start.year, start.month, 1)
    end = datetime.date(end.year, end.month, 1)
    while cur <= end:
        yield cur.year, cur.month
        # advance one month
        cur = (cur.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


if __name__ == "__main__":
    # We don't know the exact filing date for each company (only the
    # accounts period end, Accounts_LastMadeUp), and private companies
    # have up to 9 months after period end to file. So pull a rolling
    # window: from the earliest relevant period end up to today, plus a
    # buffer, and let the company-number filter do the matching -- this
    # avoids having to guess exact filing dates per company.
    earliest_period_end = TARGET_DF["Accounts_LastMadeUp"].min().date()
    today = datetime.date.today()

    print(f"Pulling monthly bulk data from {earliest_period_end:%Y-%m} to {today:%Y-%m} "
          f"({sum(1 for _ in month_range(earliest_period_end, today))} months)...")
    print("This can take a while for a wide date range -- consider narrowing "
          "earliest_period_end if you only care about recent filings.")

    all_results = []
    for year, month in month_range(earliest_period_end, today):
        print(f"  {year}-{month:02d} ...")
        try:
            monthly = pull_monthly_xbrl(year, month)
        except Exception as e:
            print(f"    skipped ({e}) -- monthly files only go back ~12 months; "
                  f"use historicmonthlyaccountsdata.html source for older periods")
            continue
        if not monthly.empty:
            all_results.append(extract_fields(monthly))

    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        combined = combined.sort_values("balance_sheet_date").drop_duplicates(
            "companies_house_registered_number", keep="last"
        )
    else:
        combined = pd.DataFrame(columns=["companies_house_registered_number", "turnover", "employees"])

    combined.to_csv("structured_results.csv", index=False)
    print(f"Got structured data for {len(combined)} of {len(TARGET_COMPANY_NUMBERS)} target companies")

    still_missing = TARGET_DF[~TARGET_DF["CH_Number"].isin(combined.get("companies_house_registered_number", []))]
    still_missing.to_csv("needs_pdf_extraction.csv", index=False)

    n_unlikely = (~still_missing["likely_turnover_disclosed"]).sum()
    print(f"{len(still_missing)} companies still need PDF extraction -> needs_pdf_extraction.csv")
    print(f"  of which {n_unlikely} are in categories unlikely to disclose turnover at all "
          f"(micro-entity/small/abridged/exemption filings) -- expect nulls for these regardless of extraction quality")
