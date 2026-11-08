from flask import Flask, request, redirect, url_for, render_template_string, flash, Response
from functools import wraps
from datetime import datetime
import os
import secrets

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "change-this-before-deployment")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "picante_direct.db")


# =========================================================
# DATABASE
# =========================================================

def using_postgres():
    return bool(DATABASE_URL)


def conn():
    if using_postgres():
        import psycopg2
        import psycopg2.extras

        c = psycopg2.connect(DATABASE_URL)
        c.autocommit = False
        return c

    import sqlite3
    c = sqlite3.connect(DATABASE_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def execute(c, sql, params=()):
    if using_postgres():
        cur = c.cursor(cursor_factory=__import__(
            "psycopg2.extras", fromlist=["RealDictCursor"]
        ).RealDictCursor)
        cur.execute(sql, params)
        return cur

    return c.execute(sql, params)


def fetchone(c, sql, params=()):
    cur = execute(c, sql, params)
    return cur.fetchone()


def fetchall(c, sql, params=()):
    cur = execute(c, sql, params)
    return cur.fetchall()


def placeholder_sql(sql):
    if using_postgres():
        return sql.replace("?", "%s")
    return sql


def q(sql, params=()):
    return placeholder_sql(sql), params


def init_db():
    if using_postgres():
        sql = """
        CREATE TABLE IF NOT EXISTS drivers(
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT,
            language TEXT NOT NULL DEFAULT 'en',
            approved INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders(
            id SERIAL PRIMARY KEY,
            ref TEXT UNIQUE NOT NULL,
            customer_name TEXT NOT NULL,
            customer_phone TEXT,
            language TEXT NOT NULL DEFAULT 'en',
            address TEXT NOT NULL,
            instructions TEXT,
            pizzas INTEGER NOT NULL,
            price_each INTEGER NOT NULL,
            total_mxn INTEGER NOT NULL,
            period TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            driver_id INTEGER,
            claimed_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS deliveries(
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL,
            driver_id INTEGER NOT NULL,
            company_card TEXT,
            machine_purchase_mxn INTEGER NOT NULL DEFAULT 0,
            customer_cash_mxn INTEGER NOT NULL DEFAULT 0,
            delivery_fee_mxn INTEGER NOT NULL DEFAULT 0,
            tip_mxn INTEGER NOT NULL DEFAULT 0,
            amount_due_office_mxn INTEGER NOT NULL DEFAULT 0,
            settlement_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settlements(
            id SERIAL PRIMARY KEY,
            driver_id INTEGER NOT NULL,
            delivery_count INTEGER NOT NULL,
            amount_mxn INTEGER NOT NULL,
            note TEXT,
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS card_transactions(
            id SERIAL PRIMARY KEY,
            driver_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            card_label TEXT NOT NULL,
            amount_mxn INTEGER NOT NULL,
            reference TEXT,
            recorded_at TEXT NOT NULL
        );
        """
    else:
        sql = """
        CREATE TABLE IF NOT EXISTS drivers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            language TEXT NOT NULL DEFAULT 'en',
            approved INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref TEXT UNIQUE NOT NULL,
            customer_name TEXT NOT NULL,
            customer_phone TEXT,
            language TEXT NOT NULL DEFAULT 'en',
            address TEXT NOT NULL,
            instructions TEXT,
            pizzas INTEGER NOT NULL,
            price_each INTEGER NOT NULL,
            total_mxn INTEGER NOT NULL,
            period TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            driver_id INTEGER,
            claimed_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS deliveries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            driver_id INTEGER NOT NULL,
            company_card TEXT,
            machine_purchase_mxn INTEGER NOT NULL DEFAULT 0,
            customer_cash_mxn INTEGER NOT NULL DEFAULT 0,
            delivery_fee_mxn INTEGER NOT NULL DEFAULT 0,
            tip_mxn INTEGER NOT NULL DEFAULT 0,
            amount_due_office_mxn INTEGER NOT NULL DEFAULT 0,
            settlement_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id),
            FOREIGN KEY(driver_id) REFERENCES drivers(id)
        );

        CREATE TABLE IF NOT EXISTS settlements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            delivery_count INTEGER NOT NULL,
            amount_mxn INTEGER NOT NULL,
            note TEXT,
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS card_transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            card_label TEXT NOT NULL,
            amount_mxn INTEGER NOT NULL,
            reference TEXT,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(driver_id) REFERENCES drivers(id),
            FOREIGN KEY(order_id) REFERENCES orders(id)
        );
        """

    with conn() as c:
        cur = c.cursor() if using_postgres() else c
        cur.execute(sql)
        c.commit()


# =========================================================
# HOSTED TEST LOGIN
# =========================================================

def require_test_login(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if os.environ.get("RENDER") or os.environ.get("HOSTED_TEST_MODE") == "1":
            expected_user = os.environ.get("TEST_USERNAME", "")
            expected_pass = os.environ.get("TEST_PASSWORD", "")

            auth = request.authorization

            if not expected_user or not expected_pass:
                return Response(
                    "Hosted test is locked. Configure TEST_USERNAME and TEST_PASSWORD.",
                    503
                )

            if (
                not auth
                or not secrets.compare_digest(auth.username or "", expected_user)
                or not secrets.compare_digest(auth.password or "", expected_pass)
            ):
                return Response(
                    "Authentication required",
                    401,
                    {"WWW-Authenticate": 'Basic realm="Picante Direct Test"'}
                )

        return fn(*args, **kwargs)

    return wrapped


# =========================================================
# HELPERS
# =========================================================

def now():
    return datetime.now().isoformat(timespec="seconds")


def new_ref():
    return "PD-" + secrets.token_hex(3).upper()


def row_value(row, key, default=None):
    if row is None:
        return default

    if isinstance(row, dict):
        return row.get(key, default)

    return row[key] if key in row.keys() else default


def unsettled_count(c, driver_id):
    sql, params = q(
        "SELECT COUNT(*) AS n FROM deliveries "
        "WHERE driver_id=? AND settlement_id IS NULL",
        (driver_id,)
    )
    row = fetchone(c, sql, params)
    return int(row_value(row, "n", 0))


def driver_is_eligible(c, driver_id):
    sql, params = q(
        "SELECT id FROM drivers "
        "WHERE id=? AND approved=1 AND active=1",
        (driver_id,)
    )
    return fetchone(c, sql, params) is not None and unsettled_count(c, driver_id) < 2


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

init_db()


# =========================================================
# BASE HTML
# =========================================================

BASE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Picante Direct</title>
<style>
body {
    font-family: Arial, sans-serif;
    background:#f4f4f4;
    margin:0;
    color:#222;
}
nav {
    background:#111;
    padding:15px;
}
nav a {
    color:#fff;
    text-decoration:none;
    margin-right:20px;
    font-weight:bold;
}
.container {
    max-width:1200px;
    margin:25px auto;
    background:#fff;
    padding:25px;
    border-radius:10px;
}
h1,h2,h3 { margin-top:0; }
.card {
    display:inline-block;
    vertical-align:top;
    min-width:190px;
    margin:8px;
    padding:18px;
    border:1px solid #ddd;
    border-radius:8px;
}
.big {
    font-size:32px;
    font-weight:bold;
}
table {
    width:100%;
    border-collapse:collapse;
    margin-top:15px;
}
th,td {
    padding:10px;
    border-bottom:1px solid #ddd;
    text-align:left;
}
th {
    background:#eee;
}
input,select,textarea {
    padding:8px;
    width:100%;
    box-sizing:border-box;
    margin:5px 0 12px;
}
button {
    background:#111;
    color:white;
    border:0;
    padding:10px 15px;
    border-radius:5px;
    cursor:pointer;
}
button.green {
    background:#16823b;
}
button.orange {
    background:#d66a00;
}
.alert {
    padding:12px;
    background:#fff3cd;
    border:1px solid #ffe69c;
    margin-bottom:15px;
}
.small {
    color:#666;
    font-size:13px;
}
</style>
</head>

<body>

<nav>
<a href="/">Dashboard</a>
<a href="/orders">Orders</a>
<a href="/orders/new">New Order</a>
<a href="/drivers">Drivers</a>
<a href="/settlements">Settlements</a>
<a href="/cards">Company Cards</a>
</nav>

<div class="container">

{% with messages = get_flashed_messages() %}
{% if messages %}
    {% for message in messages %}
    <div class="alert">{{ message }}</div>
    {% endfor %}
{% endif %}
{% endwith %}

{{ content|safe }}

</div>

</body>
</html>
"""


def page(content):
    return render_template_string(BASE, content=content)


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
@require_test_login
def dashboard():
    with conn() as c:

        open_orders = fetchone(
            c,
            *q("SELECT COUNT(*) AS n FROM orders WHERE status='OPEN'")
        )

        drivers = fetchone(
            c,
            *q("SELECT COUNT(*) AS n FROM drivers WHERE approved=1 AND active=1")
        )

        unsettled = fetchone(
            c,
            *q("SELECT COUNT(*) AS n FROM deliveries WHERE settlement_id IS NULL")
        )

        due = fetchone(
            c,
            *q(
                "SELECT COALESCE(SUM(amount_due_office_mxn),0) AS n "
                "FROM deliveries WHERE settlement_id IS NULL"
            )
        )

    content = f"""
    <h1>Picante Direct</h1>

    <div class="card">
        <div>Open Orders</div>
        <div class="big">{row_value(open_orders,'n',0)}</div>
    </div>

    <div class="card">
        <div>Approved Active Drivers</div>
        <div class="big">{row_value(drivers,'n',0)}</div>
    </div>

    <div class="card">
        <div>Unsettled Deliveries</div>
        <div class="big">{row_value(unsettled,'n',0)}</div>
    </div>

    <div class="card">
        <div>Cash Due to Office</div>
        <div class="big">MX${row_value(due,'n',0):,}</div>
    </div>

    <hr>

    <h3>Current Pricing</h3>
    <ul>
        <li>Machine pizza price: <strong>MX$175</strong></li>
        <li>Normal delivered price: <strong>MX$225</strong></li>
        <li>Normal delivery fee to driver: <strong>MX$50</strong></li>
        <li>Overnight 12:00am–6:00am customer price: <strong>MX$275</strong></li>
        <li>Overnight delivery fee to driver: <strong>MX$100</strong></li>
        <li>Driver keeps delivery fee + any customer tip.</li>
        <li>No backpack deposit.</li>
    </ul>
    """

    return page(content)


# =========================================================
# ORDERS
# =========================================================

@app.route("/orders")
@require_test_login
def orders():
    with conn() as c:
        rows = fetchall(
            c,
            *q("""
                SELECT o.*, d.name AS driver_name
                FROM orders o
                LEFT JOIN drivers d ON d.id=o.driver_id
                ORDER BY o.id DESC
            """)
        )

        drivers = fetchall(
            c,
            *q("""
                SELECT id,name,phone,language
                FROM drivers
                WHERE approved=1 AND active=1
                ORDER BY name
            """)
        )

    html = """
    <h1>Orders</h1>

    <a href="/orders/new">
        <button>+ New Order</button>
    </a>

    <table>
    <tr>
        <th>Reference</th>
        <th>Customer</th>
        <th>Pizzas</th>
        <th>Total</th>
        <th>Period</th>
        <th>Status</th>
        <th>Driver</th>
        <th>Action</th>
    </tr>
    """

    for o in rows:
        status = row_value(o, "status")
        driver = row_value(o, "driver_name") or "-"

        action = ""

        if status == "OPEN":
            options = "".join(
                f'<option value="{row_value(d,"id")}">{row_value(d,"name")}</option>'
                for d in drivers
                if driver_is_eligible(c, row_value(d, "id"))
            )

            action = f"""
            <form method="post" action="/orders/{row_value(o,'id')}/assign">
                <select name="driver_id" required>
                    <option value="">Select driver</option>
                    {options}
                </select>
                <button class="green">Assign</button>
            </form>

            <form method="post"
                  action="/orders/{row_value(o,'id')}/claim"
                  style="margin-top:5px">
                <button>Claim as driver test</button>
            </form>
            """

        elif status == "ASSIGNED":
            action = f"""
            <a href="/orders/{row_value(o,'id')}/complete">
                <button class="green">Record Delivery</button>
            </a>
            """

        html += f"""
        <tr>
            <td>{row_value(o,'ref')}</td>
            <td>
                {row_value(o,'customer_name')}<br>
                <span class="small">{row_value(o,'customer_phone') or ''}</span>
            </td>
            <td>{row_value(o,'pizzas')}</td>
            <td>MX${row_value(o,'total_mxn'):,}</td>
            <td>{row_value(o,'period')}</td>
            <td>{status}</td>
            <td>{driver}</td>
            <td>{action}</td>
        </tr>
        """

    html += "</table>"

    return page(html)


@app.route("/orders/new", methods=["GET", "POST"])
@require_test_login
def new_order():
    if request.method == "POST":

        pizzas = max(1, int(request.form.get("pizzas", "1")))
        period = request.form.get("period", "day")

        price_each = 275 if period == "overnight" else 225
        total = pizzas * price_each

        ref = new_ref()

        with conn() as c:
            sql, params = q(
                """
                INSERT INTO orders
                (ref,customer_name,customer_phone,language,address,
                 instructions,pizzas,price_each,total_mxn,period,
                 status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ref,
                    request.form.get("customer_name", "").strip(),
                    request.form.get("customer_phone", "").strip(),
                    request.form.get("language", "en"),
                    request.form.get("address", "").strip(),
                    request.form.get("instructions", "").strip(),
                    pizzas,
                    price_each,
                    total,
                    period,
                    "OPEN",
                    now()
                )
            )

            execute(c, sql, params)
            c.commit()

        flash(f"Order {ref} created.")
        return redirect(url_for("orders"))

    html = """
    <h1>New Order</h1>

    <form method="post">

    <label>Customer name</label>
    <input name="customer_name" required>

    <label>Customer phone</label>
    <input name="customer_phone">

    <label>Customer language</label>
    <select name="language">
        <option value="en">English</option>
        <option value="es">Español</option>
    </select>

    <label>Delivery address</label>
    <textarea name="address" required></textarea>

    <label>Instructions</label>
    <textarea name="instructions"></textarea>

    <label>Number of pizzas</label>
    <input type="number" name="pizzas" min="1" value="1" required>

    <label>Delivery period</label>
    <select name="period">
        <option value="day">Day — MX$225 delivered</option>
        <option value="overnight">12am–6am — MX$275 delivered</option>
    </select>

    <button class="green">Create Order</button>

    </form>
    """

    return page(html)


@app.route("/orders/<int:oid>/assign", methods=["POST"])
@require_test_login
def assign_order(oid):

    driver_id = int(request.form["driver_id"])

    with conn() as c:

        if not driver_is_eligible(c, driver_id):
            flash("Driver is not currently eligible for another delivery.")
            return redirect(url_for("orders"))

        sql, params = q(
            """
            UPDATE orders
            SET driver_id=?, status='ASSIGNED', claimed_at=?
            WHERE id=? AND status='OPEN'
            """,
            (driver_id, now(), oid)
        )

        execute(c, sql, params)
        c.commit()

    flash("Order assigned.")
    return redirect(url_for("orders"))


@app.route("/orders/<int:oid>/claim", methods=["POST"])
@require_test_login
def claim_order(oid):

    with conn() as c:

        drivers = fetchall(
            c,
            *q("""
                SELECT id FROM drivers
                WHERE approved=1 AND active=1
                ORDER BY id
            """)
        )

        selected = None

        for d in drivers:
            did = row_value(d, "id")
            if driver_is_eligible(c, did):
                selected = did
                break

        if selected is None:
            flash("No eligible driver is available.")
            return redirect(url_for("orders"))

        sql, params = q(
            """
            UPDATE orders
            SET driver_id=?, status='ASSIGNED', claimed_at=?
            WHERE id=? AND status='OPEN'
            """,
            (selected, now(), oid)
        )

        execute(c, sql, params)
        c.commit()

    flash("Order claimed by first eligible driver.")
    return redirect(url_for("orders"))


@app.route("/orders/<int:oid>/complete", methods=["GET", "POST"])
@require_test_login
def complete_order(oid):

    with conn() as c:

        sql, params = q(
            """
            SELECT o.*, d.name AS driver_name
            FROM orders o
            LEFT JOIN drivers d ON d.id=o.driver_id
            WHERE o.id=?
            """,
            (oid,)
        )

        o = fetchone(c, sql, params)

        if not o:
            return "Order not found", 404

        if request.method == "POST":

            tip = max(0, int(request.form.get("tip_mxn", "0")))
            card = request.form.get("company_card", "").strip()

            pizzas = row_value(o, "pizzas")
            period = row_value(o, "period")

            machine_cost = 175 * pizzas

            delivery_fee = (
                50 * pizzas
                if period == "day"
                else 100 * pizzas
            )

            total = row_value(o, "total_mxn")

            amount_due_office = max(
                0,
                total - delivery_fee
            )

            sql, params = q(
                """
                INSERT INTO deliveries
                (order_id,driver_id,company_card,machine_purchase_mxn,
                 customer_cash_mxn,delivery_fee_mxn,tip_mxn,
                 amount_due_office_mxn,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    oid,
                    row_value(o, "driver_id"),
                    card,
                    machine_cost,
                    total + tip,
                    delivery_fee,
                    tip,
                    amount_due_office,
                    now()
                )
            )

            execute(c, sql, params)

            sql, params = q(
                """
                UPDATE orders
                SET status='DELIVERED'
                WHERE id=?
                """,
                (oid,)
            )

            execute(c, sql, params)
            c.commit()

            flash("Delivery recorded.")
            return redirect(url_for("orders"))

        html = f"""
        <h1>Record Delivery</h1>

        <p>
        <strong>{row_value(o,'ref')}</strong><br>
        Customer: {row_value(o,'customer_name')}<br>
        Driver: {row_value(o,'driver_name')}<br>
        Pizzas: {row_value(o,'pizzas')}<br>
        Customer cash: MX${row_value(o,'total_mxn'):,}
        </p>

        <form method="post">

        <label>Customer tip (MX$)</label>
        <input type="number" name="tip_mxn" min="0" value="0">

        <label>Company card used for machine purchase</label>
        <input name="company_card"
               placeholder="Example: CARD-001">

        <button class="green">
            Record Completed Delivery
        </button>

        </form>
        """

        return page(html)


# =========================================================
# DRIVERS
# =========================================================

@app.route("/drivers", methods=["GET", "POST"])
@require_test_login
def drivers():

    if request.method == "POST":

        with conn() as c:

            sql, params = q(
                """
                INSERT INTO drivers
                (name,phone,language,approved,active,created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    request.form.get("name", "").strip(),
                    request.form.get("phone", "").strip(),
                    request.form.get("language", "en"),
                    1 if request.form.get("approved") == "1" else 0,
                    1,
                    now()
                )
            )

            execute(c, sql, params)
            c.commit()

        flash("Driver created.")
        return redirect(url_for("drivers"))

    with conn() as c:
        rows = fetchall(
            c,
            *q("""
                SELECT d.*,
                       (
                         SELECT COUNT(*)
                         FROM deliveries x
                         WHERE x.driver_id=d.id
                         AND x.settlement_id IS NULL
                       ) AS unsettled
                FROM drivers d
                ORDER BY d.id DESC
            """)
        )

    html = """
    <h1>Drivers</h1>

    <h3>Add Driver</h3>

    <form method="post">

    <label>Name</label>
    <input name="name" required>

    <label>Phone</label>
    <input name="phone">

    <label>Language</label>
    <select name="language">
        <option value="en">English</option>
        <option value="es">Español</option>
    </select>

    <label>Approval</label>
    <select name="approved">
        <option value="1">Approved</option>
        <option value="0">Pending approval</option>
    </select>

    <button class="green">Create Driver</button>

    </form>

    <h3>Driver List</h3>

    <table>
    <tr>
        <th>ID</th>
        <th>Name</th>
        <th>Phone</th>
        <th>Language</th>
        <th>Approved</th>
        <th>Active</th>
        <th>Unsettled Deliveries</th>
    </tr>
    """

    for d in rows:
        html += f"""
        <tr>
            <td>{row_value(d,'id')}</td>
            <td>{row_value(d,'name')}</td>
            <td>{row_value(d,'phone') or ''}</td>
            <td>{row_value(d,'language')}</td>
            <td>{'YES' if row_value(d,'approved') else 'NO'}</td>
            <td>{'YES' if row_value(d,'active') else 'NO'}</td>
            <td>{row_value(d,'unsettled',0)}</td>
        </tr>
        """

    html += "</table>"

    html += """
    <p class="small">
    A driver may take a maximum of two unsettled deliveries.
    After settlement is recorded, the driver becomes eligible again.
    No backpack deposit is recorded or collected by Picante Direct.
    </p>
    """

    return page(html)


# =========================================================
# SETTLEMENTS
# =========================================================

@app.route("/settlements", methods=["GET", "POST"])
@require_test_login
def settlements():

    if request.method == "POST":

        driver_id = int(request.form["driver_id"])

        with conn() as c:

            deliveries = fetchall(
                c,
                *q("""
                    SELECT id, amount_due_office_mxn
                    FROM deliveries
                    WHERE driver_id=?
                    AND settlement_id IS NULL
                    ORDER BY id
                """,
                (driver_id,))
            )

            if not deliveries:
                flash("No outstanding deliveries for this driver.")
                return redirect(url_for("settlements"))

            total = sum(
                row_value(d, "amount_due_office_mxn", 0)
                for d in deliveries
            )

            delivery_count = len(deliveries)

            # IMPORTANT:
            # PostgreSQL requires RETURNING id here because the
            # settlement ID is immediately needed below.
            if using_postgres():
                sql, params = q(
                    """
                    INSERT INTO settlements
                    (driver_id,delivery_count,amount_mxn,note,recorded_at)
                    VALUES (?,?,?,?,?)
                    RETURNING id
                    """,
                    (
                        driver_id,
                        delivery_count,
                        total,
                        request.form.get("note", ""),
                        now()
                    )
                )
            else:
                sql, params = q(
                    """
                    INSERT INTO settlements
                    (driver_id,delivery_count,amount_mxn,note,recorded_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        driver_id,
                        delivery_count,
                        total,
                        request.form.get("note", ""),
                        now()
                    )
                )

            cur = execute(c, sql, params)

            if using_postgres():
                result = cur.fetchone()
                settlement_id = result["id"]
            else:
                settlement_id = cur.lastrowid

            for d in deliveries:
                sql, params = q(
                    """
                    UPDATE deliveries
                    SET settlement_id=?
                    WHERE id=?
                    """,
                    (settlement_id, row_value(d, "id"))
                )

                execute(c, sql, params)

            c.commit()

        flash(
            f"Settlement recorded: MX${total:,} for {delivery_count} delivery/ies."
        )

        return redirect(url_for("settlements"))

    with conn() as c:

        drivers = fetchall(
            c,
            *q("""
                SELECT d.id,d.name,
                       COUNT(x.id) AS unsettled_count,
                       COALESCE(SUM(x.amount_due_office_mxn),0) AS amount_due
                FROM drivers d
                LEFT JOIN deliveries x
                    ON x.driver_id=d.id
                    AND x.settlement_id IS NULL
                GROUP BY d.id,d.name
                ORDER BY d.name
            """)
        )

        history = fetchall(
            c,
            *q("""
                SELECT s.*, d.name AS driver_name
                FROM settlements s
                JOIN drivers d ON d.id=s.driver_id
                ORDER BY s.id DESC
            """)
        )

    html = """
    <h1>Settlements</h1>

    <h3>Outstanding Driver Cash</h3>

    <table>
    <tr>
        <th>Driver</th>
        <th>Deliveries</th>
        <th>Amount Due</th>
        <th>Action</th>
    </tr>
    """

    for d in drivers:

        count = row_value(d, "unsettled_count", 0)
        amount = row_value(d, "amount_due", 0)

        action = ""

        if count:
            action = f"""
            <form method="post">
                <input type="hidden"
                       name="driver_id"
                       value="{row_value(d,'id')}">

                <input type="text"
                       name="note"
                       placeholder="Settlement note">

                <button class="green">
                    Record MX${amount:,} Settlement
                </button>
            </form>
            """

        else:
            action = "No cash outstanding"

        html += f"""
        <tr>
            <td>{row_value(d,'name')}</td>
            <td>{count}</td>
            <td>MX${amount:,}</td>
            <td>{action}</td>
        </tr>
        """

    html += """
    </table>

    <h3>Settlement History</h3>

    <table>
    <tr>
        <th>ID</th>
        <th>Driver</th>
        <th>Deliveries</th>
        <th>Amount</th>
        <th>Note</th>
        <th>Date</th>
    </tr>
    """

    for s in history:
        html += f"""
        <tr>
            <td>{row_value(s,'id')}</td>
            <td>{row_value(s,'driver_name')}</td>
            <td>{row_value(s,'delivery_count')}</td>
            <td>MX${row_value(s,'amount_mxn'):,}</td>
            <td>{row_value(s,'note') or ''}</td>
            <td>{row_value(s,'recorded_at')}</td>
        </tr>
        """

    html += "</table>"

    return page(html)


# =========================================================
# COMPANY CARDS
# =========================================================

@app.route("/cards", methods=["GET", "POST"])
@require_test_login
def cards():

    if request.method == "POST":

        with conn() as c:

            sql, params = q(
                """
                INSERT INTO card_transactions
                (driver_id,order_id,card_label,amount_mxn,reference,recorded_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    int(request.form["driver_id"]),
                    int(request.form["order_id"]),
                    request.form["card_label"].strip(),
                    int(request.form["amount_mxn"]),
                    request.form.get("reference", "").strip(),
                    now()
                )
            )

            execute(c, sql, params)
            c.commit()

        flash("Company card transaction recorded.")
        return redirect(url_for("cards"))

    with conn() as c:

        drivers = fetchall(
            c,
            *q("""
                SELECT id,name
                FROM drivers
                WHERE active=1
                ORDER BY name
            """)
        )

        orders = fetchall(
            c,
            *q("""
                SELECT id,ref,customer_name
                FROM orders
                ORDER BY id DESC
            """)
        )

        transactions = fetchall(
            c,
            *q("""
                SELECT ct.*, d.name AS driver_name, o.ref AS order_ref
                FROM card_transactions ct
                JOIN drivers d ON d.id=ct.driver_id
                JOIN orders o ON o.id=ct.order_id
                ORDER BY ct.id DESC
            """)
        )

    driver_options = "".join(
        f'<option value="{row_value(d,"id")}">{row_value(d,"name")}</option>'
        for d in drivers
    )

    order_options = "".join(
        f'<option value="{row_value(o,"id")}">'
        f'{row_value(o,"ref")} — {row_value(o,"customer_name")}'
        f'</option>'
        for o in orders
    )

    html = f"""
    <h1>Company Cards</h1>

    <h3>Record Machine Purchase</h3>

    <form method="post">

    <label>Driver</label>
    <select name="driver_id" required>
        {driver_options}
    </select>

    <label>Order</label>
    <select name="order_id" required>
        {order_options}
    </select>

    <label>Card</label>
    <input name="card_label"
           placeholder="Example: CARD-001"
           required>

    <label>Amount MX$</label>
    <input type="number"
           name="amount_mxn"
           min="0"
           value="175"
           required>

    <label>Reference</label>
    <input name="reference">

    <button class="green">
        Record Card Transaction
    </button>

    </form>

    <h3>Transaction History</h3>

    <table>
    <tr>
        <th>ID</th>
        <th>Driver</th>
        <th>Order</th>
        <th>Card</th>
        <th>Amount</th>
        <th>Reference</th>
        <th>Date</th>
    </tr>
    """

    for t in transactions:
        html += f"""
        <tr>
            <td>{row_value(t,'id')}</td>
            <td>{row_value(t,'driver_name')}</td>
            <td>{row_value(t,'order_ref')}</td>
            <td>{row_value(t,'card_label')}</td>
            <td>MX${row_value(t,'amount_mxn'):,}</td>
            <td>{row_value(t,'reference') or ''}</td>
            <td>{row_value(t,'recorded_at')}</td>
        </tr>
        """

    html += "</table>"

    return page(html)


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():
    return {
        "status": "ok",
        "database": "postgresql" if using_postgres() else "sqlite",
        "time": now()
    }


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
