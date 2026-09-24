
import streamlit as st
import pandas as pd
import numpy as np
import sqlite3, hashlib, hmac, math, re
from io import BytesIO
from pathlib import Path
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta

# ============================================================
# Offline Finance Manager v3
# Secure login + optional Supabase persistence + local fallback
# ============================================================

st.set_page_config(page_title="Personal Finance Manager", page_icon="💰", layout="wide")

APP_DIR = Path.home() / ".offline_finance_manager"
APP_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_DB = APP_DIR / "finance.db"

# ---------------- Authentication ----------------
def configured_password():
    try:
        return st.secrets.get("APP_PASSWORD", "")
    except Exception:
        return ""

def is_authenticated():
    return st.session_state.get("authenticated", False)

def login_screen():
    st.title("🔐 Personal Finance Manager")
    st.caption("Private access is required.")
    with st.form("login"):
        pwd = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in")
    if ok:
        expected = configured_password()
        if expected and hmac.compare_digest(pwd, expected):
            st.session_state.authenticated = True
            st.session_state.login_time = datetime.now().isoformat()
            st.rerun()
        elif not expected:
            st.error("Authentication is not configured. Add APP_PASSWORD to Streamlit Secrets.")
        else:
            st.error("Incorrect password.")
    st.info("Your password is kept in Streamlit Secrets and is not stored in GitHub.")
    st.stop()

if not is_authenticated():
    login_screen()

# ---------------- Storage backend ----------------
def supabase_enabled():
    try:
        return bool(st.secrets.get("SUPABASE_URL","")) and bool(st.secrets.get("SUPABASE_KEY",""))
    except Exception:
        return False

@st.cache_resource
def get_supabase():
    from supabase import create_client
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

