import os
import json
import requests
import time
import random
import string
import asyncio
import aiohttp
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes,
    ConversationHandler
)
from telegram.constants import ParseMode
import urllib3
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ============= KONFIGÜRASYON =============
BOT_TOKEN = "8928323846:AAG6Va41KbFL82MxWHq2Jnqt8NInB3ysxRA"
ADMIN_IDS = [8610336203, 8928323846]  # Admin ID'leri
OWNER_ID = 8610336203  # Ana sahip
SUPPORT_IDS = [8610336203]  # Destek ekibi

API_URL = "https://yartyccfurry.onrender.com"
CHANNEL_USERNAME = "@yartyccfurry"  # Zorunlu kanal
DAILY_LIMIT = 5  # Günlük hak
PREMIUM_LIMIT = 100  # Premium günlük hak

# Veritabanı
DB_NAME = "bot_data.db"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============= VERİTABANI =============
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()
    
    def create_tables(self):
        # Kullanıcılar tablosu
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                join_date TEXT,
                is_premium INTEGER DEFAULT 0,
                premium_expiry TEXT,
                is_banned INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                is_support INTEGER DEFAULT 0,
                total_checks INTEGER DEFAULT 0,
                daily_checks INTEGER DEFAULT 0,
                last_check_date TEXT,
                referred_by INTEGER DEFAULT 0,
                refer_count INTEGER DEFAULT 0
            )
        ''')
        
        # Günlük kullanım tablosu
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER,
                date TEXT,
                checks INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        ''')
        
        # Kart sonuçları tablosu
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS card_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                card_number TEXT,
                card_month TEXT,
                card_year TEXT,
                card_cvv TEXT,
                status TEXT,
                gateway TEXT,
                message TEXT,
                check_date TEXT
            )
        ''')
        
        # Bildirimler tablosu
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT,
                date TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        # Proxy tablosu
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proxy TEXT,
                type TEXT,
                is_active INTEGER DEFAULT 1,
                last_used TEXT,
                fail_count INTEGER DEFAULT 0
            )
        ''')
        
        self.conn.commit()
    
    def add_user(self, user_id, username, first_name, last_name):
        self.cursor.execute('''
            INSERT OR IGNORE INTO users 
            (user_id, username, first_name, last_name, join_date, total_checks, daily_checks)
            VALUES (?, ?, ?, ?, ?, 0, 0)
        ''', (user_id, username or "", first_name or "", last_name or "", datetime.now().isoformat()))
        self.conn.commit()
    
    def get_user(self, user_id):
        self.cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        return self.cursor.fetchone()
    
    def update_user(self, user_id, **kwargs):
        for key, value in kwargs.items():
            self.cursor.execute(f'UPDATE users SET {key} = ? WHERE user_id = ?', (value, user_id))
        self.conn.commit()
    
    def get_daily_checks(self, user_id):
        today = datetime.now().date().isoformat()
        self.cursor.execute('''
            SELECT checks FROM daily_usage 
            WHERE user_id = ? AND date = ?
        ''', (user_id, today))
        result = self.cursor.fetchone()
        return result[0] if result else 0
    
    def add_daily_check(self, user_id):
        today = datetime.now().date().isoformat()
        self.cursor.execute('''
            INSERT INTO daily_usage (user_id, date, checks) 
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, date) DO UPDATE SET checks = checks + 1
        ''', (user_id, today))
        self.conn.commit()
    
    def get_remaining_checks(self, user_id):
        user = self.get_user(user_id)
        if not user:
            return 0
        
        # Admin ve premium kontrol
        if user[7] == 1:  # is_admin
            return 999999
        if user[5] == 1:  # is_premium
            expiry = user[6]
            if expiry and datetime.now().isoformat() < expiry:
                return PREMIUM_LIMIT - self.get_daily_checks(user_id)
        
        # Normal kullanıcı
        return DAILY_LIMIT - self.get_daily_checks(user_id)
    
    def add_card_result(self, user_id, card, status, gateway, message):
        self.cursor.execute('''
            INSERT INTO card_results 
            (user_id, card_number, card_month, card_year, card_cvv, status, gateway, message, check_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            card.get('number', ''),
            card.get('month', ''),
            card.get('year', ''),
            card.get('cvv', ''),
            status,
            gateway,
            message[:500],
            datetime.now().isoformat()
        ))
        self.conn.commit()
        
        # Toplam kontrol sayısını güncelle
        self.cursor.execute('''
            UPDATE users SET total_checks = total_checks + 1 
            WHERE user_id = ?
        ''', (user_id,))
        self.conn.commit()
    
    def get_user_stats(self, user_id):
        self.cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as live,
                SUM(CASE WHEN status = 'declined' THEN 1 ELSE 0 END) as dead
            FROM card_results 
            WHERE user_id = ?
        ''', (user_id,))
        return self.cursor.fetchone()
    
    def get_all_users(self):
        self.cursor.execute('SELECT user_id, username, first_name, last_name, is_premium, is_banned FROM users')
        return self.cursor.fetchall()
    
    def get_banned_users(self):
        self.cursor.execute('SELECT user_id FROM users WHERE is_banned = 1')
        return [row[0] for row in self.cursor.fetchall()]
    
    def add_notification(self, message):
        self.cursor.execute('''
            INSERT INTO notifications (message, date) VALUES (?, ?)
        ''', (message, datetime.now().isoformat()))
        self.conn.commit()
    
    def get_active_notifications(self):
        self.cursor.execute('SELECT id, message FROM notifications WHERE is_active = 1 ORDER BY id DESC LIMIT 5')
        return self.cursor.fetchall()
    
    def deactivate_notification(self, notif_id):
        self.cursor.execute('UPDATE notifications SET is_active = 0 WHERE id = ?', (notif_id,))
        self.conn.commit()

