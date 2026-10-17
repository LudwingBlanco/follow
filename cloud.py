#!/usr/bin/env python3
"""
Comprobación única para GitHub Actions (modo nube).

Hace UNA pasada de vigilancia de la cuenta de config.json:
  - recibe la sessionid desde el secret IG_SESSIONID (nunca se guarda en disco)
  - detecta follows nuevos / dejados de seguir
  - avisa por Telegram
  - si RUN_KIND=daily, envía también el resumen de las últimas 24h
  - si la sesión expiró, avisa por Telegram para que actualices el secret
"""

import json
import os
import sys
from pathlib import Path

import lastfollows as lf
from instagrapi import Client

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
RUN_KIND = os.environ.get("RUN_KIND", "")
PLACEHOLDERS = {"", "cuenta_publica", "PEGA_AQUI_LA_CUENTA"}


def tg(text: str) -> None:
    print(text)
    lf.send_telegram(text, TG_TOKEN, TG_CHAT)


def main() -> None:
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    target = str(cfg.get("target", "")).strip().lstrip("@")

    sessionid = os.environ.get("IG_SESSIONID", "").strip()
    if not sessionid:
        tg("⚠️ lastfollows: falta el secret IG_SESSIONID en el repo de GitHub "
           "(Settings → Secrets and variables → Actions).")
        raise SystemExit(1)
    if target in PLACEHOLDERS:
        tg("⚠️ lastfollows: todavía no has puesto la cuenta a vigilar en config.json (campo 'target').")
        raise SystemExit(1)

    cl = Client()
    cl.set_user_agent(lf.USER_AGENT)
    try:
        ok = cl.login_by_sessionid(sessionid)
    except Exception as e:
        print(f"login_by_sessionid falló: {e}")
        ok = False
    if not ok:
        tg("🔐 lastfollows: la sesión expiró o no es válida. Copia una sessionid nueva del "
           "navegador y actualiza el secret IG_SESSIONID (Settings → Secrets → IG_SESSIONID → Update).")
        raise SystemExit(1)

    try:
        info = cl.user_info_by_username(target)
    except Exception as e:
        tg(f"⚠️ lastfollows: no pude cargar @{target}: {e}")
        raise SystemExit(1)
    if info.is_private:
        tg(f"⚠️ @{target} es una cuenta privada: su lista de following no es visible.")
        raise SystemExit(1)

    users, ordered = lf.fetch_latest_following(cl, info.pk, int(cfg.get("cantidad", 100)))
    if not users:
        tg(f"⚠️ lastfollows: Instagram no devolvió following de @{target} "
           f"(posible bloqueo temporal; lo reintento en la próxima pasada).")
        raise SystemExit(1)

    baseline = not lf.has_baseline(info.username)
    complete = len(users) >= min(int(cfg.get("cantidad", 100)), info.following_count or len(users))
    new, gone = lf.record_cycle(info.username, users, baseline=baseline, ordered=ordered, complete=complete)

    if baseline:
        tg(f"✅ lastfollows: lista base de @{info.username} guardada "
           f"({len(users)} cuentas). A partir de ahora te aviso de cada follow nuevo.")
    elif new or gone:
        lineas = [f"🔔 @{info.username} — {datetime_str()}"]
        for tipo, uname, fname in new:
            etiqueta = "SEGUIDO AHORA" if tipo == "nuevo" else "VOLVIÓ A SEGUIR"
            lineas.append(f"＋ {etiqueta}: @{uname} ({fname or 'sin nombre'})\n   https://instagram.com/{uname}")
        for uname, fname in gone:
            lineas.append(f"－ Dejó de seguir: @{uname}")
        tg("\n".join(lineas))

    if RUN_KIND == "daily":
        s = lf.get_summary(info.username, 1)
        c = s["counts"]
        tg(f"📋 Resumen diario @{info.username} (últimas 24h):\n"
           f"＋ nuevos follows: {c.get('nuevo', 0)}\n"
           f"－ dejó de seguir: {c.get('dejo', 0)}\n"
           f"vigilando: {s['activos']} cuentas")


def datetime_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    main()
