import logging
import json
import os
import random
import re
import asyncio
import signal
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters
)
from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights

load_dotenv()

# ============ КОНФИГУРАЦИЯ ============
BOT_TOKEN = os.getenv('BOT_TOKEN')
OWNER_USERNAME = os.getenv('OWNER_USERNAME', '')
OWNER_ID = int(os.getenv('OWNER_ID', '0')) if os.getenv('OWNER_ID', '').isdigit() else 0
CHANNEL_ID = os.getenv('CHANNEL_ID', '')
CHANNEL_LINK = os.getenv('CHANNEL_LINK', '')
GROUP_ID = os.getenv('GROUP_ID', '')
ADMIN_TAG = os.getenv('ADMIN_TAG', 'гарант')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')  # URL для вебхука
WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', '8443'))

SUBSCRIPTION_CHECK_ENABLED = bool(CHANNEL_ID)
WARNING_LIMIT = 2
MUTE_DURATION = 4 * 3600

# Константы для Telethon
USERBOT_API_ID = 2040
USERBOT_API_HASH = 'b18441a1ff607e10a989891a5462e627'
USERBOT_SESSION = 'user_session'

# ============ ЛОГИРОВАНИЕ ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('moderator_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============ ДАННЫЕ ============
SCAMMERS_FILE = 'scammers.json'
REPORTS_FILE = 'reports.json'
STATS_FILE = 'stats.json'
EVIDENCE_DIR = 'evidence'
WARNED_USERS_FILE = 'warned_users.json'
MUTED_USERS_FILE = 'muted_users.json'
DEALS_FILE = 'deals.json'
DEAL_COUNTER_FILE = 'deal_counter.json'

os.makedirs(EVIDENCE_DIR, exist_ok=True)

def load_json(filename: str) -> Dict:
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_json(filename: str, data: Dict):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

scammers = load_json(SCAMMERS_FILE)
reports = load_json(REPORTS_FILE)
stats = load_json(STATS_FILE)
warned_users = load_json(WARNED_USERS_FILE)
muted_users = load_json(MUTED_USERS_FILE)
deals = load_json(DEALS_FILE)
deal_counter = load_json(DEAL_COUNTER_FILE)

if not deal_counter:
    deal_counter = {'counter': 0}

if not stats:
    stats = {
        'messages_deleted': 0, 'scammers_banned': 0, 'join_messages_deleted': 0,
        'reports_processed': 0, 'reports_approved': 0, 'reports_rejected': 0,
        'subscription_warnings': 0, 'users_muted': 0,
        'deals_created': 0, 'deals_completed': 0, 'deals_cancelled': 0
    }

pending_reports = {}
pending_deals = {}
admin_invites = {}
report_assignments = {}
app_job_queue = None
bot_instance = None  # Глобальный экземпляр бота

resolved_owner_id = None
resolved_admin_ids = []
group_garant_ids = []

# ============ SIGNAL HANDLER ============
def signal_handler(sig, frame):
    print("\n👋 Завершение работы бота...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ============ РАЗРЕШЕНИЕ USERNAME ============
async def resolve_usernames(bot):
    global resolved_owner_id, resolved_admin_ids
    
    if OWNER_USERNAME:
        try:
            username = OWNER_USERNAME.replace('@', '').strip()
            user = await bot.get_chat(f'@{username}')
            resolved_owner_id = user.id
            logger.info(f"👑 Владелец: @{username} = {user.id}")
        except Exception as e:
            logger.error(f"Не удалось найти владельца: {e}")
    
    if OWNER_ID and not resolved_owner_id:
        resolved_owner_id = OWNER_ID
    
    ADMIN_IDS_RAW = os.getenv('ADMIN_IDS', '')
    ADMIN_USERNAMES = os.getenv('ADMIN_USERNAMES', '')
    
    resolved_admin_ids = [int(x.strip()) for x in ADMIN_IDS_RAW.split(',') if x.strip().isdigit()]
    
    if ADMIN_USERNAMES:
        for username in ADMIN_USERNAMES.split(','):
            username = username.replace('@', '').strip()
            if username:
                try:
                    user = await bot.get_chat(f'@{username}')
                    if user.id not in resolved_admin_ids:
                        resolved_admin_ids.append(user.id)
                except:
                    pass
    
    logger.info(f"👑 Владелец: {resolved_owner_id}, 👮 Из .env: {len(resolved_admin_ids)}")

async def load_garants_from_group(bot):
    global group_garant_ids
    if not GROUP_ID:
        return
    try:
        group_garant_ids = []
        admins = await bot.get_chat_administrators(chat_id=GROUP_ID)
        for admin in admins:
            title = getattr(admin, 'custom_title', '') or ''
            if ADMIN_TAG.lower() in title.lower():
                group_garant_ids.append(admin.user.id)
                logger.info(f"🏷 Гарант: {admin.user.first_name} (ID: {admin.user.id})")
        logger.info(f"🏷 Гарантов: {len(group_garant_ids)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки гарантов: {e}")

async def admin_status_changed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member_update = update.chat_member
    if not chat_member_update:
        return
    group_id_str = str(GROUP_ID).replace('-100', '').replace('-', '')
    chat_id_str = str(chat_member_update.chat.id).replace('-100', '').replace('-', '')
    if group_id_str != chat_id_str:
        return
    await load_garants_from_group(context.bot)

# ============ УДАЛЕНИЕ СООБЩЕНИЙ С ЗАДЕРЖКОЙ ============
async def delete_message_later(chat_id: int, message_id: int, delay: int = 10):
    """Удаление сообщения через указанное время"""
    await asyncio.sleep(delay)
    try:
        if bot_instance:
            await bot_instance.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info(f"🗑 Сообщение {message_id} удалено через {delay}с")
    except Exception as e:
        logger.error(f"Ошибка удаления сообщения {message_id}: {e}")

# ============ ПРОВЕРКА ПОДПИСКИ И МУТ ============
async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not SUBSCRIPTION_CHECK_ENABLED or not CHANNEL_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        is_subscribed = member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]
        return is_subscribed
    except Exception as e:
        logger.warning(f"Ошибка проверки подписки (пропускаем): {e}")
        return True

