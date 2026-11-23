import os
import sys
import json
import base64
import re
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any, List
import sqlite3
import aiohttp
from aiohttp import web

# ==========================================
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ==========================================
PORT = int(os.getenv("PORT", "8000"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()  # formato: "usuario/repositorio"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
WEB_PIN = os.getenv("WEB_PIN", "").strip()

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agenda.db")

# Zona horaria de Venezuela: UTC-4
VET_TZ = timezone(timedelta(hours=-4), name="VET")

def now_vet() -> datetime:
    return datetime.now(VET_TZ)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("AsistenteAgenda")

# ==========================================
# CONTROL DE ESTADO DE RESPALDO EN GITHUB
# ==========================================
backup_state = {
    "last_change_time": None,      # timestamp float de última modificación en BD
    "last_backup_time": None,      # timestamp float de último respaldo exitoso
    "last_backup_str": "Nunca",    # Texto para mostrar en la interfaz
    "last_status": "Sin iniciar",  # "Al día", "Pendiente", "Sincronizando...", "Error", etc.
    "last_error": "",
    "in_progress": False,
    "total_backups": 0
}

def mark_data_changed():
    """Marca que la base de datos sufrió un cambio para activar el respaldo automático."""
    backup_state["last_change_time"] = time.time()
    if backup_state["last_status"] != "Sincronizando...":
        backup_state["last_status"] = "Cambios pendientes de respaldo"

# ==========================================
# GESTOR DE BASE DE DATOS SQLITE
# ==========================================
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        category TEXT DEFAULT 'Personal',
        priority TEXT DEFAULT 'Normal',
        status TEXT DEFAULT 'pending',
        created_at TEXT NOT NULL,
        completed_at TEXT,
        due_date TEXT,
        reminder_at TEXT,
        reminder_sent INTEGER DEFAULT 0,
        source TEXT DEFAULT 'telegram_text',
        raw_text TEXT DEFAULT ''
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()
    conn.close()
    logger.info("Base de datos SQLite inicializada en %s", DB_FILE)

def db_add_task(
    title: str,
    category: str = "Personal",
    priority: str = "Normal",
    description: str = "",
    due_date: Optional[str] = None,
    reminder_at: Optional[str] = None,
    source: str = "telegram_text",
    raw_text: str = ""
) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    created_at = now_vet().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO tasks (title, description, category, priority, status, created_at, due_date, reminder_at, reminder_sent, source, raw_text)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, 0, ?, ?)
    """, (title.strip(), description.strip(), category, priority, created_at, due_date, reminder_at, source, raw_text))
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    mark_data_changed()
    return task_id

def db_get_task(task_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def db_list_tasks(status: Optional[str] = None, category: Optional[str] = None, search: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    query = "SELECT * FROM tasks WHERE 1=1"
    params = []

    if status and status != "all":
        query += " AND status = ?"
        params.append(status)
    if category and category != "all":
        query += " AND category = ?"
        params.append(category)
    if search:
        query += " AND (title LIKE ? OR description LIKE ?)"
        s = f"%{search}%"
        params.extend([s, s])

    query += " ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id DESC"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def db_toggle_task(task_id: int) -> Optional[Dict[str, Any]]:
    task = db_get_task(task_id)
    if not task:
        return None
    new_status = "done" if task["status"] == "pending" else "pending"
    completed_at = now_vet().strftime("%Y-%m-%d %H:%M:%S") if new_status == "done" else None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?", (new_status, completed_at, task_id))
    conn.commit()
    conn.close()
    mark_data_changed()
    return db_get_task(task_id)

def db_update_task(task_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    task = db_get_task(task_id)
    if not task:
        return None
    title = data.get("title", task["title"])
    category = data.get("category", task["category"])
    priority = data.get("priority", task["priority"])
    description = data.get("description", task["description"])
    due_date = data.get("due_date", task["due_date"])
    reminder_at = data.get("reminder_at", task["reminder_at"])

    # Si se actualiza el reminder_at, resetear reminder_sent a 0
    reset_reminder = 0 if reminder_at != task["reminder_at"] else task["reminder_sent"]

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE tasks SET title = ?, category = ?, priority = ?, description = ?, due_date = ?, reminder_at = ?, reminder_sent = ?
        WHERE id = ?
    """, (title, category, priority, description, due_date, reminder_at, reset_reminder, task_id))
    conn.commit()
    conn.close()
    mark_data_changed()
    return db_get_task(task_id)

