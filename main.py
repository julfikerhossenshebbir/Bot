# main.py
import os
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
import cloudflare
from datetime import datetime
from dotenv import load_dotenv

# Configuration
CF_API_TOKEN = os.getenv('CLOUDFLARE_API_TOKEN')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS').split(',')]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Database setup
conn = sqlite3.connect('subdomains.db')
c = conn.cursor()

# Create tables
c.execute('''CREATE TABLE IF NOT EXISTS users (
             user_id INTEGER PRIMARY KEY,
             status TEXT DEFAULT 'pending')''')

c.execute('''CREATE TABLE IF NOT EXISTS subdomains (
             subdomain TEXT,
             domain TEXT,
             user_id INTEGER,
             PRIMARY KEY (subdomain, domain))''')

c.execute('''CREATE TABLE IF NOT EXISTS logs (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             user_id INTEGER,
             activity TEXT,
             timestamp DATETIME)''')

conn.commit()

# ------------ States ------------
class SubdomainForm(StatesGroup):
    select_domain = State()
    enter_subdomain = State()
    confirm_delete = State()

# ------------ Middleware ------------
@dp.update.outer_middleware()
async def auth_middleware(handler, event, data):
    user_id = event.from_user.id
    c.execute('SELECT status FROM users WHERE user_id=?', (user_id,))
    result = c.fetchone()

    if not result:
        c.execute('INSERT INTO users (user_id) VALUES (?)', (user_id,))
        conn.commit()
        await bot.send_message(ADMIN_IDS[0], f"New user request:\nUser ID: {user_id}")
        await event.answer("⏳ Your account is pending approval. Admins have been notified.")
        return

    if result[0] != 'approved':
        await event.answer("🔒 Your account is not approved yet.")
        return

    return await handler(event, data)

# ------------ Handlers ------------
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "🌐 Welcome to Free Subdomain Manager!\n"
        "Available Commands:\n"
        "/new - Claim a new subdomain\n"
        "/delete - Remove your subdomain\n"
        "/list - View your subdomains"
    )

# ------------ Subdomain Creation ------------
@dp.message(Command("new"))
async def select_domain(message: types.Message, state: FSMContext):
    domains = cloudflare.zones.get()
    keyboard = InlineKeyboardBuilder()

    for domain in domains:
        keyboard.button(
            text=domain['name'],
            callback_data=f"domain_{domain['id']}"
        )

    await message.answer(
        "Select your domain:",
        reply_markup=keyboard.adjust(2).as_markup()
    )
    await state.set_state(SubdomainForm.select_domain)

@dp.callback_query(F.data.startswith("domain_"))
async def enter_subdomain(callback: types.CallbackQuery, state: FSMContext):
    zone_id = callback.data.split('_')[1]
    domain = next(z['name'] for z in cf.zones.get() if z['id'] == zone_id)

    await state.update_data(zone_id=zone_id, domain=domain)
    await callback.message.answer("Enter your desired subdomain name (e.g., 'blog'):")
    await state.set_state(SubdomainForm.enter_subdomain)

@dp.message(SubdomainForm.enter_subdomain)
async def create_subdomain(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    subdomain = message.text.strip().lower()
    data = await state.get_data()
    domain = data['domain']
    full_domain = f"{subdomain}.{domain}"

    # Check availability
    c.execute('''SELECT * FROM subdomains
                 WHERE subdomain=? AND domain=?''', (subdomain, domain))
    if c.fetchone():
        await message.answer("❌ This subdomain is already taken!")
        return

    try:
        # Create DNS record
        cf.zones.dns_records.post(data['zone_id'], data={
            'type': 'CNAME',
            'name': full_domain,
            'content': 'your-target-server.com',  # Set your target
            'ttl': 300
        })

        # Reserve subdomain
        c.execute('''INSERT INTO subdomains
                     VALUES (?, ?, ?)''', (subdomain, domain, user_id))
        conn.commit()

        await message.answer(f"✅ Success! Your subdomain is ready:\n{full_domain}")
        await log_activity(user_id, f"Created {full_domain}")

    except Exception as e:
        await message.answer(f"❌ Error: {str(e)}")

    await state.clear()

# ------------ Subdomain Deletion ------------
@dp.message(Command("delete"))
async def delete_subdomain(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    c.execute('''SELECT subdomain, domain
                 FROM subdomains
                 WHERE user_id=?''', (user_id,))
    subs = c.fetchall()

    if not subs:
        await message.answer("ℹ️ You don't have any active subdomains")
        return

    keyboard = InlineKeyboardBuilder()
    for sub, domain in subs:
        keyboard.button(
            text=f"{sub}.{domain}",
            callback_data=f"delete_{sub}_{domain}"
        )

    await message.answer(
        "Select subdomain to delete:",
        reply_markup=keyboard.adjust(1).as_markup()
    )
    await state.set_state(SubdomainForm.confirm_delete)

@dp.callback_query(F.data.startswith("delete_"))
async def confirm_delete(callback: types.CallbackQuery, state: FSMContext):
    _, sub, domain = callback.data.split('_')
    user_id = callback.from_user.id

    # Verify ownership
    c.execute('''DELETE FROM subdomains
                 WHERE subdomain=? AND domain=? AND user_id=?''',
              (sub, domain, user_id))

    if c.rowcount == 0:
        await callback.answer("🚫 Subdomain not found!")
        return

    # Delete DNS record
    zone_id = next(z['id'] for z in cf.zones.get() if z['name'] == domain)
    records = cf.zones.dns_records.get(zone_id, params={'name': f"{sub}.{domain}"})
    if records:
        cf.zones.dns_records.delete(zone_id, records[0]['id'])

    conn.commit()
    await callback.message.answer(f"🗑️ Successfully deleted: {sub}.{domain}")
    await log_activity(user_id, f"Deleted {sub}.{domain}")

# ------------ Admin Commands ------------
@dp.message(Command("approve"))
async def approve_user(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    try:
        user_id = int(message.text.split()[1])
        c.execute('UPDATE users SET status="approved" WHERE user_id=?', (user_id,))
        conn.commit()

        await message.answer(f"✅ User {user_id} approved")
        await bot.send_message(user_id, "🎉 Your account has been approved!")
    except:
        await message.answer("Invalid format. Use /approve USER_ID")

# ------------ Helpers ------------
async def log_activity(user_id: int, action: str):
    c.execute('''INSERT INTO logs (user_id, activity, timestamp)
                 VALUES (?, ?, ?)''',
              (user_id, action, datetime.now()))
    conn.commit()

if __name__ == "__main__":
    dp.run_polling(bot)