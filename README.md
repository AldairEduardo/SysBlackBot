# Blackboard Bot · SENATI

Bot **privado** de Telegram para consultar tus tareas próximas, fechas de entrega y eventos de Blackboard sin abrir el navegador cada vez.

Usa tu propia cuenta de SENATI (`@senati.pe` o `@senati.edu.pe`) y guarda todo en tu PC.

> ⚠️ Es un proyecto personal. Úsalo solo con tu cuenta y respeta las normas de uso de tu institución. La automatización puede dejar de funcionar si Microsoft o Blackboard cambian sus páginas.

---

## Qué hace

- 📚 `/tareas` — próximas entregas agrupadas por día, con curso, hora y cuenta atrás.
- 📅 `/eventos` — clases, eventos institucionales y horas de oficina.
- ⏰ Revisión automática cada 30 minutos: avisa de tareas nuevas y de entregas a 24 h y 2 h.
- 🔐 Login guiado por Telegram, con la opción de guardar la contraseña cifrada o mantenerla solo en memoria.
- 🧠 Usa la **API interna de Blackboard Ultra**, no scraping visual: menos frágil y más rápido.

## Cómo funciona

1. El bot abre un Chrome real con Playwright y navega al login SAML de SENATI.
2. Microsoft valida tu correo y contraseña (sin 2FA).
3. Blackboard devuelve la sesión; el bot guarda **solo las cookies**, cifradas.
4. Con esas cookies consulta:
   - `/learn/api/v1/users/me/memberships` → cursos.
   - `/learn/api/v1/calendars/calendarItems?since=…&until=…` → tareas y eventos.
5. Los elementos `GradableItem` con `attemptable` o `handle` son entregas; `startDate`/`endDate` son las fechas.

No hace falta que un administrador registre una aplicación: se reutiliza la sesión web normal.

---

## Requisitos

- Windows 10/11
- Python 3.11 o superior (probado con 3.14)
- Google Chrome o Microsoft Edge instalado
- Conexión a internet

## Instalación

### 1. Crea tu bot en Telegram

1. Abre Telegram y busca **@BotFather**.
2. Envía `/newbot`.
3. Elige un nombre (ej. *Mis Tareas SENATI*) y un usuario que termine en `bot` (ej. `misterias_senati_bot`).
4. Copia el **token** que te devuelve (parecido a `123456789:AAH...`).

> 🔒 El token es la llave total del bot. No lo compartas ni lo subas a GitHub. Si se filtra, usa `/revoke` en BotFather.

### 2. Ejecuta el instalador

Doble clic en **`run.bat`**.

La primera vez:
- crea un entorno virtual `.venv`,
- instala las dependencias,
- copia `.env.example` a `.env` y lo abre en Notepad.

Pega tu token en la línea `BOT_TOKEN=` y guarda el archivo. Vuelve a ejecutar `run.bat`.

### 3. Conecta tu cuenta

En el chat con tu bot:

```
/start
/login
```

1. Escribe tu correo institucional (ej. `nombre.apellido@senati.pe`).
2. Escribe tu contraseña de Microsoft. El bot intentará borrar ese mensaje.
3. Elige:
   - **🔐 Guardar cifrada** — el bot puede renovar la sesión solo.
   - **⏳ Solo esta sesión** — no se guarda en disco; si reinicias el bot y la sesión caduca, tendrás que usar `/login` otra vez.

Se abrirá una ventana de Chrome para completar el login. Cuando termine, verás tus cursos y tus próximas tareas.

---

## Comandos

| Comando | Qué hace |
|---|---|
| `/tareas` | Próximos 14 días |
| `/tareas 30` | Próximos 30 días (máx. 112) |
| `/hoy` | Entregas de hoy |
| `/semana` | Próximos 7 días |
| `/mes` | Próximos 31 días |
| `/eventos` | Clases y eventos del calendario |
| `/cursos` | Cursos en los que estás inscrito |
| `/login` | Conectar o renovar la cuenta |
| `/estado` | Sesión, credenciales y revisión automática |
| `/refrescar` | Forzar actualización |
| `/logout` | Borrar credenciales, sesión y recordatorios |
| `/ayuda` | Menú de ayuda |
| `/debug` | Diagnóstico de endpoints (para problemas) |