def db_postpone_task(task_id: int, new_reminder_at: str, new_due_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    task = db_get_task(task_id)
    if not task:
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    due = new_due_date if new_due_date else task["due_date"]
    cursor.execute("""
        UPDATE tasks SET reminder_at = ?, due_date = ?, reminder_sent = 0, status = 'pending'
        WHERE id = ?
    """, (new_reminder_at, due, task_id))
    conn.commit()
    conn.close()
    mark_data_changed()
    return db_get_task(task_id)

def db_delete_task(task_id: int) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        mark_data_changed()
    return deleted

def db_get_stats() -> Dict[str, Any]:
    conn = get_db_connection()
    cursor = conn.cursor()
    today_str = now_vet().strftime("%Y-%m-%d")

    # Pendientes Hoy (due_date == today o reminder_at comienza con today o sin fecha creada hoy)
    cursor.execute("""
        SELECT COUNT(*) FROM tasks
        WHERE status = 'pending' AND category != 'Ideas'
        AND (due_date = ? OR reminder_at LIKE ? OR (due_date IS NULL AND reminder_at IS NULL AND created_at LIKE ?))
    """, (today_str, f"{today_str}%", f"{today_str}%"))
    pendientes_hoy = cursor.fetchone()[0]

    # Atrasados (due_date < today o reminder_at < now, status == pending, no ideas)
    now_full = now_vet().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        SELECT COUNT(*) FROM tasks
        WHERE status = 'pending' AND category != 'Ideas'
        AND (
            (due_date IS NOT NULL AND due_date < ?)
            OR (reminder_at IS NOT NULL AND reminder_at < ?)
        )
    """, (today_str, now_full))
    atrasados = cursor.fetchone()[0]

    # Ideas abiertas
    cursor.execute("SELECT COUNT(*) FROM tasks WHERE category = 'Ideas' AND status = 'pending'")
    ideas = cursor.fetchone()[0]

    # Completados Hoy
    cursor.execute("""
        SELECT COUNT(*) FROM tasks
        WHERE status = 'done' AND completed_at LIKE ?
    """, (f"{today_str}%",))
    completados_hoy = cursor.fetchone()[0]

    # Total pendientes
    cursor.execute("SELECT COUNT(*) FROM tasks WHERE status = 'pending'")
    total_pendientes = cursor.fetchone()[0]

    conn.close()
    return {
        "pendientes_hoy": pendientes_hoy,
        "atrasados": atrasados,
        "ideas": ideas,
        "completados_hoy": completados_hoy,
        "total_pendientes": total_pendientes,
        "hora_vet": now_vet().strftime("%d/%m/%Y %I:%M %p")
    }

def db_get_due_reminders() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = now_vet().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        SELECT * FROM tasks
        WHERE status = 'pending'
        AND reminder_at IS NOT NULL
        AND reminder_at <= ?
        AND reminder_sent = 0
        ORDER BY reminder_at ASC
    """, (now_str,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def db_mark_reminder_sent(task_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    mark_data_changed()

# ==========================================
# PARSER INTELIGENTE DE TEXTO Y FECHAS
# ==========================================
def parse_smart_text(text: str) -> Dict[str, Any]:
    """
    Analiza un texto en español para extraer:
    - Título limpio
    - Categoría (Sala, Equipos/Tx, Personal, Ideas)
    - Prioridad (Alta, Normal, Baja)
    - Fecha y hora de recordatorio (en hora Venezuela VET UTC-4)
    """
    clean_text = text.strip()
    category = "Personal"
    priority = "Normal"
    reminder_at: Optional[str] = None
    due_date: Optional[str] = None

    lower = clean_text.lower()

    # 1. DETECCIÓN DE CATEGORÍA
    if lower.startswith("idea:") or lower.startswith("idea :") or "idea:" in lower or lower.startswith("💡"):
        category = "Ideas"
        # Limpiar prefijo "idea:"
        clean_text = re.sub(r'^(?:idea\s*:\s*|💡\s*)', '', clean_text, flags=re.IGNORECASE).strip()
    else:
        # Palabras de Sala Situacional / Operación Eléctrica
        sala_words = [
            "sala", "situacional", "despacho", "falla", "circuito", "subestacion", "subestación",
            "linea", "línea", "carga", "mw", "kv", "115kv", "corpoelec", "turno", "novedad",
            "evento", "disparo", "breaker", "tension", "tensión", "reconectador", "alimentador"
        ]
        # Palabras de Equipos / Transformadores
        tx_words = [
            "tx", "transformador", "transformadores", "equipo", "equipos", "interruptor",
            "seccionador", "aceite", "dga", "mantenimiento", "banco", "potencia", "temperatura",
            "bobinado", "termo", "termografía", "relé", "rele", "aislador", "pararrayos", "fuga"
        ]
        # Palabras de Ideas
        idea_words = ["idea", "proponer", "innovación", "innovacion", "ocurre", "crear algo"]

        # Chequeo por palabra o límite de palabra (Equipos/Tx tiene precedencia por ser más específico)
        if any(re.search(r'\b' + re.escape(w) + r'\b', lower) for w in tx_words):
            category = "Equipos/Tx"
        elif any(re.search(r'\b' + re.escape(w) + r'\b', lower) for w in sala_words):
            category = "Sala"
        elif any(re.search(r'\b' + re.escape(w) + r'\b', lower) for w in idea_words):
            category = "Ideas"

    # 2. DETECCIÓN DE PRIORIDAD
    if any(re.search(r'\b' + re.escape(w) + r'\b', lower) for w in ["urgente", "asap", "ya", "prioridad alta", "alta prioridad", "crítico", "critico", "emergencia"]):
        priority = "Alta"
    elif any(re.search(r'\b' + re.escape(w) + r'\b', lower) for w in ["baja", "cuando pueda", "opcional", "sin apuro"]):
        priority = "Baja"

    # 3. DETECCIÓN DE FECHA Y HORA (HORA VET UTC-4)
    now = now_vet()
    target_dt: Optional[datetime] = None
    extracted_pattern = ""

    # Caso A: "en X minutos" o "en X min"
    m = re.search(r'\ben\s+(\d+)\s*(?:minutos?|mins?|m)\b', lower)
    if m:
        mins = int(m.group(1))
        target_dt = now + timedelta(minutes=mins)
        extracted_pattern = m.group(0)

    # Caso B: "en X horas" o "en X hrs"
    if not target_dt:
        m = re.search(r'\ben\s+(\d+)\s*(?:horas?|hrs?|h)\b', lower)
        if m:
            hrs = int(m.group(1))
            target_dt = now + timedelta(hours=hrs)
            extracted_pattern = m.group(0)

    # Helper para parsear horas en texto (ej. "8am", "8:30pm", "10 de la mañana", "3 de la tarde")
    def parse_time_match(time_str: str) -> Optional[Tuple[int, int]]:
        tm = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|de\s+la\s+mañana|en\s+la\s+mañana|de\s+la\s+tarde|en\s+la\s+tarde|de\s+la\s+noche|en\s+la\s+noche)?', time_str, re.IGNORECASE)
        if not tm:
            return None
        h = int(tm.group(1))
        minute = int(tm.group(2)) if tm.group(2) else 0
        mod = tm.group(3).lower() if tm.group(3) else ""

        if any(x in mod for x in ["tarde", "noche", "pm", "p.m."]):
            if h < 12:
                h += 12
        elif any(x in mod for x in ["mañana", "am", "a.m."]):
            if h == 12:
                h = 0
        elif not mod:
            # Si no especifica y es entre 1 y 6, usualmente es tarde (ej. a las 3 = 15:00)
            if 1 <= h <= 6:
                h += 12
        return (h, minute)

    # Caso C: "hoy ... (hora)"
    if not target_dt:
        m = re.search(r'\bhoy(?:\s+(?:a\s+las?|a\s+la|a)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.|de\s+la\s+mañana|de\s+la\s+tarde|de\s+la\s+noche)?))?', lower)
        if m and m.group(1):
            t = parse_time_match(m.group(1))
            if t:
                target_dt = now.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
                extracted_pattern = m.group(0)

    # Caso D: "mañana ... (hora opcional)" - Evitar que coincida con "de la mañana"
    if not target_dt:
        m = re.search(r'(?<!de\sla\s)(?<!en\sla\s)\bmañana(?:\s+(?:a\s+las?|a\s+la|a)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.|de\s+la\s+mañana|de\s+la\s+tarde|de\s+la\s+noche)?))?', lower)
        if m:
            day = now + timedelta(days=1)
            time_part = m.group(1)
            if time_part:
                t = parse_time_match(time_part)
                if t:
                    target_dt = day.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
            else:
                # Default mañana a las 8:00 AM
                target_dt = day.replace(hour=8, minute=0, second=0, microsecond=0)
            extracted_pattern = m.group(0)

    # Caso E: "pasado mañana ... (hora opcional)"
    if not target_dt:
        m = re.search(r'\bpasado\s+mañana(?:\s+(?:a\s+las?|a\s+la|a)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.|de\s+la\s+mañana|de\s+la\s+tarde|de\s+la\s+noche)?))?', lower)
        if m:
            day = now + timedelta(days=2)
            time_part = m.group(1)
            if time_part:
                t = parse_time_match(time_part)
                if t:
                    target_dt = day.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
            else:
                target_dt = day.replace(hour=8, minute=0, second=0, microsecond=0)
            extracted_pattern = m.group(0)

    # Caso F: Sin día específico pero con hora clara ("a las 10 de la mañana", "a 10 de la mañana", "a las 3pm", "a las 10:15")
    if not target_dt:
        m = re.search(r'(?:(?:alarma|recordar|recordatorio|para)?\s*(?:a\s+las?|a\s+la|a)\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.|de\s+la\s+mañana|en\s+la\s+mañana|de\s+la\s+tarde|en\s+la\s+tarde|de\s+la\s+noche|en\s+la\s+noche))', lower)
        if m and m.group(1):
            t = parse_time_match(m.group(1))
            if t:
                candidate = now.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
                # Si la hora ya pasó hoy (hace más de 1 minuto), programar para mañana
                if candidate < now - timedelta(minutes=1):
                    target_dt = candidate + timedelta(days=1)
                else:
                    target_dt = candidate
                extracted_pattern = m.group(0)

    # Caso F: Días de la semana ("el lunes 9am", "el viernes a las 3pm")
    if not target_dt:
        days_map = {
            "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
            "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6
        }
        m = re.search(r'\b(?:el\s+)?(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)(?:\s+(?:a\s+las\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?))?', lower)
        if m:
            day_name = m.group(1).replace("é", "e").replace("á", "a")
            target_weekday = days_map.get(day_name, 0)
            days_ahead = (target_weekday - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # próximo día de la próxima semana si ya pasó
            day = now + timedelta(days=days_ahead)
            time_part = m.group(2)
            if time_part:
                t = parse_time_match(time_part)
                if t:
                    target_dt = day.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
            else:
                target_dt = day.replace(hour=8, minute=0, second=0, microsecond=0)
            extracted_pattern = m.group(0)

    # Caso G: Fecha específica "DD/MM" o "DD/MM/YYYY" o "DD-MM" con hora opcional
    if not target_dt:
        m = re.search(r'\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?:\s+(?:a\s+las\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?))?', lower)
        if m:
            try:
                day_num = int(m.group(1))
                month_num = int(m.group(2))
                year_num = int(m.group(3)) if m.group(3) else now.year
                if year_num < 100:
                    year_num += 2000
                time_part = m.group(4)
                h, minute = (8, 0)
                if time_part:
                    t = parse_time_match(time_part)
                    if t:
                        h, minute = t
                target_dt = datetime(year_num, month_num, day_num, h, minute, 0, tzinfo=VET_TZ)
                extracted_pattern = m.group(0)
            except Exception:
                pass

    if target_dt:
        reminder_at = target_dt.strftime("%Y-%m-%d %H:%M:%S")
        due_date = target_dt.strftime("%Y-%m-%d")

    # Limpiar título para que quede legible (si el patrón extraído estaba al final)
    title = clean_text
    if extracted_pattern:
        # Remover patrón si está aislado
        pattern_regex = re.compile(re.escape(extracted_pattern), re.IGNORECASE)
        candidate = pattern_regex.sub("", title).strip(" ,.-")
        if len(candidate) >= 3:
            title = candidate

    return {
        "title": title if title else clean_text,
        "category": category,
        "priority": priority,
        "reminder_at": reminder_at,
        "due_date": due_date,
        "raw_text": clean_text
    }

# ==========================================
# GITHUB GIT DATA API: PERSISTENCIA Y BACKUP
# ==========================================
def parse_github_repo_owner_name() -> Tuple[str, str]:
    clean = GITHUB_REPO.strip().replace("https://github.com/", "").replace(".git", "").strip("/")
    parts = clean.split("/")
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", ""

async def github_api_request(method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    owner, repo = parse_github_repo_owner_name()
    if not owner or not repo or not GITHUB_TOKEN:
        return 400, {"message": "GITHUB_TOKEN o GITHUB_REPO no están configurados."}

    url = f"https://api.github.com/repos/{owner}/{repo}{endpoint}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Asistente-Telegram-VET"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, json=data, timeout=30) as resp:
                status = resp.status
                try:
                    res_json = await resp.json()
                except Exception:
                    res_json = await resp.text()
                return status, res_json
    except Exception as e:
        logger.error("Error en llamada a GitHub API (%s %s): %s", method, endpoint, e)
        return 500, {"message": str(e)}

async def backup_to_github() -> Tuple[bool, str]:
    """
    Sube `agenda.db` a la rama `backup-db` del repositorio de GitHub
    utilizando la GitHub Git Data API (blobs, trees, commits, refs).
    """
    if backup_state["in_progress"]:
        return False, "Ya hay un respaldo en proceso."

    backup_state["in_progress"] = True
    backup_state["last_status"] = "Sincronizando..."

    try:
        owner, repo = parse_github_repo_owner_name()
        if not owner or not repo or not GITHUB_TOKEN:
            msg = "Falta configurar GITHUB_TOKEN o GITHUB_REPO en Render."
            backup_state["last_status"] = "No configurado"
            backup_state["last_error"] = msg
            backup_state["in_progress"] = False
            return False, msg

        if not os.path.exists(DB_FILE):
            backup_state["in_progress"] = False
            return False, "El archivo agenda.db no existe aún."

        # 1. Leer el archivo binario agenda.db y codificar en base64
        with open(DB_FILE, "rb") as f:
            db_bytes = f.read()
        b64_db = base64.b64encode(db_bytes).decode("utf-8")

        # 2. Verificar si la rama backup-db existe; si no, crearla desde main
        ref_status, ref_data = await github_api_request("GET", "/git/ref/heads/backup-db")
        parent_commit_sha = ""
        base_tree_sha = ""

        if ref_status == 404:
            logger.info("Rama 'backup-db' no encontrada. Creándola...")
            # Obtener rama principal
            repo_status, repo_info = await github_api_request("GET", "")
            if repo_status != 200:
                raise Exception(f"No se pudo consultar el repositorio: {repo_info}")
            default_branch = repo_info.get("default_branch", "main")

            def_ref_status, def_ref = await github_api_request("GET", f"/git/ref/heads/{default_branch}")
            if def_ref_status != 200:
                raise Exception(f"No se encontró la rama {default_branch}: {def_ref}")
            base_sha = def_ref["object"]["sha"]

            create_ref_status, create_ref_data = await github_api_request("POST", "/git/refs", {
                "ref": "refs/heads/backup-db",
                "sha": base_sha
            })
            if create_ref_status not in [200, 201]:
                raise Exception(f"Error al crear rama backup-db: {create_ref_data}")
            parent_commit_sha = base_sha
        elif ref_status == 200:
            parent_commit_sha = ref_data["object"]["sha"]
        else:
            raise Exception(f"Error verificando rama backup-db: {ref_data}")

        # Obtener el tree_sha del commit padre
        c_status, c_data = await github_api_request("GET", f"/git/commits/{parent_commit_sha}")
        if c_status == 200:
            base_tree_sha = c_data["tree"]["sha"]

        # 3. Crear blob para agenda.db
        blob_status, blob_data = await github_api_request("POST", "/git/blobs", {
            "content": b64_db,
            "encoding": "base64"
        })
        if blob_status not in [200, 201]:
            raise Exception(f"Error creando blob de agenda.db: {blob_data}")
        blob_sha = blob_data["sha"]

        # 4. Crear blob de metadatos (timestamp, total tareas, fecha VET)
        stats = db_get_stats()
        meta_content = json.dumps({
            "timestamp_vet": now_vet().strftime("%Y-%m-%d %H:%M:%S"),
            "size_bytes": len(db_bytes),
            "stats": stats
        }, indent=2)
        meta_status, meta_data = await github_api_request("POST", "/git/blobs", {
            "content": meta_content,
            "encoding": "utf-8"
        })
        meta_sha = meta_data["sha"] if meta_status in [200, 201] else None

        # 5. Crear el árbol (tree)
        tree_elements = [
            {
                "path": "agenda.db",
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha
            }
        ]
        if meta_sha:
            tree_elements.append({
                "path": "backup_meta.json",
                "mode": "100644",
                "type": "blob",
                "sha": meta_sha
            })

        tree_payload: Dict[str, Any] = {"tree": tree_elements}
        if base_tree_sha:
            tree_payload["base_tree"] = base_tree_sha

        tree_status, tree_data = await github_api_request("POST", "/git/trees", tree_payload)
        if tree_status not in [200, 201]:
            raise Exception(f"Error creando tree en GitHub: {tree_data}")
        new_tree_sha = tree_data["sha"]

        # 6. Crear el Commit
        commit_msg = f"Auto-backup agenda.db [{now_vet().strftime('%d/%m/%Y %I:%M:%S %p')} VET]"
        commit_payload = {
            "message": commit_msg,
            "tree": new_tree_sha,
            "parents": [parent_commit_sha] if parent_commit_sha else []
        }
        commit_status, commit_data = await github_api_request("POST", "/git/commits", commit_payload)
        if commit_status not in [200, 201]:
            raise Exception(f"Error creando commit en GitHub: {commit_data}")
        new_commit_sha = commit_data["sha"]

        # 7. Actualizar la referencia heads/backup-db
        ref_update_status, ref_update_data = await github_api_request("PATCH", "/git/refs/heads/backup-db", {
            "sha": new_commit_sha,
            "force": True
        })
        if ref_update_status != 200:
            raise Exception(f"Error actualizando ref heads/backup-db: {ref_update_data}")

        # Actualizar estado interno
        backup_state["last_backup_time"] = time.time()
        backup_state["last_change_time"] = None
        backup_state["last_backup_str"] = now_vet().strftime("%d/%m/%Y %I:%M %p")
        backup_state["last_status"] = "Al día (GitHub)"
        backup_state["last_error"] = ""
        backup_state["total_backups"] += 1
        backup_state["in_progress"] = False

        logger.info("Respaldo exitoso en GitHub rama backup-db (Commit: %s)", new_commit_sha[:7])
        return True, f"Respaldo completado en rama 'backup-db' ({new_commit_sha[:7]})"

    except Exception as e:
        logger.error("Error durante el respaldo a GitHub: %s", e)
        backup_state["last_status"] = "Error"
        backup_state["last_error"] = str(e)
        backup_state["in_progress"] = False
        return False, f"Error en respaldo: {str(e)}"

async def restore_from_github() -> Tuple[bool, str]:
    """
    Restaura `agenda.db` desde la rama `backup-db` de GitHub si existe un respaldo previo.
    Se ejecuta al iniciar el servidor para no perder datos en Render Free.
    """
    owner, repo = parse_github_repo_owner_name()
    if not owner or not repo or not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN o GITHUB_REPO no configurados. Omitiendo restauración inicial.")
        return False, "GitHub no configurado"

    logger.info("Consultando GitHub para restaurar base de datos desde rama backup-db...")
    try:
        # Descargar archivo agenda.db de la rama backup-db
        status, file_data = await github_api_request("GET", "/contents/agenda.db?ref=backup-db")
        if status == 404:
            logger.info("No existe respaldo previo en backup-db. Iniciando con base de datos limpia.")
            return False, "No existe respaldo en GitHub todavía."
        if status != 200:
            logger.warning("No se pudo obtener archivo de GitHub: %s", file_data)
            return False, f"Respuesta inesperada: {status}"

        # Descargar contenido base64
        if "content" in file_data and file_data.get("encoding") == "base64":
            raw_b64 = file_data["content"].replace("\n", "").replace("\r", "")
            db_bytes = base64.b64decode(raw_b64)
        elif "download_url" in file_data and file_data["download_url"]:
            async with aiohttp.ClientSession() as s:
                async with s.get(file_data["download_url"], headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}) as r:
                    db_bytes = await r.read()
        else:
            return False, "Formato de archivo no soportado."

        # Guardar en disco
        with open(DB_FILE, "wb") as f:
            f.write(db_bytes)

        backup_state["last_backup_str"] = "Restaurado al iniciar"
        backup_state["last_status"] = "Al día (GitHub)"
        logger.info("¡Base de datos agenda.db restaurada con éxito desde GitHub! (%d bytes)", len(db_bytes))
        return True, "Base de datos restaurada correctamente."
    except Exception as e:
        logger.error("Error restaurando base de datos desde GitHub: %s", e)
        return False, str(e)

# ==========================================
# GROQ AUDIO TRANSCRIPTION (NOTAS DE VOZ)
# ==========================================
async def transcribe_voice_groq(voice_bytes: bytes) -> Tuple[bool, str]:
    if not GROQ_API_KEY:
        return False, "No se ha configurado la variable GROQ_API_KEY en Render."

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}"
    }

    data = aiohttp.FormData()
    data.add_field("file", voice_bytes, filename="voice.ogg", content_type="audio/ogg")
    data.add_field("model", "whisper-large-v3-turbo")
    data.add_field("language", "es")
    data.add_field("temperature", "0.0")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data, timeout=45) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    transcription = res_json.get("text", "").strip()
                    return True, transcription
                else:
                    err = await resp.text()
                    logger.error("Error en Groq Whisper (%d): %s", resp.status, err)
                    return False, f"Error en Groq ({resp.status})"
    except Exception as e:
        logger.error("Excepción transcribiendo audio con Groq: %s", e)
        return False, str(e)

