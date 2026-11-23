# ⚡ Asistente Personal de Operaciones & Agenda (Telegram + Web)

Asistente inteligente diseñado para captura al vuelo de pendientes, recordatorios en hora **Venezuela (UTC-4)** e ideas de trabajo (Sala Situacional eléctrica, mantenimiento de Transformadores/Equipos y vida personal), alojado en **Render Free** con **panel web integrado** y **persistencia automática en GitHub**.

---

## 🌟 Características Principales

1. **Captura Rápida al Vuelo:**
   - Envía cualquier mensaje de texto o **nota de voz** por Telegram y se categoriza automáticamente:
     - ⚡ **Sala Situacional** (fallas, circuitos, subestaciones, carga MW, 115kV, etc.).
     - 🔌 **Equipos / Tx** (transformadores, aceite, DGA, interruptores, termografía).
     - 💡 **Ideas** (si empieza por `Idea:` o menciona ideas).
     - 👤 **Personal** (asuntos personales, pagos, diligencias).
   - Asignación automática de prioridad (Alta 🔴, Normal, Baja).
   - Detección inteligente de fecha y hora natural (*"mañana 8am"*, *"hoy 3pm"*, *"el lunes 9am"*, *"en 30 minutos"*).

2. **Transcripción de Notas de Voz (.ogg):**
   - Transcribe notas de voz automáticamente usando **Groq Whisper** (`whisper-large-v3-turbo`) de forma gratuita y en menos de 1 segundo.

3. **Notificaciones y Alarmas Automáticas:**
   - **Recordatorios puntuales:** Alerta por Telegram cuando se cumple la hora de una tarea programada.
   - **Resumen Matutino (06:00 AM VET):** Envío automático de tareas del día y atrasadas.
   - **Cierre Nocturno (07:00 PM VET):** Balance de lo completado en la jornada y lo pendiente.

4. **Panel Web Moderno (`/` en Render):**
   - Tema oscuro con Tailwind CSS.
   - Pestañas: **Hoy**, **Todos los Pendientes**, **Ideas**, **Completados**.
   - Tarjetas de métricas en tiempo real.
   - Botón de **☁️ Respaldo** y estado en vivo conectado a `/api/backup_status` y `/api/backup_now`.

5. **Persistencia Garantizada en Render Free (Sin pérdida de datos):**
   - Respaldo automático de `agenda.db` hacia una rama separada `backup-db` en GitHub usando la **GitHub Git Data API**.
   - Se guarda cada 5 minutos si hubo cambios o tras 3 minutos de inactividad.
   - Al arrancar en Render, restaura automáticamente la base de datos desde `backup-db`.

---

## 🚀 Despliegue en Render (Paso a Paso)

### 1. Variables de Entorno Requeridas en Render
En la sección **Environment** de tu servicio web en Render:

| Variable | Descripción | Ejemplo |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Token del bot de `@BotFather` | `7123456789:AAH...` |
| `TELEGRAM_CHAT_ID` | Tu ID de chat numérico privado | `987654321` |
| `GITHUB_TOKEN` | Personal Access Token con permiso `repo` | `ghp_...` |
| `GITHUB_REPO` | Tu repositorio en GitHub | `usuario/asistente-telegram` |
| `GROQ_API_KEY` | *(Opcional)* API key de Groq para transcribir voz | `gsk_...` |
| `PORT` | Render lo asigna automáticamente | `10000` |

### 2. Comandos en Render
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python server.py`

---

## 📱 Comandos del Bot en Telegram

- `/start` o `/ayuda`: Menú de ayuda e instrucciones.
- `/hoy`: Pendientes de hoy, atrasados y recordatorios.
- `/todo`: Todos los pendientes abiertos agrupados por categoría.
- `/ideas`: Lista de todas las ideas registradas.
- `/hecho <id>`: Marca una tarea como completada (ej: `/hecho 2`).
- `/posponer <id> <tiempo>`: Mueve una tarea para después (ej: `/posponer 2 mañana 9am`).
- `/borrar <id>`: Elimina una tarea (ej: `/borrar 5`).
- `/resumen`: Balance del día (completadas vs pendientes).
- `/backup`: Estado del respaldo en GitHub y opción de forzar copia manual.