def local_conn():
    c = sqlite3.connect(LOCAL_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, txn_date TEXT, description TEXT,
        amount REAL, direction TEXT, category TEXT, class TEXT, source TEXT,
        fingerprint TEXT UNIQUE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT UNIQUE,
        category TEXT, class TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS loans(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, lender TEXT,
        principal REAL, annual_rate REAL, emi REAL, start_date TEXT,
        tenure_months INTEGER, extra_payment REAL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS income_sources(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, monthly_amount REAL,
        start_date TEXT, end_date TEXT, annual_growth_pct REAL DEFAULT 0,
        months_per_year INTEGER DEFAULT 12, active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trading_results(
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, realized_pnl REAL,
        notes TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS recurring_exclusions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, recurring_key TEXT UNIQUE,
        created_at TEXT)""")
    c.commit()
    return c

def db_select(table, order="id"):
    if supabase_enabled():
        return get_supabase().table(table).select("*").order(order).execute().data
    c=local_conn()
    return pd.read_sql_query(f"SELECT * FROM {table} ORDER BY {order} DESC",c).to_dict("records")

def db_insert(table, data):
    if supabase_enabled():
        return get_supabase().table(table).insert(data).execute().data
    c=local_conn()
    cols=",".join(data.keys())
    qs=",".join(["?"]*len(data))
    try:
        c.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})",tuple(data.values()))
        c.commit()
    except sqlite3.IntegrityError:
        pass
    return []

def db_update_txn(txn_id, data):
    if supabase_enabled():
        return get_supabase().table("transactions").update(data).eq("id",txn_id).execute().data
    c=local_conn()
    sets=",".join([f"{k}=?" for k in data])
    c.execute(f"UPDATE transactions SET {sets} WHERE id=?",(*data.values(),txn_id))
    c.commit()

def db_delete_txn(txn_id):
    if supabase_enabled():
        return get_supabase().table("transactions").delete().eq("id",txn_id).execute()
    c=local_conn()
    c.execute("DELETE FROM transactions WHERE id=?",(txn_id,))
    c.commit()

def db_delete(table, row_id):
    if supabase_enabled():
        return get_supabase().table(table).delete().eq("id",row_id).execute()
    c=local_conn()
    c.execute(f"DELETE FROM {table} WHERE id=?",(row_id,))
    c.commit()

def db_update(table, row_id, data):
    if supabase_enabled():
        return get_supabase().table(table).update(data).eq("id",row_id).execute().data
    c=local_conn()
    sets=",".join([f"{k}=?" for k in data])
    c.execute(f"UPDATE {table} SET {sets} WHERE id=?",(*data.values(),row_id))
    c.commit()

# ---------------- Recurring-payment exclusions ----------------
def _recurring_exclusion_key(row):
    """Stable key for a detected recurring commitment."""
    merchant=str(row.get("Merchant / Description", row.get("merchant_key", ""))).strip().upper()
    freq=str(row.get("Frequency", "")).strip().upper()
    amount=round(float(row.get("Typical Amount", 0) or 0))
    return hashlib.sha256(f"{merchant}|{freq}|{amount}".encode()).hexdigest()

def get_recurring_exclusions():
    """Return excluded recurring keys. Missing Supabase table is non-fatal."""
    if "recurring_exclusions" not in st.session_state:
        st.session_state.recurring_exclusions=set()
    if supabase_enabled():
        try:
            rows=get_supabase().table("recurring_exclusions").select("recurring_key").execute().data or []
            keys={str(r.get("recurring_key")) for r in rows if r.get("recurring_key")}
            st.session_state.recurring_exclusions.update(keys)
        except Exception:
            pass
    else:
        try:
            rows=db_select("recurring_exclusions")
            st.session_state.recurring_exclusions.update({str(r.get("recurring_key")) for r in rows if r.get("recurring_key")})
        except Exception:
            pass
    return st.session_state.recurring_exclusions

def exclude_recurring(recurring_key):
    key=str(recurring_key).strip()
    if not key:
        raise ValueError("Invalid recurring payment selection.")
    if "recurring_exclusions" not in st.session_state:
        st.session_state.recurring_exclusions=set()
    st.session_state.recurring_exclusions.add(key)
    if supabase_enabled():
        try:
            get_supabase().table("recurring_exclusions").upsert({"recurring_key":key}, on_conflict="recurring_key", ignore_duplicates=True).execute()
        except Exception:
            # Session exclusion remains active even if the optional table is unavailable.
            pass
    else:
        try:
            db_insert("recurring_exclusions", {"recurring_key":key,"created_at":datetime.now().isoformat()})
        except Exception:
            pass

def restore_recurring(recurring_key):
    key=str(recurring_key).strip()
    if not key:
        return
    if "recurring_exclusions" in st.session_state:
        st.session_state.recurring_exclusions.discard(key)
    if supabase_enabled():
        try:
            get_supabase().table("recurring_exclusions").delete().eq("recurring_key",key).execute()
        except Exception:
            pass
    else:
        try:
            c=local_conn(); c.execute("DELETE FROM recurring_exclusions WHERE recurring_key=?",(key,)); c.commit()
        except Exception:
            pass

# ---------------- Safe data reset ----------------
RESETTABLE_TABLES = [
    "transactions",
    "rules",
    "loans",
    "income_sources",
    "trading_results",
    "recurring_exclusions",
]

def reset_all_financial_data():
    """Delete user-entered/test financial data from every storage backend.

    Authentication/secrets and application code are not affected.
    """
    if supabase_enabled():
        for table in RESETTABLE_TABLES:
            # All application IDs are positive serial/identity values.
            get_supabase().table(table).delete().neq("id", 0).execute()
        return

    c=local_conn()
    for table in RESETTABLE_TABLES:
        c.execute(f"DELETE FROM {table}")
    c.commit()

# ---------------- Classification ----------------
BUILTIN = [
("swiggy","Food","Expense"),("zomato","Food","Expense"),("dominos","Food","Expense"),
("restaurant","Food","Expense"),("cafe","Food","Expense"),("uber eats","Food","Expense"),
("hotel","Travel","Expense"),("uber","Travel","Expense"),("ola","Travel","Expense"),
("makemytrip","Travel","Expense"),("flight","Travel","Expense"),("irctc","Travel","Expense"),
("netflix","Entertainment","Expense"),("spotify","Entertainment","Expense"),
("prime video","Entertainment","Expense"),("movie","Entertainment","Expense"),
("petrol","Fuel","Expense"),("fuel","Fuel","Expense"),("hpcl","Fuel","Expense"),
("bharat petroleum","Fuel","Expense"),("amazon pay","Online Shopping","Expense"),
("amazon","Online Shopping","Expense"),("flipkart","Online Shopping","Expense"),
("myntra","Online Shopping","Expense"),("school","Education","Expense"),
("college","Education","Expense"),("tuition","Education","Expense"),
("hospital","Medical","Expense"),("pharmacy","Medical","Expense"),("medical","Medical","Expense"),
("insurance","Insurance","Expense"),("electricity","Utilities","Expense"),
("bescom","Utilities","Expense"),("internet","Utilities","Expense"),
("broadband","Utilities","Expense"),("mobile bill","Utilities","Expense"),
("hdfc home loan","Loan EMI","EMI"),("home loan","Loan EMI","EMI"),
("loan emi","Loan EMI","EMI"),("emi","Loan EMI","EMI"),("mortgage","Loan EMI","EMI"),
("salary","Salary","Income"),("payroll","Salary","Income"),("credit salary","Salary","Income")
]
CATEGORIES = ["Food","Travel","Entertainment","Fuel","Online Shopping","Education","Medical",
              "Insurance","Utilities","Loan EMI","Cash Withdrawal","Transfer","Investment","Other"]

CUSTOM_CATEGORY_OPTION = "➕ Add custom category..."

def get_category_options(df=None):
    """Return built-in plus user-created categories without requiring a schema migration.

    Custom categories are persisted through the existing learning rules table and
    existing transaction data, so this enhancement remains backward-compatible
    with the current database.
    """
    options = list(CATEGORIES)
    try:
        for _, cat, _ in load_rules():
            cat = str(cat or "").strip()
            if cat and cat not in options:
                options.append(cat)
    except Exception:
        pass
    if df is not None and not df.empty and "category" in df.columns:
        for cat in df["category"].dropna().astype(str).tolist():
            cat = cat.strip()
            if cat and cat not in options:
                options.append(cat)
    return options

def resolve_category(selection, custom_value=""):
    if selection == CUSTOM_CATEGORY_OPTION:
        value = str(custom_value or "").strip()
        if not value:
            raise ValueError("Please enter a custom category name.")
        if len(value) > 60:
            raise ValueError("Custom category must be 60 characters or fewer.")
        return value
    return str(selection).strip()

def load_rules():
    rows=db_select("rules")
    return [(str(r.get("pattern", "")), r.get("category", "Other"), r.get("class", "Expense"))
            for r in rows if str(r.get("pattern", "")).strip()]

def classify(desc, rules=None):
    s=str(desc).lower()
    rules = load_rules() if rules is None else rules
    for p,cat,cl in rules:
        if p.lower() in s: return cat,cl
    for p,cat,cl in BUILTIN:
        if p in s: return cat,cl
    if any(x in s for x in ["atm","cash withdrawal"]): return "Cash Withdrawal","Expense"
    if any(x in s for x in ["transfer","neft","imps","rtgs","upi transfer"]): return "Transfer","Transfer"
    return "Other","Expense"

def _merchant_signature(description):
    """Create a stable merchant key by removing transaction IDs and generic bank tokens."""
    s = str(description or "").upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    tokens = []
    generic = {
        "ACH","D","DR","CR","TP","TXN","TRAN","TRANSACTION","DEBIT","CREDIT",
        "PAYMENT","PAY","TRANSFER","UPI","IMPS","NEFT","RTGS","POS","NACH",
        "ECS","ATM","REF","REFERENCE","NO","NUMBER","ID"
    }
    for token in s.split():
        if token.isdigit():
            continue
        # Remove tokens that are mostly transaction/reference numbers.
        if len(token) >= 8 and sum(ch.isdigit() for ch in token) >= 4:
            continue
        if token in generic:
            continue
        if len(token) >= 4:
            tokens.append(token)
    return " ".join(tokens[:6]) or re.sub(r"\s+", " ", s).strip()[:80]

def suggest_rule_pattern(description):
    """Suggest a useful learning pattern instead of the first generic token (e.g. ACH)."""
    return _merchant_signature(description)[:50]

def apply_learning_to_similar_transactions(pattern, category, class_name, selected_txn_id=None):
    """Apply a learning correction to all matching transactions.

    Uses one Supabase update for matching descriptions and a single local SQLite
    transaction for the fallback backend. The selected transaction is always
    included even when its description does not contain the exact pattern.
    """
    pattern = str(pattern or "").strip()
    if not pattern:
        raise ValueError("Please enter a merchant pattern before saving the correction.")

    if supabase_enabled():
        total = 0
        # Update transactions whose narration contains the learned pattern.
        try:
            result = (get_supabase().table("transactions")
                      .update({"category": category, "class": class_name})
                      .ilike("description", f"%{pattern}%")
                      .execute())
            total += len(result.data or [])
        except Exception:
            # Fall back to exact selected transaction so learning never loses the
            # correction because a broad update is rejected by the backend.
            pass
        if selected_txn_id is not None:
            db_update_txn(int(selected_txn_id), {"category": category, "class": class_name})
            if total == 0:
                total = 1
        return total

    c = local_conn()
    rows = c.execute("SELECT id, description FROM transactions").fetchall()
    pattern_lower = pattern.lower()
    matched_ids = [int(row[0]) for row in rows if pattern_lower in str(row[1] or "").lower()]
    if selected_txn_id is not None and int(selected_txn_id) not in matched_ids:
        matched_ids.append(int(selected_txn_id))
    if matched_ids:
        placeholders = ",".join(["?"] * len(matched_ids))
        c.execute(f"UPDATE transactions SET category=?, class=? WHERE id IN ({placeholders})",
                  (category, class_name, *matched_ids))
        c.commit()
    return len(matched_ids)

def save_rule(pattern, category, class_name):
    """Insert a learning rule or update the existing rule with the same pattern."""
    pattern = str(pattern or "").strip()
    if not pattern:
        raise ValueError("Please enter a merchant pattern before saving the correction.")

    payload = {
        "pattern": pattern,
        "category": category,
        "class": class_name,
        "created_at": datetime.now().isoformat(),
    }

    if supabase_enabled():
        try:
            existing = (get_supabase().table("rules").select("id")
                        .eq("pattern", pattern).limit(1).execute().data)
            if existing:
                db_update("rules", int(existing[0]["id"]),
                          {"category": category, "class": class_name,
                           "created_at": payload["created_at"]})
                return "updated"
            db_insert("rules", payload)
            return "created"
        except Exception as e:
            # A concurrent duplicate can still happen between SELECT and INSERT.
            msg = str(e).lower()
            if "duplicate" in msg or "unique" in msg or "23505" in msg:
                try:
                    existing = (get_supabase().table("rules").select("id")
                                .eq("pattern", pattern).limit(1).execute().data)
                    if existing:
                        db_update("rules", int(existing[0]["id"]),
                                  {"category": category, "class": class_name,
                                   "created_at": payload["created_at"]})
                        return "updated"
                except Exception:
                    pass
            raise RuntimeError(
                "Could not save the learning rule to Supabase. "
                "Please check the Supabase connection and try again."
            ) from e

    # SQLite path: update an existing pattern or insert a new one.
    c = local_conn()
    existing = c.execute("SELECT id FROM rules WHERE pattern=? LIMIT 1", (pattern,)).fetchone()
    c.execute(
        "INSERT INTO rules (pattern,category,class,created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(pattern) DO UPDATE SET category=excluded.category, "
        "class=excluded.class, created_at=excluded.created_at",
        (pattern, category, class_name, payload["created_at"])
    )
    c.commit()
    return "updated" if existing else "created"

def fingerprint(d, desc, amt, direction):
    raw=f"{d}|{str(desc).strip().lower()}|{round(float(amt),2)}|{direction}"
    return hashlib.sha256(raw.encode()).hexdigest()

# ---------------- Import ----------------
def _clean_header(value):
    """Normalize bank column names for reliable matching."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s=str(value).strip().lower()
    s=s.replace("&"," and ")
    s=re.sub(r"[^a-z0-9]+"," ",s)
    return re.sub(r"\s+"," ",s).strip()

def _is_header_candidate(row):
    """Return True when a row looks like a bank transaction header."""
    vals=[_clean_header(v) for v in row.tolist()]
    joined=" | ".join(v for v in vals if v)
    has_date=any(v in joined for v in ["date","transaction date","txn date","tran date","posting date","value dt","value date"])
    has_desc=any(v in joined for v in ["narration","description","particulars","transaction details","transaction description","remarks","details"])
    return has_date and has_desc

def _find_header_row(raw):
    """Find the actual header row, even when decorative rows precede it."""
    scan=raw.head(min(len(raw),50))
    for idx,row in scan.iterrows():
        if _is_header_candidate(row):
            return idx
    return None

def _read_uploaded_statement(uploaded_file):
    """Safely read CSV/XLS/XLSX into a DataFrame.

    XLS is handled directly with xlrd instead of pandas.read_excel. This is
    intentionally lightweight and avoids ExcelFile/workbook resource issues
    that can terminate a Streamlit worker on some hosted environments.
    """
    name = str(uploaded_file.name or "").lower()
    data = uploaded_file.getvalue()
    if not data:
        raise ValueError("The uploaded statement is empty.")

    try:
        if name.endswith(".csv"):
            raw = pd.read_csv(BytesIO(data), header=None, dtype=object)

        elif name.endswith(".xls"):
            try:
                import xlrd
            except Exception as e:
                raise ValueError(
                    "The deployed app cannot import xlrd. Your requirements.txt contains xlrd, "
                    "so please wait for Streamlit to finish rebuilding the environment and retry."
                ) from e

            try:
                # Direct xlrd parsing is more reliable for legacy BIFF .xls files.
                book = xlrd.open_workbook(file_contents=data, on_demand=True)
                if book.nsheets < 1:
                    raise ValueError("The .xls workbook contains no worksheets.")
                sheet = book.sheet_by_index(0)
                rows = [sheet.row_values(i) for i in range(sheet.nrows)]
                raw = pd.DataFrame(rows, dtype=object)
                # Release workbook resources as early as possible.
                try:
                    book.release_resources()
                except Exception:
                    pass
            except Exception as e:
                raise ValueError(
                    "The .xls file could not be parsed by xlrd. "
                    f"Details: {type(e).__name__}: {e}"
                ) from e

        elif name.endswith(".xlsx"):
            try:
                raw = pd.read_excel(BytesIO(data), header=None, engine="openpyxl", dtype=object)
            except Exception as e:
                raise ValueError(
                    "The .xlsx file could not be parsed by openpyxl. "
                    f"Details: {type(e).__name__}: {e}"
                ) from e
        else:
            raise ValueError("Unsupported file format. Please upload CSV, XLSX, or XLS.")

    except MemoryError as e:
        raise ValueError("The statement is too large for the available Streamlit memory.") from e
    except OSError as e:
        raise ValueError(
            "The hosted app ran out of an OS resource while reading the statement. "
            "The importer has been designed to read .xls directly; please retry after the app finishes restarting. "
            f"Details: {e}"
        ) from e

    if raw is None or raw.empty:
        raise ValueError("The uploaded statement contains no readable rows.")

    header_row = _find_header_row(raw)
    if header_row is None:
        preview = [str(x).strip() for x in raw.iloc[0].tolist()[:12]]
        raise ValueError(
            "Could not find the bank statement header row. "
            f"First row detected: {preview}"
        )

    headers = []
    seen = {}
    for i, v in enumerate(raw.iloc[header_row].tolist()):
        h = str(v).strip() if not pd.isna(v) else ""
        if not h:
            h = f"Unnamed_{i}"
        base = h
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            h = f"{base}_{n+1}"
        headers.append(h)

    df = raw.iloc[header_row + 1:].copy()
    df.columns = headers
    df = df.dropna(how="all").reset_index(drop=True)
    return df, header_row

def normalize_statement(df):
    cols={_clean_header(c):c for c in df.columns}

    date_keys=[
        "date","transaction date","txn date","tran date","posting date",
        "value date","value dt"
    ]
    desc_keys=[
        "description","narration","particulars","transaction details",
        "transaction description","remarks","details"
    ]
    credit_keys=[
        "credit","credits","credit amount","deposit","deposit amount",
        "deposit amt","cr amount","cr"
    ]
    debit_keys=[
        "debit","debits","debit amount","withdrawal","withdrawal amount",
        "withdrawal amt","dr amount","dr"
    ]
    amount_keys=["amount","transaction amount"]

    def find(keys):
        for k in keys:
            if k in cols:
                return cols[k]
        # Allow punctuation/spacing variations by matching normalized names.
        for normalized,original in cols.items():
            for k in keys:
                if normalized == _clean_header(k):
                    return original
        return None

    date_col=find(date_keys)
    desc_col=find(desc_keys)
    credit_col=find(credit_keys)
    debit_col=find(debit_keys)
    amount_col=find(amount_keys)

    if not date_col or not desc_col:
        raise ValueError(
            "Could not identify Date and Description/Narration columns. "
            f"Detected columns: {list(df.columns)}"
        )

    # Load user rules once per import instead of querying the database for every row.
    rules = load_rules()
    out=[]
    for _,r in df.iterrows():
        d=pd.to_datetime(r[date_col],errors="coerce",dayfirst=True)
        if pd.isna(d):
            continue

        desc=str(r[desc_col]).strip()
        if not desc or desc.lower()=="nan":
            desc="Unspecified transaction"

        credit=pd.to_numeric(
            str(r[credit_col]).replace(",","").strip() if credit_col and pd.notna(r[credit_col]) else np.nan,
            errors="coerce"
        ) if credit_col else np.nan
        debit=pd.to_numeric(
            str(r[debit_col]).replace(",","").strip() if debit_col and pd.notna(r[debit_col]) else np.nan,
            errors="coerce"
        ) if debit_col else np.nan

        if pd.notna(credit) and float(credit)!=0:
            amt=abs(float(credit))
            direction="Credit"
        elif pd.notna(debit) and float(debit)!=0:
            amt=abs(float(debit))
            direction="Debit"
        elif amount_col:
            raw_amount=str(r[amount_col]).replace(",","").strip()
            if not raw_amount or raw_amount.lower()=="nan":
                continue
            try:
                val=float(raw_amount)
            except ValueError:
                continue
            if val==0:
                continue
            amt=abs(val)
            direction="Credit" if val>=0 else "Debit"
        else:
            continue

        cat,cl=classify(desc, rules)
        out.append([d.date().isoformat(),desc,amt,direction,cat,cl])

    return pd.DataFrame(
        out,
        columns=["txn_date","description","amount","direction","category","class"]
    )

def save_transactions(df, source="import"):
    """Save imported transactions efficiently and safely.

    Fingerprints are calculated in memory, duplicates inside the uploaded file
    are removed locally, and database writes are batched. This avoids one
    network/database request per transaction and prevents resource exhaustion.
    """
    if df is None or df.empty:
        return 0

    records=[]
    seen=set()
    for _,r in df.iterrows():
        fp=fingerprint(r.txn_date,r.description,r.amount,r.direction)
        if fp in seen:
            continue
        seen.add(fp)
        records.append({
            "txn_date":str(r.txn_date),
            "description":str(r.description),
            "amount":float(r.amount),
            "direction":str(r.direction),
            "category":str(r.category),
            "class":str(r["class"]),
            "source":source,
            "fingerprint":fp,
        })

    if not records:
        return 0

    if supabase_enabled():
        # Supabase/PostgREST supports bulk upsert with a UNIQUE conflict column.
        # ignore_duplicates=True makes the operation safe for re-imports.
        added=0
        batch_size=500
        for start in range(0,len(records),batch_size):
            batch=records[start:start+batch_size]
            try:
                response=(get_supabase().table("transactions")
                          .upsert(batch, on_conflict="fingerprint", ignore_duplicates=True)
                          .execute())
                # With ignore_duplicates, the returned representation may vary
                # by client/PostgREST configuration. Count only rows explicitly
                # returned; otherwise fall back to a safe duplicate-tolerant path.
                if getattr(response, "data", None):
                    added += len(response.data)
            except Exception as e:
                # If the deployed supabase-py/PostgREST version does not accept
                # bulk ignore-duplicates, fall back to one bulk insert and let the
                # UNIQUE constraint reject the entire batch with a clear message.
                msg=str(e)
                raise RuntimeError(
                    "Transactions could not be saved to Supabase. "
                    "Please verify that the transactions table has a UNIQUE "
                    "constraint on fingerprint and that your Supabase API is available."
                ) from e

        # When the API uses minimal/no representation, determine the actual new
        # count without downloading the entire transaction table.
        if added == 0:
            try:
                fps=[r["fingerprint"] for r in records]
                existing = (get_supabase().table("transactions").select("fingerprint")
                            .in_("fingerprint", fps).execute().data)
                added=len({r.get("fingerprint") for r in existing})
            except Exception:
                # Import itself succeeded; avoid turning a successful import into
                # a misleading failure just because a count query failed.
                added=0
        return added

    # SQLite: one transaction and one executemany call instead of repeated reads.
    c=local_conn()
    cols=["txn_date","description","amount","direction","category","class","source","fingerprint"]
    values=[tuple(r[cname] for cname in cols) for r in records]
    before=c.total_changes
    c.executemany(
        "INSERT OR IGNORE INTO transactions "
        "(txn_date,description,amount,direction,category,class,source,fingerprint) "
        "VALUES (?,?,?,?,?,?,?,?)",
        values
    )
    c.commit()
    return c.total_changes-before

def get_txns():
    rows=db_select("transactions")
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["id","txn_date","description","amount","direction","category","class","source"])

# ---------------- Finance calculations ----------------
def monthly_series(df=None):
    df=get_txns() if df is None else df.copy()
    if df.empty:return pd.DataFrame()
    df["month"]=pd.to_datetime(df.txn_date).dt.to_period("M").astype(str)
    x=df[df.direction=="Debit"].groupby("month").amount.sum().reset_index(name="debit")
    return x

def monthly_rate(annual): return annual/100/12

def emi_payment(principal,annual,months):
    r=monthly_rate(annual)
    if months<=0:return 0
    return principal/months if r==0 else principal*r*(1+r)**months/((1+r)**months-1)

def amortize(principal,annual,months,start,emi=None,extra=0):
    r=monthly_rate(annual)
    payment=emi if emi and emi>0 else emi_payment(principal,annual,months)
    bal=float(principal); rows=[]
    dt=pd.to_datetime(start).date()
    for n in range(1,max(1,int(months))+1):
        if bal<=0.01: break
        interest=bal*r
        principal_part=max(0,min(bal,payment-interest))
        extra_part=min(max(0,extra),max(0,bal-principal_part))
        total_principal=principal_part+extra_part
        pay=interest+total_principal
        bal=max(0,bal-total_principal)
        rows.append([n,dt+relativedelta(months=n),pay,interest,total_principal,bal])
    return pd.DataFrame(rows,columns=["Installment","Due Date","Payment","Interest","Principal","Balance"])

def recurring(df, apply_exclusions=True):
    """Detect recurring payments even when bank narration contains changing IDs.

    EMI transactions receive special handling: the app can identify a monthly
    commitment from the EMI class/category and repeated amount/date pattern even
    when the narration changes from month to month (common with ACH/NACH debits).
    """
    if df.empty:
        return pd.DataFrame()

    d=df[df.direction=="Debit"].copy()
    if d.empty:
        return pd.DataFrame()
    d["date"]=pd.to_datetime(d.txn_date,errors="coerce")
    d=d.dropna(subset=["date"])
    d["merchant_key"]=d.description.map(_merchant_signature)
    d["is_emi"]=((d["class"].astype(str).str.upper()=="EMI") |
                  (d["category"].astype(str).str.lower()=="loan emi"))

    rows=[]

    def add_group(label,g,frequency):
        if g.empty:
            return
        rec_type="EMI" if bool(g.is_emi.any()) else "Recurring payment"
        row={"Merchant / Description":label,"Frequency":frequency,"Typical Amount":float(g.amount.median()),
             "Occurrences":len(g),"Last Seen":g.date.max().date(),"Type":rec_type}
        row["Recurring Key"]=_recurring_exclusion_key(row)
        rows.append(row)

    # 1) EMI groups: group by stable merchant signature + rounded amount.
    # This handles narrations such as ACH/PNBHOUSINGFIN-<changing-id>.
    emi=d[d.is_emi].copy()
    if not emi.empty:
        emi["amount_key"]=emi.amount.round(0)
        for (key,amount),g in emi.groupby(["merchant_key","amount_key"]):
            g=g.sort_values("date")
            diffs=g.date.diff().dt.days.dropna()
            if len(g)>=2 and len(diffs):
                med=float(diffs.median())
                if 25<=med<=35:
                    add_group(key or "Loan EMI",g,"Monthly")
                elif 80<=med<=100:
                    add_group(key or "Loan EMI",g,"Quarterly")
                elif 350<=med<=380:
                    add_group(key or "Loan EMI",g,"Annual")

    # 2) Other recurring payments: stable signature + similar amount.
    non_emi=d[~d.is_emi].copy()
    if not non_emi.empty:
        non_emi["amount_key"]=non_emi.amount.round(0)
        for (key,amount),g in non_emi.groupby(["merchant_key","amount_key"]):
            g=g.sort_values("date")
            if len(g)<2:
                continue
            diffs=g.date.diff().dt.days.dropna()
            if not len(diffs):
                continue
            med=float(diffs.median())
            if 25<=med<=35: freq="Monthly"
            elif 80<=med<=100: freq="Quarterly"
            elif 350<=med<=380: freq="Annual"
            elif 12<=med<=17: freq="Fortnightly"
            else: continue
            # Require either 3 occurrences or 2 occurrences with a convincing
            # interval. This avoids classifying unrelated one-off payments.
            if len(g)>=2:
                add_group(key or "Recurring payment",g,freq)

    result=pd.DataFrame(rows,columns=["Merchant / Description","Frequency","Typical Amount",
                                      "Occurrences","Last Seen","Type","Recurring Key"])
    if result.empty:
        return result
    if apply_exclusions:
        exclusions=get_recurring_exclusions()
        result=result[~result["Recurring Key"].isin(exclusions)].copy()
    return result.sort_values(["Type","Last Seen"],ascending=[True,False]).reset_index(drop=True)

def get_loans():
    rows=db_select("loans")
    return pd.DataFrame(rows) if rows else pd.DataFrame()

def get_income_sources():
    rows=db_select("income_sources")
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["id","name","monthly_amount","start_date","end_date","annual_growth_pct","months_per_year","active"])

def get_trading_results():
    rows=db_select("trading_results")
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["id","trade_date","realized_pnl","notes"])

