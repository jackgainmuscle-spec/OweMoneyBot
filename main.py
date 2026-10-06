from collections import defaultdict
import hashlib
import heapq
import hmac
import json
import os
from urllib.parse import parse_qsl

from flask import Flask, jsonify, render_template_string, request
import psycopg2
import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://owemoneybot.onrender.com")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ==========================================
# DATABASE INITIALIZATION & HELPERS
# ==========================================

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS debts (
                id SERIAL PRIMARY KEY,
                group_id BIGINT NOT NULL,
                debtor_id BIGINT NOT NULL,
                debtor_name TEXT NOT NULL,
                creditor_id BIGINT NOT NULL,
                creditor_name TEXT NOT NULL,
                amount NUMERIC(10, 2) DEFAULT 0,
                CONSTRAINT unique_debt UNIQUE (group_id, debtor_id, creditor_id)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_photos (
                user_id BIGINT PRIMARY KEY,
                file_id TEXT NOT NULL
            );
        """)
        # Table to track pending inline approvals
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_splits (
                id SERIAL PRIMARY KEY,
                group_id BIGINT NOT NULL,
                creditor_id BIGINT NOT NULL,
                creditor_name TEXT NOT NULL,
                debtor_id BIGINT NOT NULL,
                debtor_name TEXT NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'PENDING'
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"DB Init Error: {e}")

init_db()

# Register webhook with Telegram on launch
try:
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    print(f"Webhook set successfully to {WEBHOOK_URL}/{BOT_TOKEN}")
except Exception as e:
    print(f"Failed to set webhook: {e}")

# ==========================================
# ALGORITHM: DEBT SIMPLIFICATION
# ==========================================

def simplify_group_debts(group_id):
    """
    Fetches all pair-wise debts for a group, nets out balances,
    and runs a greedy max-heap algorithm to find minimal settlements.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT debtor_id, debtor_name, creditor_id, creditor_name, amount
        FROM debts WHERE group_id = %s AND amount > 0;
    """, (group_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return []

    # Map user IDs to names for clean outputs
    user_names = {}
    net_balances = defaultdict(float)

    for debtor_id, debtor_name, creditor_id, creditor_name, amount in rows:
        user_names[debtor_id] = debtor_name
        user_names[creditor_id] = creditor_name
        net_balances[debtor_id] -= float(amount)
        net_balances[creditor_id] += float(amount)

    debtors = []   # Max-heap for net debtors (stored as negative debt)
    creditors = [] # Max-heap for net creditors

    for u_id, net in net_balances.items():
        if net < -0.01:
            heapq.heappush(debtors, (net, u_id))  # net is negative
        elif net > 0.01:
            heapq.heappush(creditors, (-net, u_id)) # negate to behave as max-heap

    min_settlements = []

    while debtors and creditors:
        debt_val, debtor_id = heapq.heappop(debtors)
        credit_val, creditor_id = heapq.heappop(creditors)

        debt_amount = -debt_val
        credit_amount = -credit_val

        settle_amount = min(debt_amount, credit_amount)
        min_settlements.append({
            'debtor_id': debtor_id,
            'debtor_name': user_names.get(debtor_id, f"User {debtor_id}"),
            'creditor_id': creditor_id,
            'creditor_name': user_names.get(creditor_id, f"User {creditor_id}"),
            'amount': round(settle_amount, 2)
        })

        if debt_amount > credit_amount:
            heapq.heappush(debtors, (-(debt_amount - settle_amount), debtor_id))
        elif credit_amount > debt_amount:
            heapq.heappush(creditors, (-(credit_amount - settle_amount), creditor_id))

    return min_settlements

# ==========================================
# WEBHOOK & FLASK ROUTES
# ==========================================

@app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "OK", 200

@app.route('/')
def home():
    return "Bot web service is live!", 200

# Telegram Web App Authentication Check
def verify_telegram_webapp_data(init_data: str) -> dict | None:
    """Verifies Telegram WebApp initData HMAC-SHA256 signature."""
    try:
        parsed_data = dict(parse_qsl(init_data))
        if 'hash' not in parsed_data:
            return None
        
        hash_to_check = parsed_data.pop('hash')
        data_check_string = "\n".join([f"{k}={v}" for k, v in sorted(parsed_data.items())])
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash == hash_to_check:
            return json.loads(parsed_data.get('user', '{}'))
    except Exception as e:
        print(f"Auth verification error: {e}")
    return None

@app.route('/api/dashboard', methods=['GET'])
def api_dashboard():
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    user = verify_telegram_webapp_data(init_data)
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = user.get('id')
    conn = get_db()
    cur = conn.cursor()
    
    # Get debts user owes
    cur.execute("SELECT creditor_name, amount FROM debts WHERE debtor_id = %s AND amount > 0;", (user_id,))
    owing_rows = cur.fetchall()
    
    # Get debts owed to user
    cur.execute("SELECT debtor_name, amount FROM debts WHERE creditor_id = %s AND amount > 0;", (user_id,))
    owed_rows = cur.fetchall()
    
    cur.close()
    conn.close()

    total_owing = sum(float(a) for _, a in owing_rows)
    total_owed = sum(float(a) for _, a in owed_rows)

    return jsonify({
        'user_name': user.get('first_name', 'User'),
        'total_owing': total_owing,
        'total_owed': total_owed,
        'net_balance': total_owed - total_owing,
        'owing_details': [{'name': n, 'amount': float(a)} for n, a in owing_rows],
        'owed_details': [{'name': n, 'amount': float(a)} for n, a in owed_rows]
    })

@app.route('/app')
def render_webapp():
    """Serves the frontend Telegram Mini App UI."""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>OweMoney Dashboard</title>
      <script src="https://telegram.org/js/telegram-web-app.js"></script>
      <style>
        body {
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
          background-color: var(--tg-theme-bg-color, #f4f4f5);
          color: var(--tg-theme-text-color, #18181b);
          padding: 16px;
          margin: 0;
        }
        .card {
          background: var(--tg-theme-secondary-bg-color, #ffffff);
          border-radius: 12px;
          padding: 16px;
          margin-bottom: 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .stat-box { text-align: center; padding: 12px; border-radius: 8px; background: rgba(0,0,0,0.03); }
        .amount-green { color: #16a34a; font-weight: bold; font-size: 1.2rem; }
        .amount-red { color: #dc2626; font-weight: bold; font-size: 1.2rem; }
        ul { list-style: none; padding: 0; margin: 8px 0; }
        li { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid rgba(0,0,0,0.05); }
      </style>
    </head>
    <body>
      <h2>👋 Hello, <span id="username">User</span></h2>
      
      <div class="card">
        <div class="stat-grid">
          <div class="stat-box">
            <div>You Owe</div>
            <div id="total-owing" class="amount-red">$0.00</div>
          </div>
          <div class="stat-box">
            <div>Owed to You</div>
            <div id="total-owed" class="amount-green">$0.00</div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>💸 People You Owe</h3>
        <ul id="owing-list"><li>Loading...</li></ul>
      </div>

      <div class="card">
        <h3>💰 People Who Owe You</h3>
        <ul id="owed-list"><li>Loading...</li></ul>
      </div>

      <script>
        const tg = window.Telegram.WebApp;
        tg.ready();
        tg.expand();

        async function loadData() {
          try {
            const res = await fetch('/api/dashboard', {
              headers: { 'X-Telegram-Init-Data': tg.initData }
            });
            if (!res.ok) throw new Error("Unauthorized");
            const data = await res.json();
            
            document.getElementById('username').innerText = data.user_name;
            document.getElementById('total-owing').innerText = '$' + data.total_owing.toFixed(2);
            document.getElementById('total-owed').innerText = '$' + data.total_owed.toFixed(2);

            const owingList = document.getElementById('owing-list');
            owingList.innerHTML = data.owing_details.length ? '' : '<li>No active debts! 🎉</li>';
            data.owing_details.forEach(item => {
              owingList.innerHTML += `<li><span>${item.name}</span><span class="amount-red">$${item.amount.toFixed(2)}</span></li>`;
            });

            const owedList = document.getElementById('owed-list');
            owedList.innerHTML = data.owed_details.length ? '' : '<li>Nobody owes you right now.</li>';
            data.owed_details.forEach(item => {
              owedList.innerHTML += `<li><span>${item.name}</span><span class="amount-green">$${item.amount.toFixed(2)}</span></li>`;
            });
          } catch (e) {
            document.body.innerHTML = '<h3>Failed to load dashboard. Open via Telegram.</h3>';
          }
        }
        loadData();
      </script>
    </body>
    </html>
    """
    return render_template_string(html_content)

# ==========================================
# TELEGRAM BOT COMMAND HANDLERS
# ==========================================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "👋 **Debt Tracker Bot with Smart Simplification!**\n\n"
        "**Basic Commands:**\n"
        "• `/add <amount>` (in reply) — Adds money to someone's tab.\n"
        "• `/pay <amount>` (in reply) — Subtracts money paid back.\n"
        "• `/owe` — Shows everyone you owe.\n"
        "• `/owed` — Shows everyone who owes you.\n"
        "• `/attach` — Attach payment QR code photo.\n\n"
        "**New Smart Features:**\n"
        "• `/split <amount> <description>` (in reply) — Create interactive split bill card.\n"
        "• `/simplify` — Calculates minimum transactions to settle group debts!\n"
        "• `/dashboard` — Opens your interactive Web Mini App dashboard."
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['dashboard'])
def open_dashboard(message):
    """Generates inline button that opens the Telegram Mini App."""
    markup = InlineKeyboardMarkup()
    btn = InlineKeyboardButton(text="📊 Open Balance Dashboard", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/app"))
    markup.add(btn)
    bot.reply_to(message, "Click below to view your full group balance visual dashboard:", reply_markup=markup)

@bot.message_handler(commands=['simplify'])
def handle_simplify(message):
    """Runs debt minimization on the group chat."""
    if message.chat.type == 'private':
        bot.reply_to(message, "Debt simplification works inside group chats!")
        return

    settlements = simplify_group_debts(message.chat.id)
    if not settlements:
        bot.reply_to(message, "🟢 **Group is clean!** No debts need settlement.", parse_mode="Markdown")
        return

    text = "🔄 **Simplified Group Settlement Strategy:**\n"
    text += "_Instead of multiple transfers, make these direct payments:_\n\n"
    for s in settlements:
        text += f"• **{s['debtor_name']}** pays **{s['creditor_name']}**: `${s['amount']:.2f}`\n"

    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['split'])
