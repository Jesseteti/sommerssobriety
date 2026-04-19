import mimetypes, os, uuid, re
from dotenv import load_dotenv
load_dotenv()

from datetime import date, timedelta
from flask import Flask, render_template, request, redirect, url_for, abort, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from auth import User, verify_password
from pathlib import Path
from receipts import ReceiptData, generate_payment_receipt_pdf_bytes
from decimal import Decimal
from storage import upload_bytes, create_signed_url
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix # SSL fix
from PIL import Image
from io import BytesIO

from db import (
    get_db_connection,
    get_residents_with_balances,
    ensure_rent_charges_up_to_date,
    refresh_auto_charges_for_active_residents,
    get_user_row_by_username,
    get_all_payments,
    get_most_recent_payments,
    insert_receipt_record,
    get_expenses_with_files,
    get_receipt_by_ledger_entry_id,
    get_expense_file_by_id)

ALLOWED_EXPENSE_EXTENSIONS = {"jpg", "jpeg", "png", "pdf"}

app = Flask(__name__)

##### ----------------------< FORCE HTTPS >---------------------- #####

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

@app.before_request
def force_https():
    # If request is not HTTPS, force it
    if not request.is_secure and not app.debug:
        return redirect(request.url.replace("http://", "https://", 1), code=301)

##### ------------------< IMG UPLOAD RESIZE >------------------ #####

app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 3 MB request limit
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
app.permanent_session_lifetime = timedelta(hours=8)

def process_image(file_bytes: bytes, max_size=1600, quality=80) -> tuple[bytes, str]:
    """
    Resize and compress image.
    Returns (processed_bytes, content_type)
    """
    img = Image.open(BytesIO(file_bytes))

    # Convert RGBA/PNG → RGB (JPEG doesn't support alpha)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Resize while keeping aspect ratio
    img.thumbnail((max_size, max_size))

    output = BytesIO()
    img.save(output, format="JPEG", quality=quality, optimize=True)

    return output.getvalue(), "image/jpeg"

##### ----------------------< AUTH >---------------------- #####

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.get(int(user_id))

# formatting for phone numbers, had to come before the temp
def format_phone(phone):
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}"
    return phone  # fallback if not 10 digits

app.jinja_env.filters["phone"] = format_phone

##### -------------------------< ROUTES >------------------------- #####

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        row = get_user_row_by_username(username)
        if not row or not row["is_active"]:
            return render_template("login.html", error="Invalid login")

        if not verify_password(password, row["password_hash"]):
            return render_template("login.html", error="Invalid login")

        login_user(User(row))
        session.permanent = True
        return redirect(url_for("home"))

    return render_template("login.html")

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def home():
    refresh_auto_charges_for_active_residents()

    rows = get_most_recent_payments(active_only=True)

    return render_template("home.html", rows=rows)

@app.route("/residents")
@login_required
def residents_list():
    # Keep balances current
    refresh_auto_charges_for_active_residents()

    residents = get_residents_with_balances()
    return render_template("residents.html", residents=residents)

