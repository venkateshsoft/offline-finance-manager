
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
        id INTEGER PRIMARY KEY AUTOINCREMENT, recurring_key TEXT UNIQUE, created_at TEXT)""")
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
            # The recurring_exclusions table is optional until its migration is applied.
            if table == "recurring_exclusions" and not recurring_exclusions_available():
                continue
            # All application IDs are positive serial/identity values.
            get_supabase().table(table).delete().neq("id", 0).execute()
        return

    c=local_conn()
    for table in RESETTABLE_TABLES:
        c.execute(f"DELETE FROM {table}")
    c.commit()

# ---------------- Recurring-payment exclusions ----------------
def recurring_exclusions_available():
    """Return True when the optional recurring_exclusions table is available.

    The feature is optional until the Supabase migration is applied. A missing
    table must never prevent the rest of the Finance Manager from loading.
    """
    if not supabase_enabled():
        return True
    try:
        get_supabase().table("recurring_exclusions").select("id").limit(1).execute()
        return True
    except Exception:
        return False

def get_recurring_exclusions():
    if not recurring_exclusions_available():
        return set()
    try:
        rows=db_select("recurring_exclusions")
        return {str(r.get("recurring_key")) for r in rows if r.get("recurring_key")}
    except Exception:
        return set()

def recurring_key(merchant_key, frequency, amount):
    return f"{str(merchant_key).strip().upper()}|{str(frequency).strip()}|{round(float(amount or 0))}"

def exclude_recurring(rec_key):
    if not recurring_exclusions_available():
        raise RuntimeError(
            "Recurring-payment exclusions are not enabled yet. Run the supplied "
            "supabase_recurring_exclusions_migration.sql once in Supabase SQL Editor, "
            "then reboot the Streamlit app."
        )
    payload={"recurring_key":str(rec_key),"created_at":datetime.now().isoformat()}
    if supabase_enabled():
        # Avoid a duplicate-key error if the user clicks delete twice.
        existing=(get_supabase().table("recurring_exclusions").select("id")
                  .eq("recurring_key",str(rec_key)).limit(1).execute().data)
        if not existing:
            get_supabase().table("recurring_exclusions").insert(payload).execute()
    else:
        c=local_conn()
        c.execute("INSERT OR IGNORE INTO recurring_exclusions (recurring_key,created_at) VALUES (?,?)",
                  (payload["recurring_key"],payload["created_at"]))
        c.commit()

def restore_recurring(rec_id):
    if not recurring_exclusions_available():
        raise RuntimeError("Recurring-payment exclusions table is not available. Run the Supabase migration first.")
    db_delete("recurring_exclusions",int(rec_id))

def is_emi_transaction(row):
    cls=str(row.get("class","")).upper()
    cat=str(row.get("category","")).lower()
    desc=str(row.get("description","")).lower()
    if cls=="EMI" or cat=="loan emi":
        return True
    emi_terms=("loan emi","home loan","housing finance","housingfin","pnbhousing",
               "hdfc home","mortgage","emi")
    return any(term in desc for term in emi_terms)

def recurring_emi_total(df):
    """Return the active monthly EMI commitment used by Runway.

    Only rows explicitly classified as EMI and detected as Monthly are included.
    A quarterly/annual recurring payment is not silently converted into an EMI.
    """
    rec=recurring(df)
    if rec.empty or "Type" not in rec.columns:
        return 0.0
    emi=rec[(rec["Type"]=="EMI") & (rec["Frequency"]=="Monthly")]
    if emi.empty:
        return 0.0
    return float(emi["Typical Amount"].sum())

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
              "Insurance","Utilities","Loan EMI","Cash Withdrawal","Transfer","Other"]

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

def apply_learning_rule(pattern, category, class_name, source_description=None):
    """Apply a learning correction to all similar historical transactions.

    Matching uses both the user-entered pattern and, when available, the stable
    merchant signature of the selected transaction. This is important for bank
    narrations such as ACH/NACH/loan references where the account or reference
    number changes every month. The saved rule also classifies future imports.
    """
    pattern=str(pattern or "").strip()
    target_sig=_merchant_signature(source_description) if source_description else ""
    if not pattern and not target_sig:
        return 0

    if supabase_enabled():
        # Supabase/PostgREST cannot safely express our Python merchant-signature
        # normalization in one SQL predicate, so fetch only the lightweight
        # transaction fields needed for matching and update matching IDs in
        # batches. This avoids one request per transaction.
        rows=(get_supabase().table("transactions")
              .select("id,description")
              .execute().data or [])
        ids=[]
        p=pattern.lower()
        for row in rows:
            desc=str(row.get("description", ""))
            sig=_merchant_signature(desc)
            if (target_sig and sig==target_sig) or (p and p in desc.lower()):
                ids.append(int(row["id"]))
        updated=0
        for start in range(0,len(ids),500):
            batch=ids[start:start+500]
            if not batch:
                continue
            response=(get_supabase().table("transactions")
                      .update({"category":category,"class":class_name})
                      .in_("id",batch).execute())
            updated += len(getattr(response,"data",[]) or [])
        return updated

    c=local_conn()
    rows=c.execute("SELECT id,description FROM transactions").fetchall()
    p=pattern.lower()
    ids=[]
    for row_id,desc in rows:
        desc=str(desc or "")
        sig=_merchant_signature(desc)
        if (target_sig and sig==target_sig) or (p and p in desc.lower()):
            ids.append(int(row_id))
    if ids:
        c.executemany("UPDATE transactions SET category=?, class=? WHERE id=?",
                      [(category,class_name,row_id) for row_id in ids])
        c.commit()
    return len(ids)

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

def recurring(df):
    """Detect recurring payments, with special handling for loan EMIs.

    EMI transactions are treated as recurring commitments even when only one
    historical occurrence exists. This makes a newly imported HDFC/PNB EMI
    visible immediately; the user can remove any non-mandatory item from the
    recurring list. When two or more occurrences exist, the interval is used to
    confirm Monthly/Quarterly/Annual/Fortnightly frequency.
    """
    if df.empty:
        return pd.DataFrame()
    d=df[df.direction=="Debit"].copy()
    if d.empty:
        return pd.DataFrame()
    d["date"]=pd.to_datetime(d.txn_date,errors="coerce")
    d=d.dropna(subset=["date"])
    d["merchant_key"]=d.description.map(_merchant_signature)
    d["is_emi"]=d.apply(is_emi_transaction,axis=1)
    exclusions=get_recurring_exclusions()
    rows=[]

    def detect_frequency(g, is_emi=False):
        g=g.sort_values("date")
        if len(g)<2:
            return "Monthly" if is_emi else None
        diffs=g.date.diff().dt.days.dropna()
        if diffs.empty:
            return "Monthly" if is_emi else None
        med=float(diffs.median())
        if 20<=med<=45: return "Monthly"
        if 75<=med<=110: return "Quarterly"
        if 330<=med<=400: return "Annual"
        if 12<=med<=17: return "Fortnightly"
        # EMI schedules can occasionally be affected by weekends/holidays.
        if is_emi and 45<med<75: return "Monthly"
        return None

    def add_group(key,g,frequency,is_emi):
        if g.empty or not frequency:
            return
        typical=float(g.amount.median())
        rec_key=recurring_key(key,frequency,typical)
        if rec_key in exclusions:
            return
        rows.append([key or ("Loan EMI" if is_emi else "Recurring payment"),
                     frequency,typical,len(g),g.date.max().date(),
                     "EMI" if is_emi else "Recurring payment",rec_key])

    # EMI groups: merchant signature + approximately fixed amount.
    # We use a rounded amount bucket to avoid floating-point differences while
    # still separating multiple loans from the same lender.
    emi=d[d.is_emi].copy()
    if not emi.empty:
        emi["amount_key"]=emi.amount.round(0)
        for (key,amount),g in emi.groupby(["merchant_key","amount_key"]):
            freq=detect_frequency(g,True)
            if freq:
                add_group(key,g,freq,True)

    # Other recurring payments need at least two historical occurrences.
    non_emi=d[~d.is_emi].copy()
    if not non_emi.empty:
        non_emi["amount_key"]=non_emi.amount.round(0)
        for (key,amount),g in non_emi.groupby(["merchant_key","amount_key"]):
            if len(g)<2:
                continue
            freq=detect_frequency(g,False)
            if freq:
                add_group(key,g,freq,False)

    result=pd.DataFrame(rows,columns=["Merchant / Description","Frequency","Typical Amount",
                                      "Occurrences","Last Seen","Type","_recurring_key"])
    if result.empty:
        return result
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
    st.caption("Correct one transaction and the same merchant pattern will be applied to matching historical transactions and future imports.")
    df=get_txns()
    if not df.empty:
        idx=st.selectbox("Transaction",df.index,format_func=lambda i:f"{df.loc[i,'txn_date']} | {df.loc[i,'description']} | ₹{df.loc[i,'amount']:,.2f} | {df.loc[i,'category']}")
        r=df.loc[idx]
        c1,c2,c3=st.columns(3)
        cat=c1.selectbox("Correct category",CATEGORIES,index=CATEGORIES.index(r.category) if r.category in CATEGORIES else 0)
        classes=["Expense","EMI","Income","Transfer"]
        cl=c2.selectbox("Correct class",classes,index=classes.index(r["class"]) if r["class"] in classes else 0)
        default=suggest_rule_pattern(r.description)
        pattern=c3.text_input("Merchant pattern to learn",default)
        st.caption("Use a distinctive merchant/lender term. Example: PNBHOUSINGFIN or a specific merchant name. Avoid generic terms such as ACH or UPI.")
        if st.button("Save correction & update similar transactions",type="primary"):
            try:
                action=save_rule(pattern,cat,cl)
                updated=apply_learning_rule(pattern,cat,cl,source_description=r.description)
                st.success(f"Rule {action}. Updated {updated} matching existing transaction(s). Future matching imports will use this rule automatically.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not save the learning rule: {e}")
    rules=load_rules()
    if rules:
        st.dataframe(pd.DataFrame(rules,columns=["Pattern","Category","Class"]),use_container_width=True,hide_index=True)

# Recurring
with tabs[3]:
    st.subheader("🔁 Recurring payments")
    st.caption("Recurring payments are detected from transaction history. EMI rows are used as monthly mandatory commitments in the Runway planner unless you delete them here.")
    rec=recurring(get_txns())
    if rec.empty:
        st.info("No recurring payments are currently detected. EMI transactions are surfaced even when only one historical occurrence is available; other recurring payments need at least two occurrences.")
    else:
        monthly_emi=float(rec[(rec["Type"]=="EMI") & (rec["Frequency"]=="Monthly")]["Typical Amount"].sum())
        m1,m2,m3=st.columns(3)
        m1.metric("Active monthly EMI commitments",f"₹{monthly_emi:,.0f}")
        m2.metric("Detected EMI rows",str(int((rec["Type"]=="EMI").sum())))
        m3.metric("Recurring rows",str(len(rec)))
        st.caption("Runway automatically uses the Active monthly EMI commitments amount above. Remove any non-mandatory EMI/recurring row below to exclude it from Runway; the underlying bank transactions remain untouched.")
        display_cols=["Merchant / Description","Frequency","Typical Amount","Occurrences","Last Seen","Type"]
        st.dataframe(rec[display_cols],use_container_width=True,hide_index=True)
        st.markdown("### Manage recurring commitments")
        st.caption("Removing a recurring row only excludes that detected pattern from this list and the Runway calculation. It does not delete the original transaction.")
        rec_options=list(rec.index)
        ridx=st.selectbox("Recurring payment to remove",rec_options,
                          format_func=lambda i:f"{rec.loc[i,'Merchant / Description']} | {rec.loc[i,'Frequency']} | ₹{rec.loc[i,'Typical Amount']:,.0f} | {rec.loc[i,'Type']}")
        if st.button("🗑️ Remove selected recurring payment",type="secondary"):
            exclude_recurring(rec.loc[ridx,"_recurring_key"])
            st.success("Recurring payment removed. Its transactions were not deleted, and it will no longer be included in Runway EMI commitments.")
            st.rerun()

    # Show excluded patterns so the user can restore a mistakenly removed EMI.
    excluded_df=pd.DataFrame()
    if recurring_exclusions_available():
        try:
            excluded_rows=db_select("recurring_exclusions")
            excluded_df=pd.DataFrame(excluded_rows) if excluded_rows else pd.DataFrame()
        except Exception:
            excluded_df=pd.DataFrame()
    elif supabase_enabled():
        st.info("Recurring-payment removal is ready, but the Supabase migration has not been applied yet. Run the supplied `supabase_recurring_exclusions_migration.sql` once in Supabase SQL Editor.")
    if not excluded_df.empty:
        with st.expander("↩️ Restore removed recurring payments"):
            exid=st.selectbox("Removed recurring pattern",excluded_df.id.tolist(),
                              format_func=lambda x:excluded_df.loc[excluded_df.id==x,"recurring_key"].iloc[0])
            if st.button("Restore selected recurring payment"):
                restore_recurring(int(exid)); st.rerun()

# Loans
with tabs[4]:
    st.subheader("🏦 Loans & EMI amortization")
    st.caption("Use the current outstanding principal and confirmed lender terms. This is a planning estimate, not a lender statement.")
    loans=get_loans()

    if not loans.empty:
        lid=st.selectbox("Select existing loan",loans.id.tolist(),format_func=lambda x:loans.loc[loans.id==x,"name"].iloc[0],key="loan_select")
        l=loans[loans.id==lid].iloc[0]
        st.markdown("### Edit selected loan")
        with st.form(f"edit_loan_{int(lid)}"):
            a,b,c=st.columns(3)
            name=a.text_input("Loan name",str(l.name))
            lender=a.text_input("Lender",str(l.lender or ""))
            principal=b.number_input("Current outstanding principal",min_value=0.0,value=float(l.principal),step=10000.0)
            rate=b.number_input("Annual interest %",min_value=0.0,value=float(l.annual_rate),step=0.05)
            tenure=c.number_input("Remaining tenure (months)",min_value=1,value=int(l.tenure_months),step=1)
            known_emi=c.number_input("Current EMI (0 = calculate)",min_value=0.0,value=float(l.emi),step=100.0)
            start=st.date_input("Next payment month",pd.to_datetime(l.start_date).date())
            extra=st.number_input("Optional extra principal/month",min_value=0.0,value=float(l.extra_payment or 0),step=1000.0)
            save_edit=st.form_submit_button("💾 Update loan",type="primary")
        if save_edit:
            pay=known_emi if known_emi else emi_payment(principal,rate,tenure)
            db_update("loans",int(lid),{"name":name,"lender":lender,"principal":principal,"annual_rate":rate,
                                      "emi":pay,"start_date":str(start),"tenure_months":int(tenure),"extra_payment":extra})
            st.success("Loan updated successfully.")
            st.rerun()
        if st.button("🗑️ Delete selected loan",key=f"delete_loan_{int(lid)}"):
            db_delete("loans",int(lid))
            st.success("Loan deleted. Related transactions were not deleted.")
            st.rerun()

        loans=get_loans()
        l=loans[loans.id==lid].iloc[0] if not loans.empty and int(lid) in loans.id.tolist() else None
        if l is not None:
            sch=amortize(l.principal,l.annual_rate,int(l.tenure_months),l.start_date,l.emi,l.extra_payment)
            x,y,z=st.columns(3)
            x.metric("Monthly EMI",f"₹{l.emi:,.0f}")
            y.metric("Future interest",f"₹{sch.Interest.sum():,.0f}")
            z.metric("Estimated completion",str(sch["Due Date"].iloc[-1]) if not sch.empty else "—")
            st.dataframe(sch,use_container_width=True,hide_index=True)

    st.markdown("### Add new loan")
    with st.form("loan"):
        a,b,c=st.columns(3)
        name=a.text_input("Loan name","Home Loan")
        lender=a.text_input("Lender","")
        principal=b.number_input("Current outstanding principal",min_value=0.0,value=1000000.0,step=10000.0)
        rate=b.number_input("Annual interest %",min_value=0.0,value=8.0,step=0.05)
        tenure=c.number_input("Remaining tenure (months)",min_value=1,value=120,step=1)
        known_emi=c.number_input("Current EMI (0 = calculate)",min_value=0.0,value=0.0,step=100.0)
        start=st.date_input("Next payment month",date.today(),key="new_loan_start")
        extra=st.number_input("Optional extra principal/month",min_value=0.0,value=0.0,step=1000.0,key="new_loan_extra")
        save=st.form_submit_button("Add loan",type="primary")
    if save:
        pay=known_emi if known_emi else emi_payment(principal,rate,tenure)
        db_insert("loans",{"name":name,"lender":lender,"principal":principal,"annual_rate":rate,
                           "emi":pay,"start_date":str(start),"tenure_months":int(tenure),"extra_payment":extra})
        st.success("Loan saved.")
        st.rerun()

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
    recurring_emi=recurring_emi_total(df)
    a,b,c,d=st.columns(4)
    liquid=a.number_input("Current liquid funds (₹)",min_value=0.0,value=0.0,step=10000.0)
    essential=b.number_input("Monthly essential burn excluding EMI (₹)",min_value=0.0,value=max(0.0,hist-recurring_emi),step=5000.0)
    emi_m=c.number_input("Monthly EMI commitments (₹)",min_value=0.0,value=max(0.0,recurring_emi),step=5000.0,disabled=True,help="Automatically calculated from active EMI rows in Recurring Payments. Remove a non-mandatory recurring row there to exclude it.")
    reduction=d.slider("Discretionary-spending reduction",0,100,35)
    st.caption(f"🔒 Runway is using ₹{recurring_emi:,.0f}/month from active recurring EMI commitments. Delete any non-mandatory recurring payment in the Recurring tab to remove it from this calculation.")
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
            newcat=c1.selectbox("Category",CATEGORIES,index=CATEGORIES.index(r.category) if r.category in CATEGORIES else 0)
            classes=["Expense","EMI","Income","Transfer"]
            newcl=c2.selectbox("Class",classes,index=classes.index(r["class"]) if r["class"] in classes else 0)
            newdesc=c3.text_input("Description",str(r.description))
            b1,b2=st.columns(2)
            if b1.button("Update transaction"):
                db_update_txn(int(tid),{"description":newdesc,"category":newcat,"class":newcl})
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
