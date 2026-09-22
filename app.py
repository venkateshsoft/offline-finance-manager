
import streamlit as st
import pandas as pd
import numpy as np
import sqlite3, re, hashlib, math
from pathlib import Path
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

APP_DIR = Path.home() / ".offline_finance_manager"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "finance.db"

st.set_page_config(page_title="Offline Finance Manager", page_icon="💰", layout="wide")

# ---------- Database ----------
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, txn_date TEXT, description TEXT,
            amount REAL, direction TEXT, category TEXT, class TEXT,
            source TEXT, fingerprint TEXT UNIQUE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS rules(
            id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT UNIQUE,
            category TEXT, class TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS emi_loans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, lender TEXT,
            principal REAL, annual_rate REAL, emi REAL, start_date TEXT,
            payments_made INTEGER DEFAULT 0, tenure_months INTEGER,
            extra_payment REAL DEFAULT 0)""")
        c.commit()
init_db()

BUILTIN = [
("swiggy","Food","Expense"),("zomato","Food","Expense"),("dominos","Food","Expense"),
("restaurant","Food","Expense"),("hotel","Travel","Expense"),("uber","Travel","Expense"),
("ola","Travel","Expense"),("makemytrip","Travel","Expense"),("flight","Travel","Expense"),
("netflix","Entertainment","Expense"),("spotify","Entertainment","Expense"),
("prime video","Entertainment","Expense"),("movie","Entertainment","Expense"),
("petrol","Fuel","Expense"),("fuel","Fuel","Expense"),("hpcl","Fuel","Expense"),
("bharat petroleum","Fuel","Expense"),("amazon","Online Shopping","Expense"),
("flipkart","Online Shopping","Expense"),("myntra","Online Shopping","Expense"),
("school","Education","Expense"),("college","Education","Expense"),("tuition","Education","Expense"),
("hospital","Medical","Expense"),("pharmacy","Medical","Expense"),("medical","Medical","Expense"),
("insurance","Insurance","Expense"),("electricity","Utilities","Expense"),("bescom","Utilities","Expense"),
("internet","Utilities","Expense"),("broadband","Utilities","Expense"),
("hdfc home loan","Loan EMI","EMI"),("home loan","Loan EMI","EMI"),
("loan emi","Loan EMI","EMI"),("emi","Loan EMI","EMI"),("mortgage","Loan EMI","EMI"),
("salary","Salary","Income"),("payroll","Salary","Income")
]
CATEGORIES = ["Food","Travel","Entertainment","Fuel","Online Shopping","Education","Medical",
              "Insurance","Utilities","Loan EMI","Cash Withdrawal","Transfer","Other"]

def load_rules():
    with db() as c:
        return c.execute("SELECT pattern,category,class FROM rules ORDER BY id DESC").fetchall()

def classify(desc):
    s = str(desc).lower()
    rules = load_rules()
    for p,cat,cl in rules:
        if p.lower() in s: return cat,cl
    for p,cat,cl in BUILTIN:
        if p in s: return cat,cl
    if any(x in s for x in ["atm","cash withdrawal"]): return "Cash Withdrawal","Expense"
    if any(x in s for x in ["transfer","neft","imps","rtgs","upi transfer"]): return "Transfer","Transfer"
    return "Other","Expense"

def fingerprint(d, desc, amt, direction):
    raw=f"{d}|{desc.strip().lower()}|{round(float(amt),2)}|{direction}"
    return hashlib.sha256(raw.encode()).hexdigest()

def normalize_statement(df):
    cols={str(c).strip().lower():c for c in df.columns}
    date_col=next((cols[k] for k in ["date","transaction date","txn date","value date"] if k in cols), None)
    desc_col=next((cols[k] for k in ["description","narration","transaction details","remarks","details"] if k in cols), None)
    credit_col=next((cols[k] for k in ["credit","credits","deposit","cr"] if k in cols), None)
    debit_col=next((cols[k] for k in ["debit","debits","withdrawal","dr"] if k in cols), None)
    amount_col=next((cols[k] for k in ["amount","transaction amount"] if k in cols), None)
    if not date_col or not desc_col: raise ValueError("Could not identify Date and Description/Narration columns.")
    out=[]
    for _,r in df.iterrows():
        d=pd.to_datetime(r[date_col], errors="coerce")
        if pd.isna(d): continue
        desc=str(r[desc_col])
        credit = pd.to_numeric(r[credit_col],errors="coerce") if credit_col else np.nan
        debit = pd.to_numeric(r[debit_col],errors="coerce") if debit_col else np.nan
        if pd.notna(credit) and float(credit)!=0:
            amt=float(credit); direction="Credit"
        elif pd.notna(debit) and float(debit)!=0:
            amt=float(debit); direction="Debit"
        elif amount_col:
            raw=r[amount_col]
            amt=abs(float(str(raw).replace(",",""))) if pd.notna(raw) and str(raw)!="" else 0
            direction="Credit" if float(str(raw).replace(",",""))>=0 else "Debit"
        else: continue
        cat,cl=classify(desc)
        out.append([d.date().isoformat(),desc,amt,direction,cat,cl])
    return pd.DataFrame(out,columns=["txn_date","description","amount","direction","category","class"])

def save_transactions(df, source="import"):
    added=0
    with db() as c:
        for _,r in df.iterrows():
            fp=fingerprint(r.txn_date,r.description,r.amount,r.direction)
            try:
                c.execute("""INSERT INTO transactions(txn_date,description,amount,direction,category,class,source,fingerprint)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (r.txn_date,r.description,r.amount,r.direction,r.category,r["class"],source,fp))
                added+=1
            except sqlite3.IntegrityError: pass
        c.commit()
    return added