---

## Seguridad

- **Todo se ejecuta en tu PC.** No hay servidor intermedio.
- La contraseña solo se envía a **Microsoft** (`login.microsoftonline.com`) durante el login.
- Si eliges guardarla:
  - se cifra con **Fernet**,
  - la clave se protege con **DPAPI** de Windows, así que solo tu usuario de Windows en ese equipo puede descifrarla,
  - se guarda en `%LOCALAPPDATA%\BlackboardBot\bot.db`.
- Las cookies de sesión también se guardan cifradas (`session.enc`).
- El mensaje con la contraseña se borra del chat, pero **Telegram puede conservarlo en la nube o en copias de seguridad**. Si no quieres que pase por Telegram, usa la alternativa de abajo.
- `/logout` borra contraseña, cookies y recordatorios.

### Alternativa sin pasar la contraseña por Telegram

En `.env`:

```env
BB_EMAIL=nombre.apellido@senati.pe
BB_PASSWORD=tu_contrasena
```

Quedan en texto plano en tu equipo (nunca se envían a Telegram) y el bot las usa para iniciar sesión cuando haga falta. `/logout` no las borra: elimina esas líneas del `.env`.

### Datos guardados

Carpeta por defecto: `%LOCALAPPDATA%\BlackboardBot`

| Archivo | Contenido |
|---|---|
| `bot.db` | usuario, contraseña cifrada, tareas vistas, recordatorios |
| `session.enc` | cookies de Blackboard cifradas |
| `secret.key` | clave Fernet protegida con DPAPI |
| `bot.log` | registro local (sin contraseñas) |
| `debug/` | capturas de errores de login |

Para borrar todo: `/logout` y luego elimina la carpeta `%LOCALAPPDATA%\BlackboardBot`.

---

## Problemas comunes

| Síntoma | Solución |
|---|---|
| `Falta BOT_TOKEN en .env` | Pega el token de BotFather en `.env` |
| No se abre el navegador | Deja `BB_HEADLESS=0` e instala Chrome. Alternativa: `.venv\Scripts\playwright install chromium` |
| Microsoft rechaza el login | Revisa correo/contraseña. Si tu cuenta tiene MFA, el bot no puede completarlo solo |
| `No apareció el formulario de correo` | Ejecuta `/debug`, revisa `%LOCALAPPDATA%\BlackboardBot\debug\login-error.png` |
| La sesión expira seguido | Elige **Guardar cifrada** al hacer `/login` |
| `No pude leer tus tareas` | Usa `/debug` y revisa los códigos HTTP. Puede que Blackboard haya cambiado sus endpoints |
| El bot no avisa solo | Debe estar ejecutándose (`run.bat`) y `BB_CHECK_INTERVAL_MIN` > 0 |

El log completo está en `%LOCALAPPDATA%\BlackboardBot\bot.log`.

---

## Límites conocidos

- Solo muestra **fechas de entrega**; no indica si ya entregaste (eso requeriría el libro de calificaciones).
- Las columnas de calificación manual (por ejemplo, *Actitudes* o *Participación*) no se muestran como entregas porque no requieren que envíes nada.
- Solo funciona con cuentas **sin MFA**. Con MFA habría que completar el segundo factor a mano.
- El login depende de la página de Microsoft y del SAML de SENATI; si cambian, habrá que ajustar los selectores en `bb_client.py`.
- Ventana máxima de la API de calendario: 16 semanas.
- El bot debe estar encendido en tu PC para los avisos automáticos.

## Estructura

```
blackboard-bot/
├─ bot.py            # comandos, conversación de login y avisos
├─ bb_client.py      # login SAML (Playwright) + API interna de Ultra
├─ formatting.py     # mensajes, fechas y troceado para Telegram
├─ storage.py        # SQLite: usuario, vistos y recordatorios
├─ crypto_store.py   # Fernet + DPAPI
├─ config.py         # lectura de .env
├─ selftest.py       # pruebas locales sin red
├─ requirements.txt
├─ .env.example
└─ run.bat
```