def create_split_request(message):
    """Creates an interactive split request with Accept/Dispute inline buttons."""
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 2 or not message.reply_to_message:
            bot.reply_to(message, "Reply to the person you want to split with:\n`/split <amount> [description]`", parse_mode="Markdown")
            return

        amount = float(parts[1])
        description = parts[2] if len(parts) > 2 else "Shared Expense"
        
        creditor = message.from_user
        debtor = message.reply_to_message.from_user

        if debtor.id == creditor.id:
            bot.reply_to(message, "You cannot split a bill with yourself!")
            return

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pending_splits (group_id, creditor_id, creditor_name, debtor_id, debtor_name, amount, description)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;
        """, (message.chat.id, creditor.id, creditor.first_name, debtor.id, debtor.first_name, amount, description))
        split_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        markup = InlineKeyboardMarkup()
        btn_accept = InlineKeyboardButton("✅ Accept ($%.2f)" % amount, callback_data=f"accept_split_{split_id}")
        btn_dispute = InlineKeyboardButton("❌ Dispute", callback_data=f"dispute_split_{split_id}")
        markup.add(btn_accept, btn_dispute)

        msg_text = (
            f"🧾 **Split Bill Request**\n"
            f"• **From:** {creditor.first_name}\n"
            f"• **For:** {debtor.first_name}\n"
            f"• **Amount:** `${amount:.2f}`\n"
            f"• **Reason:** {description}\n\n"
            f"_{debtor.first_name}, please confirm or dispute below:_"
        )
        bot.send_message(message.chat.id, msg_text, reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        print(f"Split error: {e}")
        bot.reply_to(message, "Error creating split request.")

@bot.callback_query_handler(func=lambda call: call.data.startswith(('accept_split_', 'dispute_split_')))
def handle_split_callback(call):
    """Processes inline keyboard presses with authorization checks."""
    try:
        action, _, split_id_str = call.data.partition('_split_')
        split_id = int(split_id_str)

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT group_id, creditor_id, creditor_name, debtor_id, debtor_name, amount, description, status
            FROM pending_splits WHERE id = %s;
        """, (split_id,))
        row = cur.fetchone()

        if not row:
            bot.answer_callback_query(call.id, "Split request not found.", show_alert=True)
            cur.close()
            conn.close()
            return

        group_id, creditor_id, creditor_name, debtor_id, debtor_name, amount, description, status = row

        # Security verification: Only assigned debtor can click!
        if call.from_user.id != debtor_id:
            bot.answer_callback_query(call.id, "⛔ This split button is for %s only!" % debtor_name, show_alert=True)
            cur.close()
            conn.close()
            return

        if status != 'PENDING':
            bot.answer_callback_query(call.id, "This split request has already been processed.", show_alert=True)
            cur.close()
            conn.close()
            return

        if action == 'accept':
            # Update debts table
            cur.execute("""
                INSERT INTO debts (group_id, debtor_id, debtor_name, creditor_id, creditor_name, amount)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (group_id, debtor_id, creditor_id)
                DO UPDATE SET amount = debts.amount + EXCLUDED.amount;
            """, (group_id, debtor_id, debtor_name, creditor_id, creditor_name, amount))
            
            cur.execute("UPDATE pending_splits SET status = 'ACCEPTED' WHERE id = %s;", (split_id,))
            conn.commit()

            bot.answer_callback_query(call.id, "Split approved!")
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"✅ **Split Approved!**\n{debtor_name} accepted `${amount:.2f}` tab for '{description}' from {creditor_name}.",
                parse_mode="Markdown"
            )

        elif action == 'dispute':
            cur.execute("UPDATE pending_splits SET status = 'DISPUTED' WHERE id = %s;", (split_id,))
            conn.commit()

            bot.answer_callback_query(call.id, "Split disputed.")
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"❌ **Split Disputed!**\n{debtor_name} rejected the `${amount:.2f}` request from {creditor_name}.",
                parse_mode="Markdown"
            )

        cur.close()
        conn.close()

    except Exception as e:
        print(f"Callback error: {e}")
        bot.answer_callback_query(call.id, "Error handling action.")

