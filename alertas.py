"""
Agente de alertas macro: EUR/USD, USD/JPY, GER 40 e índices USA.  (versión 3)

Qué hace cada vez que se ejecuta (cada 5 min):
  1. Calendario económico (Forex Factory): eventos de ALTO impacto de USD, EUR y JPY.
     (Los datos de Alemania vienen como EUR en Forex Factory: ya están incluidos.)
     Aviso 15 min antes, con análisis de Claude.
  2. Agenda matinal (lunes-viernes, HORA_RESUMEN): eventos del día + foto de mercado.
  3. Resumen antes de Fráncfort (lunes-viernes, HORA_PRE_FRA, 08:45 por defecto):
     GER 40, Bund 10A, diferencial con EE. UU. y EUR/USD, antes de la apertura de Xetra (09:00).
  4. Resumen pre-Nueva York (lunes-viernes, HORA_PRE_NY, 15:00 por defecto):
     bonos US 2A/10A, Bund 10A, JGB 10A, DXY, EUR/USD, USD/JPY y GER 40 con su cambio del día.
  5. Alertas de mercado:
     - Movimientos fuertes en el día: US 2A ±8 pb, US 10A ±10 pb, Bund 10A ±8 pb, JGB 10A ±5 pb,
       GER 40 ±1 % (y vuelve a avisar en cada múltiplo: 2x, 3x...).
     - Cruces de niveles clave: US 10A 5,00 % y 5,25 %; USD/JPY 155 y 160.
  6. De 23:00 a 07:00 (Madrid) los mensajes llegan en silencio (sin sonido).

No hace falta tocar este archivo. Lo configurable está en los "Secrets"/"Variables" de GitHub.
"""

import csv
import io
import json
import math
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

DIVISAS = [d.strip() for d in (os.getenv("DIVISAS") or "USD,EUR,JPY").split(",")]
IMPACTOS = [i.strip() for i in (os.getenv("IMPACTOS") or "High").split(",")]
MINUTOS_ANTES = int(os.getenv("MINUTOS_ANTES") or 15)
HORA_RESUMEN = os.getenv("HORA_RESUMEN") or "07:30"      # hora de Madrid
HORA_PRE_FRA = os.getenv("HORA_PRE_FRA") or "08:45"      # hora de Madrid (Xetra abre a las 09:00)
HORA_PRE_NY = os.getenv("HORA_PRE_NY") or "15:00"        # hora de Madrid
SILENCIO = os.getenv("HORAS_SILENCIO") or "23-07"        # sin sonido entre estas horas
DRY_RUN = os.getenv("DRY_RUN") == "1"                    # 1 = imprime en vez de enviar

TZ = ZoneInfo("Europe/Madrid")
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_HORAS = 2
BASE = Path(__file__).parent
ESTADO_FILE = BASE / "estado.json"
CACHE_FILE = BASE / "calendario_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

BANDERA = {"USD": "🇺🇸", "EUR": "🇪🇺", "JPY": "🇯🇵"}

