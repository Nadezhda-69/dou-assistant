import streamlit as st
import os
import json
import bcrypt
import smtplib
import ssl
import random
import string
import uuid
import base64
import requests
import io
import urllib3
import sqlite3
from openai import OpenAI
from email.message import EmailMessage
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fpdf import FPDF
from docx import Document

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============== КОНФИГУРАЦИЯ ==============
USERS_PATH = "data/users.json"
PENDING_PATH = "data/pending_users.json"
os.makedirs("data", exist_ok=True)

# Загрузка переменных: сначала из st.secrets (Streamlit Cloud), потом из .env (локально)
DEEPSEEK_API_KEY = st.secrets.get("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
GIGACHAT_CLIENT_ID = st.secrets.get("GIGACHAT_CLIENT_ID") or os.getenv("GIGACHAT_CLIENT_ID")
GIGACHAT_CLIENT_SECRET = st.secrets.get("GIGACHAT_CLIENT_SECRET") or os.getenv("GIGACHAT_CLIENT_SECRET")
SMTP_HOST = st.secrets.get("SMTP_HOST") or os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(st.secrets.get("SMTP_PORT") or os.getenv("SMTP_PORT", "465"))
SMTP_USER = st.secrets.get("SMTP_USER") or os.getenv("SMTP_USER")
SMTP_PASS = st.secrets.get("SMTP_PASS") or os.getenv("SMTP_PASS")

# ============== БАЗА ДАННЫХ ==============

def get_db_connection():
    """Получить соединение с SQLite базой данных"""
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/users.db")
    conn.row_factory = sqlite3.Row
    return conn

def hash_pwd(pwd):
    """Захешировать пароль"""
    return bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()

def check_pwd(pwd, hashed):
    """Проверить пароль"""
    return bcrypt.checkpw(pwd.encode(), hashed.encode())

def init_db():
    """Инициализировать базу данных"""
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT DEFAULT 'teacher',
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pending_users (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT DEFAULT 'teacher',
            code TEXT NOT NULL,
            expires TIMESTAMP NOT NULL,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_user(email):
    """Получить пользователя из БД"""
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if user:
        return dict(user)  # Преобразуем Row в dict
    return None

def save_user(email, password, name, role="teacher"):
    """Сохранить пользователя в БД"""
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO users (email, password, name, role) VALUES (?, ?, ?, ?)",
            (email, password, name, role)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def save_pending_user(email, password, name, code, expires, role="teacher"):
    """Сохранить пользователя в ожидании подтверждения"""
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pending_users (email, password, name, role, code, expires) VALUES (?, ?, ?, ?, ?, ?)",
            (email, password, name, role, code, expires)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"Ошибка сохранения pending: {e}")
        return False
    finally:
        conn.close()

def get_pending_user(email):
    """Получить пользователя в ожидании"""
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM pending_users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if user:
        return dict(user)  # Преобразуем Row в dict
    return None

def delete_pending_user(email):
    """Удалить пользователя из pending"""
    conn = get_db_connection()
    conn.execute("DELETE FROM pending_users WHERE email = ?", (email,))
    conn.commit()
    conn.close()

def send_code_email(email, code):
    """Отправить код подтверждения на email"""
    try:
        msg = EmailMessage()
        msg["Subject"] = "🔐 Код подтверждения: Ассистент ДОУ"
        msg["From"] = SMTP_USER
        msg["To"] = email
        msg.set_content(f"Ваш код: {code}\n🔒 Действует 15 минут.\n🚫 Не передавайте третьим лицам.")
        
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
            srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(msg)
        return True
    except Exception as e:
        print(f"Ошибка отправки email: {e}")
        return False

def load_json(path):
    """Заглушка для совместимости"""
    return {}

def save_json(path, data):
    """Заглушка для совместимости"""
    pass

# Инициализировать БД при старте
init_db()

# ============== ИИ-ДВИЖОК ==============

@st.cache_resource(ttl=1700)
def get_gigachat_token():
    auth_string = f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}"
    auth_bytes = base64.b64encode(auth_string.encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_bytes}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4())
    }
    data = {"scope": "GIGACHAT_API_PERS"}
    resp = requests.post("https://ngw.devices.sberbank.ru:9443/api/v2/oauth", headers=headers, data=data, verify=False)
    resp.raise_for_status()
    return resp.json()["access_token"]

