# session_init.py
import asyncio
import os
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from dotenv import load_dotenv

load_dotenv()

PHONE = os.getenv('PHONE', '')
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
SESSION_NAME = 'user_session'

async def main():
    print("""
╔══════════════════════════════════════╗
║   🔐 SESSION INITIALIZER          ║
║   Вход в аккаунт Telegram         ║
╚══════════════════════════════════════╝
    """)
    
    # Телефон
    phone = PHONE
    if not phone:
        phone = input("📱 Номер телефона (+79991234567): ").strip()
    
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    
    await client.connect()
    
    # Проверяем, авторизованы ли уже
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n✅ Сессия уже активна!")
        print(f"👤 {me.first_name} (@{me.username or 'нет'})")
        print(f"📱 {me.phone}")
        print(f"🆔 {me.id}")
        
        renew = input("\n🔄 Обновить сессию? (y/n): ").strip().lower()
        if renew != 'y':
            print("👋 Выход...")
            await client.disconnect()
            return
        else:
            # Выходим из текущей сессии
            await client.log_out()
            print("🔓 Старая сессия удалена")
    
    # Отправляем код
    print(f"\n📤 Отправка кода на {phone}...")
    sent = await client.send_code_request(phone)
    print(f"✅ Код отправлен (через {sent.type})")
    
    # Вводим код
    code = input("📨 Код из Telegram: ").strip()
    
    try:
        await client.sign_in(phone=phone, code=code)
        print("✅ Вход выполнен!")
        
    except SessionPasswordNeededError:
        print("\n🔐 ТРЕБУЕТСЯ ОБЛАЧНЫЙ ПАРОЛЬ (2FA)")
        password = input("🔑 Облачный пароль: ").strip()
        
        try:
            await client.sign_in(password=password)
            print("✅ Вход с паролем выполнен!")
        except Exception as e:
            print(f"❌ Неверный пароль: {e}")
            await client.disconnect()
            return
    
    # Информация об аккаунте
    me = await client.get_me()
    print(f"""
╔══════════════════════════════════════╗
║   ✅ СЕССИЯ СОЗДАНА!              ║
║   👤 {me.first_name} (@{me.username or 'нет'})              ║
║   📱 {me.phone}                    ║
║   🆔 {me.id}                       ║
║   💾 {SESSION_NAME}.session       ║
╚══════════════════════════════════════╝
    """)
    
    await client.disconnect()
    print("👋 Готово! Сессия сохранена.")

if __name__ == '__main__':
    asyncio.run(main())