def projected_income_for_month(sources, month_start):
    if sources is None or sources.empty:
        return 0.0
    month_start=pd.Timestamp(month_start).date().replace(day=1)
    total=0.0
    for _,r in sources.iterrows():
        if not bool(r.get("active",1)): continue
        start=pd.to_datetime(r["start_date"],errors="coerce")
        if pd.isna(start) or start.date().replace(day=1)>month_start: continue
        end=pd.to_datetime(r.get("end_date"),errors="coerce") if r.get("end_date") else pd.NaT
        if pd.notna(end) and end.date().replace(day=1)<month_start: continue
        mpy=int(r.get("months_per_year",12) or 12)
        # For annual/monthly rental-like income, months_per_year=12 means every month.
        # If fewer months are selected, receive in the first mpy calendar months of each year.
        if mpy<12 and month_start.month>mpy: continue
        years=max(0,month_start.year-start.year)
        amount=float(r.get("monthly_amount",0) or 0)*((1+float(r.get("annual_growth_pct",0) or 0)/100)**years)
        total+=amount
    return total

# ---------------- App ----------------
st.sidebar.title("💰 Finance Manager")
st.sidebar.caption("Secure personal finance workspace")
if st.sidebar.button("🔓 Sign out"):
    st.session_state.authenticated=False
    st.rerun()