def get_txns():
    with db() as c:
        return pd.read_sql_query("SELECT * FROM transactions ORDER BY txn_date DESC,id DESC",c)

def monthly_series():
    df=get_txns()
    if df.empty:return pd.DataFrame()
    df["month"]=pd.to_datetime(df.txn_date).dt.to_period("M").astype(str)
    x=df[df.direction=="Debit"].groupby("month").amount.sum().reset_index(name="debit")
    return x

# ---------- EMI ----------
def monthly_rate(annual): return annual/100/12
def emi_payment(principal, annual, months):
    r=monthly_rate(annual)
    if months<=0:return 0
    return principal/months if r==0 else principal*r*(1+r)**months/((1+r)**months-1)

def amortize(principal, annual, months, start, emi=None, extra=0, payments_made=0):
    r=monthly_rate(annual)
    payment=emi if emi and emi>0 else emi_payment(principal,annual,months)
    bal=float(principal)
    rows=[]
    dt=pd.to_datetime(start).date()
    # payments_made means the supplied principal is the current outstanding if entered as outstanding.
    for n in range(1, max(1,months)+1):
        if bal<=0.01: break
        interest=bal*r
        p=max(0,min(bal, payment-interest))
        extra_p=min(max(0,extra), max(0,bal-p))
        total_p=p+extra_p
        pay=interest+total_p
        bal=max(0,bal-total_p)
        due=dt+relativedelta(months=n)
        rows.append([n,due,pay,interest,total_p,bal])
    return pd.DataFrame(rows,columns=["Installment","Due Date","EMI Payment","Interest","Principal","Balance"])

def recurring(df):
    if df.empty:return pd.DataFrame()
    d=df[df.direction=="Debit"].copy()
    d["date"]=pd.to_datetime(d.txn_date)
    rows=[]
    for desc,g in d.groupby(d.description.str.upper().str.replace(r"\s+"," ",regex=True)):
        g=g.sort_values("date")
        if len(g)<3: continue
        diffs=g.date.diff().dt.days.dropna()
        med=float(diffs.median()) if len(diffs) else 0
        if 25<=med<=35: freq="Monthly"
        elif 80<=med<=100: freq="Quarterly"
        elif 350<=med<=380: freq="Annual"
        elif 12<=med<=17: freq="Fortnightly"
        else: continue
        amt=float(g.amount.median())
        rows.append([desc,freq,amt,len(g),g.date.max().date()])
    return pd.DataFrame(rows,columns=["Description","Frequency","Typical Amount","Occurrences","Last Seen"])

# ---------- UI ----------
st.title("💰 Offline Finance Manager v2")
st.caption("Local-first personal finance: your database stays on this device unless you deliberately expose the app.")

tabs=st.tabs(["📊 Dashboard","📥 Import","🧠 Learning","🔁 Recurring","🏦 EMI Planner","🛟 Financial Runway","📋 Transactions"])