@app.route("/residents/new", methods=["GET", "POST"])
@login_required
def residents_new():
    if request.method == "POST":
        full_name = request.form["full_name"]
        phone = request.form.get("phone") or None
        rate_amount = Decimal(request.form["rate_amount"])

        # READ
        rate_frequency = request.form["rate_frequency"]

        # ENFORCE
        rate_frequency = rate_frequency.strip().capitalize()
        if rate_frequency not in ("Weekly", "Monthly"):
            return "Invalid rate frequency", 400

        start_date = request.form["start_date"]
        notes = request.form.get("notes") or None

        # DB INSERT (unchanged)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO residents
                (full_name, phone, rate_amount, rate_frequency, start_date, status, notes)
            VALUES
                (%s, %s, %s, %s, %s, 'Active', %s);
            """,
            (full_name, phone, rate_amount, rate_frequency, start_date, notes),
        )
        conn.commit()
        conn.close()

        return redirect(url_for("residents_list"))

    return render_template("resident_new.html")

@app.route("/residents/<int:resident_id>")
@login_required
def resident_detail(resident_id):
    # Ensure this resident is caught up on auto rent before showing ledger/balance
    ensure_rent_charges_up_to_date(resident_id)

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM residents WHERE id = %s;", (resident_id,))
    resident = cur.fetchone()

    if not resident:
        conn.close()
        return "Resident not found", 404

    # Alias entry_date as "date" so your existing templates keep working
    cur.execute(
        """
        SELECT 
            le.id,
            le.entry_date AS date,
            le.entry_type,
            le.amount,
            le.description,
            le.source,
            r.object_path AS receipt_object_path
        FROM ledger_entries le
        LEFT JOIN receipts r ON r.ledger_entry_id = le.id
        WHERE le.resident_id = %s
        ORDER BY le.entry_date DESC, le.id DESC;
        """,
        (resident_id,),
    )
    ledger_entries = cur.fetchall()

    # Compute balance using the same convention as list query (charge +, payment -, adjustment +)
    balance = Decimal("0.00")
    for e in ledger_entries:
        amt = e["amount"]
        if e["entry_type"] in ("charge", "adjustment"):
            balance += amt
        elif e["entry_type"] == "payment":
            balance -= amt

    conn.close()

    return render_template(
        "resident_detail.html",
        resident=resident,
        ledger_entries=ledger_entries,
        balance=float(balance),
    )

@app.route("/residents/<int:resident_id>/ledger/add", methods=["POST"])
@login_required
def resident_ledger_add(resident_id):
    entry_date = request.form["date"]
    entry_type = request.form["entry_type"]
    amount = Decimal(request.form["amount"])
    description = request.form.get("description") or ""

    if entry_type not in ("charge", "payment", "adjustment"):
        return "Invalid entry type", 400

    if entry_type in ("charge", "payment") and amount <= 0:
        return "Charges and payments must be positive amounts", 400

    if entry_type == "adjustment" and amount == 0:
        return "Adjustment amount cannot be zero", 400

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        created_by = current_user.id    # track who made entry

        cur.execute(    # insert entry and get entry_id
            """
            INSERT INTO ledger_entries
            (resident_id, entry_date, entry_type, amount, description, source, created_by_user_id)
            VALUES (%s, %s, %s, %s, %s, NULL, %s) RETURNING id;
            """,
            (resident_id, entry_date, entry_type, amount, description, created_by),
        )
        entry_id = cur.fetchone()["id"]

        if entry_type == "payment":     # only generate receipts for payments

            cur.execute("SELECT full_name FROM residents WHERE id = %s;", (resident_id,))
            r = cur.fetchone()
            resident_name = r["full_name"] if r else "Unknown"

            # get balance AFTER this payment
            cur.execute(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN entry_type = 'charge' THEN amount
                        WHEN entry_type = 'payment' THEN -amount
                        WHEN entry_type = 'adjustment' THEN amount
                        ELSE 0
                    END
                ), 0) AS balance
                FROM ledger_entries
                WHERE resident_id = %s;
                """,
                (resident_id,),
            )
            balance_after = cur.fetchone()["balance"]

            # Generate PDF bytes using your banner image
            project_root = Path(app.root_path)  # points to your project folder where app.py lives
            banner_path = project_root / "static" / "images" / "ss_bannerTEST.PNG"

            receipt_data = ReceiptData(
                resident_name=resident_name,
                entry_date=date.fromisoformat(entry_date),
                amount_paid=amount,
                balance_after=Decimal(str(balance_after)),
                entry_id=entry_id,
            )

            pdf_bytes = generate_payment_receipt_pdf_bytes(receipt_data, banner_path)

            # Upload PDF bytes to Supabase Storage (private bucket)
            bucket = "receipts"
            object_path = f"resident_{resident_id}/receipt_{entry_id}.pdf"

            meta = upload_bytes(
                bucket=bucket,
                object_path=object_path,
                data=pdf_bytes,
                content_type="application/pdf",
            )

            # Store receipt metadata (storage-backed)
            insert_receipt_record(
                ledger_entry_id=entry_id,
                resident_id=resident_id,
                bucket=bucket,
                object_path=object_path,
                original_filename=f"receipt_{entry_id}.pdf",
                content_type="application/pdf",
                file_size_bytes=meta["size_bytes"],
                sha256=meta["sha256"],
                created_by_user_id=created_by,
                cur=cur,
            )

        # 3) Commit ONCE at the end (atomic)
        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return redirect(url_for("resident_detail", resident_id=resident_id))