# Instrumentos de mercado.
#   tipo "yield": rentabilidad en %, los cambios se miden en puntos básicos (pb)  -> umbral_pb
#   tipo "precio": los cambios se miden en %                                      -> umbral_pct
#   El aviso salta si el movimiento del día alcanza el umbral (y cada múltiplo).
#   niveles: aviso cuando el precio cruza el nivel (con un margen para no repetir)
INSTRUMENTOS = {
    "US2Y":   {"nombre": "Bono EE. UU. 2A",  "tipo": "yield", "cnbc": "US2Y",  "yahoo": "2YY=F",
               "umbral_pb": 8,  "niveles": [], "margen": 0.02},
    "US10Y":  {"nombre": "Bono EE. UU. 10A", "tipo": "yield", "cnbc": "US10Y", "yahoo": "^TNX",
               "umbral_pb": 10, "niveles": [5.00, 5.25], "margen": 0.02},
    "DE10Y":  {"nombre": "Bund Alemania 10A", "tipo": "yield", "cnbc": "DE10Y-DE", "yahoo": None,
               "umbral_pb": 8,  "niveles": [], "margen": 0.02},
    "JP10Y":  {"nombre": "Bono Japón 10A",   "tipo": "yield", "cnbc": "JP10Y", "yahoo": None,
               "umbral_pb": 5,  "niveles": [], "margen": 0.02},
    "DXY":    {"nombre": "DXY",              "tipo": "precio", "cnbc": ".DXY",   "yahoo": "DX-Y.NYB",
               "dec": 2, "niveles": [], "margen": 0.10},
    "EURUSD": {"nombre": "EUR/USD",          "tipo": "precio", "cnbc": "EUR=",   "yahoo": "EURUSD=X",
               "dec": 4, "niveles": [], "margen": 0.0010},
    "USDJPY": {"nombre": "USD/JPY",          "tipo": "precio", "cnbc": "JPY=",   "yahoo": "JPY=X",
               "dec": 2, "niveles": [155.0, 160.0], "margen": 0.15},
    "GER40":  {"nombre": "GER 40 (DAX)",     "tipo": "precio", "cnbc": ".GDAXI", "yahoo": "^GDAXI",
               "dec": 0, "umbral_pct": 1.0, "niveles": [], "margen": 20},
}
ORDEN = ["US2Y", "US10Y", "DE10Y", "JP10Y", "DXY", "EURUSD", "USDJPY", "GER40"]


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