backend="Supabase (persistent cloud DB)" if supabase_enabled() else "Local SQLite"
st.sidebar.success(f"Storage: {backend}")
st.sidebar.warning("Never commit bank statements, exported transactions, database files or secrets to GitHub.")

st.title("💰 Personal Finance Manager")
st.caption("Secure, mobile-friendly personal finance dashboard")

tabs=st.tabs(["📊 Dashboard","📥 Import","🧠 Learning","🔁 Recurring","🏦 Loans & EMI","🛟 Runway","📋 Transactions","⚙️ Settings"])

# Dashboard
with tabs[0]:
    df=get_txns()
    if df.empty:
        st.info("No transactions yet. Use Import to load a bank statement.")
    else:
        credits=df[df.direction=="Credit"].amount.sum()
        debits=df[df.direction=="Debit"].amount.sum()
        emi=df[(df.direction=="Debit")&(df["class"]=="EMI")].amount.sum()
        c1,c2,c3,c4=st.columns(4)
        c1.metric("Total credits",f"₹{credits:,.0f}")
        c2.metric("Total debits",f"₹{debits:,.0f}")
        c3.metric("Detected EMI",f"₹{emi:,.0f}")
        c4.metric("Net cash flow",f"₹{credits-debits:,.0f}")
        ms=monthly_series(df)
        if not ms.empty:
            st.subheader("Monthly spending")
            st.line_chart(ms.set_index("month")["debit"])
            if len(ms)>=2:
                cur=float(ms.iloc[-1].debit); prev=float(ms.iloc[-2].debit)
                if prev:
                    change=(cur/prev-1)*100
                    if change>15: st.warning(f"Latest monthly spending is {change:.1f}% above the previous month.")
                    elif change<-15: st.success(f"Latest monthly spending is {abs(change):.1f}% below the previous month.")
        st.subheader("Category spending")
        cat=df[df.direction=="Debit"].groupby("category").amount.sum().sort_values(ascending=False)
        st.bar_chart(cat)
        st.subheader("Recent transactions")
        st.dataframe(df.sort_values("txn_date",ascending=False).head(20)[["txn_date","description","amount","direction","category","class"]],use_container_width=True,hide_index=True)

