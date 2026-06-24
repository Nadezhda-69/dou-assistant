import streamlit as st
import os
import json
import bcrypt
import smtplib
import ssl
import random
import string
import uuid
import requests
from openai import OpenAI
from email.message import EmailMessage
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ==================== КОНФИГУРАЦИЯ ====================
USERS_PATH = "data/users.json"
PENDING_PATH = "data/pending_users.json"
os.makedirs("data", exist_ok=True)

# Загрузка API-ключей
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID")
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def load_json(path):
    if not os.path.exists(path): return {}
    with open(path, "r", encoding="utf-8") as f: return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def hash_pwd(pwd): return bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()
def check_pwd(pwd, hashed): return bcrypt.checkpw(pwd.encode(), hashed.encode())

def send_code_email(email, code):
    msg = EmailMessage()
    msg["Subject"] = "🔐 Код подтверждения: Ассистент ДОУ"
    msg["From"] = SMTP_USER
    msg["To"] = email
    msg.set_content(f"Ваш код: {code}\n⏳ Действует 15 минут.\n🔒 Не передавайте третьим лицам.")
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
            srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(msg)
        return True
    except Exception as e:
        st.error(f"⚠️ Ошибка отправки email: {str(e)}")
        return False

# ==================== ИИ-ДВИЖОК ====================
DEEPSEEK_CLIENT = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