with tabs[0]:
    df=get_txns()
    if df.empty:
        st.info("Import a bank statement to start.")
    else:
        credits=df[df.direction=="Credit"].amount.sum()
        debits=df[df.direction=="Debit"].amount.sum()
        emi=df[(df.direction=="Debit")&(df["class"]=="EMI")].amount.sum()
        c1,c2,c3,c4=st.columns(4)
        c1.metric("Credits",f"₹{credits:,.0f}")
        c2.metric("Debits",f"₹{debits:,.0f}")
        c3.metric("EMIs detected",f"₹{emi:,.0f}")
        c4.metric("Net cash flow",f"₹{credits-debits:,.0f}")
        ms=monthly_series()
        if len(ms)>=2:
            ms["change_pct"]=ms.debit.pct_change()*100
            st.subheader("Monthly spending trend")
            st.line_chart(ms.set_index("month")["debit"])
            latest=ms.iloc[-1]
            prev=ms.iloc[-2]
            if prev.debit and latest.debit>prev.debit*1.15:
                st.warning(f"Spending is about {(latest.debit/prev.debit-1)*100:.1f}% higher than the previous month.")
            elif prev.debit and latest.debit<prev.debit*0.85:
                st.success(f"Spending is about {(1-latest.debit/prev.debit)*100:.1f}% lower than the previous month.")
        cat=df[df.direction=="Debit"].groupby("category").amount.sum().sort_values(ascending=False)
        st.subheader("Spend by category")
        st.bar_chart(cat)
        st.subheader("3/6/9/12-month spending projection")
        avg=ms.debit.tail(6).mean() if not ms.empty else 0
        st.dataframe(pd.DataFrame({"Horizon":["3 months","6 months","9 months","12 months"],
                                   "Projected debit":[avg*3,avg*6,avg*9,avg*12]}),hide_index=True)

with tabs[1]:
    st.subheader("Import bank statement")
    st.write("Supported: CSV, XLSX, XLS. The importer looks for common Date, Description/Narration, Credit, Debit or Amount columns.")
    f=st.file_uploader("Upload statement",type=["csv","xlsx","xls"])
    if f:
        try:
            raw=pd.read_csv(f) if f.name.lower().endswith(".csv") else pd.read_excel(f)
            st.write("Detected columns:",list(raw.columns))
            norm=normalize_statement(raw)
            st.dataframe(norm.head(50),use_container_width=True,hide_index=True)
            if st.button("Import into local database"):
                n=save_transactions(norm,f.name)
                st.success(f"Imported {n} new transactions. Duplicate rows are ignored.")
                st.rerun()
        except Exception as e: st.error(str(e))

with tabs[2]:
    st.subheader("🧠 Local learning")
    st.write("Correct a transaction once; the rule is saved locally and takes priority over built-in rules.")
    df=get_txns()
    if not df.empty:
        row=st.selectbox("Transaction",df.index,format_func=lambda i:f"{df.loc[i,'txn_date']} | {df.loc[i,'description']} | ₹{df.loc[i,'amount']:,.2f} | {df.loc[i,'category']}")
        r=df.loc[row]
        col1,col2,col3=st.columns(3)
        cat=col1.selectbox("Category",CATEGORIES,index=CATEGORIES.index(r.category) if r.category in CATEGORIES else 0)
        cl=col2.selectbox("Class",["Expense","EMI","Income","Transfer"],index=["Expense","EMI","Income","Transfer"].index(r["class"]) if r["class"] in ["Expense","EMI","Income","Transfer"] else 0)
        pattern=col3.text_input("Learn from text",value=str(r.description).split()[0][:40])
        if st.button("Save correction/rule"):
            with db() as c:
                c.execute("INSERT OR REPLACE INTO rules(pattern,category,class,created_at) VALUES(?,?,?,?)",
                          (pattern,cat,cl,datetime.now().isoformat()))
                c.execute("UPDATE transactions SET category=?,class=? WHERE id=?",(cat,cl,int(r.id)))
                c.commit()
            st.success("Local rule saved. Future matching transactions will use it.")
            st.rerun()
    rules=load_rules()
    if rules: st.dataframe(pd.DataFrame(rules,columns=["Pattern","Category","Class"]),hide_index=True)

with tabs[3]:
    st.subheader("🔁 Recurring expense detector")
    rec=recurring(get_txns())
    if rec.empty: st.info("Need at least three similarly described debit transactions with a recognizable interval.")
    else:
        st.dataframe(rec,use_container_width=True,hide_index=True)
        st.caption("Recurring detection is statistical from your transaction history; confirm items before treating them as fixed commitments.")

