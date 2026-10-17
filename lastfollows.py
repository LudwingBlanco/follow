#!/usr/bin/env python3
"""
lastfollows — Últimos follows de una cuenta pública de Instagram.

Funciones:
  * Ver los últimos follows de cualquier cuenta PÚBLICA (no hace falta que sea tuya).
  * Modo bot: vigilar la cuenta y AVISAR cuando siga a alguien nuevo (consola y Telegram).
  * REGISTRO histórico en base de datos local (SQLite) de todos los follows detectados.
  * `buscar`: intenta recuperar, desde la base de datos pública de archive.org (Wayback
    Machine), copias del perfil de los últimos días para estimar cambios de follows
    en días en los que la herramienta no estaba corriendo (best-effort).

Uso:
    python lastfollows.py login-cookie             # guardar cookie sessionid (cualquier cuenta)
    python lastfollows.py login                     # iniciar sesión con usuario/contraseña
    python lastfollows.py ver @cuenta [-n 20]       # ver los últimos N follows de una cuenta
    python lastfollows.py vigilar @cuenta           # vigilar la cuenta y avisar de follows nuevos
    python lastfollows.py historial @cuenta -d 7    # registro de los últimos 7 días
    python lastfollows.py buscar @cuenta -d 30      # buscar copias públicas (archive.org)

`ver`, `vigilar` y `historial` de una cuenta pública no requieren sesión para
consultar el registro local; la sesión solo se necesita para leer Instagram.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from getpass import getpass
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from instagrapi import Client
from instagrapi.exceptions import ClientLoginRequired, ClientThrottledError

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
DB_PATH = BASE_DIR / "historial.db"
LEGACY_DIR = BASE_DIR / "data"
USER_AGENT = (
    "Instagram 155.0.0.37.107 Android (33/13; 420dpi; 1080x2400; "
    "samsung; SM-G991B; o1s; exynos2100; en_US; 239490569)"
)


# ---------------------------------------------------------------- utilidades

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def send_telegram(text: str, token: str, chat_id: str) -> bool:
    if not token or not chat_id:
        return False
    try:
        import requests

        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        return r.ok
    except Exception as e:
        log(f"  (aviso de Telegram falló: {e})")
        return False


# ---------------------------------------------------------------- base de datos local

def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS follows(
            target    TEXT NOT NULL,
            pk        TEXT NOT NULL,
            username  TEXT,
            full_name TEXT,
            first_seen TEXT,
            last_seen  TEXT,
            active    INTEGER DEFAULT 1,
            PRIMARY KEY (target, pk)
        );
        CREATE TABLE IF NOT EXISTS events(
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT,
            tipo  TEXT,          -- nuevo | dejo | volvio
            username TEXT,
            full_name TEXT,
            fecha TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_target_fecha ON events(target, fecha);
        CREATE TABLE IF NOT EXISTS meta(
            target  TEXT PRIMARY KEY,
            covered INTEGER DEFAULT 0
        );
        """
    )
    # Migración: columna 'pos' (posición en la lista ordenada por fecha)
    cols = {r[1] for r in con.execute("PRAGMA table_info(follows)")}
    if "pos" not in cols:
        con.execute("ALTER TABLE follows ADD COLUMN pos INTEGER")
        con.commit()
    return con


def import_legacy_snapshot(target: str) -> None:
    """Migra el snapshot JSON de versiones anteriores (data/<user>_following.json) a la BD."""
    path = LEGACY_DIR / f"{target.lower()}_following.json"
    if not path.exists():
        return
    con = db_connect()
    try:
        has_rows = con.execute("SELECT 1 FROM follows WHERE target=? LIMIT 1", (target,)).fetchone()
        if has_rows:
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = _now()
        for u in data.get("users", []):
            con.execute(
                "INSERT OR IGNORE INTO follows(target,pk,username,full_name,first_seen,last_seen,active)"
                " VALUES(?,?,?,?,?,?,1)",
                (target, str(u.get("pk")), u.get("username", ""), u.get("full_name", ""), ts, ts),
            )
        con.commit()
        log(f"[OK] Snapshot anterior importado a la base de datos local ({len(data.get('users', []))} cuentas).")
    finally:
        con.close()


