import json, re
from pathlib import Path
import pickle
from datetime import datetime
import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import json, re

# Scraping from https://csalab.site/conf-track/

URL = "https://csalab.site/conf-track/"
html = requests.get(URL, timeout=30).text


soup = BeautifulSoup(html, "lxml")

# Find the table
table = soup.find("table")

# Extract header row
header_cells = table.find("tr").find_all(["td", "th"])
headers = [c.get_text(strip=True).replace("▼", "") for c in header_cells]

# Extract data rows
rows = []
for tr in table.find_all("tr")[1:]:  # skip header row
    cells = tr.find_all(["td", "th"])
    if not cells:
        continue
    row = {}
    for i, col in enumerate(headers):
        if i >= len(cells):
            row[col] = ""
            continue
        cell = cells[i]
        if col.strip().lower().startswith("website"):
            a = cell.find("a", href=True)
            row[col] = a["href"].strip() if a else cell.get_text(strip=True)
        else:
            row[col] = cell.get_text(" ", strip=True)
    rows.append(row)

# Convert to DataFrame
df = pd.DataFrame(rows)

# Clean up placeholders
df = df.replace({"Click Here": "", "Missing value": ""})
for c in df.columns:
    df[c] = df[c].astype(str).str.strip().replace({"nan": ""})
    

name_to_sub = pickle.load(open("name_to_sub.pkl", "rb"))

# ---- input JSON with fields including "name" and "sub"
MAP_JSON_PATH = Path("./data.json")  # <-- change if needed

# compile once: match any standalone 4-digit year
YEAR_RE = re.compile(r"\b\d{4}\b")

def build_name_to_sub_map(json_path=MAP_JSON_PATH):
    """
    Returns dict:
        normalized_name (year stripped) -> sub
    Example key: "ACM SIGCOMM"
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    mapping = {}
    for entry in data:
        raw_name = (entry.get("name") or "").strip()
        sub_val  = (entry.get("sub") or "").strip()

        # remove 4-digit years, trim extra spaces
        norm_name = YEAR_RE.sub("", raw_name).strip()
        if not norm_name:
            continue

        # first-write wins; skip overwriting if duplicates appear
        mapping.setdefault(norm_name, sub_val)

    return mapping

# build the map
name_to_sub = build_name_to_sub_map()

# --- normalization used ONLY for matching (df stays unchanged) ---
YEAR_RE = re.compile(r"\b\d{4}\b")
CYCLE_RE = re.compile(r"(?i)(?:[-–—]?\s*)?cycle\s*(?:\d+|spring|fall)\b")  
# handles: " - Cycle 1", "Cycle 1", "– Cycle Spring", "Cycle Fall", etc.

def normalize_for_lookup(name: str) -> str:
    """
    Normalizes a conference name string for lookup purposes.

    This function removes year tokens and "Cycle ..." tokens from the input string,
    replaces multiple whitespace characters with a single space, and trims leading
    and trailing whitespace.

    Args:
        name (str): The input conference name to normalize.

    Returns:
        str: The normalized conference name suitable for lookup.
    """
    s = str(name)
    s = YEAR_RE.sub("", s)          # remove year tokens
    s = CYCLE_RE.sub("", s)         # remove "Cycle ..." tokens
    s = re.sub(r"\s+", " ", s)      # squeeze spaces
    return s.strip()

# normalize mapping keys
norm_map = { normalize_for_lookup(k): v for k, v in name_to_sub.items() }

# --- apply to your existing DataFrame (keep original names as-is) ---
# assumes your column is "Conf. Name"; change if it's "name"
df["sub"] = df["Conf. Name"].apply(lambda x: norm_map.get(normalize_for_lookup(x), ""))

# (optional) quick debug: see any that didn’t match
unmatched = df.loc[df["sub"].eq(""), "Conf. Name"].unique().tolist()
print(f"Unmatched {len(unmatched)}:\n", unmatched)

def clean_deadline(val: str) -> str:
    """
    Cleans and standardizes a conference deadline string by removing annotations, time parts, and extraneous phrases.
    Attempts to parse the cleaned string into a standardized date format.

    Args:
        val (str): The raw deadline string to clean and format.

    Returns:
        str: The cleaned and standardized deadline string in the format "Mon DD YYYY" (e.g., "Oct 10 2024"),
             or the cleaned string if parsing fails. Returns an empty string if input is invalid.
    """
    if not isinstance(val, str) or not val.strip():
        return ""
    # remove parenthetical annotations like (AOE), (UTC), (EDT), etc.
    s = re.sub(r"\(.*?\)", "", val)
    # remove "in XX days" or similar phrases
    s = re.sub(r"in\s+\d+\s+days", "", s, flags=re.IGNORECASE)
    # remove time parts like "; 11:59 PM"
    s = re.sub(r";?\s*\d{1,2}:\d{2}\s*(AM|PM)", "", s, flags=re.IGNORECASE)
    s = re.sub(r";", "", s)  # drop leftover semicolons
    s = s.strip()

    # try parsing into datetime
    for fmt in ("%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%b %d %Y")  # e.g., "Oct 10 2024"
        except ValueError:
            continue
    return s  # return original if parse fails

# apply to your DataFrame
df["Submission Deadline"] = df["Submission Deadline"].apply(clean_deadline)
df["Abstract Deadline"] = df["Abstract Deadline"].apply(clean_deadline)

# Slice only Required columns
df = df[['Conf. Name', 'sub', 'Location', 'Start Date', 'Abstract Deadline', 'Submission Deadline', 'Website']].copy()

# Rename according to JSON schema
df.rename(columns={
    "Conf. Name": "name",
    "Website": "link"
}, inplace=True)


'''Scraping EasyChair https://easychair.org/cfp/'''

def fetch_cfp_table(url, timeout=20):
    """
    Fetch an EasyChair Smart CFP table page and return a pandas DataFrame
    with clean columns + useful extras (URLs, ISO dates, topics list).
    Works for:
      - https://easychair.org/cfp/
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (+pandas script; academic use)"
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")
    tbl = soup.select_one("div.ct_tbl table.ct_table")
    if tbl is None:
        raise RuntimeError("Could not find <div class='ct_tbl'> table on the page.")

    rows = []
    for tr in tbl.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue

        # 0: Acronym (anchor)
        a0 = tds[0].find("a")
        acronym = (a0.get_text(strip=True) if a0 else tds[0].get_text(strip=True)) or None
        acronym_url = (a0["href"].strip("cfp/") if a0 and a0.has_attr("href") else None)

        # 1: Name
        name = tds[1].get_text(strip=True) or None

        # 2: Location
        location = tds[2].get_text(strip=True) or None

        # 3: Submission deadline (often empty on “New CFPs” view)
        sub_deadline_iso = tds[3].get("data-key") or None
        sub_deadline_text = tds[3].get_text(strip=True) or None

        # 4: Start date
        start_date_iso = tds[4].get("data-key") or None
        start_date_text = tds[4].get_text(strip=True) or None

        # 5: Topics (list of <span class="tag">)
        topics = [s.get_text(strip=True) for s in tds[5].select("span.tag")]
        topics_joined = "; ".join(topics) if topics else None


        rows.append({
            "Acronym": acronym,
            "Acronym_URL": acronym_url,
            "Name": name,
            "Location": location,
            "Submission_Deadline": sub_deadline_iso,
            "Submission_Deadline": sub_deadline_text,
            "Start_Date": start_date_iso,
            "Start_Date": start_date_text,
            "Topics": topics_joined
        })

    df = pd.DataFrame(rows)

    # Optional: convert ISO date fields to datetime
    for col in ["Submission_Deadline", "Start_Date"]:
        df[col] = pd.to_datetime(df[col], errors="coerce")

    return df



