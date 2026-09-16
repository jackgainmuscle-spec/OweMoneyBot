import os
import time
import threading
from flask import Flask
import psycopg2
import telebot
from telebot.apihelper import ApiTelegramException

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "👋 **Debt Tracker Bot is active!**\n\n"
        "Here's how to use me:\n"
        "• Reply to someone with `/add 15` — Adds $15 to their tab for you.\n"
        "• Reply to someone with `/pay 15` — Subtracts $15 from what you owe them.\n"
        "• Type `/owe` — Shows everyone you currently owe."
    )
    bot.reply_to(message, text, parse_mode="Markdown")

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

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

def run_bot():
    print("Starting Telegram polling thread...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except ApiTelegramException as e:
            if e.error_code == 409:
                print("409 Conflict detected (previous container shutting down). Waiting 10s...")
                time.sleep(10)
            else:
                print(f"Telegram API Error: {e}")
                time.sleep(5)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