# ==========================================
# STANDARD DEBT COMMANDS (PRESERVED)
# ==========================================

@bot.message_handler(commands=['owed'])
def check_who_owes_me(message):
    try:
        user_id = message.from_user.id
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT debtor_name, amount FROM debts 
            WHERE group_id = %s AND creditor_id = %s AND amount > 0;
        """, (message.chat.id, user_id))
        rows = cur.fetchall()
        cur.execute("SELECT file_id FROM user_photos WHERE user_id = %s;", (user_id,))
        photo_row = cur.fetchone()
        cur.close()
        conn.close()

        if not rows:
            bot.reply_to(message, "Nobody owes you money right now! 🟢")
            return

        total_owed = sum(float(amount) for _, amount in rows)
        response = "💰 **People who owe you:**\n"
        for debtor_name, amount in rows:
            response += f"• {debtor_name}: ${float(amount):.2f}\n"
        response += f"\n**Total expected back:** ${total_owed:.2f}"
        
        if photo_row:
            try:
                if len(response) <= 1024:
                    bot.send_photo(message.chat.id, photo_row[0], caption=response, parse_mode="Markdown")
                else:
                    bot.send_photo(message.chat.id, photo_row[0])
                    bot.reply_to(message, response, parse_mode="Markdown")
                return
            except Exception as e:
                print(f"Photo error: {e}")
        bot.reply_to(message, response, parse_mode="Markdown")
    except Exception as e:
        print(f"Owed error: {e}")
        bot.reply_to(message, "Error checking who owes you.")

@bot.message_handler(commands=['owe'])
def check_owe(message):
    try:
        user_id = message.from_user.id
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT creditor_name, amount FROM debts 
            WHERE group_id = %s AND debtor_id = %s AND amount > 0;
        """, (message.chat.id, user_id))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            bot.reply_to(message, "🎉 You don't owe anyone anything!")
            return

        response = "👇 **What you currently owe:**\n"
        for creditor_name, amount in rows:
            response += f"• {creditor_name}: ${float(amount):.2f}\n"
        
        bot.reply_to(message, response, parse_mode="Markdown")
    except Exception as e:
        print(f"Owe error: {e}")
        bot.reply_to(message, "Error checking debts.")

