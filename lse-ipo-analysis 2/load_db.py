"""Build lse.db from the cleaned LSEG new issues data.

This is my first data-validation script. I used Claude (Anthropic's AI assistant) as a learning tool while
writing it, to understand what to check (data types, completeness, known values) and how to structure the code.

Run from the project folder:   python3 load_db.py
Optional arguments:            python3 load_db.py --new-issues PATH --database PATH --expected-rows N

Inputs
    new_issues.csv            NOT INCLUDED IN THIS REPOSITORY (LSEG data; see the Data section of
                              the README). The "New Issues and IPOs" sheet after cleaning in Excel:
                              issue types harmonised (validated for 2005 onwards), an industry group added for
                              each FTSE sector label, and 107 blank sectors labelled (see below).
                              6,251 rows x 22 columns, plus ~1,786 empty columns that Excel adds to
                              the export (dropped here).
    sector_map.csv            Included. Each FTSE sector label -> the industry group it was given.
    manual_sector_review.csv  Included. The 107 listings with no FTSE sector, with the industry group
                              chosen by Claude (AI assistant) from sources specified by the author (company
                              website, LSE page or Wikipedia), checked by the author, with the URL as evidence.

Output
    lse.db (SQLite) with two tables:
        new_issues     the validated, typed new issues table (22 columns)
        load_metadata  file name, SHA-256 hash and row count of each source CSV, so you can prove
                       which export a database was built from

The two lookup files are used as checks: the loader confirms that the industry group in new_issues
agrees with sector_map.csv for every row, and that every blank-sector listing in the analysis
appears in manual_sector_review.csv with the same group.

Nothing is written unless every check passes; a failed run leaves any existing lse.db untouched.
Exit code 0 = built, 1 = a check failed or an input is missing.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Settings. Change these here, not inside the functions.
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
NEW_ISSUES_CSV = PROJECT_DIR / "new_issues.csv"
SECTOR_MAP_CSV = PROJECT_DIR / "sector_map.csv"
MANUAL_REVIEW_CSV = PROJECT_DIR / "manual_sector_review.csv"
DATABASE_PATH = PROJECT_DIR / "lse.db"   # generated file: kept out of git by .gitignore

# Hard-coded on purpose: if the Excel export gains or loses rows, the load stops and you
# re-check the export against the Excel pivots before changing this number.
EXPECTED_NEW_ISSUES_ROWS = 6251
FIRST_YEAR, LAST_YEAR = 1995, 2025   # AIM opened in June 1995; 2026 is incomplete
DATE_FORMAT = "%d/%m/%Y"             # how Excel writes dates in the CSV; stored in SQLite as yyyy-mm-dd

# Market segments. SFM = Specialist Fund Market; PSM = Professional Securities Market.
MAIN_MARKET_AND_AIM = ("UK Main Market", "International Main Market", "AIM")
VALID_MARKETS = set(MAIN_MARKET_AND_AIM) | {"SFM", "PSM", "Admission to Trading Only"}
VALID_IPO_FLAGS = {"IPO", "Not IPO"}   # LSEG's own IPO flag, column "LSE IPO"
UNMAPPED = "CHECK - unmapped"          # pre-2005 rows whose older sector name is not in sector_map.csv
VALID_INDUSTRY_GROUPS = {
    "Energy", "Basic Materials", "Industrials", "Consumer", "Health Care", "Telecoms",
    "Utilities", "Financials & Real Estate", "Technology", "Non-company security", UNMAPPED,
}
REQUIRED_COLUMNS = ["market", "date", "year", "lse_ipo", "company", "new_issues_type",
                    "ftse_sector", "ftse_group", "market_cap_opening_price_m", "money_raised_new_m"]

# The analysis population: IPOs admitted from 2005 onwards on the UK Main Market, International
# Main Market or AIM. 2005 is the first year in which issue types are harmonised and every sector
# maps to an industry group (the Excel lookups were applied from 2005 only). SFM and PSM are specialist segments and are excluded.
# SQL.ipynb uses exactly this filter.
ANALYSIS_WHERE = (
    "year >= 2005 AND lse_ipo = 'IPO' AND new_issues_type = 'New admission' "
    "AND market IN ('UK Main Market', 'International Main Market', 'AIM')"
)
EXPECTED_ANALYSIS_ROWS = 2282


class DataValidationError(Exception):
    """Raised when the CSV data breaks a rule the analysis depends on."""


def require(condition: bool, message: str) -> None:
    """Stop the load with a readable message if a data rule is broken (used instead of assert,
    which Python skips when run with -O)."""
    if not condition:
        raise DataValidationError(message)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def to_snake_case(column_name: str) -> str:
    """'Market Cap - Opening Price (£m)' -> 'market_cap_opening_price_m'.

    Also strips the stray spaces in the Excel headers (' FTSE Subsector '). Blank headers
    arrive from pandas as 'Unnamed: 22' and become 'unnamed_22'.
    """
    cleaned = column_name.strip().lower().replace("£m", "m").replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")


def read_text_table(csv_path: Path) -> pd.DataFrame:
    """Read a CSV with every value as text, so pandas cannot guess types for us.

    Column names become snake_case, whitespace is trimmed and empty cells
    become missing values (NULL in SQL).
    """
    table = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    table.columns = [to_snake_case(name) for name in table.columns]
    for column in table.columns:
        table[column] = table[column].str.strip().replace({"": None})
    return table


# ---------------------------------------------------------------------------
# Validation: the cleaning itself is done in Excel; these checks guard the export.
# ---------------------------------------------------------------------------
def to_numeric_or_fail(values: pd.Series, column: str) -> pd.Series:
    try:
        return pd.to_numeric(values)
    except ValueError as error:
        raise DataValidationError(f"non-numeric text in {column}: {error}") from error


def validate_new_issues(raw: pd.DataFrame, expected_rows: int = EXPECTED_NEW_ISSUES_ROWS) -> pd.DataFrame:
    """Check the new issues table, repair the three known export artefacts, return it typed.

    Checks: required columns present; exact row count; empty export columns really are empty;
    every date parses and lies in 1995 to 2025; money columns numeric after cleaning; year never
    blank; market, lse_ipo and ftse_group contain only known values; no duplicate listings.

    Also checks that each raw issue_type maps to exactly one new_issues_type, so a lookup filled
    down the wrong rows in Excel cannot slip through. Rows with a blank issue_type are skipped.
    """
    missing_columns = [name for name in REQUIRED_COLUMNS if name not in raw.columns]
    require(not missing_columns, f"missing columns {missing_columns}; found {list(raw.columns)}")
    require(len(raw) == expected_rows, f"expected {expected_rows} rows, found {len(raw)}")
    issues = raw.copy()

    # Repair 1, empty columns: Excel exports ~1,786 blank columns after the last real one.
    # Drop them, but only after confirming they hold no data.
    empty_columns = [name for name in issues.columns if name.startswith("unnamed_")]
    require(issues[empty_columns].isna().all().all(), "an unnamed export column contains data")
    issues = issues.drop(columns=empty_columns)

    # Repair 2, dates: parse with the Excel export format and store as yyyy-mm-dd text.
    dates = pd.to_datetime(issues["date"], format=DATE_FORMAT, errors="coerce")
    require(dates.notna().all(), f"some dates do not match {DATE_FORMAT} (check the Excel export format)")
    require(FIRST_YEAR <= dates.min().year and dates.max().year <= LAST_YEAR,
            f"dates outside {FIRST_YEAR} to {LAST_YEAR}: {dates.min().date()} to {dates.max().date()}")
    issues["date"] = dates.dt.strftime("%Y-%m-%d")

    # Repair 3, money columns: Excel exports "5,366.38" and "-" (not recorded). Strip commas, "-" becomes NULL.
    numeric_columns = ["year"] + [name for name in issues.columns if name.endswith("_m") or name == "issue_price"]
    for column in numeric_columns:
        issues[column] = issues[column].str.replace(",", "", regex=False).replace({"-": None})

    # Convert to real numbers so SQLite stores them as numbers; any other text stops the load.
    for column in numeric_columns:
        issues[column] = to_numeric_or_fail(issues[column], column)
    require(issues["year"].notna().all(), "blank value in year")
    issues["year"] = issues["year"].astype(int)

    # Unknown spellings (or blanks) would make filters such as market IN (...) silently drop rows.
    for column, allowed_values in (("market", VALID_MARKETS), ("lse_ipo", VALID_IPO_FLAGS),
                                   ("ftse_group", VALID_INDUSTRY_GROUPS)):
        unexpected_values = set(issues[column]) - allowed_values
        require(not unexpected_values, f"unexpected {column} values: {unexpected_values}")

    duplicates = issues.duplicated(["company", "date", "market"]).sum()
    require(duplicates == 0, f"{duplicates} duplicate listings (same company, date and market)")

    # The Excel Lookup maps each raw issue type to one harmonised category. If it was filled down the
    # wrong rows, the same raw type ends up with several categories. (Spelling variants such as
    # 'Reverse takeover' and 'Reverse Takeover' are treated as one type.)
    typed = issues.dropna(subset=["issue_type", "new_issues_type"])
    categories_per_type = typed.groupby(typed["issue_type"].str.lower())["new_issues_type"].nunique()
    inconsistent_types = categories_per_type[categories_per_type > 1].index
    inconsistent_rows = int(typed["issue_type"].str.lower().isin(inconsistent_types).sum())
    require(len(inconsistent_types) == 0,
            f"{len(inconsistent_types)} raw issue types map to more than one new_issues_type "
            f"({inconsistent_rows} rows carry one of those types, e.g. {sorted(inconsistent_types)[:3]}). "
            "Re-apply the Excel Lookup from issue_type to new_issues_type.")
    return issues


def validate_sector_map(sector_map: pd.DataFrame, issues: pd.DataFrame) -> None:
    """sector_map.csv: one row per FTSE sector label, giving the industry group it was assigned.

    The group in new_issues (built in Excel from the same lookup) must agree with the file for every
    row that has a sector label. Rows flagged CHECK - unmapped (pre-2005 listings with older sector names) are the only
    ones allowed to be missing from the map.
    """
    require(list(sector_map.columns) == ["sector_label", "industry_group"],
            f"sector_map.csv must have columns sector_label, industry_group; found {list(sector_map.columns)}")
    require(sector_map["sector_label"].notna().all(), "blank sector_label in sector_map")
    require(not sector_map["sector_label"].duplicated().any(), "duplicate sector_label in sector_map")
    unexpected_groups = set(sector_map["industry_group"]) - VALID_INDUSTRY_GROUPS - {"Unclassified"}
    require(not unexpected_groups, f"unexpected industry_group values in sector_map: {unexpected_groups}")

    labelled = issues[issues["ftse_sector"].notna() & (issues["ftse_group"] != UNMAPPED)]
    checked = labelled.merge(sector_map, left_on="ftse_sector", right_on="sector_label", how="left")
    missing = set(checked.loc[checked["industry_group"].isna(), "ftse_sector"])
    require(not missing, f"sector labels missing from sector_map: {missing}")
    disagree = checked[checked["industry_group"] != checked["ftse_group"]]
    require(disagree.empty,
            f"{len(disagree)} rows where ftse_group disagrees with sector_map, e.g. "
            f"{disagree[['ftse_sector', 'ftse_group', 'industry_group']].drop_duplicates().head(3).to_dict('records')}")


def validate_manual_review(review: pd.DataFrame, issues: pd.DataFrame) -> None:
    """manual_sector_review.csv: listings with no FTSE sector, labelled with AI assistance and checked by the author.

    Every row must match exactly one listing, carry the group that appears in new_issues, and have
    an evidence URL. Every blank-sector listing in the analysis population must be covered.
    """
    required = ["company", "date", "chosen_group", "evidence_url"]
    require(all(name in review.columns for name in required), f"manual review needs columns {required}")
    require(not review.duplicated(["company", "date"]).any(), "duplicate company and date in manual review")
    require(review["evidence_url"].notna().all(), "a manually reviewed row has no evidence URL")

    matched = review.merge(issues[["company", "date", "ftse_sector", "ftse_group"]],
                           on=["company", "date"], how="left")
    require(matched["ftse_group"].notna().all(), "a manually reviewed row does not match any listing")
    require(matched["ftse_sector"].isna().all(), "a manually reviewed row has a FTSE sector in new_issues")
    require((matched["chosen_group"] == matched["ftse_group"]).all(),
            "a manually chosen group was not applied to ftse_group in the export")

    population = issues.query(
        "year >= 2005 and lse_ipo == 'IPO' and new_issues_type == 'New admission' "
        "and market in @MAIN_MARKET_AND_AIM")
    require(len(population) == EXPECTED_ANALYSIS_ROWS,
            f"analysis population has {len(population)} rows, expected {EXPECTED_ANALYSIS_ROWS}")
    blank_sector = population[population["ftse_sector"].isna()]
    covered = blank_sector.merge(review[["company", "date"]], on=["company", "date"], how="inner")
    require(len(covered) == len(blank_sector),
            f"{len(blank_sector) - len(covered)} blank-sector listings in the analysis are missing from the manual review")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_database(new_issues: pd.DataFrame, sources: dict[Path, int], database_path: Path) -> None:
    """Write new_issues plus a load_metadata table, then swap the file into place.

    `sources` maps each source CSV to its row count. The data is written to a temporary file
    first, so a failed run never leaves a half-built lse.db behind.
    """
    temporary_path = database_path.with_name(database_path.name + ".tmp")
    temporary_path.unlink(missing_ok=True)
    load_metadata = pd.DataFrame([
        {"source_file": path.name, "sha256": file_sha256(path), "row_count": row_count}
        for path, row_count in sources.items()
    ])
    with closing(sqlite3.connect(temporary_path)) as connection:
        new_issues.to_sql("new_issues", connection, index=False)
        load_metadata.to_sql("load_metadata", connection, index=False)
    os.replace(temporary_path, database_path)


def summarise_database(database_path: Path) -> dict:
    """Headline figures to compare with your Excel pivots before running the notebook."""
    with closing(sqlite3.connect(database_path)) as connection:
        rows, first_date, last_date = connection.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM new_issues").fetchone()
        columns = connection.execute("SELECT COUNT(*) FROM pragma_table_info('new_issues')").fetchone()[0]
        analysis_rows, new_money = connection.execute(
            f"SELECT COUNT(*), ROUND(SUM(money_raised_new_m), 1) FROM new_issues WHERE {ANALYSIS_WHERE}").fetchone()
    return {"rows": rows, "columns": columns, "first_date": first_date, "last_date": last_date,
            "analysis_rows": analysis_rows, "analysis_new_money_m": new_money}


# ---------------------------------------------------------------------------
# Command line entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Validate the CSVs, build lse.db, print the reconciliation figures. Returns the exit code."""
    parser = argparse.ArgumentParser(description="Build lse.db from the cleaned LSEG new issues data.")
    parser.add_argument("--new-issues", type=Path, default=NEW_ISSUES_CSV)
    parser.add_argument("--sector-map", type=Path, default=SECTOR_MAP_CSV)
    parser.add_argument("--manual-review", type=Path, default=MANUAL_REVIEW_CSV)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--expected-rows", type=int, default=EXPECTED_NEW_ISSUES_ROWS)
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not arguments.new_issues.exists():
        logger.error(
            "%s not found.\nThe LSEG data is not included in this repository because redistribution "
            "has not been confirmed.\nSee the 'Data' section of the README for the source and the "
            "cleaning steps.", arguments.new_issues.name)
        return 1
    try:
        new_issues = validate_new_issues(read_text_table(arguments.new_issues), arguments.expected_rows)
        sector_map = read_text_table(arguments.sector_map)
        manual_review = read_text_table(arguments.manual_review)
        validate_sector_map(sector_map, new_issues)
        validate_manual_review(manual_review, new_issues)
    except DataValidationError as error:
        logger.error("CHECK FAILED: %s", error)
        return 1

    build_database(new_issues, {arguments.new_issues: len(new_issues),
                                arguments.sector_map: len(sector_map),
                                arguments.manual_review: len(manual_review)}, arguments.database)
    summary = summarise_database(arguments.database)
    logger.info("new_issues: %(rows)d rows x %(columns)d columns, %(first_date)s to %(last_date)s", summary)
    logger.info("Analysis population (2005+, IPO, New admission, Main Market + AIM): %(analysis_rows)d IPOs", summary)
    logger.info("New money raised by the analysis population (GBP m): %(analysis_new_money_m)s", summary)
    logger.info("Compare these figures with your Excel pivots before running SQL.ipynb.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
