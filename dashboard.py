#!/usr/bin/env python3
"""
Genera el panel web estático (site/index.html) a partir del registro local.
Lo publica GitHub Pages desde el workflow; no necesita sesión de Instagram.
"""

import html
import json
from datetime import datetime
from pathlib import Path

import lastfollows as lf

PLACEHOLDERS = {"", "cuenta_publica", "PEGA_AQUI_LA_CUENTA"}
TIPOS = {
    "nuevo": ("＋ SEGUIDO", "ok"),
    "dejo": ("－ DEJÓ DE SEGUIR", "bad"),
    "volvio": ("＋ VOLVIÓ", "warn"),
}


def main() -> None:
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    target = str(cfg.get("target", "")).strip().lstrip("@")
    dias = int(cfg.get("dashboard_dias", 30))

    if target in PLACEHOLDERS or not lf.has_baseline(target):
        eventos, resumen = [], {"counts": {}, "activos": 0}
        aviso = "Todavía no hay datos: configura <b>target</b> en config.json y deja corriendo el bot."
    else:
        eventos = lf.get_events(target, dias, 500)
        resumen = lf.get_summary(target, dias)
        aviso = ""

    c = resumen["counts"]
    filas = "".join(
        f'<tr class="{cls}"><td class="fecha">{html.escape(fecha)}</td>'
        f'<td>{html.escape(etiqueta)}</td>'
        f'<td><a href="https://instagram.com/{html.escape(uname)}" target="_blank">@{html.escape(uname)}</a></td>'
        f"<td>{html.escape(nombre)}</td></tr>"
        for fecha, etiqueta, cls, uname, nombre in (
            (f, TIPOS.get(t, (t, ""))[0], TIPOS.get(t, ("", ""))[1], u or "-", n or "")
            for t, u, n, f in eventos
        )
    ) or '<tr><td colspan="4" class="vacio">Sin eventos registrados en este periodo.</td></tr>'

    generado = datetime.now().strftime("%Y-%m-%d %H:%M")
    page = f"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>lastfollows — @{html.escape(target or "sin config")}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: system-ui, sans-serif; background:#0f1115; color:#e6e6e6; margin:0; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:24px 16px; }}
  h1 {{ font-size:1.3rem; }} h1 span {{ color:#8ab4ff; }}
  .sub {{ color:#9aa0a6; font-size:.85rem; margin-bottom:20px; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:24px; }}
  .card {{ background:#171a21; border:1px solid #262b36; border-radius:10px; padding:12px 16px; min-width:120px; }}
  .card b {{ display:block; font-size:1.4rem; }}
  .card small {{ color:#9aa0a6; }}
  table {{ width:100%; border-collapse:collapse; background:#171a21; border-radius:10px; overflow:hidden; }}
  th, td {{ text-align:left; padding:8px 12px; border-bottom:1px solid #262b36; font-size:.9rem; }}
  th {{ background:#1d2230; color:#9aa0a6; font-weight:600; }}
  td a {{ color:#8ab4ff; text-decoration:none; }}
  .fecha {{ white-space:nowrap; color:#9aa0a6; }}
  .ok td {{ color:#7ee787; }} .bad td {{ color:#ff7b72; }} .warn td {{ color:#e3b341; }}
  .vacio {{ color:#9aa0a6; text-align:center; }}
  .aviso {{ background:#2a2011; border:1px solid #5c4a1d; color:#e3b341; padding:10px 14px; border-radius:8px; }}
</style></head>
<body><div class="wrap">
  <h1>lastfollows — <span>@{html.escape(target or "sin config")}</span></h1>
  <div class="sub">Últimos {dias} días · generado el {generado}</div>
  {"<div class='aviso'>" + aviso + "</div>" if aviso else ""}
  <div class="cards">
    <div class="card"><b>{resumen["activos"]}</b><small>cuentas vigiladas</small></div>
    <div class="card"><b>{c.get("nuevo", 0)}</b><small>nuevos follows</small></div>
    <div class="card"><b>{c.get("dejo", 0)}</b><small>dejó de seguir</small></div>
    <div class="card"><b>{c.get("volvio", 0)}</b><small>volvió</small></div>
  </div>
  <table>
    <tr><th>Fecha y hora</th><th>Evento</th><th>Cuenta</th><th>Nombre</th></tr>
    {filas}
  </table>
</div></body></html>
"""
    out = Path("site")
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"site/index.html generado ({len(eventos)} eventos)")


if __name__ == "__main__":
    main()