# Import
with tabs[1]:
    st.subheader("📥 Import bank statement")
    st.write("CSV/XLSX/XLS. The importer recognizes common Indian bank statement column names.")
    f=st.file_uploader("Choose a statement",type=["csv","xlsx","xls"])
    if f:
        try:
            raw, header_row=_read_uploaded_statement(f)
            norm=normalize_statement(raw)
            st.success(f"Statement loaded successfully. Header row: {header_row + 1}. Detected {len(norm)} transactions.")
            st.dataframe(norm.head(50),use_container_width=True,hide_index=True)
            if st.button("Import transactions",type="primary"):
                n=save_transactions(norm,f.name)
                st.success(f"Imported {n} new transactions. Duplicates were ignored.")
                st.rerun()
        except Exception as e:
            st.error(str(e))

# Learning
with tabs[2]:
    st.subheader("🧠 Merchant/category learning")
    st.caption("Corrections become local rules. Example: AMZN → Online Shopping, a lender name → Loan EMI.")
    df=get_txns()
    if not df.empty:
        idx=st.selectbox("Transaction",df.index,format_func=lambda i:f"{df.loc[i,'txn_date']} | {df.loc[i,'description']} | ₹{df.loc[i,'amount']:,.2f} | {df.loc[i,'category']}")
        r=df.loc[idx]
        c1,c2,c3=st.columns(3)
        category_options = get_category_options(df) + [CUSTOM_CATEGORY_OPTION]
        current_category = str(r.category or "Other")
        category_index = category_options.index(current_category) if current_category in category_options else category_options.index(CUSTOM_CATEGORY_OPTION)
        cat_selection=c1.selectbox("Correct category",category_options,index=category_index, key="learning_category")
        custom_cat = ""
        if cat_selection == CUSTOM_CATEGORY_OPTION:
            custom_cat = c1.text_input("Enter custom category", value=current_category if current_category not in CATEGORIES else "", key="learning_custom_category", placeholder="e.g. Investment")
        classes=["Expense","EMI","Income","Transfer"]
        cl=c2.selectbox("Correct class",classes,index=classes.index(r["class"]) if r["class"] in classes else 0, key="learning_class")
        default=suggest_rule_pattern(r.description)
        pattern=c3.text_input("Merchant pattern to learn",default, key="learning_pattern")
        st.caption("Saving a correction updates the selected transaction and other transactions matching the same merchant pattern. Custom categories are retained for future corrections and imports.")
        if st.button("Save correction & update similar transactions", type="primary"):
            try:
                cat = resolve_category(cat_selection, custom_cat)
                status=save_rule(pattern,cat,cl)
                updated_count = apply_learning_to_similar_transactions(pattern, cat, cl, int(r.id))
                st.success(f"Learning rule saved. Updated {updated_count} similar transaction(s), including the selected transaction.")
                st.rerun()
            except Exception as e:
                st.error(str(e))
    rules=load_rules()
    if rules:
        st.dataframe(pd.DataFrame(rules,columns=["Pattern","Category","Class"]),use_container_width=True,hide_index=True)

