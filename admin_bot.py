import logging
import json
import os
import random
import re
import asyncio
import signal
import sys
import glob
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, BotCommand
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
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', '8443'))

SUBSCRIPTION_CHECK_ENABLED = bool(CHANNEL_ID)
WARNING_LIMIT = 2
MUTE_DURATION = 4 * 3600

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
PERMANENT_STATS_FILE = 'permanent_stats.json'
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

def cleanup_evidence():
    try:
        files = glob.glob(f"{EVIDENCE_DIR}/*.jpg")
        for f in files:
            try: os.remove(f)
            except: pass
        if files: logger.info(f"🧹 Очищено {len(files)} фото")
    except: pass

def cleanup_logs():
    try:
        for log_file in ['moderator_bot.log']:
            if os.path.exists(log_file): open(log_file, 'w').close()
        logger.info("🧹 Логи очищены")
    except: pass

def cleanup_reports():
    try:
        to_delete = [rid for rid, r in reports.items() if r.get('status') in ['approved', 'rejected'] and datetime.now() - datetime.fromisoformat(r.get('date', '2000-01-01T00:00:00')) > timedelta(days=1)]
        for rid in to_delete: del reports[rid]
        if to_delete: save_json(REPORTS_FILE, reports)
    except: pass

def cleanup_all():
    cleanup_evidence()
    cleanup_logs()
    cleanup_reports()
    logger.info("🧹 Полная очистка выполнена")

permanent_stats = load_json(PERMANENT_STATS_FILE) or {'total_deals_completed': 0, 'total_deals_cancelled': 0}
scammers = load_json(SCAMMERS_FILE)
reports = load_json(REPORTS_FILE)
stats = load_json(STATS_FILE) or {'messages_deleted': 0, 'scammers_banned': 0, 'join_messages_deleted': 0, 'reports_processed': 0, 'reports_approved': 0, 'reports_rejected': 0, 'subscription_warnings': 0, 'users_muted': 0, 'deals_created': 0, 'deals_completed': 0, 'deals_cancelled': 0}
warned_users = load_json(WARNED_USERS_FILE)
muted_users = load_json(MUTED_USERS_FILE)
deals = load_json(DEALS_FILE)
deal_counter = load_json(DEAL_COUNTER_FILE) or {'counter': 0}

pending_reports = {}
pending_deals = {}
admin_invites = {}
report_assignments = {}
app_job_queue = None
bot_instance = None

resolved_owner_id = None
resolved_admin_ids = []
group_garant_ids = []

def signal_handler(sig, frame): print("\n👋 Завершение..."); sys.exit(0)
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
        except: pass
    
    if OWNER_ID and not resolved_owner_id: resolved_owner_id = OWNER_ID
    
    ADMIN_IDS_RAW = os.getenv('ADMIN_IDS', '')
    ADMIN_USERNAMES = os.getenv('ADMIN_USERNAMES', '')
    
    resolved_admin_ids = [int(x.strip()) for x in ADMIN_IDS_RAW.split(',') if x.strip().isdigit()]
    
    if ADMIN_USERNAMES:
        for username in ADMIN_USERNAMES.split(','):
            username = username.replace('@', '').strip()
            if username:
                try:
                    user = await bot.get_chat(f'@{username}')
                    if user.id not in resolved_admin_ids: resolved_admin_ids.append(user.id)
                except: pass

async def load_garants_from_group(bot):
    global group_garant_ids
    if not GROUP_ID: return
    try:
        group_garant_ids = []
        admins = await bot.get_chat_administrators(chat_id=GROUP_ID)
        for admin in admins:
            title = getattr(admin, 'custom_title', '') or ''
            if ADMIN_TAG.lower() in title.lower(): group_garant_ids.append(admin.user.id)
    except: pass

async def admin_status_changed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_member: return
    await load_garants_from_group(context.bot)

# ============ УДАЛЕНИЕ СООБЩЕНИЙ ============
async def delete_message_later(chat_id: int, message_id: int, delay: int = 10):
    await asyncio.sleep(delay)
    try:
        if bot_instance: await bot_instance.delete_message(chat_id=chat_id, message_id=message_id)
    except: pass

# ============ ПРОВЕРКА ПОДПИСКИ И МУТ ============
async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not SUBSCRIPTION_CHECK_ENABLED or not CHANNEL_ID: return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]
    except: return True