with tabs[4]:
    st.subheader("🏦 EMI amortization planner")
    st.write("Enter the current outstanding principal and lender terms. The schedule is a planning estimate; actual lender schedules can differ due to rate resets, fees, daily interest and prepayments.")
    with st.form("emi_form"):
        a,b,c=st.columns(3)
        name=a.text_input("Loan name","Home Loan")
        lender=a.text_input("Lender","")
        principal=b.number_input("Current principal outstanding",min_value=0.0,value=1000000.0,step=10000.0)
        annual=b.number_input("Annual interest %",min_value=0.0,value=8.0,step=0.05)
        months=c.number_input("Remaining tenure (months)",min_value=1,value=120,step=1)
        known_emi=c.number_input("Current EMI (0 = calculate)",min_value=0.0,value=0.0,step=100.0)
        start=st.date_input("Next payment schedule starts from",date.today())
        extra=st.number_input("Optional extra principal per month",min_value=0.0,value=0.0,step=1000.0)
        save=st.form_submit_button("Calculate & save loan")
    if save:
        pay=known_emi if known_emi>0 else emi_payment(principal,annual,months)
        with db() as cx:
            cx.execute("""INSERT INTO emi_loans(name,lender,principal,annual_rate,emi,start_date,tenure_months,extra_payment)
                         VALUES(?,?,?,?,?,?,?,?)""",(name,lender,principal,annual,pay,str(start),months,extra))
            cx.commit()
        st.success("Loan saved.")
    with db() as cx: loans=pd.read_sql_query("SELECT * FROM emi_loans ORDER BY id DESC",cx)
    if not loans.empty:
        lid=st.selectbox("Saved loan",loans.id.tolist(),format_func=lambda x:loans.loc[loans.id==x,"name"].iloc[0])
        l=loans[loans.id==lid].iloc[0]
        sch=amortize(l.principal,l.annual_rate,int(l.tenure_months),l.start_date,l.emi,l.extra_payment)
        total_interest=sch.Interest.sum()
        c1,c2,c3=st.columns(3)
        c1.metric("Scheduled EMI",f"₹{l.emi:,.0f}")
        c2.metric("Future interest",f"₹{total_interest:,.0f}")
        c3.metric("Estimated completion",str(sch["Due Date"].iloc[-1]) if not sch.empty else "—")
        st.dataframe(sch,use_container_width=True,hide_index=True)

with tabs[5]:
    st.subheader("🛟 Financial Runway")
    df=get_txns()
    ms=monthly_series()
    hist=float(ms.debit.tail(6).mean()) if not ms.empty else 0
    emi_hist=float(df[(df.direction=="Debit")&(df["class"]=="EMI")].groupby(df.txn_date.str[:7]).amount.sum().tail(6).mean()) if not df.empty else 0
    a,b,c=st.columns(3)
    liquid=a.number_input("Current liquid funds",min_value=0.0,value=0.0,step=10000.0)
    essential=b.number_input("Monthly essential burn (excluding EMI)",min_value=0.0,value=max(0.0,hist-emi_hist),step=5000.0)
    emi_month=c.number_input("Monthly EMI commitment",min_value=0.0,value=max(0.0,emi_hist),step=5000.0)
    discretionary=st.slider("Discretionary-spending reduction during income gap",0,100,30)
    income_gap=st.number_input("Expected months without salary",min_value=0,max_value=60,value=6)
    effective_burn=essential*(1-discretionary/100)+emi_month
    runway=liquid/effective_burn if effective_burn else float("inf")
    st.metric("Estimated runway", "Unlimited" if not math.isfinite(runway) else f"{runway:.1f} months")
    rows=[]
    for m in [3,6,9,12]:
        spend=effective_burn*m
        rows.append([m,spend,liquid-spend, "Covered" if liquid>=spend else "Shortfall"])
    st.dataframe(pd.DataFrame(rows,columns=["Months","Projected cash need","Funds remaining","Status"]),hide_index=True)
    if income_gap:
        need=effective_burn*income_gap
        if liquid>=need: st.success(f"At the assumptions above, funds cover the {income_gap}-month no-income scenario with about ₹{liquid-need:,.0f} remaining.")
        else: st.error(f"At the assumptions above, the {income_gap}-month scenario has an estimated shortfall of ₹{need-liquid:,.0f}.")
    st.caption("Runway is an estimate based on the inputs and imported history; it is not a financial guarantee.")

with tabs[6]:
    st.subheader("📋 Transactions")
    df=get_txns()
    if df.empty: st.info("No transactions yet.")
    else:
        st.dataframe(df[["txn_date","description","amount","direction","category","class","source"]],use_container_width=True,hide_index=True)
        st.download_button("Export transactions CSV",df.to_csv(index=False).encode("utf-8"),"finance_transactions.csv","text/csv")

st.sidebar.markdown("### Privacy")
st.sidebar.info(f"Database: {DB_PATH}\n\nNo cloud AI or external database is used by this application.")
st.sidebar.markdown("**Important:** Do not commit `finance.db` or bank statements to GitHub.")