@bot.message_handler(commands=['add'])
def add_debt(message):
    try:
        parts = message.text.split()
        if len(parts) < 2 or not message.reply_to_message:
            bot.reply_to(message, "Reply to the person who owes you with: `/add <amount>`", parse_mode="Markdown")
            return

        amount = float(parts[1])
        debtor = message.reply_to_message.from_user
        creditor = message.from_user

        if debtor.id == creditor.id:
            bot.reply_to(message, "You can't owe yourself!")
            return

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT amount FROM debts 
            WHERE group_id = %s AND debtor_id = %s AND creditor_id = %s;
        """, (message.chat.id, creditor.id, debtor.id))
        opposite_row = cur.fetchone()

        if opposite_row and opposite_row[0] > 0:
            opposite_amount = float(opposite_row[0])
            if amount <= opposite_amount:
                cur.execute("""
                    UPDATE debts SET amount = amount - %s
                    WHERE group_id = %s AND debtor_id = %s AND creditor_id = %s;
                """, (amount, message.chat.id, creditor.id, debtor.id))
            else:
                remaining_add = amount - opposite_amount
                cur.execute("""
                    UPDATE debts SET amount = 0
                    WHERE group_id = %s AND debtor_id = %s AND creditor_id = %s;
                """, (message.chat.id, creditor.id, debtor.id))

                cur.execute("""
                    INSERT INTO debts (group_id, debtor_id, debtor_name, creditor_id, creditor_name, amount)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (group_id, debtor_id, creditor_id)
                    DO UPDATE SET amount = debts.amount + EXCLUDED.amount;
                """, (message.chat.id, debtor.id, debtor.first_name, creditor.id, creditor.first_name, remaining_add))
        else:
            cur.execute("""
                INSERT INTO debts (group_id, debtor_id, debtor_name, creditor_id, creditor_name, amount)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (group_id, debtor_id, creditor_id)
                DO UPDATE SET amount = debts.amount + EXCLUDED.amount;
            """, (message.chat.id, debtor.id, debtor.first_name, creditor.id, creditor.first_name, amount))

        conn.commit()
        cur.close()
        conn.close()

        bot.reply_to(message, f"Updated tab! Net balance recalculated for {debtor.first_name} and {creditor.first_name}.")
    except Exception as e:
        print(f"Add error: {e}")
        bot.reply_to(message, "Error adding debt.")

@bot.message_handler(commands=['pay'])
def pay_debt(message):
    try:
        parts = message.text.split()
        if len(parts) < 2 or not message.reply_to_message:
            bot.reply_to(message, "Reply to the person you paid with: `/pay <amount>`", parse_mode="Markdown")
            return

        amount = float(parts[1])
        debtor = message.from_user
        creditor = message.reply_to_message.from_user

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            UPDATE debts 
            SET amount = GREATEST(0, amount - %s)
            WHERE group_id = %s AND debtor_id = %s AND creditor_id = %s;
        """, (amount, message.chat.id, debtor.id, creditor.id))
        conn.commit()
        cur.close()
        conn.close()

        bot.send_message(
            message.chat.id, 
            f"🔔 **Payment Alert:** {debtor.first_name} paid back `${amount:.2f}` to {creditor.first_name}!",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Pay error: {e}")
        bot.reply_to(message, "Error recording payment.")

@bot.message_handler(content_types=['photo', 'text'],
                     func=lambda m: ((m.caption or m.text or "").split() or [""])[0].split('@')[0] == '/attach')
def attach_photo(message):
    try:
        photo = message.photo
        if not photo and message.reply_to_message:
            photo = message.reply_to_message.photo
        if not photo:
            bot.reply_to(message, "Send a photo with `/attach` as caption, or reply to a photo with `/attach`.", parse_mode="Markdown")
            return

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_photos (user_id, file_id) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET file_id = EXCLUDED.file_id;
        """, (message.from_user.id, photo[-1].file_id))
        conn.commit()
        cur.close()
        conn.close()

        bot.reply_to(message, "Photo attached! It will show when you use /owed. 📎")
    except Exception as e:
        print(f"Attach error: {e}")
        bot.reply_to(message, "Error attaching photo.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