def a_float(valor):
    """'4.254%' / '+0.012' / '157,85' / 157.85 -> float (o None)."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().replace("%", "").replace("+", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def hora_hhmm(texto):
    h, m = map(int, texto.split(":"))
    return h, m


def en_silencio(local):
    try:
        ini, fin = map(int, SILENCIO.split("-"))
    except ValueError:
        return False
    return (local.hour >= ini or local.hour < fin) if ini > fin else (ini <= local.hour < fin)


# ----------------------------------------------------------------- calendario
def obtener_calendario():
    cache = cargar_json(CACHE_FILE, {})
    if cache.get("descargado"):
        edad = ahora_utc() - datetime.fromisoformat(cache["descargado"])
        if edad < timedelta(hours=CACHE_HORAS):
            return cache["eventos"]
    try:
        r = requests.get(FEED_URL, timeout=20, headers=UA)
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


# ----------------------------------------------------------------- datos de mercado
def _cnbc(simbolos):
    """Cotizaciones de CNBC (una sola petición). Devuelve {simbolo_cnbc: (ultimo, cierre_anterior, fecha)}."""
    url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
           f"?symbols={'|'.join(simbolos)}&requestMethod=itv&noform=1&partnerId=2"
           "&fund=1&exthrs=1&output=json")
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    data = r.json()
    lista = data.get("FormattedQuoteResult", {}).get("FormattedQuote", [])
    if isinstance(lista, dict):
        lista = [lista]
    salida = {}
    for q in lista:
        ultimo = a_float(q.get("last"))
        anterior = a_float(q.get("previous_day_closing"))
        if anterior is None and ultimo is not None and a_float(q.get("change")) is not None:
            anterior = ultimo - a_float(q.get("change"))
        fecha = None
        t = q.get("last_time") or ""
        try:
            if len(t) >= 5 and t[-5] in "+-" and t[-3] != ":":
                t = t[:-2] + ":" + t[-2:]
            fecha = datetime.fromisoformat(t) if t else None
        except ValueError:
            fecha = None
        if ultimo is not None:
            salida[q.get("symbol")] = (ultimo, anterior, fecha)
    return salida


def _yahoo(simbolo):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(simbolo)}"
    r = requests.get(url, params={"range": "1d", "interval": "5m"}, headers=UA, timeout=20)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    ultimo = a_float(meta.get("regularMarketPrice"))
    anterior = a_float(meta.get("chartPreviousClose") or meta.get("previousClose"))
    ts = meta.get("regularMarketTime")
    fecha = datetime.fromtimestamp(ts, timezone.utc) if ts else None
    if simbolo == "^TNX" and ultimo and ultimo > 20:       # por si viene multiplicado por 10
        ultimo, anterior = ultimo / 10, (anterior / 10 if anterior else None)
    return ultimo, anterior, fecha


def _mof_jgb10():
    """Curva oficial del Ministerio de Finanzas de Japón (cierre diario)."""
    url = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    filas = list(csv.reader(io.StringIO(r.content.decode("utf-8", "ignore"))))
    cab = next(i for i, f in enumerate(filas) if "10Y" in [c.strip() for c in f])
    col = [c.strip() for c in filas[cab]].index("10Y")
    valores = []
    for f in filas[cab + 1:]:
        if len(f) > col and a_float(f[col]) is not None:
            valores.append(a_float(f[col]))
    return valores[-1], (valores[-2] if len(valores) > 1 else None), None


def obtener_mercado():
    """{clave: {"ultimo", "anterior", "fecha", "fuente"}} para cada instrumento que se haya podido leer."""
    datos = {}
    try:
        cnbc = _cnbc([i["cnbc"] for i in INSTRUMENTOS.values()])
        for k, i in INSTRUMENTOS.items():
            if i["cnbc"] in cnbc:
                u, a, f = cnbc[i["cnbc"]]
                datos[k] = {"ultimo": u, "anterior": a, "fecha": f, "fuente": "CNBC"}
    except Exception as e:
        print(f"AVISO: CNBC no disponible ({e})")
    for k, i in INSTRUMENTOS.items():
        if k in datos and datos[k]["anterior"] is not None:
            continue
        try:
            if i["yahoo"]:
                u, a, f = _yahoo(i["yahoo"])
                fuente = "Yahoo"
            elif k == "JP10Y":
                u, a, f = _mof_jgb10()
                fuente = "MoF Japón (cierre)"
            else:
                continue
            if u is not None:
                datos[k] = {"ultimo": u, "anterior": a, "fecha": f, "fuente": fuente}
        except Exception as e:
            print(f"AVISO: no se pudo leer {k} ({e})")
    return datos


def cambio(k, d):
    """Cambio del día: en pb para rentabilidades, en % para precios."""
    if d.get("anterior") in (None, 0):
        return None
    if INSTRUMENTOS[k]["tipo"] == "yield":
        return (d["ultimo"] - d["anterior"]) * 100
    return (d["ultimo"] / d["anterior"] - 1) * 100


def dato_fresco(d, max_min=45):
    f = d.get("fecha")
    if f is None:
        return True
    if f.tzinfo is None:
        f = f.replace(tzinfo=timezone.utc)
    return ahora_utc() - f <= timedelta(minutes=max_min)


def linea_mercado(k, d):
    i = INSTRUMENTOS[k]
    c = cambio(k, d)
    if i["tipo"] == "yield":
        txt = f'{i["nombre"]}: {d["ultimo"]:.3f} %'
        if c is not None:
            txt += f" ({c:+.1f} pb)"
    else:
        txt = f'{i["nombre"]}: {d["ultimo"]:.{i["dec"]}f}'
        if c is not None:
            txt += f" ({c:+.2f} %)"
    # Mercado cerrado (p. ej. el DAX de contado antes de las 09:00): el dato es el último cierre
    if not d["fuente"].startswith("MoF") and not dato_fresco(d, 90):
        txt += " · cierre anterior"
    return txt


def bloque_mercado(datos):
    lineas = [linea_mercado(k, datos[k]) for k in ORDEN if k in datos]
    if "US2Y" in datos and "US10Y" in datos:
        pendiente = (datos["US10Y"]["ultimo"] - datos["US2Y"]["ultimo"]) * 100
        lineas.append(f"Pendiente 2A-10A EE. UU.: {pendiente:+.0f} pb")
    if "US10Y" in datos and "DE10Y" in datos:
        dif = (datos["US10Y"]["ultimo"] - datos["DE10Y"]["ultimo"]) * 100
        lineas.append(f"Diferencial 10A EE. UU.-Alemania: {dif:.0f} pb")
    if "US10Y" in datos and "JP10Y" in datos:
        dif = (datos["US10Y"]["ultimo"] - datos["JP10Y"]["ultimo"]) * 100
        lineas.append(f"Diferencial 10A EE. UU.-Japón: {dif:.0f} pb")
    return "\n".join(lineas)


def alertas_mercado(datos, estado):
    """Devuelve una lista de textos de alerta y actualiza el estado."""
    mem = estado.setdefault("mercado", {"escalones": {}, "lados": {}})
    alertas = []
    for k, d in datos.items():
        i = INSTRUMENTOS[k]
        if d["fuente"].startswith("MoF") or not dato_fresco(d):
            continue                                   # dato de cierre o antiguo: no genera alertas
        es_yield = i["tipo"] == "yield"
        # a) Movimiento fuerte del día (por escalones: 1x, 2x, 3x el umbral)
        umbral = i.get("umbral_pb") if es_yield else i.get("umbral_pct")
        c = cambio(k, d)
        if umbral and c is not None:
            n = int(math.copysign(math.floor(abs(c) / umbral), c)) if abs(c) >= umbral else 0
            previo = mem["escalones"].get(k, {})
            if previo.get("base") != d["anterior"]:
                previo = {"base": d["anterior"], "n": 0}          # nueva sesión: reinicio
            if n != 0 and (abs(n) > abs(previo["n"]) or (n > 0) != (previo["n"] > 0)):
                direccion = "sube" if c > 0 else "baja"
                if es_yield:
                    alertas.append(f'{i["nombre"]} {direccion} {abs(c):.1f} pb en el día '
                                   f'(ahora {d["ultimo"]:.3f} %).')
                else:
                    alertas.append(f'{i["nombre"]} {direccion} {abs(c):.2f} % en el día '
                                   f'(ahora {d["ultimo"]:.{i["dec"]}f}).')
            if abs(n) >= abs(previo["n"]) or (n != 0 and (n > 0) != (previo["n"] > 0)):
                previo["n"] = n
            mem["escalones"][k] = previo
        # b) Cruce de niveles
        for nivel in i["niveles"]:
            clave = f"{k}@{nivel}"
            anterior = mem["lados"].get(clave)
            if anterior is None:                                # primera vez: solo anota el lado
                mem["lados"][clave] = "arriba" if d["ultimo"] >= nivel else "abajo"
                continue
            if d["ultimo"] >= nivel + i["margen"]:
                lado = "arriba"
            elif d["ultimo"] <= nivel - i["margen"]:
                lado = "abajo"
            else:
                continue                                        # dentro del margen: sin cambios
            mem["lados"][clave] = lado
            if anterior and anterior != lado:
                if es_yield:
                    fmt, ahora_txt = f"{nivel:.2f} %", f'{d["ultimo"]:.3f} %'
                else:
                    fmt, ahora_txt = f"{nivel:.{i['dec']}f}", f'{d["ultimo"]:.{i["dec"]}f}'
                verbo = "supera" if lado == "arriba" else "pierde"
                alertas.append(f'{i["nombre"]} {verbo} el nivel {fmt} (ahora {ahora_txt}).')
    return alertas


# ----------------------------------------------------------------- Claude
PROMPT_SISTEMA = """Eres un analista macro que asiste a traders intradía (M5-M15) de EUR/USD, USD/JPY y GER 40 (DAX)
que también siguen los índices americanos (NASDAQ 100 y S&P 500).
Escribes en español, directo, sin relleno y sin inventar cifras ni noticias: usa solo los datos que se te dan.
Si no sabes la causa de un movimiento, no la inventes: describe lo que implica, no por qué ha pasado.
Nunca des una orden de compra/venta; describe escenarios y riesgos.
Ten en cuenta que la relación bonos-dólar no siempre se cumple: si las rentabilidades suben por miedo
(inflación, deuda, petróleo) el dólar puede caer; menciónalo si el DXY no acompaña.
Para el GER 40: suele moverse con el sentimiento de riesgo global (futuros USA) y lo presionan subidas fuertes
del Bund; un euro muy fuerte perjudica a los exportadores alemanes. Son tendencias, no reglas fijas.
Si un dato aparece marcado como "cierre anterior", es el último cierre, no el precio actual: no lo trates como movimiento de hoy.
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
        print(f"AVISO: fallo al llamar a Claude ({e}). Envío el mensaje sin análisis.")
        return None


