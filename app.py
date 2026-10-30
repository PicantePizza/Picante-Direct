from flask import Flask, request, redirect, url_for, render_template_string, flash
import sqlite3, os, json, secrets
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "change-this-before-deployment")
DB_PATH = os.environ.get("DATABASE_PATH", "picante_direct.db")

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS drivers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT,
          approved INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
          backpack_deposit_mxn INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE NOT NULL,
          customer_name TEXT NOT NULL, customer_phone TEXT, address TEXT NOT NULL,
          instructions TEXT, pizzas INTEGER NOT NULL, price_each INTEGER NOT NULL,
          total_mxn INTEGER NOT NULL, period TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
          driver_id INTEGER, claimed_at TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(driver_id) REFERENCES drivers(id)
        );
        CREATE TABLE IF NOT EXISTS deliveries(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
          driver_id INTEGER NOT NULL, company_card TEXT, machine_purchase_mxn INTEGER NOT NULL DEFAULT 0,
          customer_cash_mxn INTEGER NOT NULL DEFAULT 0, delivery_fee_mxn INTEGER NOT NULL DEFAULT 0,
          tip_mxn INTEGER NOT NULL DEFAULT 0, amount_due_office_mxn INTEGER NOT NULL DEFAULT 0,
          settlement_id INTEGER, created_at TEXT NOT NULL,
          FOREIGN KEY(order_id) REFERENCES orders(id), FOREIGN KEY(driver_id) REFERENCES drivers(id)
        );
        CREATE TABLE IF NOT EXISTS settlements(
          id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id INTEGER NOT NULL,
          delivery_count INTEGER NOT NULL, amount_mxn INTEGER NOT NULL,
          note TEXT, recorded_at TEXT NOT NULL,
          FOREIGN KEY(driver_id) REFERENCES drivers(id)
        );
        CREATE TABLE IF NOT EXISTS card_transactions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id INTEGER NOT NULL,
          order_id INTEGER NOT NULL, card_label TEXT NOT NULL, amount_mxn INTEGER NOT NULL,
          reference TEXT, recorded_at TEXT NOT NULL,
          FOREIGN KEY(driver_id) REFERENCES drivers(id), FOREIGN KEY(order_id) REFERENCES orders(id)
        );
        """)
init_db()


# Hosted-test protection. Configure TEST_USERNAME and TEST_PASSWORD in the hosting dashboard.
from functools import wraps
from flask import Response

def require_test_login(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if os.environ.get("RENDER") or os.environ.get("HOSTED_TEST_MODE") == "1":
            expected_user = os.environ.get("TEST_USERNAME", "")
            expected_pass = os.environ.get("TEST_PASSWORD", "")
            auth = request.authorization
            if not expected_user or not expected_pass:
                return Response("Hosted test is locked. Configure TEST_USERNAME and TEST_PASSWORD.", 503)
            if not auth or not secrets.compare_digest(auth.username or "", expected_user) or not secrets.compare_digest(auth.password or "", expected_pass):
                return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Picante Direct Test"'})
        return fn(*args, **kwargs)
    return wrapped

PAGE = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Picante Direct Operations</title><style>
body{font-family:Arial,sans-serif;background:#101114;color:#f7f7f7;margin:0}header{padding:22px 5%;background:#17191d;border-bottom:3px solid #b6ff00}
h1{margin:0;font-size:25px}header p{margin:7px 0 0;color:#c8c8c8}.wrap{max-width:1200px;margin:24px auto;padding:0 16px}
nav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}nav a,.btn{background:#b6ff00;color:#111;padding:10px 13px;text-decoration:none;font-weight:bold;border:0;border-radius:5px;cursor:pointer}
.panel{background:#1a1c21;border:1px solid #34373e;border-radius:8px;padding:16px;margin:12px 0;overflow:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.stat{font-size:25px;color:#b6ff00;font-weight:bold}
table{border-collapse:collapse;width:100%;min-width:680px}th,td{text-align:left;padding:9px;border-bottom:1px solid #383b42;font-size:14px}th{color:#b6ff00}
input,select,textarea{padding:9px;background:#0f1013;color:#fff;border:1px solid #555;border-radius:4px;max-width:100%;box-sizing:border-box}
form.inline{display:inline}form.stack{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}label{display:flex;flex-direction:column;gap:5px;color:#ddd;font-size:13px}
.small{font-size:12px;color:#aaa}.pill{padding:4px 7px;border-radius:4px;background:#333}.warn{color:#ffcf5a}.ok{color:#b6ff00}
</style></head><body><header><h1>PICANTE DIRECT — OPERATIONS</h1><p>Separate test app · dispatch, drivers, cash settlement and card reconciliation</p></header>
<div class="wrap"><nav><a href="/">Dashboard</a><a href="/orders">Orders</a><a href="/drivers">Drivers</a><a href="/settlements">Settlements</a><a href="/cards">Company cards</a></nav>
{% with messages=get_flashed_messages() %}{% if messages %}<div class="panel">{{messages|join(' · ')}}</div>{% endif %}{% endwith %}
{{body|safe}}
<p class="small">TEST BUILD ONLY. Not connected to WhatsApp, payment systems, or any live service. Verify all records and business rules before production use.</p></div></body></html>
"""

