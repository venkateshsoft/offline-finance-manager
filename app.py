
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
    return [(r["pattern"],r["category"],r["class"]) for r in rows]

def classify(desc, rules=None):
    """Classify a transaction without re-querying the database for every row."""
    s=str(desc).lower()
    if rules is None:
        rules=load_rules()
    for p,cat,cl in rules:
        if str(p).lower() in s: return cat,cl
    for p,cat,cl in BUILTIN:
        if p in s: return cat,cl
    if any(x in s for x in ["atm","cash withdrawal"]): return "Cash Withdrawal","Expense"
    if any(x in s for x in ["transfer","neft","imps","rtgs","upi transfer"]): return "Transfer","Transfer"
    return "Other","Expense"

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

    # Load user classification rules once. The previous implementation queried
    # the database for every transaction, which could create hundreds/thousands
    # of unnecessary database calls during a bank-statement import.
    rules=load_rules()
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
    """Persist imported transactions efficiently and safely.

    The old implementation performed a database duplicate check and then an
    insert for every row (and, for SQLite, read the entire transaction table
    before and after every insert). That pattern is very expensive on
    Streamlit Community Cloud and can exhaust available resources.

    This version calculates fingerprints locally and writes in batches.
    Supabase uses its bulk upsert/ignore-duplicates support, while SQLite uses
    INSERT OR IGNORE with executemany.
    """
    if df is None or df.empty:
        return 0

    required=["txn_date","description","amount","direction","category","class"]
    missing=[c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Import data is missing required columns: {', '.join(missing)}")

    rows=[]
    seen=set()
    import_view=df[required]
    for txn_date,description,amount,direction,category,txn_class in import_view.itertuples(index=False, name=None):
        txn_date=str(txn_date)
        description=str(description)
        amount=float(amount)
        direction=str(direction)
        fp=fingerprint(txn_date,description,amount,direction)
        # Also remove duplicates occurring inside the uploaded file itself.
        if fp in seen:
            continue
        seen.add(fp)
        rows.append({
            "txn_date":txn_date,
            "description":description,
            "amount":amount,
            "direction":direction,
            "category":str(category),
            "class":str(txn_class),
            "source":source,
            "fingerprint":fp
        })

    if not rows:
        return 0

    # Keep request sizes moderate for hosted environments.
    batch_size=500
    added=0

    try:
        if supabase_enabled():
            sb=get_supabase()
            # Supabase supports bulk insert/upsert with a list of dictionaries.
            # With a UNIQUE fingerprint, ignore_duplicates makes the import
            # idempotent without a SELECT for every transaction.
            for start in range(0,len(rows),batch_size):
                batch=rows[start:start+batch_size]
                response=(
                    sb.table("transactions")
                    .upsert(batch, on_conflict="fingerprint", ignore_duplicates=True)
                    .select("id")
                    .execute()
                )
                # With select("id"), the response tells us how many rows were
                # actually inserted/upserted in this batch.
                added += len(response.data or [])
        else:
            c=local_conn()
            sql="""INSERT OR IGNORE INTO transactions
                (txn_date,description,amount,direction,category,class,source,fingerprint)
                VALUES (?,?,?,?,?,?,?,?)"""
            for start in range(0,len(rows),batch_size):
                batch=rows[start:start+batch_size]
                values=[(x["txn_date"],x["description"],x["amount"],x["direction"],
                         x["category"],x["class"],x["source"],x["fingerprint"]) for x in batch]
                before=c.total_changes
                c.executemany(sql,values)
                c.commit()
                added += c.total_changes-before
            c.close()
    except OSError as e:
        raise ValueError(
            "The hosted app ran out of an operating-system resource while saving the transactions. "
            "The importer now uses batched database writes; please reboot the Streamlit app and retry. "
            f"Details: {e}"
        ) from e
    except Exception as e:
        raise ValueError(
            "The statement was read successfully, but saving the transactions failed. "
            "No per-row import loop is used now. Please check the database/Streamlit logs. "
            f"Details: {type(e).__name__}: {e}"
        ) from e

    return int(added)

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
    if df.empty:return pd.DataFrame()
    d=df[df.direction=="Debit"].copy()
    d["date"]=pd.to_datetime(d.txn_date)
    rows=[]
    # Normalize descriptions into a stable merchant-ish key.
    d["merchant_key"]=d.description.str.upper().str.replace(r"[^A-Z0-9 ]"," ",regex=True).str.replace(r"\s+"," ",regex=True).str.strip()
    for key,g in d.groupby("merchant_key"):
        g=g.sort_values("date")
        if len(g)<3: continue
        diffs=g.date.diff().dt.days.dropna()
        med=float(diffs.median()) if len(diffs) else 0
        if 25<=med<=35: freq="Monthly"
        elif 80<=med<=100: freq="Quarterly"
        elif 350<=med<=380: freq="Annual"
        elif 12<=med<=17: freq="Fortnightly"
        else: continue
        rows.append([key,freq,float(g.amount.median()),len(g),g.date.max().date()])
    return pd.DataFrame(rows,columns=["Merchant / Description","Frequency","Typical Amount","Occurrences","Last Seen"])

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
        cat=c1.selectbox("Correct category",CATEGORIES,index=CATEGORIES.index(r.category) if r.category in CATEGORIES else 0)
        classes=["Expense","EMI","Income","Transfer"]
        cl=c2.selectbox("Correct class",classes,index=classes.index(r["class"]) if r["class"] in classes else 0)
        default=re.split(r"\s+",str(r.description).strip())[0][:50]
        pattern=c3.text_input("Merchant pattern to learn",default)
        if st.button("Save correction"):
            db_insert("rules",{"pattern":pattern,"category":cat,"class":cl,"created_at":datetime.now().isoformat()})
            db_update_txn(int(r.id),{"category":cat,"class":cl})
            st.success("Saved. Future matching transactions will use this rule.")
            st.rerun()
    rules=load_rules()
    if rules:
        st.dataframe(pd.DataFrame(rules,columns=["Pattern","Category","Class"]),use_container_width=True,hide_index=True)

# Recurring
with tabs[3]:
    st.subheader("🔁 Recurring payments")
    rec=recurring(get_txns())
    if rec.empty:
        st.info("At least three similarly described payments with a recognizable interval are needed.")
    else:
        st.dataframe(rec,use_container_width=True,hide_index=True)
        st.caption("Examples: Netflix monthly, internet monthly, insurance annual, school fees periodic, EMI monthly. Confirm detections before using them as fixed commitments.")

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
        save=st.form_submit_button("Save loan")
    if save:
        pay=known_emi if known_emi else emi_payment(principal,rate,tenure)
        db_insert("loans",{"name":name,"lender":lender,"principal":principal,"annual_rate":rate,
                           "emi":pay,"start_date":str(start),"tenure_months":int(tenure),"extra_payment":extra})
        st.success("Loan saved.")
        st.rerun()
    loans=get_loans()
    if not loans.empty:
        lid=st.selectbox("Loan",loans.id.tolist(),format_func=lambda x:loans.loc[loans.id==x,"name"].iloc[0])
        l=loans[loans.id==lid].iloc[0]
        sch=amortize(l.principal,l.annual_rate,int(l.tenure_months),l.start_date,l.emi,l.extra_payment)
        x,y,z=st.columns(3)
        x.metric("Monthly EMI",f"₹{l.emi:,.0f}")
        y.metric("Future interest",f"₹{sch.Interest.sum():,.0f}")
        z.metric("Estimated completion",str(sch["Due Date"].iloc[-1]) if not sch.empty else "—")
        st.dataframe(sch,use_container_width=True,hide_index=True)

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
    emi_hist=float(df[(df.direction=="Debit")&(df["class"]=="EMI")].groupby(df.txn_date.str[:7]).amount.sum().tail(6).mean()) if not df.empty else 0
    a,b,c,d=st.columns(4)
    liquid=a.number_input("Current liquid funds (₹)",min_value=0.0,value=0.0,step=10000.0)
    essential=b.number_input("Monthly essential burn excluding EMI (₹)",min_value=0.0,value=max(0.0,hist-emi_hist),step=5000.0)
    emi_m=c.number_input("Monthly EMI commitment (₹)",min_value=0.0,value=max(0.0,emi_hist),step=5000.0)
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