async def mute_user_via_userbot(user_id: int, chat_id: str, duration_seconds: int) -> bool:
    client = TelegramClient(USERBOT_SESSION, USERBOT_API_ID, USERBOT_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return False
        
        group = await client.get_entity(int(chat_id))
        until_date = datetime.now() + timedelta(seconds=duration_seconds)
        
        muted_rights = ChatBannedRights(
            until_date=until_date,
            view_messages=False, send_messages=True, send_media=True,
            send_stickers=True, send_gifs=True, send_games=True,
            send_inline=True, embed_links=True, send_polls=True,
            change_info=False, invite_users=False, pin_messages=False
        )
        
        await client(EditBannedRequest(channel=group, participant=user_id, banned_rights=muted_rights))
        await client.disconnect()
        logger.info(f"✅ Пользователь {user_id} замучен на {duration_seconds}с через userbot")
        return True
    except Exception as e:
        await client.disconnect()
        logger.error(f"Ошибка мута через userbot: {e}")
        return False

async def mute_user_via_bot(user_id: int, chat_id: str, duration_seconds: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        until_date = datetime.now() + timedelta(seconds=duration_seconds)
        await context.bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id,
            permissions={
                'can_send_messages': False, 'can_send_media': False,
                'can_send_other_messages': False, 'can_add_web_page_previews': False
            },
            until_date=until_date
        )
        logger.info(f"✅ Пользователь {user_id} замучен через бота на {duration_seconds}с")
        return True
    except Exception as e:
        logger.error(f"Ошибка мута через бота: {e}")
        return False

async def handle_unsubscribed_user(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Обработка неподписанного пользователя"""
    
    # Удаляем сообщение пользователя
    try:
        await update.message.delete()
    except:
        pass
    stats['messages_deleted'] = stats.get('messages_deleted', 0) + 1
    
    user_key = str(user_id)
    now = datetime.now()
    
    channel_link = CHANNEL_LINK or f"https://t.me/{CHANNEL_ID.replace('@', '')}"
    user = await context.bot.get_chat(user_id)
    user_name = user.first_name or 'Пользователь'
    
    if user_key not in warned_users:
        warned_users[user_key] = {'count': 1, 'first_warning': now.isoformat(), 'last_warning': now.isoformat()}
        save_json(WARNED_USERS_FILE, warned_users)
        
        warn_msg = await update.message.chat.send_message(
            f"⚠️ {user_name}, подпишитесь на канал чтобы писать!\n{channel_link}\n\n"
            f"Предупреждение 1/{WARNING_LIMIT}\n"
            f"После {WARNING_LIMIT} предупреждений - мут на 4 часа.\n\n"
            f"Сообщение исчезнет через 10 секунд",
            disable_web_page_preview=True
        )
        asyncio.create_task(delete_message_later(warn_msg.chat_id, warn_msg.message_id, 10))
        
        stats['subscription_warnings'] = stats.get('subscription_warnings', 0) + 1
        save_json(STATS_FILE, stats)
    else:
        warned_users[user_key]['count'] += 1
        warned_users[user_key]['last_warning'] = now.isoformat()
        warning_count = warned_users[user_key]['count']
        save_json(WARNED_USERS_FILE, warned_users)
        
        if warning_count >= WARNING_LIMIT:
            logger.info(f"🔇 Мутим {user_id} на 4 часа")
            
            muted = await mute_user_via_userbot(user_id, GROUP_ID or str(update.message.chat_id), MUTE_DURATION)
            if not muted:
                muted = await mute_user_via_bot(user_id, GROUP_ID or str(update.message.chat_id), MUTE_DURATION, context)
            
            if muted:
                stats['users_muted'] = stats.get('users_muted', 0) + 1
                save_json(STATS_FILE, stats)
                
                muted_users[user_key] = {
                    'muted_at': now.isoformat(),
                    'muted_until': (now + timedelta(seconds=MUTE_DURATION)).isoformat(),
                    'reason': f'Не подписался на канал после {WARNING_LIMIT} предупреждений'
                }
                save_json(MUTED_USERS_FILE, muted_users)
                
                mute_msg = await update.message.chat.send_message(
                    f"🔇 {user_name} замучен на 4 часа!\nПричина: не подписался на канал {channel_link}\n\nСообщение исчезнет через 10 секунд"
                )
                asyncio.create_task(delete_message_later(mute_msg.chat_id, mute_msg.message_id, 10))
                
                warned_users[user_key]['count'] = 0
                save_json(WARNED_USERS_FILE, warned_users)
            else:
                warn_msg = await update.message.chat.send_message(
                    f"⚠️ {user_name}, подпишитесь на канал!\n{channel_link}\n\n"
                    f"Предупреждение {warning_count}/{WARNING_LIMIT}\nСообщение исчезнет через 10 секунд"
                )
                asyncio.create_task(delete_message_later(warn_msg.chat_id, warn_msg.message_id, 10))
        else:
            warn_msg = await update.message.chat.send_message(
                f"⚠️ {user_name}, подпишитесь на канал чтобы писать!\n{channel_link}\n\n"
                f"Предупреждение {warning_count}/{WARNING_LIMIT}\n"
                f"После {WARNING_LIMIT} предупреждений - мут на 4 часа.\n\n"
                f"Сообщение исчезнет через 10 секунд",
                disable_web_page_preview=True
            )
            asyncio.create_task(delete_message_later(warn_msg.chat_id, warn_msg.message_id, 10))
            
            stats['subscription_warnings'] = stats.get('subscription_warnings', 0) + 1
            save_json(STATS_FILE, stats)

# ============ БАН ЧЕРЕЗ USERBOT ============
async def ban_user_via_userbot(user_id, username: str = "", group_id: str = None) -> bool:
    if not group_id:
        group_id = GROUP_ID
    if not group_id:
        return False
    
    client = TelegramClient(USERBOT_SESSION, USERBOT_API_ID, USERBOT_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return False
        
        resolved_id = user_id
        clean_username = username.replace('@', '').strip() if username else ""
        
        if (not resolved_id or resolved_id == 0) and clean_username:
            try:
                entity = await client.get_entity(f'@{clean_username}')
                resolved_id = entity.id
            except:
                pass
        
        if not resolved_id:
            await client.disconnect()
            return False
        
        group = await client.get_entity(int(group_id))
        
        banned_rights = ChatBannedRights(
            until_date=None,
            view_messages=True, send_messages=True, send_media=True,
            send_stickers=True, send_gifs=True, send_games=True,
            send_inline=True, embed_links=True, send_polls=True,
            change_info=True, invite_users=True, pin_messages=True
        )
        
        await client(EditBannedRequest(channel=group, participant=resolved_id, banned_rights=banned_rights))
        await client.disconnect()
        return True
    except:
        await client.disconnect()
        return False

# ============ ПРОВЕРКА ДОСТУПА ============
def is_owner(user_id: int) -> bool:
    return resolved_owner_id is not None and user_id == resolved_owner_id

def is_admin(user_id: int) -> bool:
    return user_id in resolved_admin_ids or user_id in group_garant_ids

def is_staff(user_id: int) -> bool:
    return is_owner(user_id) or is_admin(user_id)

def get_random_admin(exclude: List[int] = None) -> Optional[int]:
    all_admins = list(set(resolved_admin_ids + group_garant_ids))
    if not all_admins:
        return None
    available = [a for a in all_admins if a not in (exclude or [])]
    if not available:
        return None
    return random.choice(available)

def get_next_deal_number() -> int:
    deal_counter['counter'] += 1
    save_json(DEAL_COUNTER_FILE, deal_counter)
    return deal_counter['counter']

# ============ ПАРСИНГ /deal ============
def parse_deal_command(text: str) -> Optional[Dict]:
    parts = text.strip().split()
    if len(parts) < 3:
        return None
    if not parts[1].startswith('@'):
        return None
    return {
        'partner_username': parts[1].replace('@', ''),
        'price': parts[2],
        'description': ' '.join(parts[3:]) if len(parts) > 3 else ''
    }

# ============ СДЕЛКИ ============
async def deal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != 'private':
        await update.message.reply_text("⚠️ Сделки создаются в личных сообщениях с ботом!")
        return
    
    user = update.effective_user
    text = update.message.text or ""
    
    parsed = parse_deal_command(text)
    
    if not parsed:
        await update.message.reply_text(
            "❌ Неверный формат!\n\n"
            "/deal @username цена описание\n\n"
            "Примеры:\n"
            "/deal @seller 500$ Продажа канала\n"
            "/deal @buyer 0.5 BTC Обмен"
        )
        return
    
    all_admins = list(set(resolved_admin_ids + group_garant_ids))
    if not all_admins:
        await update.message.reply_text("❌ Нет доступных гарантов.")
        return
    
    deal_number = get_next_deal_number()
    deal_id = str(datetime.now().timestamp())
    
    pending_deals[user.id] = {
        'deal_id': deal_id, 'deal_number': deal_number,
        'creator_id': str(user.id), 'creator_username': user.username or str(user.id),
        'partner_username': parsed['partner_username'],
        'price': parsed['price'], 'description': parsed['description'],
        'status': 'waiting_for_group', 'created_date': datetime.now().isoformat(),
        'excluded_admins': []
    }
    
    desc = f"\n📝 {parsed['description']}" if parsed['description'] else ""
    
    await update.message.reply_text(
        f"📋 Сделка #{deal_number} создана!\n\n"
        f"👤 Вторая сторона: @{parsed['partner_username']}\n"
        f"💰 Цена: {parsed['price']}{desc}\n\n"
        f"Дальнейшие шаги:\n"
        f"1. Создайте группу для сделки\n"
        f"2. Добавьте бота в группу\n"
        f"3. Сделайте бота администратором\n"
        f"4. Добавьте второго участника @{parsed['partner_username']}\n"
        f"5. Отправьте команду /ready в группе\n\n"
        f"После этого бот пригласит гаранта."
    )

async def ready_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("⚠️ Эта команда работает только в группе сделки!")
        return
    
    user = update.effective_user
    chat_id = update.message.chat_id
    
    deal_data = pending_deals.get(user.id)
    
    if not deal_data:
        await update.message.reply_text("❌ У вас нет активной сделки. Создайте в ЛС: /deal @username цена")
        return
    
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            await update.message.reply_text("❌ Бот не администратор! Сделайте бота админом и повторите /ready")
            return
    except:
        await update.message.reply_text("❌ Бот не в группе! Добавьте бота и сделайте админом.")
        return
    
    deal_number = deal_data['deal_number']
    deal_id = deal_data['deal_id']
    
    try:
        invite = await context.bot.create_chat_invite_link(chat_id)
        invite_url = invite.invite_link
    except:
        invite_url = f"https://t.me/c/{str(chat_id).replace('-100', '')}"
    
    deals[deal_id] = {
        'deal_id': deal_id, 'deal_number': deal_number,
        'chat_id': str(chat_id), 'invite_link': invite_url,
        'creator_id': deal_data['creator_id'], 'creator_username': deal_data['creator_username'],
        'partner_username': deal_data['partner_username'],
        'price': deal_data['price'], 'description': deal_data['description'],
        'status': 'active', 'admin_accepted': False,
        'excluded_admins': deal_data.get('excluded_admins', []),
        'created_date': datetime.now().isoformat()
    }
    
    save_json(DEALS_FILE, deals)
    stats['deals_created'] = stats.get('deals_created', 0) + 1
    save_json(STATS_FILE, stats)
    
    del pending_deals[user.id]
    
    desc = f"\n📝 {deal_data['description']}" if deal_data.get('description') else ""
    
    await update.message.reply_text(
        f"📢 ОБЪЯВЛЕНИЕ О СДЕЛКЕ #{deal_number}\n\n"
        f"👤 @{deal_data['creator_username']} ↔ @{deal_data['partner_username']}\n"
        f"💰 {deal_data['price']}{desc}\n\n⏳ Ожидается гарант..."
    )
    
    await invite_admin_to_deal(context, deal_id)

async def invite_admin_to_deal(context: ContextTypes.DEFAULT_TYPE, deal_id: str):
    if deal_id not in deals: return
    deal = deals[deal_id]
    excluded = deal.get('excluded_admins', [])
    admin_id = get_random_admin(exclude=excluded)
    
    if admin_id is None:
        await cancel_deal(context, deal_id, "Все гаранты отказались или не ответили.")
        return
    
    admin_invites[deal_id] = {
        'admin_id': admin_id, 'expires': datetime.now() + timedelta(minutes=5),
        'excluded_admins': excluded + [admin_id]
    }
    deal['excluded_admins'] = excluded + [admin_id]
    save_json(DEALS_FILE, deals)
    
    keyboard = [[
        InlineKeyboardButton("✅ Принять сделку", callback_data=f"admin_accept_deal_{deal_id}"),
        InlineKeyboardButton("❌ Отказаться", callback_data=f"admin_decline_deal_{deal_id}")
    ]]
    
    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"🔒 НОВАЯ СДЕЛКА #{deal.get('deal_number', '?')}\n\n"
                 f"👤 @{deal['creator_username']} ↔ @{deal['partner_username']}\n"
                 f"💰 {deal['price']}\n🔗 {deal['invite_link']}\n\n⏳ 5 минут на ответ!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        asyncio.create_task(check_deal_timeout(context, deal_id, 300))
    except:
        await invite_admin_to_deal(context, deal_id)

async def check_deal_timeout(context, deal_id: str, delay: int = 300):
    await asyncio.sleep(delay)
    if deal_id not in deals or deals[deal_id].get('admin_accepted'): return
    invite = admin_invites.get(deal_id)
    if not invite: return
    try: await context.bot.send_message(chat_id=invite['admin_id'], text=f"⏰ Время истекло. Сделка передана.")
    except: pass
    await invite_admin_to_deal(context, deal_id)

async def cancel_deal(context: ContextTypes.DEFAULT_TYPE, deal_id: str, reason: str = ""):
    if deal_id not in deals: return
    deal = deals[deal_id]
    try: await context.bot.send_message(chat_id=int(deal['chat_id']), text=f"❌ Сделка отменена\n\n{reason}\n\nБот покидает группу.")
    except: pass
    try: await context.bot.leave_chat(chat_id=int(deal['chat_id']))
    except: pass
    deal['status'] = 'cancelled'
    save_json(DEALS_FILE, deals)
    stats['deals_cancelled'] = stats.get('deals_cancelled', 0) + 1
    save_json(STATS_FILE, stats)

async def admin_deal_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    admin = query.from_user
    if not is_staff(admin.id): await query.answer("❌ Нет прав!", show_alert=True); return
    
    data = query.data
    if 'admin_accept_deal_' in data:
        deal_id = data.replace('admin_accept_deal_', '')
        if deal_id not in deals: await query.edit_message_text("❌ Не найдена."); return
        deal = deals[deal_id]
        deal['admin_accepted'] = True
        deal['assigned_admin'] = str(admin.id)
        save_json(DEALS_FILE, deals)
        try: await context.bot.add_chat_member(chat_id=int(deal['chat_id']), user_id=admin.id)
        except: pass
        await query.edit_message_text(f"✅ Принято!\n🔗 {deal['invite_link']}")
    elif 'admin_decline_deal_' in data:
        deal_id = data.replace('admin_decline_deal_', '')
        if deal_id not in deals: await query.edit_message_text("❌ Не найдена."); return
        await query.edit_message_text("❌ Отказано. Ищем другого...")
        await invite_admin_to_deal(context, deal_id)

async def close_deal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update.effective_user.id): await update.message.reply_text("❌ Нет прав!"); return
    if update.message.chat.type != 'private': await update.message.reply_text("⚠️ Только в ЛС!"); return
    if not context.args: await update.message.reply_text("❌ /close_deal [id]"); return
    deal_id = context.args[0]
    found = next((k for k in deals if k.startswith(deal_id)), None)
    if not found: await update.message.reply_text("❌ Не найдена!"); return
    deal = deals[found]
    deal['status'] = 'completed'
    save_json(DEALS_FILE, deals)
    stats['deals_completed'] = stats.get('deals_completed', 0) + 1
    save_json(STATS_FILE, stats)
    try: await context.bot.send_message(chat_id=int(deal['chat_id']), text="✅ Сделка завершена! Бот покидает группу.")
    except: pass
    try: await context.bot.leave_chat(chat_id=int(deal['chat_id']))
    except: pass
    await update.message.reply_text("✅ Закрыта!")

# ============ ЖАЛОБЫ ============
async def scam_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.chat.type != 'private':
        await update.message.reply_text("⚠️ Пишите боту в ЛС!"); return
    args = context.args
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        if replied.forward_from:
            scammer_id = replied.forward_from.id
            scammer_username = replied.forward_from.username or f'id{scammer_id}'
        elif replied.from_user and replied.from_user.id != user.id:
            scammer_id = replied.from_user.id
            scammer_username = replied.from_user.username or f'id{scammer_id}'
        else:
            await update.message.reply_text("❌ Ответьте на сообщение скамера!"); return
        pending_reports[user.id] = {
            'state': 'waiting_evidence', 'scammer_username': scammer_username,
            'scammer_id': str(scammer_id), 'reason': ' '.join(args) if args else ''
        }
        await update.message.reply_text(f"📝 Жалоба на @{scammer_username}\n📎 Прикрепите фото или /done")
        return
    if args and args[0].startswith('@'):
        pending_reports[user.id] = {
            'state': 'waiting_evidence', 'scammer_username': args[0][1:],
            'scammer_id': '', 'reason': ' '.join(args[1:]) if len(args) > 1 else ''
        }
        await update.message.reply_text(f"📝 Жалоба на {args[0]}\n📎 Прикрепите фото или /done")
        return
    await update.message.reply_text("📝 /scam @username Причина")

async def handle_evidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_reports: return
    report = pending_reports[user.id]
    if report['state'] != 'waiting_evidence': return
    report.setdefault('photos', [])
    if len(report['photos']) >= 5: await update.message.reply_text("⚠️ Макс 5! /done"); return
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    filename = f"{EVIDENCE_DIR}/{user.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(report['photos'])}.jpg"
    await file.download_to_drive(filename)
    report['photos'].append(filename)
    await update.message.reply_text(f"📸 {len(report['photos'])}/5\n/done")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_reports: await update.message.reply_text("❌ Нет жалобы."); return
    report_data = pending_reports[user.id]
    if not report_data.get('reason'): report_data['state'] = 'waiting_reason'; await update.message.reply_text("📝 Укажите причину:"); return
    await submit_report(update, context, user, report_data)

async def handle_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_reports: return
    report = pending_reports[user.id]
    if report['state'] != 'waiting_reason': return
    if update.message.text.startswith('/'): return
    report['reason'] = update.message.text
    await submit_report(update, context, user, report)

async def submit_report(update: Update, context: ContextTypes.DEFAULT_TYPE, user, report_data: Dict):
    report_id = str(datetime.now().timestamp())
    reports[report_id] = {
        'report_id': report_id,
        'scammer_username': report_data.get('scammer_username', ''),
        'scammer_id': report_data.get('scammer_id', ''),
        'reason': report_data.get('reason', ''),
        'photos': report_data.get('photos', []),
        'reported_by': user.username or str(user.id),
        'reported_by_id': str(user.id),
        'status': 'pending', 'date': datetime.now().isoformat()
    }
    save_json(REPORTS_FILE, reports)
    del pending_reports[user.id]
    await update.message.reply_text(f"✅ Отправлено!\n🆔 {report_id[:10]}\n⏳ Ожидайте.")
    await send_report_to_admin(context, report_id)

async def send_report_to_admin(context: ContextTypes.DEFAULT_TYPE, report_id: str, excluded: List[int] = None):
    if report_id not in reports or report_id in report_assignments: return
    report = reports[report_id]
    if excluded is None: excluded = []
    admin_id = get_random_admin(exclude=excluded)
    if not admin_id: return
    report_assignments[report_id] = {
        'admin_id': admin_id, 'expires': datetime.now() + timedelta(minutes=5),
        'excluded_admins': excluded + [admin_id]
    }
    keyboard = [[
        InlineKeyboardButton("🚫 ЗАБАНИТЬ", callback_data=f"report_ban_{report_id}"),
        InlineKeyboardButton("✅ ОТКЛОНИТЬ", callback_data=f"report_reject_{report_id}")
    ]]
    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"🚨 НОВАЯ ЖАЛОБА\n\n🆔 {report_id[:10]}\n👤 @{report['scammer_username']}\n📝 {report['reason']}\n👮 @{report['reported_by']}\n📸 {len(report.get('photos', []))} фото\n⏳ 5 минут!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        for photo_path in report.get('photos', []):
            try:
                with open(photo_path, 'rb') as f:
                    await context.bot.send_photo(chat_id=admin_id, photo=f)
            except: pass
        asyncio.create_task(check_report_timeout(context, report_id, 300))
    except:
        del report_assignments[report_id]
        await send_report_to_admin(context, report_id, excluded + [admin_id])

async def check_report_timeout(context, report_id: str, delay: int = 300):
    await asyncio.sleep(delay)
    if report_id not in reports or reports[report_id]['status'] != 'pending': return
    assignment = report_assignments.get(report_id)
    if not assignment: return
    del report_assignments[report_id]
    try: await context.bot.send_message(chat_id=assignment['admin_id'], text=f"⏰ Жалоба передана.")
    except: pass
    await send_report_to_admin(context, report_id, assignment['excluded_admins'])

async def admin_report_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    admin = query.from_user
    if not is_staff(admin.id): await query.answer("❌ Нет прав!", show_alert=True); return
    
    data = query.data
    
    if 'report_ban_' in data:
        report_id = data.replace('report_ban_', '')
        if report_id not in reports: await query.edit_message_text("❌ Не найдена."); return
        
        report = reports[report_id]
        report['status'] = 'approved'
        report['reviewed_by'] = str(admin.id)
        stats['reports_approved'] = stats.get('reports_approved', 0) + 1
        stats['scammers_banned'] = stats.get('scammers_banned', 0) + 1
        stats['reports_processed'] = stats.get('reports_processed', 0) + 1
        
        scammer_id_str = report.get('scammer_id', '')
        scammer_username = report.get('scammer_username', '')
        clean_username = scammer_username.replace('@', '').strip() if scammer_username else ""
        
        scammer_id = 0
        if scammer_id_str and scammer_id_str.lstrip('-').isdigit():
            try: scammer_id = int(scammer_id_str)
            except: scammer_id = 0
        
        if (not scammer_id or scammer_id == 0) and clean_username:
            if GROUP_ID:
                try:
                    member = await context.bot.get_chat_member(GROUP_ID, f'@{clean_username}')
                    if member and member.user:
                        scammer_id = member.user.id
                        scammer_id_str = str(member.user.id)
                except: pass
            if not scammer_id:
                try:
                    chat = await context.bot.get_chat(f'@{clean_username}')
                    if chat and chat.id:
                        scammer_id = chat.id
                        scammer_id_str = str(chat.id)
                except: pass
        
        banned = await ban_user_via_userbot(scammer_id, clean_username, GROUP_ID)
        if not banned and scammer_id and GROUP_ID:
            try: await context.bot.ban_chat_member(chat_id=GROUP_ID, user_id=scammer_id); banned = True
            except: pass
        
        key = scammer_id_str if scammer_id_str else f"@{clean_username}"
        if not key or key == '@': key = f"report_{report_id[:10]}"
        
        scammers[key] = {
            'username': scammer_username, 'scammer_id': scammer_id_str,
            'reason': report['reason'], 'banned_date': datetime.now().isoformat(),
            'banned_successfully': banned, 'banned_by': admin.username or str(admin.id)
        }
        
        save_json(SCAMMERS_FILE, scammers)
        save_json(REPORTS_FILE, reports)
        save_json(STATS_FILE, stats)
        report_assignments.pop(report_id, None)
        
        status = f"✅ ЗАБАНЕН\n👤 @{scammer_username}" if banned else f"⚠️ В базе скамеров\n👤 @{scammer_username}"
        await query.edit_message_text(f"🚫 {status}")
        try:
            await context.bot.send_message(
                chat_id=int(report['reported_by_id']),
                text=f"✅ Жалоба на @{scammer_username} одобрена! {'Забанен.' if banned else 'В базе скамеров.'}"
            )
        except: pass
    
    elif 'report_reject_' in data:
        report_id = data.replace('report_reject_', '')
        if report_id not in reports: await query.edit_message_text("❌ Не найдена."); return
        report = reports[report_id]
        report['status'] = 'rejected'
        report['reviewed_by'] = str(admin.id)
        stats['reports_rejected'] = stats.get('reports_rejected', 0) + 1
        stats['reports_processed'] = stats.get('reports_processed', 0) + 1
        save_json(REPORTS_FILE, reports)
        save_json(STATS_FILE, stats)
        report_assignments.pop(report_id, None)
        await query.edit_message_text("✅ ОТКЛОНЕНО")
        try: await context.bot.send_message(chat_id=int(report['reported_by_id']), text=f"❌ Жалоба отклонена.")
        except: pass

# ============ ПРОСМОТР ЗАЯВОК ============
async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update.effective_user.id): await update.message.reply_text("❌ Нет прав!"); return
    if update.message.chat.type != 'private': await update.message.reply_text("⚠️ Только в ЛС!"); return
    pending = {k: v for k, v in reports.items() if v['status'] == 'pending'}
    if not pending: await update.message.reply_text("✅ Нет активных заявок!"); return
    sorted_reports = sorted(pending.items(), key=lambda x: x[1]['date'], reverse=True)[:5]
    for report_id, report in sorted_reports:
        text = f"🚨 ЗАЯВКА {report_id[:10]}\n\n👤 @{report['scammer_username']}\n📝 {report['reason']}\n👮 @{report['reported_by']}\n📸 {len(report.get('photos', []))} фото"
        keyboard = [[
            InlineKeyboardButton("🚫 ЗАБАНИТЬ", callback_data=f"report_ban_{report_id}"),
            InlineKeyboardButton("✅ ОТКЛОНИТЬ", callback_data=f"report_reject_{report_id}")
        ]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        for photo_path in report.get('photos', []):
            try:
                with open(photo_path, 'rb') as f:
                    await context.bot.send_photo(chat_id=update.message.chat_id, photo=f)
            except: pass

# ============ ГРУППОВЫЕ ОБРАБОТЧИКИ ============
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user: return
    user = update.message.from_user
    
    if SUBSCRIPTION_CHECK_ENABLED and not await check_subscription(user.id, context):
        await handle_unsubscribed_user(update, context, user.id)
        return
    
    if str(user.id) in scammers:
        try:
            await update.message.delete()
            await context.bot.ban_chat_member(chat_id=update.message.chat_id, user_id=user.id)
        except: pass

async def delete_join_left_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    is_join = update.message.new_chat_members is not None
    is_left = update.message.left_chat_member is not None
    if is_join or is_left:
        try:
            await update.message.delete()
            stats['join_messages_deleted'] = stats.get('join_messages_deleted', 0) + 1
            save_json(STATS_FILE, stats)
        except: pass
        if is_join:
            for member in update.message.new_chat_members:
                if str(member.id) in scammers:
                    try: await context.bot.ban_chat_member(chat_id=update.message.chat_id, user_id=member.id)
                    except: pass

# ============ КОМАНДЫ ============
async def scammer_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not scammers: await update.message.reply_text("✅ Пусто!"); return
    text = f"🚫 Скамеры ({len(scammers)}):\n\n"
    for uid, data in list(scammers.items())[:20]:
        text += f"• {uid}" + (f" @{data['username']}" if data.get('username') else "") + "\n"
    await update.message.reply_text(text[:4000])

async def deals_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update.effective_user.id): await update.message.reply_text("❌ Нет прав!"); return
    if update.message.chat.type != 'private': await update.message.reply_text("⚠️ Только в ЛС!"); return
    active = {k: v for k, v in deals.items() if v['status'] == 'active'}
    if not active: await update.message.reply_text("✅ Нет активных сделок!"); return
    text = f"🔒 Сделки ({len(active)}):\n\n"
    for did, deal in list(active.items())[:10]:
        text += f"🆔 {did[:10]} | #{deal.get('deal_number', '?')}\n💰 {deal['price']}\n👤 @{deal['creator_username']} ↔ @{deal['partner_username']}\n👮 {'✅' if deal.get('admin_accepted') else '⏳'}\n\n"
    await update.message.reply_text(text[:4000])

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update.effective_user.id): await update.message.reply_text("❌ Нет прав!"); return
    if update.message.chat.type != 'private': await update.message.reply_text("⚠️ Только в ЛС!"); return
    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"🗑 Сообщений: {stats.get('messages_deleted', 0)}\n"
        f"🚪 Входов/выходов: {stats.get('join_messages_deleted', 0)}\n"
        f"⚠️ Предупреждений: {stats.get('subscription_warnings', 0)}\n"
        f"🔇 Замучено: {stats.get('users_muted', 0)}\n"
        f"🚫 Скамеров: {len(scammers)}\n"
        f"📋 Жалоб: {stats.get('reports_processed', 0)}\n"
        f"🔒 Сделок: {stats.get('deals_created', 0)}"
    )

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.message.chat.type
    if chat_type == 'private':
        if is_staff(user.id):
            role = "👑 Владелец" if is_owner(user.id) else "👮 Гарант/Админ"
            await update.message.reply_text(
                f"🤖 ILLUMI DEAL BOT\n\n{role}\n\n"
                f"📝 /scam @user — жалоба\n🔒 /deal @user цена — сделка\n"
                f"🚫 /scammer_list — скамеры\n📋 /reports — заявки\n"
                f"📊 /stats — статистика\n📋 /deals — сделки\n"
                f"/close_deal [id] — закрыть"
            )
        else:
            await update.message.reply_text(
                f"🤖 ILLUMI DEAL BOT\n\n"
                f"📝 /scam @user — жалоба\n🔒 /deal @user цена — сделка\n"
                f"🚫 /scammer_list — скамеры"
            )
    else:
        await update.message.reply_text(
            f"🤖 ILLUMI DEAL BOT\n\n"
            f"🔒 /deal @user цена (в ЛС)\n📝 /scam @user (в ЛС)\n"
            f"🚫 /scammer_list — скамеры"
        )

# ============ ЗАПУСК ============
# ============ ЗАПУСК ============
# ============ ЗАПУСК ============
def main():
    global app_job_queue, bot_instance
    
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN не указан!")
        return
    
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )
    
    app_job_queue = application.job_queue
    
    application.add_handler(CommandHandler('deal', deal_command))
    application.add_handler(CommandHandler('ready', ready_command))
    application.add_handler(CallbackQueryHandler(admin_deal_response, pattern='^admin_'))
    application.add_handler(CommandHandler('close_deal', close_deal_cmd))
    application.add_handler(CommandHandler('scam', scam_command))
    application.add_handler(CommandHandler('done', done_command))
    application.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_evidence))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_reason))
    application.add_handler(CallbackQueryHandler(admin_report_response, pattern='^report_'))
    application.add_handler(CommandHandler('reports', reports_cmd))
    application.add_handler(CommandHandler('deals', deals_list_cmd))
    application.add_handler(CommandHandler('stats', stats_cmd))
    application.add_handler(CommandHandler('scammer_list', scammer_list_cmd))
    application.add_handler(MessageHandler(~filters.COMMAND & filters.ChatType.GROUPS, message_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.ALL, delete_join_left_messages))
    application.add_handler(ChatMemberHandler(admin_status_changed, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(CommandHandler('start', start_cmd))
    
    async def post_init(app):
        global bot_instance
        bot_instance = app.bot
        
        await resolve_usernames(app.bot)
        await load_garants_from_group(app.bot)
        
        async def keep_alive(ctx):
            logger.debug("💓 Keep-alive")
        
        if app.job_queue:
            app.job_queue.run_repeating(keep_alive, interval=300)
        
        print(f"""
╔══════════════════════════════════════╗
║   🤖 ILLUMI DEAL BOT v8.0         ║
║   👑 {resolved_owner_id or 'не указан'}                       ║
║   👮 .env: {len(resolved_admin_ids)}  🏷 Гарантов: {len(group_garant_ids)}      ║
║   🔇 Мут после {WARNING_LIMIT} предупреждений         ║
║   💓 Keep-alive: 5 мин            ║
╚══════════════════════════════════════╝
        """)
    
    application.post_init = post_init
    
    print("🤖 Запуск бота...")
    
    if WEBHOOK_URL:
        print(f"🔗 Webhook: {WEBHOOK_URL}")
        application.run_webhook(
            listen="0.0.0.0",
            port=WEBHOOK_PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            drop_pending_updates=True
        )
    else:
        print("🔄 Polling...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

        
if __name__ == '__main__':
    main()