def page(body, **ctx):
    return render_template_string(PAGE, body=render_template_string(body, **ctx))

@app.route("/")
@require_test_login
def dashboard():
    with conn() as c:
        orders=c.execute("SELECT COUNT(*) n FROM orders WHERE status='OPEN'").fetchone()["n"]
        active=c.execute("SELECT COUNT(*) n FROM drivers WHERE approved=1 AND active=1").fetchone()["n"]
        unsettled=c.execute("SELECT COUNT(*) n FROM deliveries WHERE settlement_id IS NULL").fetchone()["n"]
        due=c.execute("SELECT COALESCE(SUM(amount_due_office_mxn),0) n FROM deliveries WHERE settlement_id IS NULL").fetchone()["n"]
        latest=c.execute("""SELECT o.*,d.name driver_name FROM orders o LEFT JOIN drivers d ON d.id=o.driver_id ORDER BY o.id DESC LIMIT 8""").fetchall()
    body="""<div class="grid"><div class="panel">Open orders<div class="stat">{{orders}}</div></div><div class="panel">Approved active drivers<div class="stat">{{active}}</div></div><div class="panel">Unsettled deliveries<div class="stat">{{unsettled}}</div></div><div class="panel">Cash due to office<div class="stat">MX${{due}}</div></div></div>
    <div class="panel"><h2>Recent orders</h2><table><tr><th>Ref</th><th>Customer</th><th>Pizzas</th><th>Total</th><th>Status</th><th>Driver</th></tr>{% for o in latest %}<tr><td>{{o.ref}}</td><td>{{o.customer_name}}</td><td>{{o.pizzas}}</td><td>MX${{o.total_mxn}}</td><td>{{o.status}}</td><td>{{o.driver_name or '—'}}</td></tr>{% endfor %}</table></div>
    <div class="panel"><h2>Record a test order</h2><form class="stack" method="post" action="/orders/new"><label>Customer name<input name="customer_name" required></label><label>Phone<input name="customer_phone"></label><label>Delivery address<input name="address" required></label><label>Instructions<input name="instructions"></label><label>Pizza quantity<input type="number" min="1" max="20" name="pizzas" value="1" required></label><label>Order time period<select name="period"><option value="day">06:00–23:59 (MX$225 each)</option><option value="overnight">00:00–05:59 (MX$275 each)</option></select></label><button class="btn">Create test order</button></form></div>"""
    return page(body, orders=orders, active=active, unsettled=unsettled, due=due, latest=latest)