def linea_evento(ev):
    hora = "hora por confirmar" if ev["sin_hora"] else ev["hora"].astimezone(TZ).strftime("%H:%M")
    return f'{BANDERA.get(ev["divisa"], "")} {hora} · {ev["divisa"]} · {ev["titulo"]} (previsto {ev["previsto"]} · anterior {ev["anterior"]})'


# ----------------------------------------------------------------- mensajes
def mensaje_alerta(grupo, mercado_txt=""):
    hora_local = grupo[0]["hora"].astimezone(TZ).strftime("%H:%M")
    minutos = max(0, round((grupo[0]["hora"] - ahora_utc()).total_seconds() / 60))
    datos = "\n".join(linea_evento(e) for e in grupo)
    contexto = f"\n\nMercado ahora:\n{mercado_txt}" if mercado_txt else ""

    analisis = preguntar_claude(
        f"""En {minutos} minutos (a las {hora_local} hora de Madrid) se publica:
{datos}{contexto}

Escribe una alerta breve (máx. 120 palabras) con este formato exacto:
Qué es: una frase sobre qué mide y por qué mueve el mercado.
("Mejor" = mejor para la economía de esa divisa; ojo con paro y peticiones de subsidio, donde un número más alto es PEOR.)
Si sale MEJOR de lo previsto: reacción típica de los pares/índices afectados.
Si sale PEOR de lo previsto: reacción típica de los pares/índices afectados.
Cuidado: un riesgo operativo concreto (spread, latigazo inicial, noticias encadenadas, riesgo de intervención del yen, etc.).
Afectados: dato de USD -> EUR/USD, USD/JPY, NASDAQ/S&P 500 y, por contagio, GER 40;
dato de EUR/Alemania o BCE -> EUR/USD y GER 40; dato de JPY -> USD/JPY."""
    )

    texto = f"⚠️ <b>ALERTA MACRO · {hora_local} (en {minutos} min)</b>\n\n{esc(datos)}"
    if analisis:
        texto += f"\n\n{esc(analisis)}"
    return texto