@app.route("/residents/<int:resident_id>/set_status/<new_status>", methods=["POST"])
@login_required
def resident_set_status(resident_id, new_status):
    if new_status not in ("Active", "Inactive"):
        return "Invalid status", 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE residents SET status = %s WHERE id = %s;",
        (new_status, resident_id),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("resident_detail", resident_id=resident_id))

@app.route("/residents/<int:resident_id>/delete", methods=["POST"])
@login_required
def resident_delete(resident_id):
    conn = get_db_connection()
    cur = conn.cursor()

    # FK ON DELETE CASCADE will remove ledger_entries automatically
    cur.execute("DELETE FROM residents WHERE id = %s;", (resident_id,))

    conn.commit()
    conn.close()

    return redirect(url_for("residents_list"))

## --------------- finances --------------- ##
@app.route("/finances/payments")
@login_required
def payments():
    payments = get_all_payments()
    return render_template("payments.html", payments=payments)

def allowed_expense_file(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXPENSE_EXTENSIONS

@app.route("/finances/expenses", methods=["GET", "POST"])
@login_required
def expenses():
    if request.method == "POST":
        vendor = request.form["vendor"].strip()
        expense_date = request.form["expense_date"].strip()  # yyyy-mm-dd
        amount = Decimal(request.form["amount"])
        category = (request.form.get("category") or "").strip() or None
        notes = (request.form.get("notes") or "").strip() or None

        if not vendor:
            return "Vendor is required", 400
        if amount <= 0:
            return "Amount must be positive", 400

        files = request.files.getlist("files")

        conn = get_db_connection()
        try:
            cur = conn.cursor()

            # 1) insert expense row
            cur.execute(
                """
                INSERT INTO expenses (vendor, expense_date, amount, category, notes, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (vendor, expense_date, amount, category, notes, current_user.id),
            )
            expense_id = cur.fetchone()["id"]

            # 2) upload files (0..many)
            for f in files:
                if not f or not f.filename:
                    continue

                filename = secure_filename(f.filename)
                if not allowed_expense_file(filename):
                    raise RuntimeError(f"File type not allowed: {filename}")

                data = f.read()
                if not data:
                    continue

                ext = filename.rsplit(".", 1)[1].lower()

                # 👉 PROCESS IMAGES ONLY
                if ext in ("jpg", "jpeg", "png"):
                    try:
                        # Always resize (recommended)
                        data, content_type = process_image(data)

                        # force extension to jpg since we re-encode
                        filename = filename.rsplit(".", 1)[0] + ".jpg"

                    except Exception:
                        raise RuntimeError(f"Failed to process image: {filename}")

                elif ext == "pdf":
                    content_type = "application/pdf"

                else:
                    raise RuntimeError(f"Unsupported file type: {filename}")

                # Unique storage path
                bucket = "expenses"
                object_path = f"expense_{expense_id}/{uuid.uuid4().hex}_{filename}"

                meta = upload_bytes(
                    bucket=bucket,
                    object_path=object_path,
                    data=data,
                    content_type=content_type,
                )

                # 3) insert expense_files metadata (same transaction)
                cur.execute(
                    """
                    INSERT INTO expense_files
                      (expense_id, bucket, object_path, original_filename, content_type, file_size_bytes, sha256, uploaded_by_user_id)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        expense_id,
                        bucket,
                        object_path,
                        filename,
                        content_type,
                        meta["size_bytes"],
                        meta["sha256"],
                        current_user.id,
                    ),
                )

            conn.commit()
            return redirect(url_for("expenses"))

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # GET: show list
    items = get_expenses_with_files()
    return render_template("expenses.html", expenses=items)

@app.route("/expenses/files/<int:file_id>")
@login_required
def expense_file_view(file_id):
    row = get_expense_file_by_id(file_id)
    if not row:
        abort(404)

    signed = create_signed_url(
        bucket=row["bucket"],
        object_path=row["object_path"],
        expires_in_seconds=300,
    )
    return redirect(signed)

@app.route("/receipts/<int:ledger_entry_id>")
@login_required
def receipt_view(ledger_entry_id):
    r = get_receipt_by_ledger_entry_id(ledger_entry_id)
    if not r:
        abort(404)

    signed = create_signed_url(
        bucket=r["bucket"],
        object_path=r["object_path"],
        expires_in_seconds=300,
    )

    return redirect(signed)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)