@app.post("/orders/new")
@require_test_login
def new_order():
    name=request.form["customer_name"].strip(); address=request.form["address"].strip()
    pizzas=max(1,min(20,int(request.form.get("pizzas",1))))
    period=request.form.get("period","day"); price=275 if period=="overnight" else 225
    ref="PD-"+secrets.token_hex(3).upper()
    with conn() as c:
        c.execute("INSERT INTO orders(ref,customer_name,customer_phone,address,instructions,pizzas,price_each,total_mxn,period,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ref,name,request.form.get("customer_phone",""),address,request.form.get("instructions",""),pizzas,price,pizzas*price,period,datetime.now().isoformat(timespec="seconds")))
    flash(f"Test order {ref} created.")
    return redirect(url_for("orders"))

@app.route("/orders")
@require_test_login
def orders():
    with conn() as c:
        rows=c.execute("""SELECT o.*,d.name driver_name FROM orders o LEFT JOIN drivers d ON d.id=o.driver_id ORDER BY o.id DESC""").fetchall()
        drivers=c.execute("SELECT * FROM drivers WHERE approved=1 AND active=1 ORDER BY name").fetchall()
    body="""<div class="panel"><h2>Orders & dispatch</h2><table><tr><th>Ref</th><th>Customer / address</th><th>Qty</th><th>Total</th><th>Status</th><th>Driver</th><th>Actions</th></tr>{% for o in rows %}<tr><td>{{o.ref}}</td><td>{{o.customer_name}}<br><span class="small">{{o.address}}</span></td><td>{{o.pizzas}}</td><td>MX${{o.total_mxn}}</td><td>{{o.status}}</td><td>{{o.driver_name or '—'}}</td><td>{% if o.status=='OPEN' %}<form class="inline" method="post" action="/orders/{{o.id}}/claim"><select name="driver_id" required><option value="">Assign driver…</option>{% for d in drivers %}<option value="{{d.id}}">{{d.name}}</option>{% endfor %}</select><button class="btn">Assign</button></form>{% elif o.status=='ASSIGNED' %}<form class="inline" method="post" action="/orders/{{o.id}}/complete"><button class="btn">Record delivery</button></form>{% endif %}</td></tr>{% endfor %}</table></div>"""
    return page(body, rows=rows, drivers=drivers)

@app.post("/orders/<int:oid>/claim")
def claim(oid):
    did=int(request.form["driver_id"])
    with conn() as c:
        d=c.execute("SELECT * FROM drivers WHERE id=? AND approved=1 AND active=1",(did,)).fetchone()
        o=c.execute("SELECT * FROM orders WHERE id=? AND status='OPEN'",(oid,)).fetchone()
        pending=c.execute("SELECT COUNT(*) n FROM deliveries WHERE driver_id=? AND settlement_id IS NULL",(did,)).fetchone()["n"]
        if not d or not o: flash("Assignment failed: order is no longer open or driver is not approved/active.")
        elif pending>=2: flash("Blocked: this driver must settle outstanding deliveries before taking another.")
        else:
            changed=c.execute("UPDATE orders SET status='ASSIGNED',driver_id=?,claimed_at=? WHERE id=? AND status='OPEN'",(did,datetime.now().isoformat(timespec="seconds"),oid)).rowcount
            if changed: flash("Driver assigned.")
            else: flash("Another dispatcher/driver already claimed this order.")
    return redirect(url_for("orders"))

@app.post("/orders/<int:oid>/complete")
def complete(oid):
    with conn() as c:
        o=c.execute("SELECT * FROM orders WHERE id=? AND status='ASSIGNED'",(oid,)).fetchone()
        if not o: flash("Order is not assigned.")
        else:
            did=o["driver_id"]
            pending=c.execute("SELECT COUNT(*) n FROM deliveries WHERE driver_id=? AND settlement_id IS NULL",(did,)).fetchone()["n"]
            if pending>=2: flash("Cannot record a third unsettled delivery; settle first.")
            else:
                # Simplified test accounting: delivered price less pizza-machine cost at MX$175 each is amount owed to office.
                machine_cost=175*o["pizzas"]
                delivery_fee=50*o["pizzas"] if o["period"]=="day" else 100*o["pizzas"]
                due=max(0,o["total_mxn"]-delivery_fee)
                c.execute("""INSERT INTO deliveries(order_id,driver_id,machine_purchase_mxn,customer_cash_mxn,delivery_fee_mxn,amount_due_office_mxn,created_at)
                VALUES(?,?,?,?,?,?,?)""",(o["id"],did,machine_cost,o["total_mxn"],delivery_fee,due,datetime.now().isoformat(timespec="seconds")))
                c.execute("UPDATE orders SET status='DELIVERED' WHERE id=?",(oid,))
                flash("Delivery recorded. Driver settlement balance updated.")
    return redirect(url_for("orders"))

@app.route("/drivers", methods=["GET","POST"])
@require_test_login
def drivers():
    if request.method=="POST":
        name=request.form["name"].strip()
        with conn() as c:
            c.execute("INSERT INTO drivers(name,phone,approved,active,backpack_deposit_mxn,created_at) VALUES(?,?,?,?,?,?)",
            (name,request.form.get("phone",""),1 if request.form.get("approved")=="1" else 0,1,150 if request.form.get("deposit")=="1" else 0,datetime.now().isoformat(timespec="seconds")))
        flash("Driver record added.")
        return redirect(url_for("drivers"))
    with conn() as c:
        rows=c.execute("""SELECT d.*, (SELECT COUNT(*) FROM deliveries x WHERE x.driver_id=d.id AND x.settlement_id IS NULL) pending,
        (SELECT COALESCE(SUM(amount_due_office_mxn),0) FROM deliveries x WHERE x.driver_id=d.id AND x.settlement_id IS NULL) due FROM drivers d ORDER BY d.id DESC""").fetchall()
    body="""<div class="panel"><h2>Drivers</h2><form class="stack" method="post"><label>Name<input name="name" required></label><label>Phone<input name="phone"></label><label>Approval<select name="approved"><option value="0">Not approved</option><option value="1">Approved</option></select></label><label>Backpack deposit record<select name="deposit"><option value="0">Not received</option><option value="1">MX$150 received</option></select></label><button class="btn">Add driver</button></form></div>
    <div class="panel"><table><tr><th>Name</th><th>Phone</th><th>Approved</th><th>Backpack deposit (separate)</th><th>Unsettled deliveries</th><th>Office balance</th><th>Status</th></tr>{% for d in rows %}<tr><td>{{d.name}}</td><td>{{d.phone}}</td><td>{{'Yes' if d.approved else 'No'}}</td><td>MX${{d.backpack_deposit_mxn}}</td><td>{{d.pending}}</td><td>MX${{d.due}}</td><td>{{'Active' if d.active else 'Inactive'}}</td></tr>{% endfor %}</table><p class="small">The MX$150 backpack deposit is recorded separately and is not included in delivery cash or settlement calculations.</p></div>"""
    return page(body, rows=rows)

@app.route("/settlements", methods=["GET","POST"])
@require_test_login
def settlements():
    if request.method=="POST":
        did=int(request.form["driver_id"]); note=request.form.get("note","")
        with conn() as c:
            rows=c.execute("SELECT * FROM deliveries WHERE driver_id=? AND settlement_id IS NULL ORDER BY id",(did,)).fetchall()
            if not rows: flash("No unsettled deliveries for this driver.")
            else:
                amount=sum(r["amount_due_office_mxn"] for r in rows)
                sid=c.execute("INSERT INTO settlements(driver_id,delivery_count,amount_mxn,note,recorded_at) VALUES(?,?,?,?,?)",
                  (did,len(rows),amount,note,datetime.now().isoformat(timespec="seconds"))).lastrowid
                c.execute("UPDATE deliveries SET settlement_id=? WHERE driver_id=? AND settlement_id IS NULL",(sid,did))
                flash(f"Settlement #{sid} recorded: MX${amount}. Driver is eligible for new deliveries if approved and active.")
        return redirect(url_for("settlements"))
    with conn() as c:
        drivers=c.execute("SELECT * FROM drivers WHERE approved=1 AND active=1 ORDER BY name").fetchall()
        rows=c.execute("""SELECT s.*,d.name FROM settlements s JOIN drivers d ON d.id=s.driver_id ORDER BY s.id DESC""").fetchall()
        balances=c.execute("""SELECT d.id,d.name,COUNT(x.id) count,COALESCE(SUM(x.amount_due_office_mxn),0) due FROM drivers d LEFT JOIN deliveries x ON x.driver_id=d.id AND x.settlement_id IS NULL GROUP BY d.id ORDER BY d.name""").fetchall()
    body="""<div class="panel"><h2>Record cash settlement</h2><form class="stack" method="post"><label>Driver<select name="driver_id" required>{% for d in drivers %}<option value="{{d.id}}">{{d.name}}</option>{% endfor %}</select></label><label>Note / receipt reference<input name="note"></label><button class="btn">Record settlement of all outstanding deliveries</button></form><p class="small">This prototype records the settlement amount based on outstanding delivery records; verify that cash was physically received before submitting.</p></div>
    <div class="panel"><h2>Driver balances</h2><table><tr><th>Driver</th><th>Unsettled deliveries</th><th>Cash due to office</th></tr>{% for b in balances %}<tr><td>{{b.name}}</td><td>{{b.count}}</td><td>MX${{b.due}}</td></tr>{% endfor %}</table></div>
    <div class="panel"><h2>Settlement history</h2><table><tr><th>#</th><th>Driver</th><th>Deliveries</th><th>Amount</th><th>Recorded</th><th>Note</th></tr>{% for s in rows %}<tr><td>{{s.id}}</td><td>{{s.name}}</td><td>{{s.delivery_count}}</td><td>MX${{s.amount_mxn}}</td><td>{{s.recorded_at}}</td><td>{{s.note}}</td></tr>{% endfor %}</table></div>"""
    return page(body, drivers=drivers, rows=rows, balances=balances)

@app.route("/cards", methods=["GET","POST"])
@require_test_login
def cards():
    if request.method=="POST":
        with conn() as c:
            c.execute("INSERT INTO card_transactions(driver_id,order_id,card_label,amount_mxn,reference,recorded_at) VALUES(?,?,?,?,?,?)",
             (int(request.form["driver_id"]),int(request.form["order_id"]),request.form["card_label"],int(request.form["amount_mxn"]),request.form.get("reference",""),datetime.now().isoformat(timespec="seconds")))
        flash("Company-card transaction recorded.")
        return redirect(url_for("cards"))
    with conn() as c:
        drivers=c.execute("SELECT * FROM drivers ORDER BY name").fetchall()
        orders=c.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
        rows=c.execute("""SELECT x.*,d.name,o.ref FROM card_transactions x JOIN drivers d ON d.id=x.driver_id JOIN orders o ON o.id=x.order_id ORDER BY x.id DESC""").fetchall()
    body="""<div class="panel"><h2>Record company-card machine purchase</h2><form class="stack" method="post"><label>Driver<select name="driver_id" required>{% for d in drivers %}<option value="{{d.id}}">{{d.name}}</option>{% endfor %}</select></label><label>Order<select name="order_id" required>{% for o in orders %}<option value="{{o.id}}">{{o.ref}} — {{o.customer_name}}</option>{% endfor %}</select></label><label>Card label / last 4<input name="card_label" required></label><label>Amount MXN<input type="number" name="amount_mxn" min="1" required></label><label>Transaction reference<input name="reference"></label><button class="btn">Record purchase</button></form></div>
    <div class="panel"><h2>Card transaction ledger</h2><table><tr><th>Date</th><th>Driver</th><th>Order</th><th>Card</th><th>Amount</th><th>Reference</th></tr>{% for r in rows %}<tr><td>{{r.recorded_at}}</td><td>{{r.name}}</td><td>{{r.ref}}</td><td>{{r.card_label}}</td><td>MX${{r.amount_mxn}}</td><td>{{r.reference}}</td></tr>{% endfor %}</table></div>"""
    return page(body, drivers=drivers, orders=orders, rows=rows)

@app.route("/health")
def health():
    return {"status":"ok","mode":"test-only","whatsapp_connected":False}, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