def has_baseline(target: str) -> bool:
    con = db_connect()
    try:
        return bool(con.execute("SELECT 1 FROM follows WHERE target=? LIMIT 1", (target,)).fetchone())
    finally:
        con.close()


def record_cycle(target: str, users: list, baseline: bool = False,
                 ordered: bool = True, complete: bool = True):
    """Guarda un ciclo de following en la BD.

    Evita falsos positivos:
    - `covered` (en meta) es la ventana de posiciones que hemos visto completas y
      ordenadas por fecha; solo se generan eventos dentro de esa ventana.
    - Si la lista viene desordenada (fallback) o incompleta, no se generan eventos.
    Devuelve (nuevos, dejados).
    """
    now = _now()
    new, gone = [], []
    con = db_connect()
    try:
        cur = con.cursor()
        row = cur.execute("SELECT covered FROM meta WHERE target=?", (target,)).fetchone()
        if row is None:
            covered = cur.execute(
                "SELECT COUNT(*) FROM follows WHERE target=? AND active=1", (target,)
            ).fetchone()[0]
        else:
            covered = row[0] or 0
        rows = cur.execute(
            "SELECT pk, username, full_name, active, pos FROM follows WHERE target=?", (target,)
        ).fetchall()
        known = {r[0]: r for r in rows}
        current_pks = set()
        entraron = 0  # cuentas nuevas que entran a la lista (desplazan a las demás hacia abajo)

        for i, u in enumerate(users, 1):
            pk = str(u.pk)
            uname = u.username or ""
            fname = (u.full_name or "").strip()
            current_pks.add(pk)
            if pk in known:
                was_active = known[pk][3]
                if ordered:
                    cur.execute(
                        "UPDATE follows SET username=?, full_name=?, last_seen=?, active=1, pos=?"
                        " WHERE target=? AND pk=?",
                        (uname, fname, now, i, target, pk),
                    )
                else:
                    cur.execute(
                        "UPDATE follows SET username=?, full_name=?, last_seen=?, active=1"
                        " WHERE target=? AND pk=?",
                        (uname, fname, now, target, pk),
                    )
                if not baseline and ordered and not was_active and i <= covered:
                    cur.execute(
                        "INSERT INTO events(target,tipo,username,full_name,fecha) VALUES(?,?,?,?,?)",
                        (target, "volvio", uname, fname, now),
                    )
                    new.append(("volvio", uname, fname))
            else:
                entraron += 1
                if ordered:
                    cur.execute(
                        "INSERT INTO follows(target,pk,username,full_name,first_seen,last_seen,active,pos)"
                        " VALUES(?,?,?,?,?,?,1,?)",
                        (target, pk, uname, fname, now, now, i),
                    )
                else:
                    cur.execute(
                        "INSERT INTO follows(target,pk,username,full_name,first_seen,last_seen,active,pos)"
                        " VALUES(?,?,?,?,?,?,1,NULL)",
                        (target, pk, uname, fname, now, now),
                    )
                if not baseline and ordered and i <= covered:
                    cur.execute(
                        "INSERT INTO events(target,tipo,username,full_name,fecha) VALUES(?,?,?,?,?)",
                        (target, "nuevo", uname, fname, now),
                    )
                    new.append(("nuevo", uname, fname))

        if not baseline and ordered and complete:
            # 'entraron' empuja a las demás hacia abajo: si una cuenta cae fuera de la
            # ventana solo por ese empujón (pos + entraron > len(users)), NO es unfollow.
            for pk, uname, fname, _act, pos in rows:
                if pk in current_pks:
                    continue
                if pos is None or pos + entraron > len(users):
                    continue  # quedó fuera de la ventana por el desplazamiento, no por unfollow
                cur.execute(
                    "UPDATE follows SET active=0, last_seen=? WHERE target=? AND pk=?",
                    (now, target, pk),
                )
                cur.execute(
                    "INSERT INTO events(target,tipo,username,full_name,fecha) VALUES(?,?,?,?,?)",
                    (target, "dejo", uname, fname, now),
                )
                gone.append((uname, fname))

        new_covered = max(covered, len(users)) if (ordered and complete) else covered
        cur.execute(
            "INSERT INTO meta(target, covered) VALUES(?,?)"
            " ON CONFLICT(target) DO UPDATE SET covered=excluded.covered",
            (target, new_covered),
        )
        con.commit()
    finally:
        con.close()
    return new, gone


