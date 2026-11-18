import os, json, hmac, hashlib, sqlite3, time, threading
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "gold_miner.db")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in your hosting environment.")
if not WEBAPP_URL.startswith("https://"):
    raise RuntimeError("Set WEBAPP_URL to your public HTTPS Mini App URL.")

app = Flask(__name__)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER NOT NULL DEFAULT 0,
            wallet TEXT DEFAULT '', referred_by INTEGER, created_at INTEGER NOT NULL,
            last_tap INTEGER DEFAULT 0, taps INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount_ton REAL NOT NULL, wallet TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL)""")

def validate_init_data(init_data):
    if not init_data:
        return None
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        return None
    data_check = "\\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None
    try:
        user = json.loads(values.get("user", "{}"))
        auth_date = int(values.get("auth_date", "0"))
        if time.time() - auth_date > 86400:
            return None
        return user
    except Exception:
        return None

def authorized_user():
    user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user or not user.get("id"):
        return None
    return user

@app.get("/")
def home():
    return app.send_static_file("index.html")

@app.get("/api/me")
def me():
    user = authorized_user()
    if not user:
        return jsonify(error="Telegram authentication failed"), 401
    uid = int(user["id"])
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            c.execute("INSERT INTO users(user_id,username,created_at) VALUES(?,?,?)",
                      (uid, user.get("username",""), int(time.time())))
            row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return jsonify(balance=row["balance"], wallet=row["wallet"] or "", user=user.get("first_name","Player"))

@app.post("/api/tap")
def tap():
    user = authorized_user()
    if not user:
        return jsonify(error="Unauthorized"), 401
    uid, now = int(user["id"]), int(time.time())
    with db() as c:
        row = c.execute("SELECT last_tap,balance,taps FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            c.execute("INSERT INTO users(user_id,username,created_at) VALUES(?,?,?)",
                      (uid, user.get("username",""), now))
            row = {"last_tap": 0, "balance": 0, "taps": 0}
        if now - row["last_tap"] < 1:
            return jsonify(error="Please wait a moment"), 429
        c.execute("UPDATE users SET balance=balance+1,last_tap=?,taps=taps+1 WHERE user_id=?",
                  (now, uid))
        balance = row["balance"] + 1
    return jsonify(balance=balance, earned=1)

@app.post("/api/wallet")
def wallet():
    user = authorized_user()
    if not user:
        return jsonify(error="Unauthorized"), 401
    address = (request.json or {}).get("wallet", "").strip()
    if len(address) < 20 or len(address) > 120:
        return jsonify(error="Enter a valid TON wallet address"), 400
    with db() as c:
        c.execute("UPDATE users SET wallet=? WHERE user_id=?", (address, int(user["id"])))
    return jsonify(ok=True)

@app.post("/api/withdraw")
def withdraw():
    user = authorized_user()
    if not user:
        return jsonify(error="Unauthorized"), 401
    data = request.json or {}
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify(error="Invalid amount"), 400
    uid = int(user["id"])
    with db() as c:
        row = c.execute("SELECT balance,wallet FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row or not row["wallet"]:
            return jsonify(error="Save your TON wallet first"), 400
        # This demo stores a request only. It does not transfer TON.
        if amount * 1000 > row["balance"]:
            return jsonify(error="Not enough demo points"), 400
        c.execute("INSERT INTO withdrawals(user_id,amount_ton,wallet,created_at) VALUES(?,?,?,?)",
                  (uid, amount, row["wallet"], int(time.time())))
    return jsonify(ok=True, message="Request received. This is not an on-chain payment.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0].startswith("ref_"):
        try:
            ref_id = int(args[0][4:])
            uid = update.effective_user.id
            if ref_id != uid:
                with db() as c:
                    cur = c.execute("INSERT OR IGNORE INTO users(user_id,username,created_at,referred_by) VALUES(?,?,?,?)",
                                    (uid, update.effective_user.username or "", int(time.time()), ref_id))
                    if cur.rowcount == 1:
                        c.execute("INSERT OR IGNORE INTO users(user_id,username,created_at) VALUES(?,?,?)",
                                  (ref_id, "", int(time.time())))
                        c.execute("UPDATE users SET balance=balance+10 WHERE user_id=?", (ref_id,))
        except ValueError:
            pass
    keyboard = [[InlineKeyboardButton("⛏ Open GOLD MINER", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        "Welcome to GOLD MINER!\\nOpen the Mini App to collect demo points. Points are not TON and have no cash value.",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    await update.message.reply_text(f"Demo points: {row['balance'] if row else 0}")

def run_bot():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance_cmd))
    application.run_polling()

if __name__ == "__main__":
    init_db()
    # Run the HTTPS-facing web service and Telegram bot in the same process.
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), use_reloader=False),
        daemon=True
    ).start()
    run_bot()