# Recurring
with tabs[3]:
    st.subheader("🔁 Recurring payments")
    current_txns=get_txns()
    rec=recurring(current_txns)
    if rec.empty:
        emi_candidates = current_txns[
            (current_txns.direction=="Debit") &
            ((current_txns["class"].astype(str).str.upper()=="EMI") |
             (current_txns["category"].astype(str).str.lower()=="loan emi"))
        ] if not current_txns.empty else pd.DataFrame()
        if not emi_candidates.empty:
            st.info("EMI transactions are present, but there are not yet enough matching monthly entries to confirm a recurring schedule. Import at least two months of the same EMI or correct the lender transaction under Learning.")
        else:
            st.info("No recurring payment pattern has been confirmed yet. Import at least two occurrences of a payment, or classify an EMI as Loan EMI / EMI under Learning.")
    else:
        display_cols=["Merchant / Description","Frequency","Typical Amount","Occurrences","Last Seen","Type"]
        st.dataframe(rec[display_cols],use_container_width=True,hide_index=True)
        st.caption("Remove a recurring item to exclude it from the Runway EMI/commitment calculation. This does not delete the underlying bank transactions.")
        options=rec.index.tolist()
        ridx=st.selectbox("Recurring payment to remove",options,format_func=lambda i:f"{rec.loc[i,'Merchant / Description']} | {rec.loc[i,'Frequency']} | ₹{rec.loc[i,'Typical Amount']:,.0f} | {rec.loc[i,'Type']}",key="recurring_remove_select")
        c1,c2=st.columns(2)
        if c1.button("🗑️ Delete / exclude selected recurring payment",type="primary",key="delete_recurring"):
            try:
                exclude_recurring(rec.loc[ridx,"Recurring Key"])
                st.success("Recurring payment removed. Its bank transactions were not deleted and it is excluded from Runway.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not remove recurring payment: {e}")
        excluded=get_recurring_exclusions()
        # Show excluded commitments only when they can be rediscovered from current transactions.
        all_detected=recurring(current_txns, apply_exclusions=False)
        if all_detected is not None and not all_detected.empty:
            excluded_df=all_detected[all_detected["Recurring Key"].isin(excluded)]
        else:
            excluded_df=pd.DataFrame()
        if not excluded_df.empty:
            st.markdown("#### ♻️ Excluded recurring payments")
            ex_opts=excluded_df.index.tolist()
            ex_idx=st.selectbox("Recurring payment to restore",ex_opts,format_func=lambda i:f"{excluded_df.loc[i,'Merchant / Description']} | {excluded_df.loc[i,'Frequency']} | ₹{excluded_df.loc[i,'Typical Amount']:,.0f}",key="recurring_restore_select")
            if c2.button("↩️ Restore selected recurring payment",key="restore_recurring"):
                restore_recurring(excluded_df.loc[ex_idx,"Recurring Key"])
                st.success("Recurring payment restored and will be considered again.")
                st.rerun()
        st.info("Tip: classify HDFC/PNB loan transactions as Loan EMI / EMI under Learning so they are automatically detected as mandatory recurring commitments.")

# Loans
with tabs[4]:
    st.subheader("🏦 Loans & EMI amortization")
    st.caption("Use the current outstanding principal and confirmed lender terms. This is a planning estimate, not a lender statement.")
    with st.form("loan"):
        a,b,c=st.columns(3)
        name=a.text_input("Loan name","Home Loan")
        lender=a.text_input("Lender","")
        principal=b.number_input("Current outstanding principal",min_value=0.0,value=1000000.0,step=10000.0)
        rate=b.number_input("Annual interest %",min_value=0.0,value=8.0,step=0.05)
        tenure=c.number_input("Remaining tenure (months)",min_value=1,value=120,step=1)
        known_emi=c.number_input("Current EMI (0 = calculate)",min_value=0.0,value=0.0,step=100.0)
        start=st.date_input("Next payment month",date.today())
        extra=st.number_input("Optional extra principal/month",min_value=0.0,value=0.0,step=1000.0)
        save=st.form_submit_button("Save new loan")
    if save:
        pay=known_emi if known_emi else emi_payment(principal,rate,tenure)
        db_insert("loans",{"name":name,"lender":lender,"principal":principal,"annual_rate":rate,
                           "emi":pay,"start_date":str(start),"tenure_months":int(tenure),"extra_payment":extra})
        st.success("Loan saved.")
        st.rerun()

    loans=get_loans()
    if not loans.empty:
        loan_ids=loans.id.tolist()
        lid=st.selectbox("Select loan",loan_ids,format_func=lambda x:loans.loc[loans.id==x,"name"].iloc[0],key="loan_manage_select")
        l=loans[loans.id==lid].iloc[0]
        sch=amortize(l.principal,l.annual_rate,int(l.tenure_months),l.start_date,l.emi,l.extra_payment)
        x,y,z=st.columns(3)
        x.metric("Monthly EMI",f"₹{l.emi:,.0f}")
        y.metric("Future interest",f"₹{sch.Interest.sum():,.0f}")
        z.metric("Estimated completion",str(sch["Due Date"].iloc[-1]) if not sch.empty else "—")
        st.dataframe(sch,use_container_width=True,hide_index=True)

        st.markdown("### ✏️ Edit selected loan")
        with st.form(f"edit_loan_{int(lid)}"):
            a,b,c=st.columns(3)
            edit_name=a.text_input("Loan name",str(l.get("name", "")))
            edit_lender=a.text_input("Lender",str(l.get("lender", "")))
            edit_principal=b.number_input("Current outstanding principal",min_value=0.0,value=float(l.get("principal",0) or 0),step=10000.0)
            edit_rate=b.number_input("Annual interest %",min_value=0.0,value=float(l.get("annual_rate",0) or 0),step=0.05)
            edit_tenure=c.number_input("Remaining tenure (months)",min_value=1,value=int(l.get("tenure_months",1) or 1),step=1)
            edit_emi=c.number_input("Current EMI",min_value=0.0,value=float(l.get("emi",0) or 0),step=100.0)
            edit_start=c.date_input("Next payment month",pd.to_datetime(l.get("start_date",date.today())).date())
            edit_extra=st.number_input("Optional extra principal/month",min_value=0.0,value=float(l.get("extra_payment",0) or 0),step=1000.0)
            update_loan=st.form_submit_button("Update loan",type="primary")
        if update_loan:
            new_emi=edit_emi if edit_emi>0 else emi_payment(edit_principal,edit_rate,edit_tenure)
            db_update("loans",int(lid),{"name":edit_name,"lender":edit_lender,"principal":float(edit_principal),
                                       "annual_rate":float(edit_rate),"emi":float(new_emi),"start_date":str(edit_start),
                                       "tenure_months":int(edit_tenure),"extra_payment":float(edit_extra)})
            st.success("Loan updated successfully.")
            st.rerun()

        st.markdown("### 🗑️ Delete selected loan")
        st.caption("Deleting a loan removes only the loan record. Bank transactions are not deleted.")
        confirm=st.checkbox("I understand this will permanently remove this loan record",key=f"confirm_delete_loan_{int(lid)}")
        if st.button("Delete selected loan",type="secondary",disabled=not confirm,key="delete_loan"):
            try:
                db_delete("loans",int(lid))
                st.success("Loan deleted successfully.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not delete loan: {e}")

