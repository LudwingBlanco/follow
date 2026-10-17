# lastfollows — Últimos follows de una cuenta pública de Instagram

Herramienta en Python para **ver, vigilar y registrar** los follows de una cuenta
**pública** de Instagram (no tiene que ser la tuya; es solo lectura):

- **Avisos**: detecta cuando la cuenta sigue a alguien nuevo (consola y/o Telegram).
- **Registro histórico**: base de datos local (`historial.db`) con todos los follows
  detectados, consultable por días (`historial`).
- **Búsqueda pública** (`buscar`): intenta recuperar desde archive.org (Wayback Machine,
  una base de datos pública de páginas archivadas) copias del perfil de los últimos días
  para estimar cambios en días en los que la herramienta no estaba corriendo.

> Instagram no tiene API oficial para esto, así que usa la API interna de la app móvil
> (vía `instagrapi`). Es un método **no oficial**: úsalo con moderación, desde tu propia
> máquina, y ten en cuenta los límites de peticiones de Instagram.

## Requisitos

- Python 3.10+ (probado en Python 3.14)
- `pip install -r requirements.txt`  (instala `instagrapi`)

## Uso rápido

```powershell
cd C:\Users\Usuario\instagram-lastfollows

# 1. Sesión (una sola vez, con cualquier cuenta descartable — solo lectura)
python lastfollows.py login-cookie

# 2. Ver los últimos follows de una cuenta pública
python lastfollows.py ver "@cuenta_publica" -n 20

# 3. Modo bot: vigilar, avisar de follows nuevos y registrarlos
python lastfollows.py vigilar "@cuenta_publica"

# 4. Consultar el registro de los últimos 7 días (no necesita sesión)
python lastfollows.py historial "@cuenta_publica" -d 7

# 5. Buscar copias públicas del perfil en archive.org (no necesita sesión)
python lastfollows.py buscar "@cuenta_publica" -d 30
```

> En PowerShell encierra siempre el usuario entre comillas (`"@cuenta"`) o quita la @.

## 1. Sesión: por qué y cómo

Instagram exige tener *alguna* sesión válida para leer la lista de following de
cualquier perfil (público incluido). Esa sesión **no tiene que ser la tuya**: sirve
cualquier cuenta descartable. La herramienta solo lee; no sigue, no da likes, no publica.

**Opción A — cookie del navegador (recomendada, sin contraseña):**

```powershell
python lastfollows.py login-cookie
```

Te pide solo la cookie `sessionid` (F12 → Application → Cookies → instagram.com) de
cualquier cuenta con la que inicies sesión en el navegador.

**Opción B — usuario + contraseña:** `python lastfollows.py login`

La sesión se guarda en `session.json`; **no lo compartas**.

## 2. `ver` — últimos follows

```powershell
python lastfollows.py ver "@cuenta" -n 50
```

Lista ordenada por fecha de follow (**más recientes primero**). Cada consulta también
alimenta el registro local: si detecta follows nuevos desde la última vez, te lo avisa.

## 3. `vigilar` — avisos + registro continuo

```powershell
python lastfollows.py vigilar "@cuenta" -n 100 --intervalo 300
```

- Primera ejecución: guarda la lista base.
- Cada pasada (default cada 5 min): detecta follows nuevos, cuentas que dejó de seguir
  y volvió a seguir; los imprime en pantalla y los guarda en `historial.db`.

**Avisos por Telegram (opcional):**

1. Crea un bot con [@BotFather](https://t.me/BotFather) → copia el token.
2. Habla con tu bot una vez y obtén tu chat ID (por ejemplo con `@userinfobot`).
3. Lanza:

```powershell
$env:IG_LF_TG_TOKEN = "123456:ABC..."
$env:IG_LF_TG_CHAT  = "123456789"
python lastfollows.py vigilar "@cuenta"
```

O directamente con flags: `--tg-token "TOKEN" --tg-chat "ID"`.

## 4. `historial` — registro de los últimos días

```powershell
python lastfollows.py historial "@cuenta" -d 7     # últimos 7 días
python lastfollows.py historial "@cuenta" -d 30    # últimos 30 días
```

Muestra cada follow detectado con fecha y hora, más un resumen (nuevos, dejó de seguir,
volvió, total vigilado). No necesita sesión: consulta la base de datos local
`historial.db` (SQLite). **Limitación**: solo registra lo que ocurrió mientras la
herramienta estaba corriendo (o desde la última consulta de `ver`).

## 5. `buscar` — copias públicas (archive.org)

```powershell
python lastfollows.py buscar "@cuenta" -d 30
```

Consulta la base de datos pública del **Wayback Machine** (archive.org): busca copias
archivadas del perfil de los últimos días y de su lista de following. Si encuentra copias
de la lista following, te da los enlaces. Honestamente: **casi nunca las hay**, porque
Instagram pone muro de login desde 2022 y los rastreadores no pueden archivar esa lista;
por eso la forma fiable de tener histórico es dejar `vigilar` corriendo (o ejecutar
`ver` cada día, que también alimenta el registro).

## Notas y límites

- **Solo cuentas públicas** (o privadas que la cuenta de la sesión siga).
- No abuses del intervalo: 5–15 minutos es razonable. Intervalos muy cortos pueden
  provocar bloqueos temporales (rate limit) o deslogueo.
- `session.json` contiene tu sesión: no lo compartas.
- Si Instagram cambia sus endpoints, actualiza la librería: `pip install -U instagrapi`.

## Archivos

- `lastfollows.py` — el programa.
- `session.json` — la sesión (se crea con `login` o `login-cookie`).
- `historial.db` — base de datos local SQLite con el registro histórico de follows.
- `data/` — snapshots de versiones anteriores (se migran automáticamente a la BD).