def mensaje_resumen(eventos_hoy, mercado_txt):
    local = ahora_utc().astimezone(TZ)
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    fecha = f"{dias[local.weekday()]} {local:%d/%m}"
    texto = f"☀️ <b>Agenda macro · {fecha}</b>\n\n"
    if eventos_hoy:
        datos = "\n".join(linea_evento(e) for e in eventos_hoy)
        texto += esc(datos)
    else:
        datos = "Sin eventos de alto impacto hoy."
        texto += (f"Hoy no hay eventos de alto impacto de {', '.join(DIVISAS)}. "
                  f"Día guiado por flujo técnico y noticias no programadas.")
    if mercado_txt:
        texto += f"\n\n<b>Mercado</b>\n{esc(mercado_txt)}"
    analisis = preguntar_claude(
        f"""Agenda de hoy (horas de Madrid), eventos de alto impacto:
{datos}

Mercado ahora (cambio respecto al cierre anterior):
{mercado_txt or 'sin datos'}

Escribe un briefing matinal (máx. 170 palabras):
1) Evento clave del día y por qué.
2) Qué dicen los bonos (EE. UU., Alemania y Japón) sobre el sesgo de USD, EUR y JPY hoy.
3) Franjas horarias a vigilar o evitar para EUR/USD, USD/JPY, GER 40 e índices USA.""",
        max_tokens=700,
    )
    if analisis:
        texto += f"\n\n{esc(analisis)}"
    return texto


