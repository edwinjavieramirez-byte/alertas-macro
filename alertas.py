"""
Agente de alertas macro para EUR/USD e índices USA.

Qué hace cada vez que se ejecuta (cada 5 min):
  1. Descarga el calendario económico semanal (Forex Factory, JSON gratuito).
     Lo guarda en caché para no pedirlo más de una vez cada 2 horas.
  2. Filtra eventos de ALTO impacto de USD y EUR.
  3. Si un evento empieza en los próximos 15 min y aún no se avisó -> resumen con Claude -> Telegram.
  4. Una vez al día (07:30 Madrid, lunes-viernes) envía el resumen matinal con la agenda del día.

No hace falta tocar este archivo. Todo lo configurable está en las variables de abajo
(o en los "Secrets"/"Variables" de GitHub).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------- configuración
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-4-5"

DIVISAS = [d.strip() for d in (os.getenv("DIVISAS") or "USD,EUR").split(",")]
IMPACTOS = [i.strip() for i in (os.getenv("IMPACTOS") or "High").split(",")]
MINUTOS_ANTES = int(os.getenv("MINUTOS_ANTES") or 15)
HORA_RESUMEN = os.getenv("HORA_RESUMEN") or "07:30"   # hora de Madrid
DRY_RUN = os.getenv("DRY_RUN") == "1"                 # 1 = imprime en vez de enviar

TZ = ZoneInfo("Europe/Madrid")
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_HORAS = 2
BASE = Path(__file__).parent
ESTADO_FILE = BASE / "estado.json"
CACHE_FILE = BASE / "calendario_cache.json"

BANDERA = {"USD": "🇺🇸", "EUR": "🇪🇺"}


# ----------------------------------------------------------------- utilidades
def cargar_json(path, defecto):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return defecto


def guardar_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def ahora_utc():
    return datetime.now(timezone.utc)


def esc(texto):
    """Escapa texto para el modo HTML de Telegram."""
    return str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------- calendario
def obtener_calendario():
    cache = cargar_json(CACHE_FILE, {})
    if cache.get("descargado"):
        edad = ahora_utc() - datetime.fromisoformat(cache["descargado"])
        if edad < timedelta(hours=CACHE_HORAS):
            return cache["eventos"]
    try:
        r = requests.get(FEED_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0 alertas-macro"})
        r.raise_for_status()
        eventos = r.json()
        guardar_json(CACHE_FILE, {"descargado": ahora_utc().isoformat(), "eventos": eventos})
        print(f"Calendario descargado: {len(eventos)} eventos")
        return eventos
    except Exception as e:
        print(f"AVISO: no se pudo descargar el calendario ({e}). Uso la caché.")
        return cache.get("eventos", [])


def eventos_relevantes(eventos):
    salida = []
    for ev in eventos:
        if ev.get("country") not in DIVISAS or ev.get("impact") not in IMPACTOS:
            continue
        fecha = datetime.fromisoformat(ev["date"])          # viene en hora de Nueva York
        # Forex Factory pone a medianoche (NY) los eventos sin hora fija ("Tentative"/"All Day")
        sin_hora = fecha.hour == 0 and fecha.minute == 0
        salida.append({
            "id": f'{ev["country"]}|{ev["title"]}|{ev["date"]}',
            "titulo": ev["title"],
            "divisa": ev["country"],
            "hora": fecha.astimezone(timezone.utc),
            "sin_hora": sin_hora,
            "previsto": ev.get("forecast") or "—",
            "anterior": ev.get("previous") or "—",
        })
    return sorted(salida, key=lambda e: e["hora"])


# ----------------------------------------------------------------- Claude
PROMPT_SISTEMA = """Eres un analista macro que asiste a un trader intradía de EUR/USD (M5-M15)
que también sigue los índices americanos (NASDAQ 100 y S&P 500).
Escribes en español, directo, sin relleno y sin inventar cifras: usa solo los datos que se te dan.
Nunca des una orden de compra/venta; describe escenarios y riesgos.
Texto plano, sin markdown, sin asteriscos ni almohadillas."""


def preguntar_claude(prompt, max_tokens=500):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "system": PROMPT_SISTEMA,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"]).strip()
    except Exception as e:
        print(f"AVISO: fallo al llamar a Claude ({e}). Envío la alerta sin análisis.")
        return None


def linea_evento(ev):
    hora = "hora por confirmar" if ev["sin_hora"] else ev["hora"].astimezone(TZ).strftime("%H:%M")
    return f'{BANDERA.get(ev["divisa"], "")} {hora} · {ev["divisa"]} · {ev["titulo"]} (previsto {ev["previsto"]} · anterior {ev["anterior"]})'


# ----------------------------------------------------------------- mensajes
def mensaje_alerta(grupo):
    hora_local = grupo[0]["hora"].astimezone(TZ).strftime("%H:%M")
    minutos = max(0, round((grupo[0]["hora"] - ahora_utc()).total_seconds() / 60))
    datos = "\n".join(linea_evento(e) for e in grupo)

    analisis = preguntar_claude(
        f"""En {minutos} minutos (a las {hora_local} hora de Madrid) se publica:
{datos}