# ==========================================
# CLIENTE Y BOT DE TELEGRAM
# ==========================================
user_active_modes: Dict[str, str] = {}  # chat_id -> 'urgente' | 'idea' | 'sala' | 'tx'

def get_main_keyboard() -> Dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📅 Hoy"}, {"text": "📋 Ver Todo"}],
            [{"text": "🔴 Urgente"}, {"text": "💡 Anotar Idea"}],
            [{"text": "⚡ Sala Situacional"}, {"text": "🔌 Equipos / Tx"}],
            [{"text": "⏱️ Probar Alarma (1m)"}, {"text": "📊 Resumen Diario"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }

async def telegram_send_message(
    chat_id: str,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: Optional[Dict[str, Any]] = None
) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    else:
        # Enviar teclado principal por defecto para tener siempre el menú accesible
        payload["reply_markup"] = get_main_keyboard()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=15) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error("Error enviando mensaje a Telegram: %s", e)
        return False

async def telegram_answer_callback(callback_id: str, text: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload: Dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error("Error respondiendo callback: %s", e)

async def handle_telegram_callback(cq: Dict[str, Any]):
    cq_id = cq.get("id", "")
    from_user = cq.get("from", {})
    chat_id = str(from_user.get("id", ""))
    data = cq.get("data", "")

    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        await telegram_answer_callback(cq_id, "⛔ No autorizado")
        return

    if data.startswith("done:"):
        task_id = int(data.split(":")[1])
        updated = db_toggle_task(task_id)
        st = "completada ✅" if updated and updated["status"] == "done" else "reabierta ⏳"
        await telegram_answer_callback(cq_id, f"Tarea #{task_id} {st}")
        await telegram_send_message(chat_id, f"🎯 Tarea *#{task_id}* marcada como {st}.")
        return

    if data.startswith("postpone:"):
        parts = data.split(":")
        task_id = int(parts[1])
        add_minutes = int(parts[2]) if len(parts) > 2 else 30
        new_dt = now_vet() + timedelta(minutes=add_minutes)
        new_str = new_dt.strftime("%Y-%m-%d %H:%M:%S")
        db_postpone_task(task_id, new_str, new_dt.strftime("%Y-%m-%d"))
        await telegram_answer_callback(cq_id, f"Pospuesta {add_minutes} min")
        await telegram_send_message(chat_id, f"⏰ Tarea *#{task_id}* pospuesta para: *{new_dt.strftime('%I:%M %p')}* VET.")
        return

    if data.startswith("del:"):
        task_id = int(data.split(":")[1])
        task = db_get_task(task_id)
        title = task["title"] if task else f"#{task_id}"
        db_delete_task(task_id)
        await telegram_answer_callback(cq_id, f"Tarea #{task_id} eliminada")
        await telegram_send_message(chat_id, f"🗑️ Tarea *#{task_id}* eliminada:\n_{title}_")
        return

    await telegram_answer_callback(cq_id)

async def telegram_get_file_bytes(file_id: str) -> Optional[bytes]:
    if not TELEGRAM_BOT_TOKEN:
        return None
    url_file_info = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url_file_info, timeout=15) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                file_path = data.get("result", {}).get("file_path")
                if not file_path:
                    return None

            download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
            async with session.get(download_url, timeout=30) as dl_resp:
                if dl_resp.status == 200:
                    return await dl_resp.read()
    except Exception as e:
        logger.error("Error descargando archivo de Telegram: %s", e)
    return None