df_main = fetch_cfp_table("https://easychair.org/cfp/")

# RAW unique topics (trimmed, case preserved)
raw_topics = set()
for cell in df_main["Topics"].dropna():
    for t in (x.strip() for x in cell.split(";")):
        if t:
            raw_topics.add(t)

# NORMALIZED unique topics (lowercased, internal whitespace collapsed)
def normalize(s: str) -> str:
    return " ".join(s.lower().split())

normalized_topics = {normalize(t) for t in raw_topics}

print(f"Unique topics (raw): {len(raw_topics)}")
print(f"Unique topics (normalized): {len(normalized_topics)}")
# If you need a sorted list:
sorted_raw = sorted(raw_topics)
sorted_norm = sorted(normalized_topics)

'''This Needs to modified'''
interest_terms = {"5g", "6g", "communication",
                  "network", "wireless", "security", "privacy", "signal"}

def find_sub(row):
    """
    Finds and returns the first relevant topic or interest term from a DataFrame row.

    This function checks the "Topics" column of the given row for any tags that match
    a term from the global `interest_terms` list. If a match is found, it returns the
    first matching tag. If no match is found in "Topics", it then checks the "Name"
    column for any occurrence of an interest term and returns the matching term if found.
    If neither column contains a match, the function returns None.

    Args:
        row (pd.Series or dict): A row from a pandas DataFrame, expected to have "Topics" and "Name" keys.

    Returns:
        str or None: The first matching topic/tag or interest term, or None if no match is found.
    """
    # 1) Try Topics column first
    topics_cell = row.get("Topics")
    if pd.notna(topics_cell):
        tags = [t.strip() for t in str(topics_cell).split(";") if t.strip()]
        for tag in tags:
            for term in interest_terms:
                if term.lower() in tag.lower():
                    return tag   # return the first matching interest term

    # 2) If nothing matched, try Name column
    name_cell = row.get("Name")
    if pd.notna(name_cell):
        for term in interest_terms:
            if term.lower() in str(name_cell).lower():
                return term

    return None