async def mute_user(user_id: int, chat_id: str, duration: int, context) -> bool:
    client = TelegramClient(USERBOT_SESSION, USERBOT_API_ID, USERBOT_API_HASH)
    try:
        await client.connect()
        if await client.is_user_authorized():
            g = await client.get_entity(int(chat_id))
            until = datetime.now() + timedelta(seconds=duration)
            rights = ChatBannedRights(
                until_date=until, view_messages=False,
                send_messages=True, send_media=True, send_stickers=True,
                send_gifs=True, send_games=True, send_inline=True,
                embed_links=True, send_polls=True,
                change_info=False, invite_users=False, pin_messages=False
            )
            await client(EditBannedRequest(channel=g, participant=user_id, banned_rights=rights))
            await client.disconnect()
            return True
    except: pass
    finally:
        try: await client.disconnect()
        except: pass
    try:
        until = datetime.now() + timedelta(seconds=duration)
        await context.bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id,
            permissions={'can_send_messages': False, 'can_send_media': False, 'can_send_other_messages': False, 'can_add_web_page_previews': False},
            until_date=until
        )
        return True
    except: return False

async def handle_unsubscribed_user(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    try: await update.message.delete()
    except: pass
    stats['messages_deleted'] = stats.get('messages_deleted', 0) + 1
    user_key = str(user_id)
    now = datetime.now()
    link = CHANNEL_LINK or f"https://t.me/{CHANNEL_ID.replace('@', '')}"
    user = await context.bot.get_chat(user_id)
    name = user.first_name or 'Пользователь'
    
    if user_key not in warned_users:
        warned_users[user_key] = {'count': 1}
        save_json(WARNED_USERS_FILE, warned_users)
        msg = await update.message.chat.send_message(f"⚠️ {name}, подпишитесь: {link}\n\nПредупреждение 1/{WARNING_LIMIT}\nПосле {WARNING_LIMIT} - мут 4ч\n\nИсчезнет через 10с", disable_web_page_preview=True)
        asyncio.create_task(delete_message_later(msg.chat_id, msg.message_id, 10))
        stats['subscription_warnings'] = stats.get('subscription_warnings', 0) + 1
    else:
        warned_users[user_key]['count'] += 1
        cnt = warned_users[user_key]['count']
        save_json(WARNED_USERS_FILE, warned_users)
        if cnt >= WARNING_LIMIT:
            if await mute_user(user_id, GROUP_ID or str(update.message.chat_id), MUTE_DURATION, context):
                stats['users_muted'] = stats.get('users_muted', 0) + 1
                muted_users[user_key] = {'at': now.isoformat(), 'until': (now + timedelta(seconds=MUTE_DURATION)).isoformat()}
                save_json(MUTED_USERS_FILE, muted_users)
                msg = await update.message.chat.send_message(f"🔇 {name} замучен на 4ч\nИсчезнет через 10с")
                asyncio.create_task(delete_message_later(msg.chat_id, msg.message_id, 10))
                warned_users[user_key]['count'] = 0
                save_json(WARNED_USERS_FILE, warned_users)
        else:
            msg = await update.message.chat.send_message(f"⚠️ {name}, подпишитесь: {link}\n\nПредупреждение {cnt}/{WARNING_LIMIT}\nИсчезнет через 10с", disable_web_page_preview=True)
            asyncio.create_task(delete_message_later(msg.chat_id, msg.message_id, 10))
            stats['subscription_warnings'] = stats.get('subscription_warnings', 0) + 1
    save_json(STATS_FILE, stats)

# ============ БАН ============
async def ban_user(user_id, username: str = "", group_id: str = None) -> bool:
    if not group_id: group_id = GROUP_ID
    if not group_id: return False
    client = TelegramClient(USERBOT_SESSION, USERBOT_API_ID, USERBOT_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized(): await client.disconnect(); return False
        resolved = user_id
        clean = username.replace('@', '').strip() if username else ""
        if (not resolved or resolved == 0) and clean:
            try: resolved = (await client.get_entity(f'@{clean}')).id
            except: pass
        if not resolved: await client.disconnect(); return False
        g = await client.get_entity(int(group_id))
        rights = ChatBannedRights(until_date=None, view_messages=True, send_messages=True, send_media=True, send_stickers=True, send_gifs=True, send_games=True, send_inline=True, embed_links=True, send_polls=True, change_info=True, invite_users=True, pin_messages=True)
        await client(EditBannedRequest(channel=g, participant=resolved, banned_rights=rights))
        await client.disconnect()
        return True
    except:
        try: await client.disconnect()
        except: pass
        return False

# ============ ПРОВЕРКА ДОСТУПА ============
def is_owner(uid: int) -> bool: return resolved_owner_id is not None and uid == resolved_owner_id
def is_admin(uid: int) -> bool: return uid in resolved_admin_ids or uid in group_garant_ids
def is_staff(uid: int) -> bool: return is_owner(uid) or is_admin(uid)

def get_random_admin(exclude: List[int] = None) -> Optional[int]:
    all_a = list(set(resolved_admin_ids + group_garant_ids))
    if not all_a: return None
    avail = [a for a in all_a if a not in (exclude or [])]
    return random.choice(avail) if avail else None

def get_next_deal_number() -> int:
    deal_counter['counter'] += 1
    save_json(DEAL_COUNTER_FILE, deal_counter)
    return deal_counter['counter']

def parse_deal(text: str) -> Optional[Dict]:
    parts = text.strip().split()
    if len(parts) < 3 or not parts[1].startswith('@'): return None
    return {'partner': parts[1][1:], 'price': parts[2], 'desc': ' '.join(parts[3:]) if len(parts) > 3 else ''}

# ============ СДЕЛКИ ============
async def deal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != 'private': await update.message.reply_text("⚠️ Сделки в ЛС!"); return
    user = update.effective_user
    p = parse_deal(update.message.text or "")
    if not p: await update.message.reply_text("❌ /deal @user цена [описание]"); return
    if not list(set(resolved_admin_ids + group_garant_ids)): await update.message.reply_text("❌ Нет гарантов"); return
    
    dn = get_next_deal_number()
    did = str(datetime.now().timestamp())
    pending_deals[user.id] = {'did': did, 'dn': dn, 'creator': str(user.id), 'cname': user.username or str(user.id), 'partner': p['partner'], 'price': p['price'], 'desc': p['desc'], 'status': 'waiting', 'excluded': []}
    d = f"\n📝 {p['desc']}" if p['desc'] else ""
    await update.message.reply_text(f"📋 Сделка #{dn}\n👤 @{p['partner']}\n💰 {p['price']}{d}\n\n1. Создайте группу\n2. Добавьте бота админом\n3. Добавьте @{p['partner']}\n4. /ready в группе\n\n⚠️ Настоящий гарант подтверждается ботом в чате!")

async def ready_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type not in ['group', 'supergroup']: return
    user = update.effective_user
    dd = pending_deals.get(user.id)
    if not dd: await update.message.reply_text("❌ Нет сделки. /deal в ЛС"); return
    try:
        if (await context.bot.get_chat_member(update.message.chat_id, context.bot.id)).status != ChatMemberStatus.ADMINISTRATOR:
            await update.message.reply_text("❌ Бот не админ!"); return
    except: await update.message.reply_text("❌ Бот не в группе!"); return
    
    try: link = (await context.bot.create_chat_invite_link(update.message.chat_id)).invite_link
    except: link = f"https://t.me/c/{str(update.message.chat_id).replace('-100', '')}"
    
    deals[dd['did']] = {'did': dd['did'], 'dn': dd['dn'], 'cid': str(update.message.chat_id), 'link': link, 'creator': dd['creator'], 'cname': dd['cname'], 'partner': dd['partner'], 'price': dd['price'], 'desc': dd['desc'], 'status': 'active', 'accepted': False, 'excluded': dd.get('excluded', [])}
    save_json(DEALS_FILE, deals)
    stats['deals_created'] = stats.get('deals_created', 0) + 1
    save_json(STATS_FILE, stats)
    del pending_deals[user.id]
    d = f"\n📝 {dd['desc']}" if dd.get('desc') else ""
    await update.message.reply_text(f"📢 СДЕЛКА #{dd['dn']}\n👤 @{dd['cname']} ↔ @{dd['partner']}\n💰 {dd['price']}{d}\n🆔 `{dd['did'][:10]}`\n⏳ Гарант...")
    await invite_admin(context, dd['did'])

async def invite_admin(context, did: str):
    if did not in deals: return
    d = deals[did]
    aid = get_random_admin(exclude=d.get('excluded', []))
    if not aid: await cancel_deal(context, did, "Нет гарантов"); return
    
    # Сохраняем context для таймаута
    admin_invites[did] = {
        'aid': aid,
        'exp': datetime.now() + timedelta(minutes=5),
        'excluded': d.get('excluded', []) + [aid],
        'message_id': None,
        'chat_id': aid,
        'context': context,  # СОХРАНЯЕМ CONTEXT
        'deal_id': did
    }
    d['excluded'] = d.get('excluded', []) + [aid]
    save_json(DEALS_FILE, deals)
    
    kb = [[
        InlineKeyboardButton("✅ Принять", callback_data=f"ad_{did}"),
        InlineKeyboardButton("❌ Отказ", callback_data=f"adc_{did}")
    ]]
    
    try:
        msg = await context.bot.send_message(
            aid,
            f"🔒 СДЕЛКА #{d.get('dn','?')}\n\n"
            f"👤 @{d['cname']} ↔ @{d['partner']}\n"
            f"💰 {d['price']}\n"
            f"🔗 {d['link']}\n"
            f"🆔 `{did[:10]}`\n\n"
            f"⏳ 5 минут!\n"
            f"При входе в группу бот подтвердит вас.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        admin_invites[did]['message_id'] = msg.message_id
        logger.info(f"✅ Приглашение отправлено гаранту {aid} для сделки {did[:10]}")
        
        # Запускаем таймер
        asyncio.create_task(check_deal_timeout(did, 300))
    except Exception as e:
        logger.error(f"Ошибка отправки гаранту {aid}: {e}")
        # Пробуем следующего
        await invite_admin(context, did)

async def check_deal_timeout(did: str, delay: int = 300):
    """Таймаут ответа гаранта с удалением сообщения"""
    await asyncio.sleep(delay)
    
    if did not in deals:
        logger.info(f"Сделка {did[:10]} уже не существует")
        return
    
    if deals[did].get('accepted'):
        logger.info(f"Сделка {did[:10]} уже принята")
        return
    
    invite = admin_invites.get(did)
    if not invite:
        logger.info(f"Приглашение для {did[:10]} не найдено")
        return
    
    logger.info(f"⏰ Таймаут сделки {did[:10]}, удаляю сообщение у {invite['aid']}")
    
    # Удаляем сообщение с кнопками у старого гаранта
    try:
        if invite.get('message_id') and invite.get('chat_id'):
            context = invite.get('context')
            if context:
                await context.bot.delete_message(
                    chat_id=invite['chat_id'],
                    message_id=invite['message_id']
                )
                logger.info(f"🗑 Сообщение у гаранта {invite['aid']} удалено")
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
    
    # Временное уведомление
    try:
        context = invite.get('context')
        if context:
            notify = await context.bot.send_message(
                invite['aid'],
                "⏰ Время на сделку истекло. Сделка передана другому гаранту."
            )
            asyncio.create_task(delete_message_later(invite['aid'], notify.message_id, 30))
    except Exception as e:
        logger.error(f"Ошибка уведомления: {e}")
    
    # Приглашаем следующего гаранта
    context = invite.get('context')
    if context:
        logger.info(f"📤 Отправляю приглашение следующему гаранту для {did[:10]}")
        await invite_admin(context, did)
    else:
        logger.error(f"❌ Нет context для приглашения следующего гаранта!")

async def cancel_deal(context, did: str, reason: str = ""):
    if did not in deals: return
    d = deals[did]
    try: await context.bot.send_message(int(d['cid']), f"❌ Отменена\n{reason}")
    except: pass
    try: await context.bot.leave_chat(int(d['cid']))
    except: pass
    d['status'] = 'cancelled'
    save_json(DEALS_FILE, deals)
    stats['deals_cancelled'] = stats.get('deals_cancelled', 0) + 1
    permanent_stats['total_deals_cancelled'] += 1
    save_json(STATS_FILE, stats)
    save_json(PERMANENT_STATS_FILE, permanent_stats)

async def admin_deal_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_staff(q.from_user.id): await q.answer("❌ Нет прав!", show_alert=True); return
    data = q.data
    
    if data.startswith('ad_'):
        did = data[3:]
        if did not in deals: await q.edit_message_text("❌"); return
        
        d = deals[did]
        d['accepted'] = True
        d['admin_id'] = str(q.from_user.id)
        d['admin_name'] = q.from_user.username or str(q.from_user.id)
        save_json(DEALS_FILE, deals)
        
        try: await context.bot.add_chat_member(int(d['cid']), q.from_user.id)
        except: pass
        
        # Подтверждение в группе
        try:
            await context.bot.send_message(int(d['cid']), f"👮 *Гарант подтвержден!*\n\nГарант @{d['admin_name']} присоединился к сделке #{d.get('dn','?')}\n🆔 `{did[:10]}`\n\n✅ Это НАСТОЯЩИЙ гарант.\n⚠️ Сохраняйте скриншоты! \n если гарант не присоеденился, хотя подтверждение было. Владелец должен добавить гаранта вручную по нужному @username.", parse_mode=ParseMode.MARKDOWN)
        except: pass
        
        await q.edit_message_text(f"✅ Вы приняли сделку #{d.get('dn','?')}\n🆔 `{did[:10]}`\n🔗 {d['link']}", parse_mode=ParseMode.MARKDOWN)
        
        try: await context.bot.send_message(int(d['creator']), f"✅ Гарант @{d['admin_name']} назначен на сделку #{d.get('dn','?')}\n🆔 `{did[:10]}`")
        except: pass
    
    elif data.startswith('adc_'):
        did = data[4:]
        if did not in deals: await q.edit_message_text("❌"); return
        await q.edit_message_text("❌ Вы отказались. Ищем другого...")
        await invite_admin(context, did)

async def close_deal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update.effective_user.id): await update.message.reply_text("❌"); return
    if not context.args: await update.message.reply_text("❌ /close_deal [id]"); return
    found = next((k for k in deals if k.startswith(context.args[0])), None)
    if not found: await update.message.reply_text("❌"); return
    deals[found]['status'] = 'completed'
    save_json(DEALS_FILE, deals)
    stats['deals_completed'] = stats.get('deals_completed', 0) + 1
    permanent_stats['total_deals_completed'] += 1
    save_json(STATS_FILE, stats)
    save_json(PERMANENT_STATS_FILE, permanent_stats)
    try: await context.bot.send_message(int(deals[found]['cid']), "✅ Завершена!")
    except: pass
    try: await context.bot.leave_chat(int(deals[found]['cid']))
    except: pass
    await update.message.reply_text("✅")

# ============ ЖАЛОБЫ ============
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.chat.type != 'private': await update.message.reply_text("⚠️ ЛС!"); return
    cap = update.message.caption or update.message.text or ""
    args_text = cap.replace('/report', '', 1).strip() if cap.startswith('/report') else cap
    args = args_text.split() if args_text else []
    sid, suname, reason, photos = "", "", "", []
    if update.message.reply_to_message:
        r = update.message.reply_to_message
        if r.forward_from: sid, suname = str(r.forward_from.id), r.forward_from.username or f'id{sid}'
        elif r.from_user and r.from_user.id != user.id: sid, suname = str(r.from_user.id), r.from_user.username or f'id{sid}'
        reason = args_text
    elif args:
        for i, a in enumerate(args):
            if a.startswith('@'): suname = a[1:]; reason = ' '.join(args[i+1:]) if i+1 < len(args) else ''; break
        if not suname: reason = ' '.join(args)
    if not suname and not sid: await update.message.reply_text("📝 /report @user причина + фото"); return
    if update.message.photo:
        ph = update.message.photo[-1]; f = await context.bot.get_file(ph.file_id)
        fn = f"{EVIDENCE_DIR}/{user.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_0.jpg"
        await f.download_to_drive(fn); photos.append(fn)
    rid = str(datetime.now().timestamp())
    reports[rid] = {'rid': rid, 'suname': suname, 'sid': sid, 'reason': reason or 'Не указана', 'photos': photos, 'by': user.username or str(user.id), 'by_id': str(user.id), 'status': 'pending', 'date': datetime.now().isoformat()}
    save_json(REPORTS_FILE, reports)
    await update.message.reply_text(f"✅ Отправлено!\n🆔 {rid[:10]}\n⏳ Ожидайте")
    await send_report(context, rid)

async def scam_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await report_command(update, context)
async def handle_evidence(update: Update, context: ContextTypes.DEFAULT_TYPE): await report_command(update, context)
async def handle_reason(update, context): pass
async def done_command(update, context): pass

async def send_report(context, rid: str, excluded: List[int] = None):
    if rid not in reports or rid in report_assignments: return
    r = reports[rid]
    aid = get_random_admin(exclude=excluded or [])
    if not aid: return
    report_assignments[rid] = {'aid': aid, 'exp': datetime.now() + timedelta(minutes=5), 'excluded': (excluded or []) + [aid]}
    kb = [[InlineKeyboardButton("🚫 БАН", callback_data=f"rb_{rid}"), InlineKeyboardButton("✅ ОТКЛОН", callback_data=f"rr_{rid}")]]
    photos = r.get('photos', [])
    try:
        if photos:
            with open(photos[0], 'rb') as pf: await context.bot.send_photo(aid, pf, caption=f"🚨 ЖАЛОБА\n🆔 {rid[:10]}\n👤 @{r['suname']}\n📝 {r['reason']}\n👮 @{r['by']}\n📸 {len(photos)} фото\n⏳ 5 мин!", reply_markup=InlineKeyboardMarkup(kb))
            for pp in photos[1:]:
                try:
                    with open(pp, 'rb') as pf: await context.bot.send_photo(aid, pf)
                except: pass
        else:
            await context.bot.send_message(aid, f"🚨 ЖАЛОБА\n🆔 {rid[:10]}\n👤 @{r['suname']}\n📝 {r['reason']}\n👮 @{r['by']}\n⏳ 5 мин!", reply_markup=InlineKeyboardMarkup(kb))
    except:
        del report_assignments[rid]
        await send_report(context, rid, (excluded or []) + [aid])

async def admin_report_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_staff(q.from_user.id): await q.answer("❌", show_alert=True); return
    data = q.data
    if data.startswith('rb_'):
        rid = data[3:]
        if rid not in reports: await q.edit_message_text("❌"); return
        r = reports[rid]; r['status'] = 'approved'
        stats['reports_approved'] = stats.get('reports_approved', 0) + 1
        stats['scammers_banned'] = stats.get('scammers_banned', 0) + 1
        stats['reports_processed'] = stats.get('reports_processed', 0) + 1
        clean = r['suname'].replace('@', '').strip()
        uid = int(r['sid']) if r.get('sid', '').lstrip('-').isdigit() else 0
        if not uid and clean:
            try: uid = (await context.bot.get_chat_member(GROUP_ID, f'@{clean}')).user.id
            except:
                try: uid = (await context.bot.get_chat(f'@{clean}')).id
                except: pass
        banned = await ban_user(uid, clean, GROUP_ID)
        scammers[str(uid) if uid else clean] = {'username': r['suname'], 'sid': str(uid), 'reason': r['reason'], 'banned': banned, 'date': datetime.now().isoformat()}
        save_json(SCAMMERS_FILE, scammers)
        save_json(REPORTS_FILE, reports)
        save_json(STATS_FILE, stats)
        report_assignments.pop(rid, None)
        for pp in r.get('photos', []):
            try: os.remove(pp)
            except: pass
        await q.edit_message_text(f"🚫 {'✅ ЗАБАНЕН' if banned else '⚠️ В базе'}\n👤 @{r['suname']}")
    elif data.startswith('rr_'):
        rid = data[3:]
        if rid not in reports: await q.edit_message_text("❌"); return
        r = reports[rid]; r['status'] = 'rejected'
        stats['reports_rejected'] = stats.get('reports_rejected', 0) + 1
        stats['reports_processed'] = stats.get('reports_processed', 0) + 1
        save_json(REPORTS_FILE, reports)
        save_json(STATS_FILE, stats)
        report_assignments.pop(rid, None)
        for pp in r.get('photos', []):
            try: os.remove(pp)
            except: pass
        await q.edit_message_text("✅ ОТКЛОНЕНО")

# ============ ПРОСМОТР ЗАЯВОК ============
async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update.effective_user.id): await update.message.reply_text("❌"); return
    pending = {k: v for k, v in reports.items() if v['status'] == 'pending'}
    if not pending: await update.message.reply_text("✅ Нет заявок"); return
    for rid, r in sorted(pending.items(), key=lambda x: x[1]['date'], reverse=True)[:5]:
        kb = [[InlineKeyboardButton("🚫 БАН", callback_data=f"rb_{rid}"), InlineKeyboardButton("✅ ОТКЛОН", callback_data=f"rr_{rid}")]]
        await update.message.reply_text(f"🚨 {rid[:10]}\n👤 @{r['suname']}\n📝 {r['reason']}\n👮 @{r['by']}", reply_markup=InlineKeyboardMarkup(kb))
        for pp in r.get('photos', []):
            try:
                with open(pp, 'rb') as pf: await context.bot.send_photo(update.message.chat_id, pf)
            except: pass

# ============ ГРУППА ============
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user: return
    u = update.message.from_user
    if SUBSCRIPTION_CHECK_ENABLED and not await check_subscription(u.id, context): await handle_unsubscribed_user(update, context, u.id); return
    if str(u.id) in scammers:
        try: await update.message.delete(); await context.bot.ban_chat_member(update.message.chat_id, u.id)
        except: pass

async def delete_join_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    if update.message.new_chat_members or update.message.left_chat_member:
        try: await update.message.delete(); stats['join_messages_deleted'] = stats.get('join_messages_deleted', 0) + 1; save_json(STATS_FILE, stats)
        except: pass

# ============ ПАГИНАЦИЯ СКАМЕРОВ ============
SCAMMERS_PER_PAGE = 10

async def scammer_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args and context.args[0].isdigit(): page = int(context.args[0])
    items = list(scammers.items())
    total_pages = max(1, (len(items) + SCAMMERS_PER_PAGE - 1) // SCAMMERS_PER_PAGE)
    if page < 1: page = 1
    if page > total_pages: page = total_pages
    start = (page - 1) * SCAMMERS_PER_PAGE
    end = start + SCAMMERS_PER_PAGE
    page_items = items[start:end]
    if not scammers: await update.message.reply_text("✅ Пусто!"); return
    text = f"🚫 *Скамеры* (стр. {page}/{total_pages}, всего {len(scammers)}):\n\n"
    for uid, data in page_items:
        text += f"• `{uid}`"
        if data.get('username'): text += f" @{data['username']}"
        if data.get('reason'): text += f" — {data['reason'][:40]}"
        text += "\n"
    buttons = []
    row = []
    if page > 1: row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"sc_page_{page-1}"))
    if page < total_pages: row.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"sc_page_{page+1}"))
    if row: buttons.append(row)
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(text[:4000], parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

async def scammer_pagination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data
    if data.startswith('sc_page_'):
        page = int(data.replace('sc_page_', ''))
        items = list(scammers.items())
        total_pages = max(1, (len(items) + SCAMMERS_PER_PAGE - 1) // SCAMMERS_PER_PAGE)
        if page < 1: page = 1
        if page > total_pages: page = total_pages
        start = (page - 1) * SCAMMERS_PER_PAGE
        end = start + SCAMMERS_PER_PAGE
        page_items = items[start:end]
        text = f"🚫 *Скамеры* (стр. {page}/{total_pages}, всего {len(scammers)}):\n\n"
        for uid, data in page_items:
            text += f"• `{uid}`"
            if data.get('username'): text += f" @{data['username']}"
            if data.get('reason'): text += f" — {data['reason'][:40]}"
            text += "\n"
        buttons = []
        row = []
        if page > 1: row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"sc_page_{page-1}"))
        if page < total_pages: row.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"sc_page_{page+1}"))
        if row: buttons.append(row)
        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        try: await q.edit_message_text(text[:4000], parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except: await q.edit_message_text(text[:4000], reply_markup=reply_markup)

# ============ HELP ============
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if is_owner(u.id):
        text = "🤖 ILLUMI BOT — Владелец\n\n/report @user — жалоба\n/deal @user цена — сделка\n/scammer_list — скамеры\n/reports — заявки\n/stats — статистика\n/deals — сделки\n/close_deal [id] — закрыть\n/help — помощь"
    elif is_admin(u.id):
        text = "🤖 ILLUMI BOT — Гарант\n\n/report @user — жалоба\n/deal @user цена — сделка\n/scammer_list — скамеры\n/reports — заявки\n/deals — сделки\n/close_deal [id] — закрыть\n/help — помощь"
    else:
        text = "🤖 ILLUMI BOT\n\n/report @user — жалоба\n/deal @user цена — сделка\n/scammer_list — скамеры\n/help — помощь"
    
    buttons = [["/help"]]
    reply_markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text(text, reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/help": await help_cmd(update, context)

# ============ ДРУГИЕ КОМАНДЫ ============
async def deals_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_staff(update.effective_user.id): return
    if update.message.chat.type != 'private': return
    active = {k: v for k, v in deals.items() if v['status'] == 'active'}
    if not active: await update.message.reply_text("✅ Нет сделок"); return
    t = f"🔒 Сделки ({len(active)}):\n\n"
    for did, d in list(active.items())[:10]: t += f"🆔 {did[:10]} | #{d.get('dn','?')}\n💰 {d['price']}\n👤 @{d['cname']} ↔ @{d['partner']}\n👮 {'✅' if d.get('accepted') else '⏳'}\n\n"
    await update.message.reply_text(t[:4000])

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if update.message.chat.type != 'private': return
    await update.message.reply_text(f"📊 Статистика\n\n🗑 Удалено: {stats.get('messages_deleted', 0)}\n🚪 Входы/выходы: {stats.get('join_messages_deleted', 0)}\n⚠️ Предупреждения: {stats.get('subscription_warnings', 0)}\n🔇 Муты: {stats.get('users_muted', 0)}\n🚫 Скамеры: {len(scammers)}\n📋 Жалобы: {stats.get('reports_processed', 0)}\n🔒 Сделок создано: {stats.get('deals_created', 0)}\n📊 ВСЕГО:\n✅ Успешных: {permanent_stats.get('total_deals_completed', 0)}\n❌ Отмененных: {permanent_stats.get('total_deals_cancelled', 0)}")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if is_owner(u.id): await update.message.reply_text("🤖 ILLUMI BOT — Владелец\n\n/deal | /report | /scammer_list | /reports | /stats | /deals | /close_deal | /help")
    elif is_admin(u.id): await update.message.reply_text("🤖 ILLUMI BOT — Гарант\n\n/deal | /report | /scammer_list | /reports | /deals | /close_deal | /help")
    else: await update.message.reply_text("🤖 ILLUMI BOT\n\n/deal | /report | /scammer_list | /help")

async def set_bot_commands(app):
    commands = [BotCommand("deal", "создать сделку"), BotCommand("report", "пожаловаться на скамера"), BotCommand("scammer_list", "список скамеров"), BotCommand("help", "помощь")]
    await app.bot.set_my_commands(commands)

# ============ ЗАПУСК ============
def main():
    global app_job_queue, bot_instance
    if not BOT_TOKEN: print("❌ BOT_TOKEN!"); return
    
    application = Application.builder().token(BOT_TOKEN).read_timeout(60).write_timeout(60).connect_timeout(60).pool_timeout(60).build()
    app_job_queue = application.job_queue
    
    application.add_handler(CommandHandler('deal', deal_command))
    application.add_handler(CommandHandler('ready', ready_command))
    application.add_handler(CallbackQueryHandler(admin_deal_response, pattern='^ad'))
    application.add_handler(CommandHandler('close_deal', close_deal_cmd))
    application.add_handler(CommandHandler('report', report_command))
    application.add_handler(CommandHandler('scam', scam_command))
    application.add_handler(CommandHandler('done', done_command))
    application.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_evidence))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_reason))
    application.add_handler(CallbackQueryHandler(admin_report_response, pattern='^r'))
    application.add_handler(CallbackQueryHandler(scammer_pagination, pattern='^sc_page_'))
    application.add_handler(CommandHandler('reports', reports_cmd))
    application.add_handler(CommandHandler('deals', deals_list_cmd))
    application.add_handler(CommandHandler('stats', stats_cmd))
    application.add_handler(CommandHandler('scammer_list', scammer_list_cmd))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(MessageHandler(~filters.COMMAND & filters.ChatType.GROUPS, message_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.ALL, delete_join_left))
    application.add_handler(ChatMemberHandler(admin_status_changed, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(CommandHandler('start', start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, button_handler))
    
    async def post_init(app):
        global bot_instance
        bot_instance = app.bot
        await resolve_usernames(app.bot)
        await load_garants_from_group(app.bot)
        await set_bot_commands(app)
        
        async def keep_alive(ctx):
            try: await ctx.bot.get_me()
            except: pass
        
        async def cleanup_job(ctx): cleanup_all()
        
        if app.job_queue:
            app.job_queue.run_repeating(keep_alive, interval=30, first=5)
            app.job_queue.run_repeating(cleanup_job, interval=259200, first=60)
        
        print(f"🤖 ILLUMI BOT v13 | 👑 {resolved_owner_id} | 👮 {len(resolved_admin_ids)} | 🏷 {len(group_garant_ids)}")
    
    application.post_init = post_init
    
    if WEBHOOK_URL:
        application.run_webhook(listen="0.0.0.0", port=WEBHOOK_PORT, url_path=BOT_TOKEN, webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}", drop_pending_updates=True)
    else:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()