def mensaje_pre_fra(mercado_txt, eventos_resto):
    texto = f"🇩🇪 <b>Antes de Fráncfort · {ahora_utc().astimezone(TZ):%H:%M}</b>\n\n{esc(mercado_txt)}"
    agenda = "\n".join(linea_evento(e) for e in eventos_resto) or "Sin eventos de alto impacto el resto del día."
    texto += f"\n\n<b>Resto del día</b>\n{esc(agenda)}"
    analisis = preguntar_claude(
        f"""Faltan unos 15 minutos para la apertura de Xetra (09:00 Madrid). Mercado (cambio respecto al cierre anterior):
{mercado_txt}

Eventos de alto impacto que quedan hoy:
{agenda}

En máx. 90 palabras: qué sesgo sugieren el Bund y el diferencial EE. UU.-Alemania para el EUR y el GER 40 en la
sesión europea, si EUR/USD y DAX apuntan en la misma dirección o no, y qué eventos del día pueden romper ese sesgo.
Si el GER 40 aparece como "cierre anterior", di que aún no hay precio de hoy y no deduzcas un gap. Si las señales se contradicen, dilo.""",
        max_tokens=400,
    )
    if analisis:
        texto += f"\n\n{esc(analisis)}"
    return texto


def mensaje_pre_ny(mercado_txt, eventos_tarde):
    texto = f"🗽 <b>Antes de Nueva York · {ahora_utc().astimezone(TZ):%H:%M}</b>\n\n{esc(mercado_txt)}"
    agenda = "\n".join(linea_evento(e) for e in eventos_tarde) or "Sin eventos de alto impacto el resto del día."
    texto += f"\n\n<b>Resto del día</b>\n{esc(agenda)}"
    analisis = preguntar_claude(
        f"""Faltan 30 minutos para la apertura de Nueva York. Mercado (cambio respecto al cierre anterior):
{mercado_txt}

Eventos de alto impacto que quedan hoy:
{agenda}

En máx. 100 palabras: qué sesgo sugieren los bonos para USD y JPY en la sesión de NY, si el DXY confirma o
contradice a los bonos, y qué implica para EUR/USD, USD/JPY, NASDAQ y el GER 40 (que cierra a las 17:30).
Si las señales se contradicen, dilo.""",
        max_tokens=450,
    )
    if analisis:
        texto += f"\n\n{esc(analisis)}"
    return texto


def mensaje_alerta_mercado(alertas, mercado_txt):
    texto = "📈 <b>ALERTA DE MERCADO</b>\n\n" + esc("\n".join(alertas))
    texto += f"\n\n{esc(mercado_txt)}"
    analisis = preguntar_claude(
        f"""Alertas de mercado ahora mismo:
{chr(10).join(alertas)}

Foto completa del mercado:
{mercado_txt}

En máx. 70 palabras: qué implica para USD, EUR, JPY, EUR/USD, USD/JPY, GER 40 e índices USA. No inventes la causa.""",
        max_tokens=300,
    )
    if analisis:
        texto += f"\n\n{esc(analisis)}"
    return texto


def enviar_telegram(texto):
    silencioso = en_silencio(ahora_utc().astimezone(TZ))
    if DRY_RUN:
        print(f"----- [SIMULACIÓN] mensaje a Telegram{' (silencioso)' if silencioso else ''} -----\n"
              + texto + "\n-------------------------------------------")
        return True
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": texto[:4000], "parse_mode": "HTML",
              "disable_web_page_preview": True, "disable_notification": silencioso},
        timeout=20,
    )
    if not r.ok:
        print(f"ERROR Telegram: {r.status_code} {r.text}")
        return False
    return True