# Apply row-wise
df_main["sub"] = df_main.apply(find_sub, axis=1)

# Drop rows where still no match
df_main = df_main.dropna(subset=["sub"])
df_main = df_main.reset_index(drop=True)

def fmt_date(x):
    """
    Formats a date-like value into a human-readable string in the format "Mon D, YYYY" (e.g., "Sep 5, 2025").

    Parameters:
        x (any): The input value to format as a date. Can be a string, datetime, or any value convertible by pandas.to_datetime.

    Returns:
        str: The formatted date string if conversion is successful, otherwise an empty string.
    """
    if pd.isna(x) or x == "":
        return ""
    dt = pd.to_datetime(x, errors="coerce")
    if pd.isna(dt):
        return ""
    # "Short Month Date Year" (e.g., "Sep 5, 2025")
    return dt.strftime("%b %d, %Y").replace(" 0", " ")

cols = ["Submission_Deadline", "Start_Date"]
for c in cols:
    df_main[c] = df_main[c].apply(fmt_date)
    
    
SESSION = requests.Session()
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; cfp-scraper/1.0)"}

def fetch_website_from_detail(rel_url, base_url="https://easychair.org/cfp/", timeout=20):
    """
    Fetches the conference website URL from a detail page on EasyChair CFP.

    Given a relative URL to a conference detail page, this function constructs the full URL,
    fetches the page, and attempts to extract the conference website link from a table with class 'date_table'.
    It searches for common label variants such as "conference website", "website", etc.

    Args:
        rel_url (str): The relative URL to the conference detail page.
        base_url (str, optional): The base URL to prepend to rel_url. Defaults to "https://easychair.org/cfp/".
        timeout (int, optional): Timeout for the HTTP request in seconds. Defaults to 20.

    Returns:
        str or None: The extracted conference website URL if found, otherwise None.
    """

    if not rel_url or not isinstance(rel_url, str):
        return None
    try:
        detail_url = urljoin(base_url, rel_url)
        r = SESSION.get(detail_url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # robust search in <table class="date_table">
        site = None
        for tr in soup.select("table.date_table tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            label = tds[0].get_text(" ", strip=True)
            label = label.replace("\u00a0", " ").strip().lower()
            # accept common variants
            if any(key in label for key in (
                "conference link", "conference website", "website", "conference site"
            )):
                a = tds[1].find("a")
                if a and a.has_attr("href"):
                    site = a["href"]
                break
        return site
    except Exception:
        return None


def _fmt_short_month_date_year(x: str) -> str:
    """
    Input like 'September 12, 2025' -> 'Sep 12, 2025'
    Returns '' if parsing fails.
    """
    if not x:
        return ""
    dt = pd.to_datetime(x, errors="coerce")
    if pd.isna(dt):
        return ""
    return dt.strftime("%b %d, %Y").replace(" 0", " ")

    
def fetch_abstract_deadline(rel_url, base_url="https://easychair.org/cfp/", timeout=20):
    """
    Fetches the 'Abstract registration deadline' from the date_table of a detail page.
    Returns '' if not found or parse fails.
    """
    if not rel_url or not isinstance(rel_url, str):
        return ""
    try:
        detail_url = urljoin(base_url, rel_url)
        r = SESSION.get(detail_url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        for tr in soup.select("table.date_table tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            label = tds[0].get_text(" ", strip=True).replace("\u00a0", " ").strip().lower()
            if "abstract registration deadline" in label or "abstract deadline" in label:
                raw = tds[1].get_text(" ", strip=True)
                return _fmt_short_month_date_year(raw)
        return ""
    except Exception:
        return ""
    

df_main["link"] = df_main["Acronym_URL"].apply(fetch_website_from_detail)
df_main["Abstract Deadline"] = df_main["Acronym_URL"].apply(fetch_abstract_deadline)

df_main = df_main[["Acronym", "sub", "Location", "Start_Date", "Abstract Deadline", "Submission_Deadline", "link"]].copy()
df_main.rename(columns={
    "Acronym": "name",
    "Start_Date": "Start Date",
    "Submission_Deadline": "Submission Deadline"
}, inplace=True)


final_df = pd.concat([df_main, df], ignore_index=True).fillna('')

final_df["End Date"] = ""
final_df["Notification"] = ""
final_df = final_df[[
    "name",
    "sub",
    "Location",
    "Start Date",
    "End Date",
    "Abstract Deadline",
    "Submission Deadline",
    "Notification",
    "link"
]]

json_str = df.to_json(orient="records", indent=2, force_ascii=False)
json_str = json_str.replace("\\/", "/")

with open("conferences.json", "w", encoding="utf-8") as f:
    f.write(json_str)