@st.cache_resource(ttl=1700)
def get_gigachat_token():
    headers = {"Authorization": f"Basic {uuid.uuid4()}", "RqUID": str(uuid.uuid4()), "Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post("https://ngw.devices.sberbank.ru:9443/api/v2/oauth", headers=headers, data={"scope": "GIGACHAT_API_PERS"})
    resp.raise_for_status()
    return resp.json()["access_token"]

def ask_ai(prompt, model, age_group):
    system = f"""Ты методический ИИ-ассистент для воспитателей ДОУ. Работай строго по ФОП ДО, ФГОС ДО, СанПиН 2.4.3648-20 и 273-ФЗ.
Возрастная группа: {age_group}. Не запрашивай и не храни ФИО детей. Если норматив не найден — помечай: «Требует методической проверки».
Формат: структурированный текст, готовый к копированию."""
    
    try:
        if model == "GigaChat":
            token = get_gigachat_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "RqUID": str(uuid.uuid4())}
            payload = {"model": "GigaChat", "messages": [{"role":"system","content":system},{"role":"user","content":prompt}], "temperature":0.3}
            resp = requests.post("https://gigachat.devices.sberbank.ru/api/v1/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        else:
            res = DEEPSEEK_CLIENT.chat.completions.create(model="deepseek-chat", messages=[{"role":"system","content":system},{"role":"user","content":prompt}], temperature=0.3)
            return res.choices[0].message.content
    except Exception as e:
        return f"⚠️ Ошибка ИИ ({model}): {str(e)}"

# ==================== АВТОРИЗАЦИЯ ====================
if "auth_step" not in st.session_state: st.session_state.auth_step = "login"
st.set_page_config(page_title="Ассистент ДОУ", layout="wide")

# 🔹 ВХОД
if st.session_state.auth_step == "login":
    st.title("🔐 Вход в систему")
    email = st.text_input("Email")
    password = st.text_input("Пароль", type="password")
    col1, col2 = st.columns(2)
    if col1.button("Войти", use_container_width=True):
        users = load_json(USERS_PATH)
        u = users.get(email)
        if u and check_pwd(password, u["password"]):
            st.session_state.user = u
            st.session_state.auth_step = "dashboard"
            st.rerun()
        else: st.error("Неверный email или пароль")
    if col2.button("📝 Регистрация", use_container_width=True):
        st.session_state.auth_step = "register"
        st.rerun()
    st.stop()

# 🔹 РЕГИСТРАЦИЯ
elif st.session_state.auth_step == "register":
    st.title("📝 Регистрация воспитателя")
    em = st.text_input("Корпоративный email")
    name = st.text_input("ФИО")
    p1 = st.text_input("Пароль", type="password")
    p2 = st.text_input("Повторите пароль", type="password")
    if st.button("Получить код", use_container_width=True):
        if not all([em,name,p1,p2]): st.error("Заполните все поля")
        elif p1!=p2: st.error("Пароли не совпадают")
        elif em in load_json(USERS_PATH) or em in load_json(PENDING_PATH): st.error("Email уже используется")
        else:
            code = "".join(random.choices(string.digits, k=6))
            save_json(PENDING_PATH, load_json(PENDING_PATH) | {em: {"name":name, "password":hash_pwd(p1), "role":"teacher", "code":code, "expires":(datetime.now()+timedelta(minutes=15)).isoformat()}})
            if send_code_email(em, code):
                st.session_state.pending_email = em
                st.session_state.auth_step = "verify"
                st.success("✅ Код отправлен. Проверьте папку «Спам».")
                st.rerun()
    if st.button("← Назад"): st.session_state.auth_step = "login"; st.rerun()
    st.stop()

# 🔹 ПОДТВЕРЖДЕНИЕ
elif st.session_state.auth_step == "verify":
    st.title("🔑 Подтверждение email")
    st.info(f"Код отправлен на: {st.session_state.pending_email}")
    code_input = st.text_input("6-значный код")
    if st.button("Подтвердить", use_container_width=True):
        pend = load_json(PENDING_PATH)
        usr = pend.get(st.session_state.pending_email)
        if not usr: st.error("Сессия истекла"); st.session_state.auth_step="register"
        elif datetime.now().isoformat() > usr["expires"]: st.error("⏳ Код просрочен")
        elif code_input != usr["code"]: st.error("❌ Неверный код")
        else:
            users = load_json(USERS_PATH)
            users[st.session_state.pending_email] = {"name":usr["name"], "password":usr["password"], "role":usr["role"], "created":datetime.now().isoformat()}
            save_json(USERS_PATH, users)
            del pend[st.session_state.pending_email]
            save_json(PENDING_PATH, pend)
            st.session_state.user = users[st.session_state.pending_email]
            st.session_state.auth_step = "dashboard"
            st.success("✅ Аккаунт активирован!")
            st.rerun()
    st.stop()

# ==================== ГЛАВНЫЙ ИНТЕРФЕЙС ====================
elif st.session_state.auth_step == "dashboard" and "user" in st.session_state:
    user = st.session_state.user
    st.sidebar.success(f"👤 {user['name']} | {user['role'].capitalize()}")
    if st.sidebar.button("🚪 Выйти"):
        del st.session_state.user; st.session_state.auth_step="login"; st.rerun()

    ai_model = st.sidebar.selectbox("🤖 Модель ИИ", ["GigaChat", "DeepSeek"])
    age_group = st.sidebar.selectbox("🎯 Возрастная группа", ["2-3 года", "3-4 года", "4-5 лет", "5-6 лет", "6-7 лет"])

    menu = ["📅 Календарный план", "📊 Диагностика", "🤖 ИИ-чат", "📖 НПА"]
    if user["role"] == "admin": menu.append("👥 Пользователи")
    page = st.sidebar.radio("Модули", menu)

    st.title(page)

    if page == "📅 Календарный план":
        theme = st.text_input("Тема недели/месяца")
        focus = st.multiselect("Фокус", ["Речь", "Познание", "Социализация", "Движение", "Творчество"])
        if st.button("🧠 Сгенерировать план"):
            with st.spinner("Генерация..."):
                res = ask_ai(f"Составь календарный план для группы {age_group}. Тема: '{theme}'. Фокус: {', '.join(focus)}. Включи режимные моменты, НОД, прогулку, игры, работу с родителями. Соответствие ФОП/ФГОС.", ai_model, age_group)
            st.markdown(res)
            st.download_button("💾 Скачать .txt", res, file_name="plan.txt")

    elif page == "📊 Диагностика":
        period = st.selectbox("Период", ["Начало года", "Середина года", "Конец года"])
        domain = st.selectbox("Область", ["Речевое", "Познавательное", "Социально-коммуникативное", "Физическое"])
        data_in = st.text_area("Наблюдаемые показатели (без ФИО, используйте ID или обобщённо)")
        if st.button("🔍 Анализ"):
            with st.spinner("Анализ..."):
                res = ask_ai(f"Анализ диагностики за {period}, группа {age_group}, область: {domain}. Данные: {data_in}. Выводы, динамика, коррекционные действия. ФГОС/ФОП.", ai_model, age_group)
            st.markdown(res)

    elif page == "🤖 ИИ-чат":
        if "chat" not in st.session_state: st.session_state.chat = []
        inp = st.chat_input("Задайте вопрос...")
        if inp:
            st.session_state.chat.append({"role":"user","content":inp})
            with st.spinner("Думаю..."):
                full = "\n".join([f"{m['role']}: {m['content']}" for m in st.session_state.chat[-4:]])
                reply = ask_ai(full, ai_model, age_group)
                st.session_state.chat.append({"role":"assistant","content":reply})
            for m in st.session_state.chat:
                with st.chat_message(m["role"]): st.write(m["content"])

    elif page == "📖 НПА":
        st.info("База нормативов в разработке. Сейчас ИИ работает с актуальными версиями ФОП, ФГОС, СанПиН, 273-ФЗ через промпт-контекст.")

    elif page == "👥 Пользователи" and user["role"]=="admin":
        st.warning("В MVP управление через `data/users.json`. В продакшене будет веб-форма с ролями и аудитом.")
        st.code('{"admin@dou.ru": {"password":"bcrypt_hash", "role":"admin", "name":"Заведующая"}}', language="json")