def ask_ai(prompt, model, age_group, use_kustuk=False):
    system = """Ты методический ИИ-ассистент для воспитателей ДОУ. Работай строго по ФГОС ДО, СанПиН 2.4.3648-20 и 273-ФЗ."""
    
    if use_kustuk:
        system += """
        
🌈 РЕГИОНАЛЬНАЯ ПРОГРАММА «КУСТУК» (Республика Саха (Якутия)):
- Интегрируй этнокультурный компонент: якутский фольклор, традиции, природу Севера
- Включай национальные подвижные и дидактические игры
- Учитывай художественно-эстетическое развитие через олонхо, тойук, якутский орнамент
- Физическое воспитание: традиционные упражнения
- Речевое развитие: элементы якутского языка (посильно возрасту)
- Социально-коммуникативное: уважение к старшим, бережное отношение к природе
- При генерации указывай, какие элементы относятся к федеральному компоненту, а какие — к региональному «Кустук»."""
    
    system += f"\n\nВозрастная группа: {age_group}."
    
    try:
        if model == "GigaChat":
            token = get_gigachat_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "GigaChat",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "temperature": 0.3
            }
            resp = requests.post("https://gigachat.devices.sberbank.ru/api/v1/chat/completions", headers=headers, json=payload, verify=False)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        else:
            if not DEEPSEEK_API_KEY:
                return "⚠️ DeepSeek API ключ не найден. Проверьте Secrets на Streamlit Cloud."
            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
            res = client.chat.completions.create(model="deepseek-chat", messages=[{"role":"system","content":system},{"role":"user","content":prompt}], temperature=0.3)
            return res.choices[0].message.content
    except Exception as e:
        return f"⚠️ Ошибка ИИ ({model}): {str(e)}"

# ============== АВТОРИЗАЦИЯ ==============
if "auth_step" not in st.session_state:
    st.session_state.auth_step = "login"

st.set_page_config(page_title="Ассистент ДОУ «Кустук»", layout="wide")

if st.session_state.auth_step == "login":
    st.title("🔐 Вход в систему")
    email = st.text_input("Email", key="login_email")
    password = st.text_input("Пароль", type="password", key="login_pass")
    
    col1, col2 = st.columns(2)
    
    if col1.button("🔐 Войти", use_container_width=True):
        u = get_user(email)
        if u and check_pwd(password, u["password"]):
            st.session_state.user = {"email": email, "name": u["name"], "role": u.get("role", "teacher")}
            st.session_state.auth_step = "dashboard"
            st.success(f"Добро пожаловать, {u['name']}!")
            st.rerun()
        else:
            st.error("❌ Неверный email или пароль")
    
    if col2.button("📝 Регистрация", use_container_width=True):
        st.session_state.auth_step = "register"
        st.rerun()
    
    st.stop()

