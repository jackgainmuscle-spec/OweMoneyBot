import os
from flask import Flask, request
import psycopg2
import telebot

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://owemoneybot.onrender.com")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Register webhook with Telegram on launch
try:
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    print(f"Webhook set successfully to {WEBHOOK_URL}/{BOT_TOKEN}")
except Exception as e:
    print(f"Failed to set webhook: {e}")

@app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "OK", 200

@app.route('/')
def home():
    return "Bot web service is live!", 200

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "👋 **Debt Tracker Bot is active!**\n\n"
        "Here's how to use me:\n"
        "• Reply to someone with `/add 15` — Adds $15 to their tab for you.\n"
        "• Reply to someone with `/pay 15` — Subtracts $15 from what you owe them.\n"
        "• Type `/owe` — Shows everyone you currently owe.\n"
        "• Type `/owed` — Shows everyone who owes you money."
    )
    bot.reply_to(message, text, parse_mode="Markdown")

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
        cur.close()
        conn.close()

        if not rows:
            bot.reply_to(message, "Nobody owes you money right now! 🟢")
            return

        total_owed = sum(amount for _, amount in rows)
        response = "💰 **People who owe you:**\n"
        for debtor_name, amount in rows:
            response += f"• {debtor_name}: ${amount:.2f}\n"
        
        response += f"\n**Total expected back:** ${total_owed:.2f}"
        
        bot.reply_to(message, response, parse_mode="Markdown")
    except Exception as e:
        print(f"Owed error: {e}")
        bot.reply_to(message, "Error checking who owes you.")

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
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"DB Init Error: {e}")

# Call table creator on app startup
init_db()

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
            INSERT INTO debts (group_id, debtor_id, debtor_name, creditor_id, creditor_name, amount)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (group_id, debtor_id, creditor_id)
            DO UPDATE SET amount = debts.amount + EXCLUDED.amount;
        """, (message.chat.id, debtor.id, debtor.first_name, creditor.id, creditor.first_name, amount))
        conn.commit()
        cur.close()
        conn.close()

        bot.reply_to(message, f"Added ${amount:.2f} to {debtor.first_name}'s tab for {creditor.first_name}.")
    except Exception as e:
        print(f"Add error: {e}")
        bot.reply_to(message, "Error. Reply to their message and write: `/add 15.50`", parse_mode="Markdown")

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
            f"🔔 **Payment Alert:** {debtor.first_name} paid back ${amount:.2f} to {creditor.first_name}!",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Pay error: {e}")
        bot.reply_to(message, "Error recording payment.")

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
            response += f"• {creditor_name}: ${amount:.2f}\n"
        
        bot.reply_to(message, response, parse_mode="Markdown")
    except Exception as e:
        print(f"Owe error: {e}")
        bot.reply_to(message, "Error checking debts.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