def format_task_line(t: Dict[str, Any]) -> str:
    category_icon = {
        "Sala": "⚡",
        "Equipos/Tx": "🔌",
        "Personal": "👤",
        "Ideas": "💡"
    }.get(t["category"], "📌")

    prio_icon = "🔴 " if t["priority"] == "Alta" else ""
    date_info = ""
    if t.get("reminder_at"):
        try:
            dt = datetime.strptime(t["reminder_at"], "%Y-%m-%d %H:%M:%S")
            date_info = f" _(⏰ {dt.strftime('%d/%m %I:%M%p')})_"
        except Exception:
            date_info = f" _(⏰ {t['reminder_at']})_"
    elif t.get("due_date"):
        date_info = f" _(📅 {t['due_date']})_"

    return f"• *#{t['id']}* {category_icon} {prio_icon}{t['title']}{date_info}"

async def handle_telegram_message(msg: Dict[str, Any]):
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    user_name = msg.get("from", {}).get("first_name", "Usuario")

    # Validación estricta de seguridad: solo permitir el Chat ID autorizado
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        logger.warning("Intento de acceso denegado de Chat ID: %s (Usuario: %s)", chat_id, user_name)
        await telegram_send_message(
            chat_id,
            "⛔ *Acceso no autorizado.*\nEste bot es un asistente personal privado."
        )
        return

    text = msg.get("text", "").strip()
    voice = msg.get("voice")

    # 1. GESTIÓN DE NOTAS DE VOZ
    if voice:
        file_id = voice.get("file_id")
        await telegram_send_message(chat_id, "🎙️ *Procesando tu nota de voz...*")
        voice_bytes = await telegram_get_file_bytes(file_id)
        if not voice_bytes:
            await telegram_send_message(chat_id, "❌ No se pudo descargar la nota de voz de Telegram.")
            return

        ok, transcript = await transcribe_voice_groq(voice_bytes)
        if not ok:
            await telegram_send_message(
                chat_id,
                f"⚠️ *No se pudo transcribir el audio:*\n_{transcript}_\n\n"
                "👉 Si no has configurado `GROQ_API_KEY`, agrégala en Render para transcripción gratuita ultrarrápida con Whisper."
            )
            return

        await telegram_send_message(chat_id, f"🎙️ *Transcripción:* \n_\"{transcript}\"_")
        text = transcript

    if not text:
        return

    # 2. BOTONES DEL MENÚ PRINCIPAL Y COMANDOS RÁPIDOS
    lower = text.lower()

    if lower in ["📅 hoy", "hoy"]:
        lower = "/hoy"
    elif lower in ["📋 ver todo", "ver todo", "todos", "todo"]:
        lower = "/todo"
    elif lower in ["📊 resumen diario", "resumen diario", "resumen"]:
        lower = "/resumen"
    elif lower in ["⏱️ probar alarma (1m)", "probar alarma", "probar alarma 1m"]:
        lower = "/probar_alarma"
    elif lower in ["🔴 urgente", "urgente"]:
        user_active_modes[chat_id] = "urgente"
        await telegram_send_message(
            chat_id,
            "🔴 *Modo Urgente Activado*\n\n"
            "Escribe o graba por voz tu tarea a continuación. Se guardará directamente con *Prioridad Alta* 🔴 (y con alarma si indicas hora)."
        )
        return
    elif lower in ["💡 anotar idea", "anotar idea", "nueva idea"]:
        user_active_modes[chat_id] = "idea"
        await telegram_send_message(
            chat_id,
            "💡 *Modo Idea Activado*\n\n"
            "Escribe o graba tu idea a continuación. Se guardará directamente en tu bandeja de *Ideas*."
        )
        return
    elif lower in ["⚡ sala situacional", "sala situacional", "novedad sala"]:
        user_active_modes[chat_id] = "sala"
        await telegram_send_message(
            chat_id,
            "⚡ *Modo Sala Situacional Activado*\n\n"
            "Escribe la novedad, disparo de circuito, carga en MW o maniobra."
        )
        return
    elif lower in ["🔌 equipos / tx", "🔌 equipos/tx", "equipos / tx", "equipos/tx", "tx"]:
        user_active_modes[chat_id] = "tx"
        await telegram_send_message(
            chat_id,
            "🔌 *Modo Equipos / Transformadores Activado*\n\n"
            "Escribe la inspección del transformador, nivel de aceite, DGA o equipo."
        )
        return

    if lower in ["/start", "/ayuda", "ayuda", "menu"]:
        help_msg = (
            f"👋 *¡Hola {user_name}! Soy tu Asistente Personal.*\n\n"
            "Puedo guardar pendientes, ideas y recordatorios al vuelo tanto por *texto* como por *nota de voz*.\n\n"
            "📌 *Captura automática:*\n"
            "• Escribe o graba cualquier mensaje: lo guardo y categorizo (⚡ *Sala*, 🔌 *Equipos/Tx*, 👤 *Personal*).\n"
            "• Si empieza con *Idea:* se guarda en 💡 *Ideas*.\n"
            "• Si dices fecha u hora _(ej: 'mañana 8am', 'hoy 3pm', 'lunes 9am')_, programo un recordatorio en hora Venezuela (`UTC-4`).\n\n"
            "⚡ *Comandos rápidos:*\n"
            "• `/hoy` - Pendientes de hoy, atrasados y recordatorios\n"
            "• `/todo` - Todos los pendientes abiertos por categoría\n"
            "• `/ideas` - Todas las ideas registradas\n"
            "• `/hecho <id>` - Marcar pendiente como completado\n"
            "• `/posponer <id> <tiempo>` - Posponer tarea (ej: `/posponer 3 mañana 9am`)\n"
            "• `/borrar <id>` - Eliminar tarea\n"
            "• `/resumen` - Balance del día\n"
            "• `/backup` - Estado y forzar respaldo en GitHub"
        )
        await telegram_send_message(chat_id, help_msg)
        return

    if lower == "/hoy":
        stats = db_get_stats()
        today_str = now_vet().strftime("%Y-%m-%d")
        now_str = now_vet().strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db_connection()
        cursor = conn.cursor()
        # Pendientes de hoy
        cursor.execute("""
            SELECT * FROM tasks WHERE status = 'pending' AND category != 'Ideas'
            AND (due_date = ? OR reminder_at LIKE ? OR (due_date IS NULL AND reminder_at IS NULL AND created_at LIKE ?))
            ORDER BY id ASC
        """, (today_str, f"{today_str}%", f"{today_str}%"))
        hoy_tasks = [dict(r) for r in cursor.fetchall()]

        # Atrasados
        cursor.execute("""
            SELECT * FROM tasks WHERE status = 'pending' AND category != 'Ideas'
            AND ((due_date IS NOT NULL AND due_date < ?) OR (reminder_at IS NOT NULL AND reminder_at < ?))
            ORDER BY id ASC
        """, (today_str, now_str))
        atrasadas = [dict(r) for r in cursor.fetchall()]
        conn.close()

        lines = [f"📅 *AGENDA DE HOY ({now_vet().strftime('%d/%m/%Y')})*\n"]
        if atrasadas:
            lines.append("⚠️ *ATRASADOS:*")
            for t in atrasadas:
                lines.append(format_task_line(t))
            lines.append("")

        if hoy_tasks:
            lines.append("📋 *PENDIENTES HOY:*")
            for t in hoy_tasks:
                lines.append(format_task_line(t))
        else:
            lines.append("🎉 _¡No tienes más pendientes anotados para hoy!_")

        lines.append(f"\n💡 Usa `/hecho <id>` para completar.")
        await telegram_send_message(chat_id, "\n".join(lines))
        return

    if lower == "/todo":
        tasks = db_list_tasks(status="pending")
        if not tasks:
            await telegram_send_message(chat_id, "🎉 *¡Estás al día!* No hay ningún pendiente abierto.")
            return

        cats: Dict[str, List[Dict[str, Any]]] = {}
        for t in tasks:
            c = t["category"]
            cats.setdefault(c, []).append(t)

        msg_parts = ["📋 *TODOS LOS PENDIENTES ABIERTOS:*\n"]
        for cat, items in cats.items():
            icon = {"Sala": "⚡", "Equipos/Tx": "🔌", "Personal": "👤", "Ideas": "💡"}.get(cat, "📌")
            msg_parts.append(f"*{icon} {cat} ({len(items)}):*")
            for it in items:
                msg_parts.append(format_task_line(it))
            msg_parts.append("")

        msg_parts.append("Usa `/hecho <id>` para completar.")
        await telegram_send_message(chat_id, "\n".join(msg_parts))
        return

    if lower == "/ideas":
        ideas = db_list_tasks(status="pending", category="Ideas")
        if not ideas:
            await telegram_send_message(chat_id, "💡 No tienes ideas guardadas aún. Envía `Idea: tu propuesta` para registrarla.")
            return
        lines = [f"💡 *BANDEJA DE IDEAS ({len(ideas)}):*\n"]
        for i in ideas:
            lines.append(f"• *#{i['id']}* {i['title']}")
        await telegram_send_message(chat_id, "\n".join(lines))
        return

    if lower.startswith("/hecho"):
        parts = text.split()
        if len(parts) < 2:
            await telegram_send_message(chat_id, "⚠️ Indica el ID. Ej: `/hecho 5` o `/hecho #5`")
            return
        clean_id = parts[1].replace("#", "")
        if not clean_id.isdigit():
            await telegram_send_message(chat_id, "⚠️ El ID debe ser un número.")
            return
        t_id = int(clean_id)
        task = db_get_task(t_id)
        if not task:
            await telegram_send_message(chat_id, f"❌ No se encontró la tarea #{t_id}.")
            return
        updated = db_toggle_task(t_id)
        st_text = "completada ✅" if updated and updated["status"] == "done" else "reabierta ⏳"
        await telegram_send_message(chat_id, f"🎯 Tarea *#{t_id}* marcada como {st_text}:\n_{task['title']}_")
        return

    if lower.startswith("/posponer"):
        # Sintaxis: /posponer <id> <nueva fecha o tiempo>
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await telegram_send_message(chat_id, "⚠️ Uso: `/posponer <id> <tiempo>`\nEjemplos:\n• `/posponer 2 mañana 9am`\n• `/posponer 2 hoy 4pm`\n• `/posponer 2 en 2 horas`")
            return
        clean_id = parts[1].replace("#", "")
        if not clean_id.isdigit():
            await telegram_send_message(chat_id, "⚠️ El ID debe ser numérico.")
            return
        t_id = int(clean_id)
        parsed = parse_smart_text(parts[2])
        if not parsed["reminder_at"]:
            await telegram_send_message(chat_id, "⚠️ No reconocí la fecha/hora. Intenta: `mañana 8am`, `hoy 3pm`, `en 30 minutos`.")
            return
        db_postpone_task(t_id, parsed["reminder_at"], parsed["due_date"])
        await telegram_send_message(chat_id, f"⏰ Tarea *#{t_id}* pospuesta para: *{parsed['reminder_at']}* (VET).")
        return

    if lower.startswith("/borrar"):
        parts = text.split()
        if len(parts) < 2:
            await telegram_send_message(chat_id, "⚠️ Indica el ID a borrar. Ej: `/borrar 4`")
            return
        clean_id = parts[1].replace("#", "")
        if not clean_id.isdigit():
            await telegram_send_message(chat_id, "⚠️ ID inválido.")
            return
        t_id = int(clean_id)
        task = db_get_task(t_id)
        if not task:
            await telegram_send_message(chat_id, f"❌ Tarea #{t_id} no encontrada.")
            return
        db_delete_task(t_id)
        await telegram_send_message(chat_id, f"🗑️ Tarea *#{t_id}* eliminada:\n_{task['title']}_")
        return

    if lower in ["/probar_alarma", "/alarma"]:
        target = now_vet() + timedelta(minutes=1)
        t_str = target.strftime("%Y-%m-%d %H:%M:%S")
        t_id = db_add_task(
            title="🔔 Prueba de alarma en 1 minuto",
            category="Personal",
            priority="Alta",
            due_date=target.strftime("%Y-%m-%d"),
            reminder_at=t_str,
            source="telegram_text",
            raw_text="/probar_alarma"
        )
        await telegram_send_message(
            chat_id,
            f"⏱️ *¡Alarma de prueba agendada (# {t_id})!*\n\n"
            f"Sonará exactamente a las: *{target.strftime('%I:%M %p')}* (en 60 segundos).\n"
            f"🔔 Te llegará una alerta automática aquí mismo."
        )
        return

    if lower == "/resumen":
        stats = db_get_stats()
        msg_res = (
            f"📊 *RESUMEN DE SITUACIÓN ({stats['hora_vet']})*\n\n"
            f"📋 *Pendientes Hoy:* {stats['pendientes_hoy']}\n"
            f"⚠️ *Atrasados:* {stats['atrasados']}\n"
            f"✅ *Completados Hoy:* {stats['completados_hoy']}\n"
            f"💡 *Ideas guardadas:* {stats['ideas']}\n"
            f"⏳ *Total pendientes abiertos:* {stats['total_pendientes']}\n\n"
            f"☁️ *Respaldo GitHub:* {backup_state['last_status']} ({backup_state['last_backup_str']})"
        )
        await telegram_send_message(chat_id, msg_res)
        return

    if lower == "/backup":
        await telegram_send_message(chat_id, "☁️ *Iniciando respaldo manual a GitHub (backup-db)...*")
        ok, res_msg = await backup_to_github()
        icon = "✅" if ok else "❌"
        await telegram_send_message(chat_id, f"{icon} *Resultado Respaldo:*\n{res_msg}")
        return

    # 3. CAPTURA AL VUELO INTELIGENTE DE TEXTO
    mode = user_active_modes.pop(chat_id, None)
    parsed = parse_smart_text(text)

    # Aplicar modo activo si el usuario tocó un botón del menú previamente
    if mode == "urgente":
        parsed["priority"] = "Alta"
    elif mode == "idea":
        parsed["category"] = "Ideas"
    elif mode == "sala":
        parsed["category"] = "Sala"
    elif mode == "tx":
        parsed["category"] = "Equipos/Tx"

    task_id = db_add_task(
        title=parsed["title"],
        category=parsed["category"],
        priority=parsed["priority"],
        due_date=parsed["due_date"],
        reminder_at=parsed["reminder_at"],
        source="telegram_voice" if voice else "telegram_text",
        raw_text=text
    )

    cat_emojis = {"Sala": "⚡ Sala Situacional", "Equipos/Tx": "🔌 Equipos/Tx", "Personal": "👤 Personal", "Ideas": "💡 Idea"}
    cat_label = cat_emojis.get(parsed["category"], f"📌 {parsed['category']}")

    reminder_msg = ""
    if parsed["reminder_at"]:
        try:
            r_dt = datetime.strptime(parsed["reminder_at"], "%Y-%m-%d %H:%M:%S")
            is_today = (r_dt.date() == now_vet().date())
            day_label = "Hoy" if is_today else f"Para el {r_dt.strftime('%d/%m')}"
            reminder_msg = f"\n⏰ *Alarma programada:* {day_label} a las *{r_dt.strftime('%I:%M %p')}* (VET)"
        except Exception:
            reminder_msg = f"\n⏰ *Alarma programada:* `{parsed['reminder_at']}` (VET)"
    else:
        reminder_msg = "\nℹ️ _(Sin fecha/hora de alarma detectada)_"

    prio_msg = " | 🔴 *Alta Prioridad*" if parsed["priority"] == "Alta" else ""

    confirm_text = (
        f"✅ *Guardado #{task_id}*\n"
        f"📌 *{parsed['title']}*\n"
        f"🏷️ *Categoría:* {cat_label}{prio_msg}"
        f"{reminder_msg}"
    )

    # Botones de acción directa debajo del mensaje para tocar con un dedo
    inline_actions = {
        "inline_keyboard": [
            [
                {"text": "✅ Hecho", "callback_data": f"done:{task_id}"},
                {"text": "⏰ +30 min", "callback_data": f"postpone:{task_id}:30"},
                {"text": "🗑️ Borrar", "callback_data": f"del:{task_id}"}
            ]
        ]
    }
    await telegram_send_message(chat_id, confirm_text, reply_markup=inline_actions)