def get_events(target: str, days: int, limit: int) -> list:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    con = db_connect()
    try:
        return con.execute(
            "SELECT tipo, username, full_name, fecha FROM events"
            " WHERE target=? AND fecha>=? ORDER BY id DESC LIMIT ?",
            (target, cutoff, limit),
        ).fetchall()
    finally:
        con.close()


def get_summary(target: str, days: int) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    con = db_connect()
    try:
        rows = con.execute(
            "SELECT tipo, COUNT(*) FROM events WHERE target=? AND fecha>=? GROUP BY tipo",
            (target, cutoff),
        ).fetchall()
        total = con.execute(
            "SELECT COUNT(*) FROM follows WHERE target=? AND active=1", (target,)
        ).fetchone()[0]
        return {"counts": dict(rows), "activos": total}
    finally:
        con.close()


# ---------------------------------------------------------------- sesión

def make_client() -> Client:
    """Crea un cliente reutilizando la sesión guardada (sessionid) si existe."""
    cl = Client()
    cl.set_user_agent(USER_AGENT)

    if SESSION_FILE.exists():
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        sessionid = data.get("sessionid", "")
        if sessionid:
            try:
                if cl.login_by_sessionid(sessionid):
                    log(f"[OK] Sesión restaurada ({data.get('username', '?')})\n")
                    return cl
            except Exception:
                pass
            log("[!] La sesión guardada expiró. Ejecuta:  python lastfollows.py login-cookie\n")
        raise SystemExit(2)

    log("[!] No hay sesión guardada. Ejecuta primero:  python lastfollows.py login-cookie")
    log("    (solo hace falta la cookie sessionid de cualquier cuenta descartable)")
    raise SystemExit(2)