# ============= PROXY YÖNETİCİ =============
class ProxyManager:
    def __init__(self):
        self.db = Database()
        self.proxies = []
        self.current_index = 0
        self.load_proxies()
    
    def load_proxies(self):
        self.db.cursor.execute('SELECT id, proxy, type FROM proxies WHERE is_active = 1')
        self.proxies = self.db.cursor.fetchall()
        logger.info(f"{len(self.proxies)} proxy yüklendi")
    
    def get_proxy(self):
        if not self.proxies:
            return None
        
        # Döngüsel proxy seçimi
        proxy = self.proxies[self.current_index % len(self.proxies)]
        self.current_index += 1
        
        # Proxy'yi kullanıldı olarak işaretle
        self.db.cursor.execute('''
            UPDATE proxies SET last_used = ? WHERE id = ?
        ''', (datetime.now().isoformat(), proxy[0]))
        self.db.conn.commit()
        
        return {"http": proxy[1], "https": proxy[1]}
    
    def add_proxy(self, proxy, proxy_type="http"):
        self.db.cursor.execute('''
            INSERT INTO proxies (proxy, type) VALUES (?, ?)
        ''', (proxy, proxy_type))
        self.db.conn.commit()
        self.load_proxies()
        return True
    
    def remove_proxy(self, proxy_id):
        self.db.cursor.execute('UPDATE proxies SET is_active = 0 WHERE id = ?', (proxy_id,))
        self.db.conn.commit()
        self.load_proxies()
        return True
    
    def mark_fail(self, proxy_id):
        self.db.cursor.execute('''
            UPDATE proxies SET fail_count = fail_count + 1 
            WHERE id = ?
        ''', (proxy_id,))
        self.db.conn.commit()
        
        # 3 başarısız denemeden sonra proxy'yi pasif yap
        self.db.cursor.execute('''
            UPDATE proxies SET is_active = 0 
            WHERE id = ? AND fail_count >= 3
        ''', (proxy_id,))
        self.db.conn.commit()
        self.load_proxies()

# ============= API İSTEK =============
class APIClient:
    def __init__(self):
        self.session = requests.Session()
        self.proxy_manager = ProxyManager()
        self.setup_session()
    
    def setup_session(self):
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def make_request(self, endpoint, data=None, method="GET"):
        url = f"{API_URL}{endpoint}"
        max_attempts = 3
        
        for attempt in range(max_attempts):
            try:
                proxy = self.proxy_manager.get_proxy()
                
                if method == "GET":
                    response = self.session.get(url, proxies=proxy, timeout=30)
                else:
                    response = self.session.post(url, json=data, proxies=proxy, timeout=30)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    time.sleep(5)
                    continue
                else:
                    if proxy:
                        # Proxy'yi pasif yap
                        self.proxy_manager.mark_fail(proxy.get('id', 0))
                    continue
                    
            except Exception as e:
                logger.error(f"İstek hatası: {e}")
                time.sleep(2)
                continue
        
        return None