Escribe una alerta breve (máx. 110 palabras) con este formato exacto:
Qué es: una frase sobre qué mide y por qué mueve el mercado.
("Mejor" = mejor para la economía de esa divisa; ojo con paro y peticiones de subsidio, donde un número más alto es PEOR.)
Si sale MEJOR de lo previsto: reacción típica de EUR/USD y de NASDAQ/S&P 500.
Si sale PEOR de lo previsto: reacción típica de EUR/USD y de NASDAQ/S&P 500.
Cuidado: un riesgo operativo concreto (spread, latigazo inicial, noticias encadenadas, etc.)."""
    )

    texto = f"⚠️ <b>ALERTA MACRO · {hora_local} (en {minutos} min)</b>\n\n{esc(datos)}"
    if analisis:
        texto += f"\n\n{esc(analisis)}"
    return texto


def mensaje_resumen(eventos_hoy):
    local = ahora_utc().astimezone(TZ)
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    fecha = f"{dias[local.weekday()]} {local:%d/%m}"
    if not eventos_hoy:
        return (f"☀️ <b>Agenda macro · {fecha}</b>\n\nHoy no hay eventos de alto impacto de "
                f"{', '.join(DIVISAS)}. Día guiado por flujo técnico y noticias no programadas.")
    datos = "\n".join(linea_evento(e) for e in eventos_hoy)
    analisis = preguntar_claude(
        f"""Agenda de hoy (horas de Madrid), eventos de alto impacto:
{datos}

Escribe un briefing matinal (máx. 150 palabras):
1) Cuál es el evento clave del día y por qué.
2) Franjas horarias a vigilar o evitar para EUR/USD y para índices USA.
3) Qué escenario haría más volátil la sesión.""",
        max_tokens=600,
    )
    texto = f"☀️ <b>Agenda macro · {fecha}</b>\n\n{esc(datos)}"
    if analisis:
        texto += f"\n\n{esc(analisis)}"
    return texto


def enviar_telegram(texto):
    if DRY_RUN:
        print("----- [SIMULACIÓN] mensaje a Telegram -----\n" + texto + "\n-------------------------------------------")
        return True
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        print(f"ERROR Telegram: {r.status_code} {r.text}")
        return False
    return True


# ----------------------------------------------------------------- principal
def main():
    if "--prueba" in sys.argv:
        ok = enviar_telegram("✅ Tu agente de alertas macro está conectado correctamente.")
        sys.exit(0 if ok else 1)

    if not DRY_RUN and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en los Secrets de GitHub.")

    estado = cargar_json(ESTADO_FILE, {"avisados": [], "resumen_enviado": ""})
    eventos = eventos_relevantes(obtener_calendario())
    ahora = ahora_utc()
    local = ahora.astimezone(TZ)

    # 1) Resumen matinal (lunes a viernes, una vez al día)
    hoy = local.date().isoformat()
    h, m = map(int, HORA_RESUMEN.split(":"))
    if local.weekday() < 5 and estado.get("resumen_enviado") != hoy and \
            (local.hour, local.minute) >= (h, m) and local.hour < 12:
        de_hoy = [e for e in eventos if e["hora"].astimezone(TZ).date() == local.date()]
        if enviar_telegram(mensaje_resumen(de_hoy)):
            estado["resumen_enviado"] = hoy
            print("Resumen matinal enviado")

    # 2) Alertas 15 min antes (agrupa eventos que salen a la misma hora)
    pendientes = [e for e in eventos
                  if not e["sin_hora"]
                  and e["id"] not in estado["avisados"]
                  and timedelta(0) < e["hora"] - ahora <= timedelta(minutes=MINUTOS_ANTES + 1)]
    grupos = {}
    for e in pendientes:
        grupos.setdefault(e["hora"], []).append(e)
    for _, grupo in sorted(grupos.items()):
        if enviar_telegram(mensaje_alerta(grupo)):
            estado["avisados"].extend(e["id"] for e in grupo)
            print(f"Alerta enviada: {[e['titulo'] for e in grupo]}")

    if not pendientes:
        print(f"{local:%Y-%m-%d %H:%M} Madrid · sin eventos en los próximos {MINUTOS_ANTES} min")

    estado["avisados"] = estado["avisados"][-300:]   # que el archivo no crezca sin límite
    guardar_json(ESTADO_FILE, estado)


if __name__ == "__main__":
    main()