def cmd_login(_args) -> None:
    username = input("Tu usuario de Instagram: ").strip()
    password = getpass("Tu contraseña: ")

    cl = Client()
    cl.set_user_agent(USER_AGENT)

    def ask_2fa() -> str:
        return input("Código de verificación (2FA): ").strip()

    try:
        cl.login(username, password, verification_code=ask_2fa)
    except Exception as e:
        log(f"\n[ERROR] No se pudo iniciar sesión: {e}")
        log("Posibles causas: contraseña incorrecta, verificación en dos pasos pendiente,")
        log("o Instagram bloqueó el inicio de sesión desde esta IP. Intenta desde otra red")
        log("o vuelve a intentarlo en unos minutos.")
        raise SystemExit(1)

    settings = cl.get_settings()
    sessionid = (settings.get("cookies") or {}).get("sessionid", "")
    if not sessionid:
        log("[ERROR] Login ok pero no se pudo extraer la cookie sessionid.")
        raise SystemExit(1)

    SESSION_FILE.write_text(
        json.dumps({"username": username, "sessionid": sessionid}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"\n[OK] Sesión guardada en {SESSION_FILE.name}. Ya no hace falta volver a loguearte.")


def cmd_login_cookie(_args) -> None:
    """Guarda solo la cookie sessionid del navegador (de cualquier cuenta, incluso descartable)."""
    log("Pega la cookie 'sessionid' de tu navegador (no hace falta contraseña):")
    log("  1. Abre instagram.com en el navegador e inicia sesión con CUALQUIER cuenta")
    log("     (puede ser una cuenta descartable, no tiene que ser la tuya).")
    log("  2. F12 → pestaña Application (Aplicaciones) → Cookies → https://www.instagram.com")
    log("  3. Copia el valor de la cookie 'sessionid' y pégalo aquí.")
    sessionid = input("\nsessionid: ").strip()
    if not sessionid:
        log("[ERROR] sessionid vacío.")
        raise SystemExit(1)

    cl = Client()
    cl.set_user_agent(USER_AGENT)
    try:
        if not cl.login_by_sessionid(sessionid):
            raise RuntimeError("login_by_sessionid devolvió False")
    except Exception as e:
        log(f"[ERROR] Esa sessionid no es válida o ya expiró: {e}")
        raise SystemExit(1)

    me = cl.account_info()
    SESSION_FILE.write_text(
        json.dumps({"username": getattr(me, "username", ""), "sessionid": sessionid}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"\n[OK] Sesión guardada como @{getattr(me, 'username', '?')} (solo lectura de cuentas públicas).")


# ---------------------------------------------------------------- consulta a Instagram

def get_target(cl: Client, username: str):
    username = username.lstrip("@")
    try:
        info = cl.user_info_by_username(username)
    except Exception as e:
        log(f"[ERROR] No se pudo cargar @{username}: {e}")
        raise SystemExit(1)
    if info.is_private:
        log(f"[AVISO] @{username} es una cuenta PRIVADA: su lista de following no es visible.")
        log("Solo funciona con cuentas públicas (o si la cuenta de la sesión sigue a esa cuenta privada).")
    return info


def fetch_latest_following(cl: Client, user_id: int, amount: int, quiet: bool = False):
    """Devuelve (users, ordered): los 'amount' follows más recientes y si la lista
    viene con garantía de orden por fecha de follow."""
    try:
        return cl.user_following_private_gql(str(user_id), amount=amount, order="date_followed_latest"), True
    except Exception as e:
        if not quiet:
            log(f"[AVISO] Endpoint GraphQL con orden por fecha falló ({e.__class__.__name__}); usando endpoint clásico (sin orden garantizado: no se generarán eventos).")
        try:
            return cl.user_following(str(user_id), amount=amount), False
        except Exception as e2:
            log(f"[ERROR] No se pudo obtener la lista de following: {e2}")
            return [], False


def fmt_user(i: int, u) -> str:
    verified = " ✓" if getattr(u, "is_verified", False) else ""
    full = (getattr(u, "full_name", "") or "").strip()
    return f" {i:>3}. @{getattr(u, 'username', '?')}{verified} — {full}"


# ---------------------------------------------------------------- comandos

def cmd_ver(args) -> None:
    target = args.cuenta.lstrip("@")
    cl = make_client()
    info = get_target(cl, target)

    log(f"Últimos {args.cantidad} follows de @{info.username} "
        f"({info.full_name or 'sin nombre'}) — {info.following_count:,} siguiendo:\n")

    users, ordered = fetch_latest_following(cl, info.pk, args.cantidad)
    if not users:
        log("No se obtuvieron resultados.")
        return

    baseline = not has_baseline(info.username)
    complete = len(users) >= min(args.cantidad, info.following_count or args.cantidad)
    new, _ = record_cycle(info.username, users, baseline=baseline, ordered=ordered, complete=complete)

    for i, u in enumerate(users, 1):
        log(fmt_user(i, u))
        log(f"      https://instagram.com/{u.username}")

    log(f"\nTotal mostrados: {len(users)}")
    if baseline:
        log("[OK] Lista base guardada en el registro local; a partir de ahora se detectarán cambios.")
    elif new:
        log(f"[¡!] Se detectaron {len(new)} follow(s) nuevo(s) desde la última consulta:")
        for tipo, uname, fname in new:
            log(f"   + @{uname} — {fname}")


def cmd_vigilar(args) -> None:
    target = args.cuenta.lstrip("@")
    cl = make_client()
    info = get_target(cl, target)

    tg_token = args.tg_token or os.environ.get("IG_LF_TG_TOKEN", "")
    tg_chat = args.tg_chat or os.environ.get("IG_LF_TG_CHAT", "")

    import_legacy_snapshot(info.username)
    baseline = not has_baseline(info.username)

    log(f"Vigilando follows de @{info.username} cada {args.intervalo}s. Ctrl+C para parar.")
    log(f"Registro local: {DB_PATH.name} (consulta con: python lastfollows.py historial @{info.username})")
    if baseline:
        log("[1ª ejecución] Guardando lista base; las detecciones empiezan a la próxima pasada.\n")

    while True:
        try:
            users, ordered = fetch_latest_following(cl, info.pk, args.cantidad)
        except ClientLoginRequired:
            log("[!] Sesión expirada. Ejecuta de nuevo: python lastfollows.py login-cookie")
            raise SystemExit(1)
        except ClientThrottledError:
            log(f"[!] Límite de peticiones alcanzado. Esperando {args.intervalo * 2}s…")
            time.sleep(args.intervalo)
            continue
        except Exception as e:
            log(f"[ERROR] {e.__class__.__name__}: {e}")
            time.sleep(args.intervalo)
            continue

        complete = len(users) >= min(args.cantidad, info.following_count or args.cantidad)
        new, gone = record_cycle(info.username, users, baseline=baseline, ordered=ordered, complete=complete)
        stamp = _now()

        if baseline:
            log(f"[{stamp}] Base guardada: {len(users)} cuentas vigiladas.")
            baseline = False
        elif not ordered:
            log(f"[{stamp}] @{info.username}: {len(users)} cuentas (lista sin orden fiable; solo actualizo el registro, sin eventos)")
        elif new or gone:
            log(f"[{stamp}] @{info.username}: {len(new)} follow(s) nuevo(s), {len(gone)} dejado(s) de seguir")
            for tipo, uname, fname in new:
                etiqueta = "SEGUIDO AHORA" if tipo == "nuevo" else "VOLVIÓ A SEGUIR"
                log(f"   + {etiqueta}: @{uname} — {fname}")
                send_telegram(
                    f"🔔 @{info.username} acaba de seguir a @{uname} ({fname or 'sin nombre'})\n"
                    f"https://instagram.com/{uname}",
                    tg_token, tg_chat,
                )
            for uname, fname in gone:
                log(f"   - Dejó de seguir: @{uname} — {fname}")
        else:
            log(f"[{stamp}] @{info.username}: sin cambios (vigilando {len(users)} cuentas)")

        time.sleep(args.intervalo)


def cmd_historial(args) -> None:
    target = args.cuenta.lstrip("@").lower()
    import_legacy_snapshot(target)

    if not has_baseline(target):
        log(f"[!] No hay registro local de @{target} todavía.")
        log("    Ejecuta al menos una vez:  python lastfollows.py vigilar @" + target)
        return

    events = get_events(target, args.dias, args.limite)
    summary = get_summary(target, args.dias)

    log(f"Registro de @{target} — últimos {args.dias} día(s):\n")
    if not events:
        log("  Sin follows nuevos registrados en ese periodo.")
    for tipo, uname, fname, fecha in events:
        marca = {"nuevo": "＋ SEGUIDO", "dejo": "－ DEJÓ DE SEGUIR", "volvio": "＋ VOLVIÓ"}.get(tipo, tipo)
        log(f"  {fecha}  [{marca}] @{uname} — {fname}")

    counts = summary["counts"]
    log("\nResumen del periodo:")
    log(f"  nuevos follows : {counts.get('nuevo', 0)}")
    log(f"  dejó de seguir : {counts.get('dejo', 0)}")
    log(f"  volvió         : {counts.get('volvio', 0)}")
    log(f"  total vigilado : {summary['activos']} cuentas en la lista")


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lastfollows/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def cmd_buscar(args) -> None:
    """Best-effort: busca copias públicas del perfil en archive.org (Wayback Machine)."""
    user = args.cuenta.lstrip("@")
    since = (datetime.now() - timedelta(days=args.dias)).strftime("%Y%m%d")
    until = datetime.now().strftime("%Y%m%d")

    log(f"Buscando copias públicas de @{user} en archive.org (últimos {args.dias} días)…\n")

    cdx = (
        "https://web.archive.org/cdx/search/cdx"
        f"?url=instagram.com/{user}&matchType=prefix&output=json"
        "&fl=timestamp,original,statuscode&filter=statuscode:200"
        f"&collapse=timestamp:8&from={since}&to={until}&limit=200"
    )
    try:
        data = json.loads(_http_get(cdx).decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"[ERROR] No se pudo consultar archive.org: {e}")
        return
    if not data or len(data) < 2:
        log("No hay copias públicas de esa cuenta en archive.org en ese periodo.")
        log("Instagram bloquea el acceso sin login, así que casi nunca archivan la lista de")
        log("following. La forma fiable de tener registro es dejar corriendo el modo 'vigilar'.")
        return

    rows = data[1:]
    profile_re = re.compile(rf"^https?://(www\.)?instagram\.com/{re.escape(user)}/?(#\??.*)?$", re.I)
    perfiles, following_pages = [], []
    for ts, original, _status in rows:
        if "/following" in original:
            following_pages.append((ts, original))
        elif profile_re.match(original):
            perfiles.append((ts, original))

    log(f"Copias encontradas: {len(perfiles)} del perfil, {len(following_pages)} de la lista following.")
    if following_pages:
        log("\n★ Hay copias de la LISTA DE FOLLOWING (las más valiosas):")
        for ts, original in following_pages[:20]:
            fecha = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
            log(f"  {fecha}  https://web.archive.org/web/{ts}/{original}")

    if not perfiles:
        log("\nNo se encontraron copias del perfil para estimar la evolución.")
        return

    count_re = re.compile(r'"edge_follow":\{"count":(\d+)|"follows_count":(\d+)')
    dias = {}
    for ts, _original in perfiles[: args.max]:
        try:
            html = _http_get(f"https://web.archive.org/web/{ts}id_/https://www.instagram.com/{user}/").decode("utf-8", errors="replace")
        except Exception:
            continue
        m = count_re.search(html)
        if not m:
            continue
        count = int(m.group(1) or m.group(2))
        fecha = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        dias.setdefault(fecha, count)  # primera copia del día gana

    if not dias:
        log("\nLas copias del perfil no contienen la cifra de 'siguiendo' (Instagram pone muro de")
        log("login desde 2022 y esas páginas archivadas quedaron vacías).")
        return

    log("\nEvolución de la cifra 'siguiendo' según archive.org (estimación):")
    prev = None
    for fecha in sorted(dias):
        count = dias[fecha]
        delta = "" if prev is None else (f"   (+{count - prev} ese día)" if count > prev
                                         else f"   ({count - prev} ese día)" if count < prev else "   (sin cambio)")
        log(f"  {fecha}  siguiendo: {count:,}{delta}")
        prev = count
    log("\nNOTA: es una estimación a partir de copias públicas; puede haber días sin copia.")


# ---------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ver, vigilar y registrar los últimos follows de una cuenta pública de Instagram."
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    sub.add_parser("login", help="Iniciar sesión con usuario/contraseña y guardar la sesión")
    sub.add_parser("login-cookie", help="Guardar solo la cookie sessionid del navegador (sin contraseña)")

    p_ver = sub.add_parser("ver", help="Ver los últimos follows de una cuenta")
    p_ver.add_argument("cuenta", help="Usuario de Instagram, con o sin @")
    p_ver.add_argument("-n", "--cantidad", type=int, default=20, help="Cuántos mostrar (default 20)")

    p_vig = sub.add_parser("vigilar", help="Vigilar una cuenta, avisar de follows nuevos y registrarlos")
    p_vig.add_argument("cuenta", help="Usuario de Instagram, con o sin @")
    p_vig.add_argument("-n", "--cantidad", type=int, default=100, help="Cuántos follows vigilar (default 100)")
    p_vig.add_argument("--intervalo", type=int, default=300, help="Segundos entre comprobaciones (default 300)")
    p_vig.add_argument("--tg-token", default="", help="Token de bot de Telegram (o var IG_LF_TG_TOKEN)")
    p_vig.add_argument("--tg-chat", default="", help="Chat ID de Telegram (o var IG_LF_TG_CHAT)")

    p_his = sub.add_parser("historial", help="Registro local de follows detectados (no necesita sesión)")
    p_his.add_argument("cuenta", help="Usuario de Instagram, con o sin @")
    p_his.add_argument("-d", "--dias", type=int, default=7, help="Días hacia atrás (default 7)")
    p_his.add_argument("-n", "--limite", type=int, default=100, help="Máximo de eventos (default 100)")

    p_bus = sub.add_parser("buscar", help="Buscar copias públicas del perfil en archive.org (no necesita sesión)")
    p_bus.add_argument("cuenta", help="Usuario de Instagram, con o sin @")
    p_bus.add_argument("-d", "--dias", type=int, default=30, help="Días hacia atrás (default 30)")
    p_bus.add_argument("--max", type=int, default=12, help="Máximo de copias a analizar (default 12)")

    args = parser.parse_args()
    if args.comando == "login":
        cmd_login(args)
    elif args.comando == "login-cookie":
        cmd_login_cookie(args)
    elif args.comando == "ver":
        cmd_ver(args)
    elif args.comando == "vigilar":
        cmd_vigilar(args)
    elif args.comando == "historial":
        cmd_historial(args)
    elif args.comando == "buscar":
        cmd_buscar(args)


if __name__ == "__main__":
    main()
