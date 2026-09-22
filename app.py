
import streamlit as st
import pandas as pd
import numpy as np
import sqlite3, hashlib, hmac, math, re
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

def classify(desc):
    s=str(desc).lower()
    for p,cat,cl in load_rules():
        if p.lower() in s: return cat,cl
    for p,cat,cl in BUILTIN:
        if p in s: return cat,cl
    if any(x in s for x in ["atm","cash withdrawal"]): return "Cash Withdrawal","Expense"
    if any(x in s for x in ["transfer","neft","imps","rtgs","upi transfer"]): return "Transfer","Transfer"
    return "Other","Expense"

def fingerprint(d, desc, amt, direction):
    raw=f"{d}|{str(desc).strip().lower()}|{round(float(amt),2)}|{direction}"
    return hashlib.sha256(raw.encode()).hexdigest()

# ---------------- Import ----------------
def normalize_statement(df):
    cols={str(c).strip().lower():c for c in df.columns}
    date_col=next((cols[k] for k in ["date","transaction date","txn date","value date"] if k in cols),None)
    desc_col=next((cols[k] for k in ["description","narration","transaction details","remarks","details"] if k in cols),None)
    credit_col=next((cols[k] for k in ["credit","credits","deposit","cr"] if k in cols),None)
    debit_col=next((cols[k] for k in ["debit","debits","withdrawal","dr"] if k in cols),None)
    amount_col=next((cols[k] for k in ["amount","transaction amount"] if k in cols),None)
    if not date_col or not desc_col:
        raise ValueError("Could not identify Date and Description/Narration columns.")
    out=[]
    for _,r in df.iterrows():
        d=pd.to_datetime(r[date_col],errors="coerce")
        if pd.isna(d): continue
        desc=str(r[desc_col])
        credit=pd.to_numeric(r[credit_col],errors="coerce") if credit_col else np.nan
        debit=pd.to_numeric(r[debit_col],errors="coerce") if debit_col else np.nan
        if pd.notna(credit) and float(credit)!=0:
            amt=abs(float(credit)); direction="Credit"
        elif pd.notna(debit) and float(debit)!=0:
            amt=abs(float(debit)); direction="Debit"
        elif amount_col:
            raw=str(r[amount_col]).replace(",","").strip()
            if not raw or raw.lower()=="nan": continue
            val=float(raw)
            amt=abs(val); direction="Credit" if val>=0 else "Debit"
        else: continue
        cat,cl=classify(desc)
        out.append([d.date().isoformat(),desc,amt,direction,cat,cl])
    return pd.DataFrame(out,columns=["txn_date","description","amount","direction","category","class"])

def save_transactions(df, source="import"):
    added=0
    for _,r in df.iterrows():
        fp=fingerprint(r.txn_date,r.description,r.amount,r.direction)
        data={"txn_date":r.txn_date,"description":r.description,"amount":float(r.amount),
              "direction":r.direction,"category":r.category,"class":r["class"],
              "source":source,"fingerprint":fp}
        if supabase_enabled():
            exists=get_supabase().table("transactions").select("id").eq("fingerprint",fp).execute().data
            if not exists:
                db_insert("transactions",data); added+=1
        else:
            before=len(get_txns())
            db_insert("transactions",data)
            after=len(get_txns())
            if after>before: added+=1
    return added

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
            raw=pd.read_csv(f) if f.name.lower().endswith(".csv") else pd.read_excel(f)
            norm=normalize_statement(raw)
            st.write(f"Detected {len(norm)} transactions.")
            st.dataframe(norm.head(50),use_container_width=True,hide_index=True)
            if st.button("Import transactions",type="primary"):
                n=save_transactions(norm,f.name)
                st.success(f"Imported {n} new transactions. Duplicates were ignored.")
                st.rerun()
        except Exception as e: st.error(str(e))

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
    df=get_txns(); ms=monthly_series(df)
    hist=float(ms.debit.tail(6).mean()) if not ms.empty else 0
    emi_hist=float(df[(df.direction=="Debit")&(df["class"]=="EMI")].groupby(df.txn_date.str[:7]).amount.sum().tail(6).mean()) if not df.empty else 0
    a,b,c=st.columns(3)
    liquid=a.number_input("Current liquid funds",min_value=0.0,value=0.0,step=10000.0)
    essential=b.number_input("Monthly essential burn (excluding EMI)",min_value=0.0,value=max(0.0,hist-emi_hist),step=5000.0)
    emi_m=c.number_input("Monthly EMI commitment",min_value=0.0,value=max(0.0,emi_hist),step=5000.0)
    reduction=st.slider("Discretionary-spending reduction",0,100,30)
    gap=st.number_input("Expected no-income period (months)",min_value=0,max_value=60,value=6)
    burn=essential*(1-reduction/100)+emi_m
    runway=liquid/burn if burn else float("inf")
    st.metric("Estimated runway","Unlimited" if not math.isfinite(runway) else f"{runway:.1f} months")
    rows=[]
    for m in [3,6,9,12]:
        need=burn*m
        rows.append([m,need,liquid-need,"Covered" if liquid>=need else "Shortfall"])
    st.dataframe(pd.DataFrame(rows,columns=["Months","Cash needed","Funds remaining","Status"]),hide_index=True)
    if gap:
        need=burn*gap
        if liquid>=need: st.success(f"{gap}-month scenario: approximately ₹{liquid-need:,.0f} remains.")
        else: st.error(f"{gap}-month scenario: estimated shortfall ₹{need-liquid:,.0f}.")
    st.caption("Runway is an estimate from your inputs/history and should be reviewed as income, spending and loan terms change.")

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