# ==========================================
# CICLO DE POLLING DE TELEGRAM
# ==========================================
async def telegram_polling_loop():
    logger.info("Iniciando loop de polling de Telegram...")
    offset = 0

    while True:
        if not TELEGRAM_BOT_TOKEN:
            logger.warning("TELEGRAM_BOT_TOKEN no configurado. Esperando 10 segundos...")
            await asyncio.sleep(10)
            continue

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"offset": offset, "timeout": 20}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=25) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        updates = data.get("result", [])
                        for update in updates:
                            offset = update["update_id"] + 1
                            if "message" in update:
                                try:
                                    await handle_telegram_message(update["message"])
                                except Exception as e:
                                    logger.error("Error manejando mensaje de Telegram: %s", e)
                            elif "callback_query" in update:
                                try:
                                    await handle_telegram_callback(update["callback_query"])
                                except Exception as e:
                                    logger.error("Error manejando callback query: %s", e)
                    else:
                        err_text = await resp.text()
                        logger.error("Error en getUpdates (%d): %s", resp.status, err_text)
                        await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Excepción en polling de Telegram: %s", e)
            await asyncio.sleep(4)

# ==========================================
# TAREAS EN SEGUNDO PLANO (RECORDATORIOS, RESÚMENES, BACKUP)
# ==========================================
async def background_scheduler():
    logger.info("Scheduler en segundo plano iniciado (Hora VET: %s)", now_vet().strftime("%H:%M:%S"))
    last_morning_sent_date = ""
    last_nightly_sent_date = ""

    while True:
        try:
            vet_time = now_vet()
            today_date = vet_time.strftime("%Y-%m-%d")
            hour_minute = vet_time.strftime("%H:%M")

            # 1. Chequeo de Recordatorios Puntuales
            due_reminders = db_get_due_reminders()
            for r in due_reminders:
                if TELEGRAM_CHAT_ID:
                    alert_msg = (
                        f"⏰ *¡ALERTA DE RECORDATORIO! (# {r['id']})*\n\n"
                        f"📌 *{r['title']}*\n"
                        f"🏷️ Categoría: *{r['category']}* | Prioridad: *{r['priority']}*"
                    )
                    alert_buttons = {
                        "inline_keyboard": [
                            [
                                {"text": "✅ Completado", "callback_data": f"done:{r['id']}"},
                                {"text": "⏰ +15 min", "callback_data": f"postpone:{r['id']}:15"},
                                {"text": "⏰ +1 hora", "callback_data": f"postpone:{r['id']}:60"}
                            ]
                        ]
                    }
                    await telegram_send_message(TELEGRAM_CHAT_ID, alert_msg, reply_markup=alert_buttons)
                db_mark_reminder_sent(r["id"])

            # 2. Resumen Matutino (06:00 AM VET)
            if hour_minute == "06:00" and last_morning_sent_date != today_date:
                last_morning_sent_date = today_date
                if TELEGRAM_CHAT_ID:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT * FROM tasks WHERE status = 'pending' AND category != 'Ideas'
                        AND (due_date = ? OR reminder_at LIKE ? OR (due_date IS NULL AND reminder_at IS NULL))
                    """, (today_date, f"{today_date}%"))
                    hoy_tasks = [dict(row) for row in cursor.fetchall()]

                    cursor.execute("""
                        SELECT * FROM tasks WHERE status = 'pending' AND category != 'Ideas'
                        AND (due_date IS NOT NULL AND due_date < ?)
                    """, (today_date,))
                    atrasadas = [dict(row) for row in cursor.fetchall()]
                    conn.close()

                    msg_morning = [
                        f"🌅 *BUENOS DÍAS | RESUMEN MATUTINO (06:00 AM VET)*\n",
                        f"📋 *Tareas para hoy ({len(hoy_tasks)}):*"
                    ]
                    for t in hoy_tasks:
                        msg_morning.append(format_task_line(t))
                    if atrasadas:
                        msg_morning.append(f"\n⚠️ *Atrasadas ({len(atrasadas)}):*")
                        for t in atrasadas:
                            msg_morning.append(format_task_line(t))
                    msg_morning.append("\n¡Excelente jornada y control operativo!")
                    await telegram_send_message(TELEGRAM_CHAT_ID, "\n".join(msg_morning))

            # 3. Cierre Nocturno (07:00 PM VET = 19:00)
            if hour_minute == "19:00" and last_nightly_sent_date != today_date:
                last_nightly_sent_date = today_date
                if TELEGRAM_CHAT_ID:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM tasks WHERE status = 'done' AND completed_at LIKE ?", (f"{today_date}%",))
                    hechas_hoy = [dict(row) for row in cursor.fetchall()]

                    cursor.execute("SELECT * FROM tasks WHERE status = 'pending' AND category != 'Ideas'")
                    quedan_pend = [dict(row) for row in cursor.fetchall()]
                    conn.close()

                    msg_night = [
                        f"🌙 *CIERRE DE JORNADA (07:00 PM VET)*\n",
                        f"✅ *Lograste completar hoy ({len(hechas_hoy)}):*"
                    ]
                    for t in hechas_hoy:
                        msg_night.append(f"• #{t['id']} {t['title']}")
                    msg_night.append(f"\n⏳ *Quedan pendientes para mañana ({len(quedan_pend)}):*")
                    for t in quedan_pend[:8]:  # Mostrar hasta 8
                        msg_night.append(format_task_line(t))
                    if len(quedan_pend) > 8:
                        msg_night.append(f"• _...y {len(quedan_pend) - 8} más (ver en `/todo`)_")

                    msg_night.append("\nBuen descanso y recarga de energías.")
                    await telegram_send_message(TELEGRAM_CHAT_ID, "\n".join(msg_night))

            # 4. Auto-Respaldo en GitHub:
            # Si hubo cambios y pasaron 5 minutos desde último backup O 3 minutos de inactividad tras un cambio
            if backup_state["last_change_time"] is not None and not backup_state["in_progress"]:
                now_loop = time.time()
                elapsed_since_change = now_loop - backup_state["last_change_time"]
                elapsed_since_backup = now_loop - (backup_state["last_backup_time"] or 0)

                # Regla: 5 minutos desde el último respaldo (300s) o 3 minutos de inactividad tras cambio (180s)
                if elapsed_since_backup >= 300 or elapsed_since_change >= 180:
                    logger.info("Disparando respaldo automático a GitHub por cambios...")
                    await backup_to_github()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error en background_scheduler: %s", e)

        await asyncio.sleep(20)

# ==========================================
# RUTAS DE LA API WEB Y SERVIDOR ESTÁTICO
# ==========================================
async def api_get_tasks(request: web.Request) -> web.Response:
    status = request.query.get("status")
    category = request.query.get("category")
    search = request.query.get("search")
    tasks = db_list_tasks(status=status, category=category, search=search)
    return web.json_response({"success": True, "tasks": tasks})

async def api_create_task(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        title = data.get("title", "").strip()
        if not title:
            return web.json_response({"success": False, "error": "El título es obligatorio."}, status=400)

        category = data.get("category", "Personal")
        priority = data.get("priority", "Normal")
        due_date = data.get("due_date")
        reminder_at = data.get("reminder_at")
        description = data.get("description", "")

        task_id = db_add_task(
            title=title,
            category=category,
            priority=priority,
            description=description,
            due_date=due_date,
            reminder_at=reminder_at,
            source="web",
            raw_text=title
        )
        task = db_get_task(task_id)
        return web.json_response({"success": True, "task": task})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_toggle_task(request: web.Request) -> web.Response:
    task_id = int(request.match_info["id"])
    task = db_toggle_task(task_id)
    if not task:
        return web.json_response({"success": False, "error": "Tarea no encontrada"}, status=404)
    return web.json_response({"success": True, "task": task})

async def api_update_task(request: web.Request) -> web.Response:
    task_id = int(request.match_info["id"])
    data = await request.json()
    task = db_update_task(task_id, data)
    if not task:
        return web.json_response({"success": False, "error": "Tarea no encontrada"}, status=404)
    return web.json_response({"success": True, "task": task})

async def api_delete_task(request: web.Request) -> web.Response:
    task_id = int(request.match_info["id"])
    ok = db_delete_task(task_id)
    return web.json_response({"success": ok})

async def api_get_stats(request: web.Request) -> web.Response:
    stats = db_get_stats()
    return web.json_response({"success": True, "stats": stats})

async def api_backup_status(request: web.Request) -> web.Response:
    return web.json_response({
        "success": True,
        "backup": {
            "last_backup_str": backup_state["last_backup_str"],
            "last_status": backup_state["last_status"],
            "last_error": backup_state["last_error"],
            "in_progress": backup_state["in_progress"],
            "total_backups": backup_state["total_backups"],
            "github_configured": bool(GITHUB_TOKEN and GITHUB_REPO)
        }
    })

async def api_backup_now(request: web.Request) -> web.Response:
    ok, msg = await backup_to_github()
    return web.json_response({
        "success": ok,
        "message": msg,
        "status": backup_state["last_status"]
    })

async def serve_index(request: web.Request) -> web.Response:
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "index.html")
    if os.path.exists(index_path):
        return web.FileResponse(index_path)
    return web.Response(text="<h1>Panel Web Asistente</h1><p>Archivo public/index.html no encontrado.</p>", content_type="text/html")

# ==========================================
# LIFESPAN Y ARRANQUE DE LA APLICACIÓN
# ==========================================
async def on_startup(app: web.Application):
    logger.info("Arrancando Asistente Personal...")
    init_db()

    # Intentar restaurar desde GitHub antes de empezar a servir
    try:
        await restore_from_github()
    except Exception as e:
        logger.error("Error al restaurar al arrancar: %s", e)

    # Iniciar polling de Telegram y scheduler en segundo plano
    app["telegram_task"] = asyncio.create_task(telegram_polling_loop())
    app["scheduler_task"] = asyncio.create_task(background_scheduler())

async def on_cleanup(app: web.Application):
    logger.info("Deteniendo tareas en segundo plano...")
    if "telegram_task" in app:
        app["telegram_task"].cancel()
    if "scheduler_task" in app:
        app["scheduler_task"].cancel()

def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Rutas Web y API
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/tasks", api_get_tasks)
    app.router.add_post("/api/tasks", api_create_task)
    app.router.add_post("/api/tasks/{id}/toggle", api_toggle_task)
    app.router.add_put("/api/tasks/{id}", api_update_task)
    app.router.add_delete("/api/tasks/{id}", api_delete_task)
    app.router.add_get("/api/stats", api_get_stats)
    app.router.add_get("/api/backup_status", api_backup_status)
    app.router.add_post("/api/backup_now", api_backup_now)

    # Archivos estáticos en /public
    public_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
    if os.path.exists(public_dir):
        app.router.add_static("/static/", public_dir)

    return app

if __name__ == "__main__":
    app = create_app()
    logger.info("Iniciando servidor en puerto %d (Render Free Web Service)...", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)