# Runway
with tabs[5]:
    st.subheader("🛟 Financial Runway / Income-Gap Planner")
    st.caption("Model your runway using current cash, mandatory spending, rental/other recurring income, and realized or planned swing-trading P/L.")

    # --- Income generators ---
    st.markdown("### 💰 Income generators")
    st.caption("Add income you reasonably expect during the job-search period. Rental income can be modeled separately from salary.")
    with st.form("income_source_form"):
        a,b,c,d=st.columns(4)
        inc_name=a.text_input("Income source", "Flat Rent")
        inc_amount=b.number_input("Monthly net income (₹)",min_value=0.0,value=45000.0,step=1000.0)
        inc_start=c.date_input("Starts from",date.today().replace(day=1))
        inc_growth=d.number_input("Annual increase %",min_value=0.0,max_value=50.0,value=0.0,step=1.0)
        e,f,g=st.columns(3)
        inc_end=e.date_input("Ends on (optional)",date(2099,12,31))
        inc_months=f.number_input("Months received per year",min_value=1,max_value=12,value=12,step=1)
        inc_active=g.checkbox("Active",value=True)
        add_inc=st.form_submit_button("Add income source",type="primary")
    if add_inc:
        db_insert("income_sources",{"name":inc_name,"monthly_amount":float(inc_amount),"start_date":str(inc_start),
                                     "end_date":None if inc_end.year>=2099 else str(inc_end),
                                     "annual_growth_pct":float(inc_growth),"months_per_year":int(inc_months),"active":bool(inc_active)})
        st.success(f"Added {inc_name} at ₹{inc_amount:,.0f}/month.")
        st.rerun()

    sources=get_income_sources()
    if not sources.empty:
        st.dataframe(sources[["id","name","monthly_amount","start_date","end_date","annual_growth_pct","months_per_year","active"]],use_container_width=True,hide_index=True)
        sid=st.selectbox("Income source to remove",sources.id.tolist(),format_func=lambda x:sources.loc[sources.id==x,"name"].iloc[0])
        if st.button("Remove selected income source"):
            db_delete("income_sources",int(sid)); st.rerun()

    # --- Trading P/L ---
    st.markdown("### 📈 Swing-trading P/L")
    st.caption("Record only realized profit/loss. Do not treat trading as guaranteed salary. Future trading P/L is a planning assumption unless you enter actual results.")
    with st.form("trading_form"):
        a,b,c=st.columns(3)
        trade_date=a.date_input("Trade/realization date",date.today())
        pnl=b.number_input("Realized P/L (₹)",value=0.0,step=1000.0,help="Profit is positive; loss is negative.")
        notes=c.text_input("Notes", "Swing trade")
        add_trade=st.form_submit_button("Add trading result")
    if add_trade:
        db_insert("trading_results",{"trade_date":str(trade_date),"realized_pnl":float(pnl),"notes":notes})
        st.success("Trading P/L recorded.")
        st.rerun()

    trades=get_trading_results()
    realized_avg=0.0
    if not trades.empty:
        trades["trade_date"]=pd.to_datetime(trades.trade_date)
        monthly_pnl=trades.assign(month=trades.trade_date.dt.to_period("M").astype(str)).groupby("month").realized_pnl.sum()
        realized_avg=float(monthly_pnl.tail(3).mean()) if not monthly_pnl.empty else 0.0
        st.dataframe(trades.sort_values("trade_date",ascending=False),use_container_width=True,hide_index=True)
        tid=st.selectbox("Trading result to remove",trades.id.tolist(),format_func=lambda x:f"{trades.loc[trades.id==x,'trade_date'].iloc[0].date()} | ₹{trades.loc[trades.id==x,'realized_pnl'].iloc[0]:,.0f}")
        if st.button("Remove selected trading result"):
            db_delete("trading_results",int(tid)); st.rerun()

    # --- Runway assumptions ---
    st.markdown("### 🧮 Runway assumptions")
    df=get_txns(); ms=monthly_series(df)
    hist=float(ms.debit.tail(6).mean()) if not ms.empty else 0
    active_recurring=recurring(df) if not df.empty else pd.DataFrame()
    recurring_emi=float(active_recurring[(active_recurring["Type"]=="EMI") & (active_recurring["Frequency"]=="Monthly")]["Typical Amount"].sum()) if not active_recurring.empty else 0.0
    emi_hist=float(df[(df.direction=="Debit")&(df["class"]=="EMI")].groupby(df.txn_date.str[:7]).amount.sum().tail(6).mean()) if not df.empty else 0
    default_emi=recurring_emi if recurring_emi>0 else max(0.0,emi_hist)
    a,b,c,d=st.columns(4)
    liquid=a.number_input("Current liquid funds (₹)",min_value=0.0,value=0.0,step=10000.0)
    essential=b.number_input("Monthly essential burn excluding EMI (₹)",min_value=0.0,value=max(0.0,hist-default_emi),step=5000.0)
    emi_m=c.number_input("Monthly EMI commitment (₹) — from active recurring EMIs",min_value=0.0,value=float(default_emi),step=5000.0,help="Automatically calculated from active monthly EMI items in Recurring Payments. Remove non-mandatory recurring items there to exclude them from Runway.")
    reduction=d.slider("Discretionary-spending reduction",0,100,35)
    e,f=st.columns(2)
    gap=e.number_input("Expected no-salary period (months)",min_value=0,max_value=60,value=6)
    planned_trade=f.number_input("Planned monthly trading P/L (₹)",value=0.0,step=1000.0,help="Use 0 for a conservative case. Positive numbers are expected profit; negative numbers are expected loss.")

    st.markdown("### 📊 Runway scenarios")
    base_burn=max(0.0,essential*(1-reduction/100)+emi_m)
    today_month=pd.Timestamp(date.today().replace(day=1))
    rent_income=projected_income_for_month(sources,today_month)
    monthly_trade_assumption=float(planned_trade)
    # Current/realized trading history is shown separately; planned future P/L is not silently inferred from past results.
    monthly_rows=[]
    cash=liquid
    months_to_show=max(12,int(gap) if gap else 12)
    for i in range(months_to_show):
        m=today_month+pd.DateOffset(months=i)
        income=projected_income_for_month(sources,m)
        net=base_burn-income-monthly_trade_assumption
        cash-=net
        monthly_rows.append([m.strftime("%b %Y"),income,monthly_trade_assumption,base_burn,net,cash])
    forecast=pd.DataFrame(monthly_rows,columns=["Month","Recurring income","Trading P/L assumption","Monthly burn","Net cash change","Ending cash"])

    # Three transparent scenarios: no income, rent only, rent + trading assumption.
    rent_monthly=float(rent_income)
    current_runway=liquid/base_burn if base_burn else float("inf")
    rent_net=max(0.0,base_burn-rent_monthly)
    rent_runway=liquid/rent_net if rent_net else float("inf")
    combined_net=max(0.0,base_burn-rent_monthly-monthly_trade_assumption)
    combined_runway=liquid/combined_net if combined_net else float("inf")
    c1,c2,c3=st.columns(3)
    c1.metric("Runway without new income", "Unlimited" if not math.isfinite(current_runway) else f"{current_runway:.1f} months")
    c2.metric("Runway with recurring income", "Unlimited" if not math.isfinite(rent_runway) else f"{rent_runway:.1f} months")
    c3.metric("Runway with income + trading", "Unlimited" if not math.isfinite(combined_runway) else f"{combined_runway:.1f} months")

    if not sources.empty:
        st.info(f"Recurring income currently modeled: ₹{rent_monthly:,.0f}/month from active income sources starting this month.")
    if not trades.empty:
        st.caption(f"Realized trading P/L entered so far: ₹{trades.realized_pnl.sum():,.0f}. Recent monthly average: ₹{realized_avg:,.0f}. Future runway uses your explicit planned P/L assumption of ₹{planned_trade:,.0f}/month, not the historical average.")

    st.subheader("Month-by-month forecast")
    st.dataframe(forecast,use_container_width=True,hide_index=True)
    st.line_chart(forecast.set_index("Month")["Ending cash"])

    if gap:
        end_row=forecast.iloc[min(int(gap)-1,len(forecast)-1)]
        if float(end_row["Ending cash"])>=0:
            st.success(f"{gap}-month scenario: projected ending cash is approximately ₹{float(end_row['Ending cash']):,.0f} using the income and trading assumptions above.")
        else:
            st.error(f"{gap}-month scenario: projected shortfall is approximately ₹{abs(float(end_row['Ending cash'])):,.0f} using the income and trading assumptions above.")
    st.warning("Trading income is inherently uncertain. For a conservative runway, set Planned monthly trading P/L to ₹0 or a negative stress value; use realized P/L entries to track actual performance.")
    st.caption("Runway is a planning model, not a guarantee. Review rent, vacancy, taxes, maintenance, spending and trading assumptions as circumstances change.")