# ----------------------------------------------------------------- principal
def main():
    if "--prueba" in sys.argv:
        datos = obtener_mercado()
        print("Datos de mercado leídos:")
        for k, d in datos.items():
            print(f"  {k}: {d}")
        faltan = [k for k in INSTRUMENTOS if k not in datos]
        texto = "✅ Tu agente de alertas macro está conectado correctamente."
        if datos:
            fuentes = sorted({d["fuente"] for d in datos.values()})
            texto += f"\n\n<b>Prueba de datos de mercado</b>\n{esc(bloque_mercado(datos))}"
            texto += f"\n\nFuentes: {esc(', '.join(fuentes))}"
        if faltan:
            texto += f"\n\n⚠️ No se pudieron leer: {', '.join(faltan)}"
        ok = enviar_telegram(texto)
        sys.exit(0 if ok else 1)

    if not DRY_RUN and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en los Secrets de GitHub.")

    estado = cargar_json(ESTADO_FILE, {})
    estado.setdefault("avisados", [])
    eventos = eventos_relevantes(obtener_calendario())
    ahora = ahora_utc()
    local = ahora.astimezone(TZ)
    hoy = local.date().isoformat()
    laborable = local.weekday() < 5

    # Datos de mercado (solo entre semana; el fin de semana el mercado está cerrado)
    mercado = obtener_mercado() if laborable else {}
    mercado_txt = bloque_mercado(mercado) if mercado else ""

    def restantes_hoy():
        return [e for e in eventos if e["hora"] > ahora and e["hora"].astimezone(TZ).date() == local.date()]

    # 1) Agenda matinal
    if laborable and estado.get("resumen_enviado") != hoy and \
            (local.hour, local.minute) >= hora_hhmm(HORA_RESUMEN) and local.hour < 12:
        de_hoy = [e for e in eventos if e["hora"].astimezone(TZ).date() == local.date()]
        if enviar_telegram(mensaje_resumen(de_hoy, mercado_txt)):
            estado["resumen_enviado"] = hoy
            print("Resumen matinal enviado")

    # 2) Resumen antes de Fráncfort (apertura de Xetra)
    if laborable and mercado and estado.get("prefra_enviado") != hoy and \
            (local.hour, local.minute) >= hora_hhmm(HORA_PRE_FRA) and local.hour < 11:
        if enviar_telegram(mensaje_pre_fra(mercado_txt, restantes_hoy())):
            estado["prefra_enviado"] = hoy
            print("Resumen pre-Fráncfort enviado")

    # 3) Resumen antes de Nueva York
    if laborable and mercado and estado.get("preny_enviado") != hoy and \
            (local.hour, local.minute) >= hora_hhmm(HORA_PRE_NY) and local.hour < 18:
        if enviar_telegram(mensaje_pre_ny(mercado_txt, restantes_hoy())):
            estado["preny_enviado"] = hoy
            print("Resumen pre-NY enviado")

    # 4) Alertas 15 min antes de cada evento (agrupa los que salen a la misma hora)
    pendientes = [e for e in eventos
                  if not e["sin_hora"]
                  and e["id"] not in estado["avisados"]
                  and timedelta(0) < e["hora"] - ahora <= timedelta(minutes=MINUTOS_ANTES + 1)]
    grupos = {}
    for e in pendientes:
        grupos.setdefault(e["hora"], []).append(e)
    for _, grupo in sorted(grupos.items()):
        if enviar_telegram(mensaje_alerta(grupo, mercado_txt)):
            estado["avisados"].extend(e["id"] for e in grupo)
            print(f"Alerta enviada: {[e['titulo'] for e in grupo]}")

    # 5) Alertas de mercado (bonos, GER 40 y niveles)
    if mercado:
        avisos = alertas_mercado(mercado, estado)
        if avisos and enviar_telegram(mensaje_alerta_mercado(avisos, mercado_txt)):
            print(f"Alerta de mercado enviada: {avisos}")

    if not pendientes:
        print(f"{local:%Y-%m-%d %H:%M} Madrid · sin eventos en los próximos {MINUTOS_ANTES} min")
    if mercado_txt:
        print(mercado_txt)

    estado["avisados"] = estado["avisados"][-300:]   # que el archivo no crezca sin límite
    guardar_json(ESTADO_FILE, estado)


if __name__ == "__main__":
    main()
