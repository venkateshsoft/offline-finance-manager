# Offline Finance Manager v2

A local-first personal finance manager built with Streamlit.

## Features
- CSV/XLSX/XLS bank-statement import
- Credit/debit extraction
- Rule-based categorization with locally learned corrections
- Categories: Food, Travel, Entertainment, Fuel, Online Shopping, Education, Medical, Insurance, Utilities, Loan EMI, etc.
- Recurring-payment detection: monthly, quarterly, annual, fortnightly
- EMI amortization: payment, interest, principal, balance, due dates and estimated completion
- Financial runway planner for 3/6/9/12-month no-income scenarios
- Spending trend alerts
- Local SQLite database
- CSV export
- No external AI/API required

## Run locally

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

Open the local URL shown by Streamlit.

## Data location

The database is stored at:

`~/.offline_finance_manager/finance.db`

Bank statements are processed in memory during import. The app does not upload them to a cloud service.

## GitHub + mobile/laptop access

GitHub stores the source code; it does not run this Python/Streamlit application.

For private access from your phone/laptop, a practical architecture is:
1. Keep the app and database on an always-on home PC/NAS.
2. Run Streamlit on that machine.
3. Install Tailscale on the host and phone/laptop.
4. Access the Streamlit port over your private Tailscale network.

If the host PC is powered off, the app will not be available.

Do NOT commit:
- `finance.db`
- bank statements
- `.streamlit/secrets.toml`
- exported transaction files

A `.gitignore` is included.

## EMI note

The amortization calculator is a planning model. Actual lender schedules can differ because of rate changes, daily-interest calculations, fees, insurance, prepayments, payment-date differences, and other lender-specific rules. Enter the current outstanding principal and confirmed lender terms.

## Security note

This is a personal finance tool, not a bank-grade security product. If you expose it beyond your local/private network, add authentication, HTTPS, access controls and backups before using real financial data.