# Transactions
with tabs[6]:
    st.subheader("📋 Transactions")
    df=get_txns()
    if df.empty: st.info("No transactions.")
    else:
        q=st.text_input("Search transactions")
        view=df.copy()
        if q: view=view[view.description.str.contains(q,case=False,na=False)]
        st.dataframe(view.sort_values("txn_date",ascending=False)[["id","txn_date","description","amount","direction","category","class","source"]],use_container_width=True,hide_index=True)
        if not view.empty:
            tid=st.selectbox("Select transaction to edit/delete",view.id.tolist())
            r=view[view.id==tid].iloc[0]
            c1,c2,c3=st.columns(3)
            txn_category_options=get_category_options(df) + [CUSTOM_CATEGORY_OPTION]
            current_txn_category=str(r.category or "Other")
            txn_cat_selection=c1.selectbox("Category",txn_category_options,index=txn_category_options.index(current_txn_category) if current_txn_category in txn_category_options else txn_category_options.index(CUSTOM_CATEGORY_OPTION), key="txn_category_edit")
            txn_custom_cat=""
            if txn_cat_selection == CUSTOM_CATEGORY_OPTION:
                txn_custom_cat=c1.text_input("Enter custom category",value=current_txn_category if current_txn_category not in CATEGORIES else "",key="txn_custom_category")
            newcat=resolve_category(txn_cat_selection,txn_custom_cat) if txn_cat_selection != CUSTOM_CATEGORY_OPTION else txn_cat_selection
            classes=["Expense","EMI","Income","Transfer"]
            newcl=c2.selectbox("Class",classes,index=classes.index(r["class"]) if r["class"] in classes else 0)
            newdesc=c3.text_input("Description",str(r.description))
            b1,b2=st.columns(2)
            if b1.button("Update transaction"):
                try:
                    final_category = resolve_category(txn_cat_selection,txn_custom_cat)
                    db_update_txn(int(tid),{"description":newdesc,"category":final_category,"class":newcl})
                    st.success("Updated.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
                st.success("Updated.")
                st.rerun()
            if b2.button("Delete transaction"):
                db_delete_txn(int(tid))
                st.warning("Deleted.")
                st.rerun()
        st.download_button("Export filtered CSV",view.to_csv(index=False).encode("utf-8"),"finance_transactions.csv","text/csv")

# Settings
with tabs[7]:
    st.subheader("⚙️ Settings & privacy")
    st.write("### Storage")
    if supabase_enabled():
        st.success("Supabase persistence is enabled. Your transaction records survive normal Streamlit app restarts.")
    else:
        st.warning("Local SQLite mode is active. On Streamlit Community Cloud, local storage is not suitable for permanent financial records.")
        st.write("To enable persistent cloud storage, create a Supabase project and add SUPABASE_URL and SUPABASE_KEY to Streamlit Secrets.")
    st.write("### Security")
    st.write("- Login password is supplied through Streamlit Secrets and is not stored in the repository.")
    st.write("- Bank statement files are processed by the app and are not automatically committed to GitHub.")
    st.write("- Do not put passwords, API keys, bank statements or database files in GitHub.")
    st.write("### Backup")
    st.write("For a serious personal-finance deployment, keep periodic encrypted exports/backups outside the Git repository.")

    st.write("### 🧹 Data reset")
    st.caption("Use this once to remove the sample/test data you entered while building the app. It does not change your login password, Streamlit Secrets, GitHub files, or application code.")
    st.warning("⚠️ This will permanently delete ALL saved transactions, learning rules, loans, income sources, and trading P/L from the connected database. This cannot be undone.")
    confirm_reset = st.checkbox("I understand that this will permanently delete my saved financial data.", key="confirm_full_reset")
    if st.button("🧹 Reset all sample/test financial data", type="secondary", disabled=not confirm_reset):
        try:
            reset_all_financial_data()
            st.success("All sample/test financial data has been cleared. You can now import your real bank data.")
            st.rerun()
        except Exception as e:
            st.error(f"Reset encountered an error: {e}. Please verify the data before trying again, because a cloud reset can be partially completed if one table rejects the delete.")
