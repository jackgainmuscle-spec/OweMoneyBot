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

    debtors = []   # Max-heap for net debtors (