elif st.session_state.auth_step == "register":
    st.title("📝 Регистрация воспитателя")
    em = st.text_input("Корпоративный email", key="reg_email")
    name = st.text_input("ФИО", key="reg_name")
    p1 = st.text_input("Пароль", type="password", key="reg_pass1")
    p2 = st.text_input("Повторите пароль", type="password", key="reg_pass2")
    
    if st.button("📧 Получить код", use_container_width=True):
        if not all([em, name, p1, p2]):
            st.error("❌ Заполните все поля")
        elif p1 != p2:
            st.error("❌ Пароли не совпадают")
        else:
            existing = get_user(em)
            if existing:
                st.error("❌ Email уже зарегистрирован")
            else:
                code = "".join(random.choices(string.digits, k=6))
                hashed = hash_pwd(p1)
                expires = (datetime.now() + timedelta(minutes=15)).isoformat()
                
                if save_pending_user(em, hashed, name, code, expires, "teacher"):
                    if send_code_email(em, code):
                        st.session_state.pending_email = em
                        st.session_state.auth_step = "verify"
                        st.success("✅ Код отправлен. Проверьте папку «Спам».")
                        st.rerun()
                    else:
                        st.error("❌ Ошибка отправки email. Проверьте настройки SMTP.")
                else:
                    st.error("❌ Ошибка сохранения данных")
    
    if st.button("← Назад"):
        st.session_state.auth_step = "login"
        st.rerun()
    
    st.stop()

elif st.session_state.auth_step == "verify":
    st.title("🔐 Подтверждение кода")
    code_input = st.text_input("Код из email")
    
    if st.button("Подтвердить"):
        em = st.session_state.get("pending_email", "")
        pending = get_pending_user(em)
        
        if pending:
            # Проверка срока действия
            try:
                expires = datetime.fromisoformat(pending["expires"])
                if datetime.now() > expires:
                    st.error("❌ Срок действия кода истёк. Зарегистрируйтесь заново.")
                    delete_pending_user(em)
                    st.rerun()
            except:
                st.error("❌ Ошибка проверки срока")
                st.rerun()
            
            if pending["code"] == code_input:
                if save_user(em, pending["password"], pending["name"], pending["role"]):
                    delete_pending_user(em)
                    st.success("✅ Регистрация успешна! Теперь войдите.")
                    st.session_state.auth_step = "login"
                    st.rerun()
                else:
                    st.error("❌ Ошибка сохранения пользователя")
            else:
                st.error("❌ Неверный код")
        else:
            st.error("❌ Пользователь не найден. Зарегистрируйтесь заново.")
    
    if st.button("← Назад"):
        st.session_state.auth_step = "login"
        st.rerun()
    
    st.stop()