# ============= ANA BOT =============
class SuperCardBot:
    def __init__(self):
        self.db = Database()
        self.api = APIClient()
        self.app = None
        self.proxy_manager = ProxyManager()
        
        # Conversation states
        self.ADMIN_MESSAGE, self.REPLY_MESSAGE, self.ADD_PROXY, self.REMOVE_PROXY = range(4)
        self.ADD_CARD, self.CHECK_CARD, self.MULTI_CHECK = range(10, 13)
    
    # ============= KONTROL FONKSİYONLARI =============
    async def is_admin(self, user_id):
        user = self.db.get_user(user_id)
        return user and (user[7] == 1 or user_id in ADMIN_IDS)
    
    async def is_banned(self, user_id):
        user = self.db.get_user(user_id)
        return user and user[8] == 1
    
    async def check_channel_member(self, user_id):
        try:
            member = await self.app.bot.get_chat_member(CHANNEL_USERNAME, user_id)
            return member.status in ['member', 'administrator', 'creator']
        except:
            return False
    
    # ============= KOMUTLAR =============
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        
        # Kullanıcıyı veritabanına ekle
        self.db.add_user(user_id, user.username, user.first_name, user.last_name)
        
        # Ban kontrolü
        if await self.is_banned(user_id):
            await update.message.reply_text("🚫 **YASAKLANDIN!** Bu botu kullanamazsın.", parse_mode=ParseMode.MARKDOWN)
            return
        
        # Kanal kontrolü
        if not await self.check_channel_member(user_id):
            keyboard = [[InlineKeyboardButton("📢 Kanala Katıl", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"⚠️ **Önce kanala katılmalısın!**\n\n"
                f"🔗 Kanal: {CHANNEL_USERNAME}\n\n"
                f"Katıldıktan sonra /start yaz.",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Hoş geldin mesajı
        remaining = self.db.get_remaining_checks(user_id)
        user_data = self.db.get_user(user_id)
        is_premium = user_data[5] == 1 if user_data else False
        
        welcome_text = f"""
🚀 **SUPER CC CHECKER BOT**

Merhaba {user.first_name}! 

**📊 İstatistikler:**
• Kalan Hak: {remaining}
• Premium: {'✅ Evet' if is_premium else '❌ Hayır'}
• Toplam Kontrol: {user_data[10] if user_data else 0}

**📌 Komutlar:**
/generate - Rastgele kart üret
/check - Tek kart kontrol
/check_multiple - Çoklu kart kontrol
/stats - İstatistikler
/help - Yardım
/premium - Premium bilgileri
/refer - Referans sistemi

**⚡ Özellikler:**
✅ Proxy desteği
✅ Günlük 5 ücretsiz hak
✅ Premium paketler
✅ Referans sistemi
✅ Detaylı istatistikler
        """
        
        keyboard = [
            [
                InlineKeyboardButton("🎲 Kart Üret", callback_data="generate"),
                InlineKeyboardButton("✅ Tek Kart", callback_data="check_single")
            ],
            [
                InlineKeyboardButton("📋 Çoklu Kart", callback_data="check_multiple"),
                InlineKeyboardButton("📊 İstatistik", callback_data="stats")
            ],
            [
                InlineKeyboardButton("⭐ Premium", callback_data="premium"),
                InlineKeyboardButton("👥 Referans", callback_data="refer")
            ],
            [
                InlineKeyboardButton("❓ Yardım", callback_data="help"),
                InlineKeyboardButton("🔄 Güncelle", callback_data="refresh")
            ]
        ]
        
        # Admin butonları
        if await self.is_admin(user_id):
            keyboard.append([
                InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")
            ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    
    async def generate_cards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        # Ban kontrolü
        if await self.is_banned(user_id):
            await update.message.reply_text("🚫 **YASAKLANDIN!**", parse_mode=ParseMode.MARKDOWN)
            return
        
        # Kanal kontrolü
        if not await self.check_channel_member(user_id):
            await update.message.reply_text(f"⚠️ Önce {CHANNEL_USERNAME} kanalına katıl!", parse_mode=ParseMode.MARKDOWN)
            return
        
        # Hak kontrolü
        remaining = self.db.get_remaining_checks(user_id)
        if remaining <= 0:
            await update.message.reply_text(
                "❌ **Günlük hakkın bitti!**\n\n"
                "Premium alarak sınırsız kullanabilirsin.\n"
                "Yarın tekrar dene!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        try:
            count = 1
            if context.args and context.args[0].isdigit():
                count = min(int(context.args[0]), remaining, 20)
            
            status_msg = await update.message.reply_text("⏳ Kart üretiliyor...")
            
            data = self.api.make_request(f"/api/generate?count={count}")
            
            if data and data.get('status') == 'success':
                cards = data.get('cards', [])
                
                # Hak düş
                self.db.add_daily_check(user_id)
                
                message = f"🎲 **{len(cards)} Kart Üretildi:**\n\n"
                for i, card in enumerate(cards, 1):
                    message += f"{i}. `{card['number']}|{card['month']}|{card['year']}|{card['cvv']}`\n"
                
                # Kalan hak
                remaining = self.db.get_remaining_checks(user_id)
                message += f"\n📊 Kalan Hak: {remaining}"
                
                await status_msg.edit_text(message, parse_mode=ParseMode.MARKDOWN)
                
                # JSON dosyası
                with open(f'cards_{user_id}.json', 'w') as f:
                    json.dump(cards, f, indent=2)
                await update.message.reply_document(
                    document=open(f'cards_{user_id}.json', 'rb'),
                    filename=f'cards_{user_id}.json',
                    caption="📄 Üretilen kartlar"
                )
            else:
                await status_msg.edit_text("❌ Kart üretilirken hata oluştu!")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Hata: {str(e)}")
    
    async def check_single_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        # Ban kontrolü
        if await self.is_banned(user_id):
            await update.message.reply_text("🚫 **YASAKLANDIN!**", parse_mode=ParseMode.MARKDOWN)
            return
        
        # Kanal kontrolü
        if not await self.check_channel_member(user_id):
            await update.message.reply_text(f"⚠️ Önce {CHANNEL_USERNAME} kanalına katıl!", parse_mode=ParseMode.MARKDOWN)
            return
        
        # Hak kontrolü
        remaining = self.db.get_remaining_checks(user_id)
        if remaining <= 0:
            await update.message.reply_text(
                "❌ **Günlük hakkın bitti!**\n\n"
                "Premium alarak sınırsız kullanabilirsin.\n"
                "Yarın tekrar dene!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        try:
            if context.args:
                card_data = context.args[0]
            else:
                await update.message.reply_text(
                    "❌ Lütfen kart bilgilerini girin!\n\n"
                    "Format: `/check 4111111111111111|12|2026|123`",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            parts = card_data.split('|')
            if len(parts) != 4:
                await update.message.reply_text(
                    "❌ Hatalı format!\n\n"
                    "Doğru format: `4111111111111111|12|2026|123`",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            card = {
                "number": parts[0].strip(),
                "month": parts[1].strip().zfill(2),
                "year": parts[2].strip(),
                "cvv": parts[3].strip()
            }
            
            status_msg = await update.message.reply_text("⏳ Kart kontrol ediliyor...")
            
            data = self.api.make_request("/api/check-single", card, "POST")
            
            if data and data.get('status') == 'success':
                result = data.get('result', {})
                card_status = result.get('status', 'unknown')
                gateway = result.get('gateway', 'Bilinmiyor')
                message_text = result.get('message', '')
                
                # Sonucu kaydet
                self.db.add_card_result(user_id, card, card_status, gateway, message_text)
                
                # Hak düş
                self.db.add_daily_check(user_id)
                
                if card_status == 'approved':
                    emoji = "✅"
                    status_text = "**CANLI** 🟢"
                    await update.message.reply_text("🎉 **TEBRİKLER! KART CANLI!**", parse_mode=ParseMode.MARKDOWN)
                elif card_status == 'declined':
                    emoji = "❌"
                    status_text = "**ÖLÜ** 🔴"
                else:
                    emoji = "⚠️"
                    status_text = "**BİLİNMİYOR** ⚠️"
                
                response_text = f"""
{emoji} **Kart Kontrol Sonucu**

📱 **Kart:** `{card['number']}`
📅 **Tarih:** {card['month']}/{card['year']}
🔐 **CVV:** {card['cvv']}

📊 **Durum:** {status_text}
🏦 **Gateway:** {gateway}
💬 **Mesaj:** {message_text[:200]}
⏱️ **Zaman:** {datetime.now().strftime('%H:%M:%S')}

📊 Kalan Hak: {self.db.get_remaining_checks(user_id)}
                """
                
                await status_msg.edit_text(response_text, parse_mode=ParseMode.MARKDOWN)
            else:
                await status_msg.edit_text("❌ Hata oluştu! Lütfen tekrar dene.")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Hata: {str(e)}")
    
    async def check_multiple_cards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        # Ban kontrolü
        if await self.is_banned(user_id):
            await update.message.reply_text("🚫 **YASAKLANDIN!**", parse_mode=ParseMode.MARKDOWN)
            return
        
        # Kanal kontrolü
        if not await self.check_channel_member(user_id):
            await update.message.reply_text(f"⚠️ Önce {CHANNEL_USERNAME} kanalına katıl!", parse_mode=ParseMode.MARKDOWN)
            return
        
        # Hak kontrolü
        remaining = self.db.get_remaining_checks(user_id)
        if remaining <= 0:
            await update.message.reply_text(
                "❌ **Günlük hakkın bitti!**\n\n"
                "Premium alarak sınırsız kullanabilirsin.\n"
                "Yarın tekrar dene!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        await update.message.reply_text(
            "📋 **Lütfen kartları gönderin!**\n\n"
            "Her kartı aşağıdaki formatta yazın:\n"
            "`4111111111111111|12|2026|123`\n\n"
            "Kartları alt alta yazın.\n"
            "İşlemi iptal etmek için /cancel yazın.\n\n"
            f"📊 Kalan Hak: {remaining}",
            parse_mode=ParseMode.MARKDOWN
        )
        
        context.user_data['multi_check'] = True
    
    async def handle_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text
        
        # Ban kontrolü
        if await self.is_banned(user_id):
            await update.message.reply_text("🚫 **YASAKLANDIN!**", parse_mode=ParseMode.MARKDOWN)
            return
        
        # Multi-check modu
        if context.user_data.get('multi_check'):
            if text.lower() == '/cancel':
                context.user_data['multi_check'] = False
                await update.message.reply_text("✅ İşlem iptal edildi!")
                return
            
            cards = []
            lines = text.strip().split('\n')
            
            remaining = self.db.get_remaining_checks(user_id)
            max_cards = min(len(lines), remaining)
            
            for line in lines[:max_cards]:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                parts = line.split('|')
                if len(parts) == 4:
                    cards.append({
                        "number": parts[0].strip(),
                        "month": parts[1].strip().zfill(2),
                        "year": parts[2].strip(),
                        "cvv": parts[3].strip()
                    })
            
            if not cards:
                await update.message.reply_text("❌ Geçerli kart bulunamadı!")
                return
            
            status_msg = await update.message.reply_text(f"⏳ {len(cards)} kart kontrol ediliyor...")
            
            data = self.api.make_request("/api/check", {"cards": cards}, "POST")
            
            if data and data.get('status') == 'success':
                results = data.get('results', [])
                live_count = data.get('live_count', 0)
                
                # Sonuçları kaydet
                for result in results:
                    card = result.get('card', {})
                    status = result.get('status', 'unknown')
                    gateway = result.get('gateway', '')
                    message_text = result.get('message', '')
                    self.db.add_card_result(user_id, card, status, gateway, message_text)
                
                # Hak düş
                for _ in range(len(cards)):
                    self.db.add_daily_check(user_id)
                
                # Sonuçları formatla
                message = f"📊 **{len(results)} Kart Kontrol Sonucu:**\n"
                message += f"✅ Canlı: {live_count}\n"
                message += f"❌ Ölü: {len(results) - live_count}\n\n"
                
                # Live cards
                live_cards = []
                dead_cards = []
                
                for result in results:
                    card = result.get('card', {})
                    status = result.get('status', 'unknown')
                    card_str = f"{card.get('number', '')}|{card.get('month', '')}|{card.get('year', '')}|{card.get('cvv', '')}"
                    
                    if status == 'approved':
                        live_cards.append(f"✅ {card_str} - {result.get('gateway', '')}")
                    else:
                        dead_cards.append(f"❌ {card_str} - {result.get('message', '')[:50]}")
                
                if live_cards:
                    message += "**🟢 CANLI KARTLAR:**\n"
                    message += "\n".join(live_cards[:10])
                    if len(live_cards) > 10:
                        message += f"\n... ve {len(live_cards) - 10} daha"
                    message += "\n\n"
                
                if dead_cards:
                    message += "**🔴 ÖLÜ KARTLAR:**\n"
                    message += "\n".join(dead_cards[:5])
                    if len(dead_cards) > 5:
                        message += f"\n... ve {len(dead_cards) - 5} daha"
                
                message += f"\n📊 Kalan Hak: {self.db.get_remaining_checks(user_id)}"
                
                await status_msg.edit_text(message, parse_mode=ParseMode.MARKDOWN)
                
                # Dosya olarak kaydet
                with open(f'results_{user_id}.json', 'w') as f:
                    json.dump(results, f, indent=2)
                await update.message.reply_document(
                    document=open(f'results_{user_id}.json', 'rb'),
                    filename=f'results_{user_id}.json',
                    caption="📄 Detaylı sonuçlar"
                )
                
                if live_cards:
                    with open(f'live_{user_id}.txt', 'w') as f:
                        f.write('\n'.join([c.replace('✅ ', '') for c in live_cards]))
                    await update.message.reply_document(
                        document=open(f'live_{user_id}.txt', 'rb'),
                        filename=f'live_{user_id}.txt',
                        caption="✅ Canlı kartlar"
                    )
                
                context.user_data['multi_check'] = False
            else:
                await status_msg.edit_text("❌ Hata oluştu! Lütfen tekrar dene.")
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        # Ban kontrolü
        if await self.is_banned(user_id):
            await update.message.reply_text("🚫 **YASAKLANDIN!**", parse_mode=ParseMode.MARKDOWN)
            return
        
        user = self.db.get_user(user_id)
        stats = self.db.get_user_stats(user_id)
        remaining = self.db.get_remaining_checks(user_id)
        
        if not user:
            await update.message.reply_text("❌ Kullanıcı bulunamadı!")
            return
        
        is_premium = user[5] == 1
        premium_expiry = user[6] if is_premium else "Yok"
        
        message = f"""
📊 **İSTATİSTİKLERİN**

👤 **Kullanıcı:** @{user[1] or user[2] or 'Bilinmiyor'}
🆔 **ID:** {user_id}

📊 **Kart İstatistikleri:**
• Toplam Kontrol: {stats[0] if stats else 0}
• Canlı Kart: {stats[1] if stats else 0}
• Ölü Kart: {stats[2] if stats else 0}
• Başarı Oranı: {f"{(stats[1]/stats[0]*100):.1f}%" if stats and stats[0] > 0 else "0%"}

📅 **Günlük Durum:**
• Kalan Hak: {remaining}
• Günlük Limit: {'Sınırsız' if is_premium else DAILY_LIMIT}

⭐ **Premium Durumu:**
• Premium: {'✅ Aktif' if is_premium else '❌ Pasif'}
• Bitiş: {premium_expiry[:10] if premium_expiry != 'Yok' else 'Yok'}

👥 **Referans:**
• Gönderdiğin Kişi: {user[12] if user[12] else 'Yok'}
• Kazandığın Hak: {user[13] if user[13] else 0}

📅 **Katılım:** {user[3][:10] if user[3] else 'Bilinmiyor'}
        """
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        help_text = """
📖 **KULLANIM KILAVUZU**

**🔹 Temel Komutlar:**
/generate - Rastgele kart üret
/check - Tek kart kontrol et
/check_multiple - Çoklu kart kontrol et
/stats - İstatistiklerini gör
/help - Bu yardım menüsü

**🔹 Premium Komutlar:**
/premium - Premium paketleri gör
/refer - Referans sistemini kullan

**🔹 Admin Komutları:**
/admin - Admin paneli
/broadcast - Duyuru gönder
/add_premium - Premium ver
/remove_premium - Premium al
/ban - Kullanıcı banla
/unban - Ban kaldır
/add_proxy - Proxy ekle
/remove_proxy - Proxy sil
/stats_all - Tüm istatistikler

**📋 Kart Formatı:**
`4111111111111111|12|2026|123`

**⚡ Özellikler:**
• Günlük 5 ücretsiz hak
• Premium ile sınırsız
• Referans sistemi
• Proxy desteği
• Detaylı istatistikler
• Canlı kart bildirimi

**❓ Sorun mu var?**
/admin yazıp destek ekibine ulaşabilirsin.
        """
        
        keyboard = [
            [
                InlineKeyboardButton("📊 İstatistikler", callback_data="stats"),
                InlineKeyboardButton("⭐ Premium", callback_data="premium")
            ],
            [
                InlineKeyboardButton("👥 Referans", callback_data="refer"),
                InlineKeyboardButton("🔄 Güncelle", callback_data="refresh")
            ]
        ]
        
        if await self.is_admin(user_id):
            keyboard.append([
                InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")
            ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(help_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    
    # ============= ADMIN KOMUTLARI =============
    async def admin_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            await update.message.reply_text("❌ Bu komut sadece adminler içindir!")
            return
        
        # İstatistikler
        users = self.db.get_all_users()
        total_users = len(users)
        premium_users = sum(1 for u in users if u[4] == 1)
        banned_users = sum(1 for u in users if u[5] == 1)
        
        # Sonuç istatistikleri
        self.db.cursor.execute('SELECT COUNT(*), SUM(CASE WHEN status="approved" THEN 1 ELSE 0 END) FROM card_results')
        total_checks, live_checks = self.db.cursor.fetchone()
        
        message = f"""
👑 **ADMIN PANELİ**

📊 **Genel İstatistikler:**
• Toplam Kullanıcı: {total_users}
• Premium Kullanıcı: {premium_users}
• Yasaklı Kullanıcı: {banned_users}
• Toplam Kontrol: {total_checks or 0}
• Canlı Kart: {live_checks or 0}

📌 **Admin Komutları:**
/broadcast - Duyuru gönder
/add_premium - Premium ver
/remove_premium - Premium al
/ban - Kullanıcı banla
/unban - Ban kaldır
/add_proxy - Proxy ekle
/remove_proxy - Proxy sil
/stats_all - Tüm istatistikler
/message - Admin mesaj gönder

🔄 **Proxy Durumu:**
• Toplam Proxy: {len(self.proxy_manager.proxies)}
        """
        
        keyboard = [
            [
                InlineKeyboardButton("📢 Duyuru", callback_data="admin_broadcast"),
                InlineKeyboardButton("👥 Kullanıcılar", callback_data="admin_users")
            ],
            [
                InlineKeyboardButton("⭐ Premium", callback_data="admin_premium"),
                InlineKeyboardButton("🚫 Ban", callback_data="admin_ban")
            ],
            [
                InlineKeyboardButton("🔄 Proxy", callback_data="admin_proxy"),
                InlineKeyboardButton("📊 Detay", callback_data="admin_stats")
            ],
            [
                InlineKeyboardButton("💬 Destek", callback_data="admin_support")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    
    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ Mesaj gir!\nFormat: `/broadcast Merhaba herkese!`", parse_mode=ParseMode.MARKDOWN)
            return
        
        message = ' '.join(context.args)
        
        # Tüm kullanıcılara gönder
        users = self.db.get_all_users()
        sent = 0
        failed = 0
        
        status_msg = await update.message.reply_text(f"⏳ {len(users)} kullanıcıya mesaj gönderiliyor...")
        
        for user in users:
            try:
                await self.app.bot.send_message(
                    user[0],
                    f"📢 **DUYURU**\n\n{message}",
                    parse_mode=ParseMode.MARKDOWN
                )
                sent += 1
                await asyncio.sleep(0.1)  # Rate limit
            except:
                failed += 1
        
        # Duyuruyu kaydet
        self.db.add_notification(message)
        
        await status_msg.edit_text(f"✅ **Duyuru gönderildi!**\n\n📤 Gönderilen: {sent}\n❌ Başarısız: {failed}")
    
    async def add_premium(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            return
        
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Kullanıcı ID ve süre girin!\n"
                "Format: `/add_premium 123456 30` (30 gün)",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        try:
            target_id = int(context.args[0])
            days = int(context.args[1])
            
            expiry = (datetime.now() + timedelta(days=days)).isoformat()
            self.db.update_user(target_id, is_premium=1, premium_expiry=expiry)
            
            # Kullanıcıya bildir
            try:
                await self.app.bot.send_message(
                    target_id,
                    f"⭐ **PREMIUM VERİLDİ!**\n\n"
                    f"📅 Süre: {days} gün\n"
                    f"📆 Bitiş: {expiry[:10]}\n\n"
                    f"Artık sınırsız kontrole sahipsin!",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
            
            await update.message.reply_text(f"✅ **Premium verildi!**\n\nKullanıcı: {target_id}\nSüre: {days} gün")
            
        except:
            await update.message.reply_text("❌ Hatalı format!")
    
    async def remove_premium(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ Kullanıcı ID girin!\nFormat: `/remove_premium 123456`", parse_mode=ParseMode.MARKDOWN)
            return
        
        try:
            target_id = int(context.args[0])
            self.db.update_user(target_id, is_premium=0, premium_expiry=None)
            
            # Kullanıcıya bildir
            try:
                await self.app.bot.send_message(
                    target_id,
                    "❌ **PREMIUM ALINDI!**\n\nPremium avantajların sona erdi.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
            
            await update.message.reply_text(f"✅ Premium alındı: {target_id}")
            
        except:
            await update.message.reply_text("❌ Hatalı format!")
    
    async def ban_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ Kullanıcı ID girin!\nFormat: `/ban 123456`", parse_mode=ParseMode.MARKDOWN)
            return
        
        try:
            target_id = int(context.args[0])
            self.db.update_user(target_id, is_banned=1)
            
            # Kullanıcıya bildir
            try:
                await self.app.bot.send_message(
                    target_id,
                    "🚫 **YASAKLANDIN!**\n\nBu botu kullanman yasaklandı.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
            
            await update.message.reply_text(f"✅ Kullanıcı yasaklandı: {target_id}")
            
        except:
            await update.message.reply_text("❌ Hatalı format!")
    
    async def unban_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ Kullanıcı ID girin!\nFormat: `/unban 123456`", parse_mode=ParseMode.MARKDOWN)
            return
        
        try:
            target_id = int(context.args[0])
            self.db.update_user(target_id, is_banned=0)
            
            # Kullanıcıya bildir
            try:
                await self.app.bot.send_message(
                    target_id,
                    "✅ **YASAK KALDIRILDI!**\n\nArtık botu tekrar kullanabilirsin.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
            
            await update.message.reply_text(f"✅ Yasa kaldırıldı: {target_id}")
            
        except:
            await update.message.reply_text("❌ Hatalı format!")
    
    async def add_proxy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ Proxy girin!\n"
                "Format: `/add_proxy http://user:pass@ip:port`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        proxy = context.args[0]
        self.proxy_manager.add_proxy(proxy)
        
        await update.message.reply_text(f"✅ Proxy eklendi: {proxy}")
    
    async def remove_proxy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ Proxy ID girin!\nFormat: `/remove_proxy 1`", parse_mode=ParseMode.MARKDOWN)
            return
        
        try:
            proxy_id = int(context.args[0])
            self.proxy_manager.remove_proxy(proxy_id)
            await update.message.reply_text(f"✅ Proxy silindi: {proxy_id}")
        except:
            await update.message.reply_text("❌ Hatalı format!")
    
    async def stats_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not await self.is_admin(user_id):
            return
        
        # Tüm istatistikler
        self.db.cursor.execute('''
            SELECT 
                COUNT(DISTINCT user_id) as total_users,
                SUM(CASE WHEN is_premium = 1 THEN 1 ELSE 0 END) as premium,
                SUM(CASE WHEN is_banned = 1 THEN 1 ELSE 0 END) as banned,
                SUM(total_checks) as total_checks,
                (SELECT COUNT(*) FROM card_results WHERE status = 'approved') as live_cards
            FROM users
        ''')
        stats = self.db.cursor.fetchone()
        
        # Son 7 gün
        self.db.cursor.execute('''
            SELECT date, SUM(checks) 
            FROM daily_usage 
            WHERE date >= date('now', '-7 days')
            GROUP BY date
            ORDER BY date DESC
        ''')
        daily = self.db.cursor.fetchall()
        
        message = f"""
📊 **TÜM İSTATİSTİKLER**

👥 **Kullanıcılar:**
• Toplam: {stats[0] or 0}
• Premium: {stats[1] or 0}
• Yasaklı: {stats[2] or 0}

💳 **Kartlar:**
• Toplam Kontrol: {stats[3] or 0}
• Canlı Kart: {stats[4] or 0}
• Başarı Oranı: {f"{(stats[4]/stats[3]*100):.1f}%" if stats[3] and stats[3] > 0 else "0%"}

📅 **Son 7 Gün:**
        """
        
        for date, checks in daily:
            message += f"• {date}: {checks} kontrol\n"
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    
    # ============= CALLBACK HANDLER =============
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        await query.answer()
        
        data = query.data
        
        # Ban kontrolü
        if await self.is_banned(user_id):
            await query.edit_message_text("🚫 **YASAKLANDIN!**", parse_mode=ParseMode.MARKDOWN)
            return
        
        if data == "generate":
            await self.generate_cards(update, context)
        
        elif data == "check_single":
            await query.edit_message_text(
                "✅ **Tek Kart Kontrol**\n\n"
                "Format: `/check 4111111111111111|12|2026|123`",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "check_multiple":
            await query.edit_message_text(
                "📋 **Çoklu Kart Kontrol**\n\n"
                "Kartları alt alta gönder:\n"
                "`/check_multiple`\n"
                "Ardından kartları yapıştır.",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "stats":
            await self.stats(update, context)
        
        elif data == "premium":
            message = """
⭐ **PREMIUM PAKETLER**

🚀 **Premium ile sınırsız kontrol!**

**📦 Paketler:**
• 7 Gün - 5$ (veya 10 referans)
• 30 Gün - 15$ (veya 25 referans)
• 90 Gün - 35$ (veya 50 referans)
• 365 Gün - 100$ (veya 100 referans)

**✨ Premium Avantajları:**
✅ Sınırsız kart kontrol
✅ Öncelikli destek
✅ Özel gateway'ler
✅ Hızlı kontrol
✅ Daha yüksek başarı oranı

**📞 İletişim:**
Premium almak için @wortexbabax yaz.
            """
            await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN)
        
        elif data == "refer":
            user = self.db.get_user(user_id)
            if not user:
                return
            
            ref_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
            ref_count = user[13] if user[13] else 0
            
            message = f"""
👥 **REFERANS SİSTEMİ**

Her referans için **1 ekstra hak** kazan!

📌 **Referans Linkin:**
`{ref_link}`

👤 **Toplam Referans:** {ref_count}
⭐ **Kazandığın Hak:** {ref_count}

**📋 Nasıl Çalışır?**
1. Linki arkadaşlarına gönder
2. Arkadaşın botu başlatsın
3. 1 hak kazan!

**🎁 Bonus:**
10 referans = 7 gün premium
25 referans = 30 gün premium
            """
            
            keyboard = [[InlineKeyboardButton("📤 Paylaş", switch_inline_query=ref_link)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        
        elif data == "refresh":
            await query.edit_message_text("🔄 Güncelleniyor...")
            await self.start(update, context)
        
        elif data == "help":
            await self.help(update, context)
        
        # Admin callback'leri
        elif data == "admin_panel":
            await self.admin_panel(update, context)
        
        elif data == "admin_broadcast":
            await query.edit_message_text(
                "📢 **Duyuru Gönder**\n\n"
                "Format: `/broadcast Mesajın`",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "admin_users":
            users = self.db.get_all_users()
            message = "👥 **Kullanıcılar:**\n\n"
            for user in users[:20]:
                status = "⭐" if user[4] else "👤"
                ban = "🚫" if user[5] else "✅"
                message += f"{status} @{user[1] or user[2] or user[0]} - {ban}\n"
            if len(users) > 20:
                message += f"\n... ve {len(users)-20} daha"
            await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN)
        
        elif data == "admin_premium":
            await query.edit_message_text(
                "⭐ **Premium Yönetimi**\n\n"
                "Format: `/add_premium 123456 30` (30 gün)\n"
                "Format: `/remove_premium 123456` (al)",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "admin_ban":
            await query.edit_message_text(
                "🚫 **Ban Yönetimi**\n\n"
                "Format: `/ban 123456` (banla)\n"
                "Format: `/unban 123456` (aç)",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "admin_proxy":
            await query.edit_message_text(
                "🔄 **Proxy Yönetimi**\n\n"
                "Format: `/add_proxy http://user:pass@ip:port`\n"
                "Format: `/remove_proxy 1` (id ile sil)",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "admin_stats":
            await self.stats_all(update, context)
        
        elif data == "admin_support":
            await query.edit_message_text(
                "💬 **Destek Ekibi**\n\n"
                "Destek için @wortexbabax yazabilirsin.\n\n"
                "Admin mesaj göndermek için:\n"
                "/message KULLANICI_ID MESAJ",
                parse_mode=ParseMode.MARKDOWN
            )
    
    # ============= BOTU BAŞLAT =============
    def run(self):
        self.app = Application.builder().token(BOT_TOKEN).build()
        
        # Komutlar
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help))
        self.app.add_handler(CommandHandler("generate", self.generate_cards))
        self.app.add_handler(CommandHandler("check", self.check_single_card))
        self.app.add_handler(CommandHandler("check_multiple", self.check_multiple_cards))
        self.app.add_handler(CommandHandler("stats", self.stats))
        self.app.add_handler(CommandHandler("cancel", self.cancel))
        
        # Admin komutları
        self.app.add_handler(CommandHandler("admin", self.admin_panel))
        self.app.add_handler(CommandHandler("broadcast", self.broadcast))
        self.app.add_handler(CommandHandler("add_premium", self.add_premium))
        self.app.add_handler(CommandHandler("remove_premium", self.remove_premium))
        self.app.add_handler(CommandHandler("ban", self.ban_user))
        self.app.add_handler(CommandHandler("unban", self.unban_user))
        self.app.add_handler(CommandHandler("add_proxy", self.add_proxy))
        self.app.add_handler(CommandHandler("remove_proxy", self.remove_proxy))
        self.app.add_handler(CommandHandler("stats_all", self.stats_all))
        
        # Mesaj handler
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_messages))
        
        # Callback handler
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        
        print("🚀 Super CC Checker Bot başlatılıyor...")
        print(f"👑 Adminler: {ADMIN_IDS}")
        print(f"📌 Kanal: {CHANNEL_USERNAME}")
        print(f"📊 Günlük Limit: {DAILY_LIMIT}")
        print(f"🔄 Proxy sayısı: {len(self.proxy_manager.proxies)}")
        print("✅ Bot çalışıyor!")
        
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    bot = SuperCardBot()
    bot.run()