# ============== ГЛАВНЫЙ ИНТЕРФЕЙС ==============
elif st.session_state.auth_step == "dashboard" and "user" in st.session_state:
    user = st.session_state.user
    st.sidebar.success(f"👤 {user['name']} | {user['role'].capitalize()}")
    
    if st.sidebar.button("🚪 Выйти"):
        del st.session_state.user
        st.session_state.auth_step = "login"
        st.rerun()
    
    ai_model = st.sidebar.selectbox("🤖 Модель ИИ", ["GigaChat", "DeepSeek"])
    age_group = st.sidebar.selectbox("👶 Возрастная группа", ["2-3 года", "3-4 года", "4-5 лет", "5-6 лет", "6-7 лет"])
    use_kustuk = st.sidebar.checkbox("🌈 Программа «Кустук»", value=True)
    
    st.sidebar.markdown("---")
    module = st.sidebar.radio("📚 Модули", ["Календарный план", "Диагностика", "ИИ-чат", "Программа «Кустук»", "НПА"])
    
    st.title(f"📋 {module}")
    
    if module == "Календарный план":
        period = st.selectbox("Период", ["Сентябрь", "Октябрь", "Ноябрь", "Декабрь", "Январь", "Февраль", "Март", "Апрель", "Май"])
        theme = st.text_input("Тема недели", placeholder="Например: Осень, Зима, Весна...")
        
        if st.button("🤖 Сгенерировать план"):
            if not theme:
                st.error("❌ Введите тему недели")
            else:
                with st.spinner("⏳ ИИ генерирует календарный план..."):
                    prompt = f"Создай календарный план на {period} для детей {age_group}. Тема: {theme}. Включи: образовательную деятельность, режимные моменты, работу с родителями."
                    result = ask_ai(prompt, ai_model, age_group, use_kustuk)
                    st.markdown(result)
                    st.download_button("📥 Скачать", result, "plan.txt")
    
    elif module == "Диагностика":
        period = st.selectbox("Период", ["Начало года", "Середина года", "Конец года"])
        area = st.selectbox("Область", ["Речевое", "Познавательное", "Художественно-эстетическое", "Физическое", "Социально-коммуникативное"])
        indicators = st.text_area("Наблюдаемые показатели (без ФИО, используйте ID или обобщённо)", height=150)
        
        if st.button("🔍 Анализ"):
            if not indicators:
                st.error("❌ Введите показатели")
            else:
                with st.spinner("⏳ ИИ анализирует диагностику..."):
                    prompt = f"Проведи анализ диагностики ({period}) для детей {age_group}. Область: {area}. Показатели:\n{indicators}\n\nДай рекомендации по развитию."
                    result = ask_ai(prompt, ai_model, age_group, use_kustuk)
                    st.markdown(result)
                    st.download_button("📥 Скачать PDF", result, "diagnostics.txt")
    
    elif module == "ИИ-чат":
        st.markdown("### 💬 Задайте вопрос ИИ-ассистенту")
        user_input = st.text_area("Ваш вопрос", height=100, placeholder="Например: Как провести занятие по развитию речи на тему 'Осень'?")
        
        if st.button("💬 Отправить"):
            if not user_input:
                st.error("❌ Введите вопрос")
            else:
                with st.spinner("⏳ ИИ думает..."):
                    response = ask_ai(user_input, ai_model, age_group, use_kustuk)
                    st.markdown(response)
                    st.download_button("📥 Сохранить ответ", response, "answer.txt")
    
    elif module == "Программа «Кустук»":
        st.markdown("""
        ### 🌈 Региональная программа «Кустук»
        
        **Основные направления:**
        - 🎭 Этнокультурный компонент (якутский фольклор, традиции)
        - 🎨 Художественно-эстетическое развитие (олонхо, тойук, орнамент)
        - 🏃 Физическое воспитание (традиционные игры и упражнения)
        - 🗣️ Речевое развитие (элементы якутского языка)
        - 👥 Социально-коммуникативное (уважение к старшим, природа)
        """)
        
        topic = st.text_input("Тема занятия", placeholder="Например: Якутские народные игры")
        
        if st.button("🤖 Сгенерировать занятие"):
            if not topic:
                st.error("❌ Введите тему")
            else:
                with st.spinner("⏳ ИИ создаёт занятие..."):
                    prompt = f"Разработай занятие по программе «Кустук» для детей {age_group}. Тема: {topic}. Включи цели, задачи, оборудование, ход занятия, интеграцию с ФГОС."
                    result = ask_ai(prompt, ai_model, age_group, use_kustuk=True)
                    st.markdown(result)
                    st.download_button("📥 Скачать", result, "kustuk_lesson.txt")
    
    elif module == "НПА":
        st.markdown("""
        ### 📚 Нормативно-правовые акты
        
        **Основные документы:**
        - 📜 ФЗ-273 "Об образовании в РФ"
        - 📜 ФГОС ДО (приказ №1155)
        - 📜 СанПиН 2.4.3648-20
        - 📜 Профессиональный стандарт педагога
        - 📜 Региональные нормативы РС(Я)
        """)
        
        doc_type = st.selectbox("Тип документа", ["ФГОС ДО", "СанПиН", "Профстандарт", "ФЗ-273"])
        question = st.text_area("Ваш вопрос по НПА", height=100)
        
        if st.button("🔍 Найти информацию"):
            if not question:
                st.error("❌ Введите вопрос")
            else:
                with st.spinner("⏳ ИИ ищет информацию..."):
                    prompt = f"Ответьте на вопрос по {doc_type}: {question}. Ссылайтесь на конкретные пункты и статьи."
                    result = ask_ai(prompt, ai_model, age_group, False)
                    st.markdown(result)
                    st.download_button("📥 Сохранить", result, "npa_answer.txt")
