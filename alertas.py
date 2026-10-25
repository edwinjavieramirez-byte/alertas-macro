"""
Agente de alertas macro: EUR/USD, USD/JPY, GER 40 e índices USA.  (versión 4)

Qué hace cada vez que se ejecuta (cada 5 min):
  1. Calendario económico (Forex Factory) de USD, EUR y JPY.
     - Alerta 15 min antes de cada evento de ALTO impacto, con análisis de Claude
       (contexto de mercado + escenarios por encima / por debajo / en línea con lo previsto).
     - Reacción al dato: 15 min después de la publicación, mide cuánto se han movido bonos,
       DXY, pares, GER 40 y sentimiento desde justo antes del dato y Claude interpreta la reacción.
     - La agenda y los resúmenes incluyen también los eventos de impacto MEDIO.
  2. Agenda matinal (lunes-viernes, HORA_RESUMEN, hora de Madrid).
  3. Resumen antes de Fráncfort (lunes-viernes, HORA_PRE_FRA, 08:45 Madrid; Xetra abre a las 09:00).
  4. Resumen antes de Nueva York (lunes-viernes, HORA_PRE_NY, 09:00 HORA DE NUEVA YORK,
     30 min antes de la apertura; se ajusta solo a los cambios de hora de EE. UU. y Europa).
  5. Alertas de mercado:
     - Movimientos fuertes en el día: US 2A ±8 pb, US 10A ±10 pb, Bund 10A ±8 pb, JGB 10A ±5 pb,
       GER 40 ±1 % (y vuelve a avisar en cada múltiplo: 2x, 3x...).
     - Cruces de niveles clave: US 10A 5,00 % y 5,25 %; USD/JPY 155 y 160.
  6. De 23:00 a 07:00 (Madrid) los mensajes llegan en silencio (sin sonido).

Formato de los mensajes: primero el análisis; los datos de mercado van al final, plegados
(se despliegan tocando el bloque). Si Claude falla, los datos se muestran sin plegar.

Datos de sentimiento (VIX, futuros S&P 500 y Nasdaq 100, petróleo WTI y oro): solo se usan
como contexto para el análisis; no generan alertas.

No hace falta tocar este archivo. Lo configurable está en los "Secrets"/"Variables" de GitHub.
"""

import csv
import html
import io
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------- configuración
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5"

DIVISAS = [d.strip() for d in (os.getenv("DIVISAS") or "USD,EUR,JPY").split(",")]
# Impactos que generan alerta 15 min antes (y reacción al dato)
IMPACTOS_ALERTA = [i.strip() for i in (os.getenv("IMPACTOS") or "High").split(",")]
# Impactos que aparecen en la agenda y en los resúmenes
IMPACTOS_AGENDA = [i.strip() for i in (os.getenv("IMPACTOS_AGENDA") or "High,Medium").split(",")]
MINUTOS_ANTES = int(os.getenv("MINUTOS_ANTES") or 15)
MINUTOS_REACCION = int(os.getenv("MINUTOS_REACCION") or 15)
HORA_RESUMEN = os.getenv("HORA_RESUMEN") or "07:30"      # hora de Madrid
HORA_PRE_FRA = os.getenv("HORA_PRE_FRA") or "08:45"      # hora de Madrid (Xetra abre a las 09:00)
HORA_PRE_NY = os.getenv("HORA_PRE_NY") or "09:00"        # hora de NUEVA YORK (Wall Street abre a las 09:30)
SILENCIO = os.getenv("HORAS_SILENCIO") or "23-07"        # sin sonido entre estas horas
DRY_RUN = os.getenv("DRY_RUN") == "1"                    # 1 = imprime en vez de enviar

TZ = ZoneInfo("Europe/Madrid")
TZ_NY = ZoneInfo("America/New_York")
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_HORAS = 2
LIMITE_TELEGRAM = 3900
BASE = Path(__file__).parent
ESTADO_FILE = BASE / "estado.json"
CACHE_FILE = BASE / "calendario_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

BANDERA = {"USD": "🇺🇸", "EUR": "🇪🇺", "JPY": "🇯🇵"}
MARCA_IMPACTO = {"High": "🔴", "Medium": "🟠", "Low": "⚪"}

# Instrumentos de mercado.
#   tipo "yield": rentabilidad en %, los cambios se miden en puntos básicos (pb)  -> umbral_pb
#   tipo "precio": los cambios se miden en %                                      -> umbral_pct
#   El aviso salta si el movimiento del día alcanza el umbral (y cada múltiplo).
#   niveles: aviso cuando el precio cruza el nivel (con un margen para no repetir)
#   Los de sentimiento no tienen umbral ni niveles: solo dan contexto a Claude.
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
    # --- sentimiento (solo contexto)
    "VIX":    {"nombre": "VIX",              "tipo": "precio", "cnbc": ".VIX",   "yahoo": "^VIX",
               "dec": 2, "niveles": [], "margen": 0},
    "SPF":    {"nombre": "Futuro S&P 500",   "tipo": "precio", "cnbc": "@SP.1",  "yahoo": "ES=F",
               "dec": 0, "niveles": [], "margen": 0},
    "NQF":    {"nombre": "Futuro Nasdaq 100", "tipo": "precio", "cnbc": "@ND.1", "yahoo": "NQ=F",
               "dec": 0, "niveles": [], "margen": 0},
    "WTI":    {"nombre": "Petróleo WTI",     "tipo": "precio", "cnbc": "@CL.1",  "yahoo": "CL=F",
               "dec": 2, "niveles": [], "margen": 0},
    "ORO":    {"nombre": "Oro",              "tipo": "precio", "cnbc": "@GC.1",  "yahoo": "GC=F",
               "dec": 0, "niveles": [], "margen": 0},
}
ORDEN = ["US2Y", "US10Y", "DE10Y", "JP10Y", "DXY", "EURUSD", "USDJPY", "GER40"]
ORDEN_SENTIMIENTO = ["VIX", "SPF", "NQF", "WTI", "ORO"]


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


def reloj():
    """Hora actual para dar contexto a Claude."""
    a = ahora_utc()
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    local = a.astimezone(TZ)
    return (f"{dias[local.weekday()]} {local:%d/%m/%Y}, {local:%H:%M} en Madrid "
            f"({a.astimezone(TZ_NY):%H:%M} en Nueva York)")


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
        if ev.get("country") not in DIVISAS or ev.get("impact") not in IMPACTOS_AGENDA + IMPACTOS_ALERTA:
            continue
        fecha = datetime.fromisoformat(ev["date"])          # viene en hora de Nueva York
        # Forex Factory pone a medianoche (NY) los eventos sin hora fija ("Tentative"/"All Day")
        sin_hora = fecha.hour == 0 and fecha.minute == 0
        salida.append({
            "id": f'{ev["country"]}|{ev["title"]}|{ev["date"]}',
            "titulo": ev["title"],
            "divisa": ev["country"],
            "impacto": ev.get("impact"),
            "hora": fecha.astimezone(timezone.utc),
            "sin_hora": sin_hora,
            "previsto": ev.get("forecast") or "—",
            "anterior": ev.get("previous") or "—",
        })
    return sorted(salida, key=lambda e: e["hora"])


def linea_evento(ev):
    hora = "hora por confirmar" if ev["sin_hora"] else ev["hora"].astimezone(TZ).strftime("%H:%M")
    marca = MARCA_IMPACTO.get(ev.get("impacto"), "")
    return (f'{marca}{BANDERA.get(ev["divisa"], "")} {hora} · {ev["divisa"]} · {ev["titulo"]} '
            f'(previsto {ev["previsto"]} · anterior {ev["anterior"]})')


def lista_eventos(eventos, vacio):
    return "\n".join(linea_evento(e) for e in eventos) or vacio


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


def cotiza_ahora(d):
    """True si el dato es de la sesión en curso (no un cierre anterior)."""
    return not d["fuente"].startswith("MoF") and dato_fresco(d, 90)


def fmt_valor(k, v):
    i = INSTRUMENTOS[k]
    return f"{v:.3f} %" if i["tipo"] == "yield" else f"{v:.{i['dec']}f}"


def linea_mercado(k, d):
    i = INSTRUMENTOS[k]
    c = cambio(k, d)
    txt = f'{i["nombre"]}: {fmt_valor(k, d["ultimo"])}'
    if c is not None:
        txt += f" ({c:+.1f} pb)" if i["tipo"] == "yield" else f" ({c:+.2f} %)"
    # Mercado cerrado (p. ej. el DAX de contado antes de las 09:00): el dato es el último cierre
    if not d["fuente"].startswith("MoF") and not dato_fresco(d, 90):
        txt += " · cierre anterior"
    return txt


def bloque_mercado(datos):
    """Lo que ve el usuario (plegado al final del mensaje)."""
    lineas = [linea_mercado(k, datos[k]) for k in ORDEN if k in datos]
    if "US10Y" in datos and "DE10Y" in datos:
        dif = (datos["US10Y"]["ultimo"] - datos["DE10Y"]["ultimo"]) * 100
        lineas.append(f"Diferencial 10A EE. UU.-Alemania: {dif:.0f} pb")
    senti = [linea_mercado(k, datos[k]) for k in ORDEN_SENTIMIENTO if k in datos]
    if senti:
        lineas += ["", "Sentimiento:"] + senti
    return "\n".join(lineas)


def bloque_claude(datos):
    """Todos los datos para Claude, con indicaciones de lectura para que no los malinterprete."""
    lineas = ["Bonos, divisas y DAX (valor actual y cambio respecto al cierre anterior):"]
    lineas += [linea_mercado(k, datos[k]) for k in ORDEN if k in datos]
    faltan = [INSTRUMENTOS[k]["nombre"] for k in ORDEN if k not in datos]
    if "US2Y" in datos and "US10Y" in datos:
        pendiente = (datos["US10Y"]["ultimo"] - datos["US2Y"]["ultimo"]) * 100
        estado_curva = "positiva: la curva NO está invertida" if pendiente > 0 else "negativa: curva invertida"
        lineas.append(f"Pendiente 2A-10A EE. UU.: {pendiente:+.0f} pb ({estado_curva})")
    if "US10Y" in datos and "DE10Y" in datos:
        dif = (datos["US10Y"]["ultimo"] - datos["DE10Y"]["ultimo"]) * 100
        lineas.append(f"Diferencial 10A EE. UU.-Alemania: {dif:.0f} pb")
    if "US10Y" in datos and "JP10Y" in datos:
        dif = (datos["US10Y"]["ultimo"] - datos["JP10Y"]["ultimo"]) * 100
        lineas.append(f"Diferencial 10A EE. UU.-Japón: {dif:.0f} pb")
    lineas.append("")
    lineas.append("Sentimiento de riesgo (solo contexto):")
    senti = [linea_mercado(k, datos[k]) for k in ORDEN_SENTIMIENTO if k in datos]
    lineas += senti or ["sin datos"]
    faltan += [INSTRUMENTOS[k]["nombre"] for k in ORDEN_SENTIMIENTO if k not in datos]
    if faltan:
        lineas.append("")
        lineas.append(f"SIN DATOS (no hables de ellos): {', '.join(faltan)}. "
                      "Tampoco tienes datos del S&P 500 ni del Nasdaq de contado.")
    return "\n".join(lineas)


def foto_mercado(datos):
    """Valores de los instrumentos que cotizan ahora (para medir la reacción a un dato)."""
    return {k: d["ultimo"] for k, d in datos.items() if cotiza_ahora(d)}


def lineas_reaccion(foto, datos):
    lineas = []
    for k in ORDEN + ORDEN_SENTIMIENTO:
        if k not in foto or k not in datos or not cotiza_ahora(datos[k]):
            continue
        antes, ahora = foto[k], datos[k]["ultimo"]
        if INSTRUMENTOS[k]["tipo"] == "yield":
            delta = f"{(ahora - antes) * 100:+.1f} pb"
        else:
            delta = f"{(ahora / antes - 1) * 100:+.2f} %" if antes else "—"
        lineas.append(f'{INSTRUMENTOS[k]["nombre"]}: {fmt_valor(k, antes)} → {fmt_valor(k, ahora)} ({delta})')
    return lineas


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
PROMPT_SISTEMA = """Eres un analista macro que asiste a un trader intradía de EUR/USD, USD/JPY y GER 40 (DAX)
que también sigue el NASDAQ 100 y el S&P 500. No opera la publicación de los datos: entra como pronto
15 minutos después, cuando pasa la primera reacción. Le interesa hacia dónde puede ir el precio después
del ruido inicial y por qué, apoyado en datos.

REGLAS OBLIGATORIAS
1. Lo único que sabes del mercado de hoy son los datos de este mensaje. No menciones noticias, declaraciones
   de bancos centrales, expectativas de tipos, posicionamiento, flujos ni ningún hecho de actualidad que no
   aparezca en los datos. Tu conocimiento general tiene fecha de corte y está desfasado: no lo uses para
   describir la situación actual.
2. Puedes usar conocimiento general sobre cómo suele reaccionar el mercado (por ejemplo, que un IPC por encima
   de lo previsto suele subir las rentabilidades), pero preséntalo como tendencia habitual ("suele",
   "habitualmente"), nunca como algo que esté pasando hoy.
3. Toda afirmación sobre el mercado de hoy debe apoyarse en una cifra de los datos, y la citas.
4. Si algo no se puede valorar con los datos, dilo en pocas palabras o no lo menciones. Nunca lo supongas.
   En particular: no tienes el S&P 500 ni el Nasdaq de contado, solo sus futuros si aparecen en los datos.
5. No inventes causas de los movimientos.
6. Sin relleno. Las longitudes que se te piden son MÁXIMOS, no objetivos: si hay poco que decir, di poco.
   Nada de frases genéricas que valdrían para cualquier día.
7. Nunca des órdenes de compra o venta: describe escenarios y qué los confirmaría o invalidaría.
8. Si las señales se contradicen, dilo con las cifras.

CÓMO CALIFICAR LOS MOVIMIENTOS DEL DÍA (no exageres)
- Bonos EE. UU.: menos de 3 pb normal; 3-7 pb notable; 8 pb o más fuerte. Bund y JGB: menos de 3 pb normal;
  3-5 pb notable; más de 5 pb fuerte.
- Divisas y DXY: menos de 0,3 % normal; 0,3-0,6 % notable; más de 0,6 % fuerte.
- DAX y futuros de índices: menos de 0,5 % normal; 0,5-1 % notable; más de 1 % fuerte.
- Petróleo y oro: menos de 1 % normal; 1-2 % notable; más de 2 % fuerte.
- VIX (nivel): por debajo de 15 calma; 15-20 normal; 20-30 tensión; por encima de 30 miedo.

LECTURA DE LOS DATOS
- Pendiente 2A-10A positiva = curva NO invertida. Solo es "invertida" si es negativa.
- Si sube el 2A más que el 10A la curva se aplana; si sube más el 10A, se empina. Fíjate en las cifras.
- Un dato marcado "cierre anterior" es el último cierre, no el precio actual: no lo trates como movimiento de hoy.
- La relación bonos-dólar no siempre se cumple: si las rentabilidades suben por miedo (inflación, deuda,
  petróleo) el dólar puede caer. Señálalo si el DXY no acompaña a los bonos.
- GER 40: suele moverse con el apetito de riesgo global (futuros USA, VIX); lo presionan las subidas fuertes
  del Bund; un euro muy fuerte perjudica a los exportadores alemanes. Son tendencias, no reglas fijas.

Texto plano, sin markdown, sin asteriscos ni almohadillas. Frases cortas. Español."""


def preguntar_claude(prompt, max_tokens=900):
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
            timeout=90,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"]).strip()
    except Exception as e:
        print(f"AVISO: fallo al llamar a Claude ({e}). Envío el mensaje sin análisis.")
        return None


# ----------------------------------------------------------------- mensajes
def montar(cabecera, analisis, datos_txt, titulo_datos="Datos de mercado"):
    """cabecera: HTML ya escapado. analisis y datos_txt: texto plano.
    Con análisis, los datos van al final en un bloque plegado; sin análisis, se muestran abiertos."""
    def construir(a):
        partes = [cabecera]
        if a:
            partes.append(esc(a))
            if datos_txt:
                partes.append(f"<blockquote expandable>📊 {esc(titulo_datos)}\n{esc(datos_txt)}</blockquote>")
        elif datos_txt:
            partes.append(f"<b>{esc(titulo_datos)}</b>\n{esc(datos_txt)}")
        return "\n\n".join(partes)

    texto = construir(analisis)
    while len(texto) > LIMITE_TELEGRAM and analisis:
        analisis = analisis[:max(0, len(analisis) - (len(texto) - LIMITE_TELEGRAM) - 20)].rstrip() + "…"
        texto = construir(analisis if len(analisis) > 1 else None)
        if len(analisis) <= 1:
            break
    return texto


INSTRUCCION_AFECTADOS = ("Afectados según la divisa del dato: USD -> EUR/USD, USD/JPY, futuros S&P 500/Nasdaq y, "
                         "por contagio, GER 40; EUR/Alemania o BCE -> EUR/USD y GER 40; JPY -> USD/JPY.")


def mensaje_alerta(grupo, mercado):
    hora_local = grupo[0]["hora"].astimezone(TZ).strftime("%H:%M")
    minutos = max(0, round((grupo[0]["hora"] - ahora_utc()).total_seconds() / 60))
    datos = "\n".join(linea_evento(e) for e in grupo)
    mercado_claude = bloque_claude(mercado) if mercado else "sin datos de mercado"

    analisis = preguntar_claude(
        f"""Ahora: {reloj()}.
En {minutos} minutos (a las {hora_local} hora de Madrid) se publica:
{datos}

Mercado ahora:
{mercado_claude}

Escribe el análisis (máximo 250 palabras; la extensión la marcan los datos, no rellenes) con estas partes:
Qué es: qué mide y por qué mueve el mercado. Si previsto y anterior difieren, qué supone esa diferencia.
Contexto ahora: qué están haciendo los bonos, el DXY y el apetito de riesgo (VIX, futuros), con cifras,
y qué dirección parece estar descontando el mercado antes del dato. Si no hay dirección clara, dilo.
Si sale POR ENCIMA de lo previsto: reacción habitual y si encajaría con el contexto actual (más probable
que el movimiento continúe pasada la reacción inicial) o iría contra él (más riesgo de giro).
Si sale POR DEBAJO: lo mismo.
Si sale EN LÍNEA: qué suele pasar.
(Ojo: en paro y peticiones de subsidio, un número por encima de lo previsto es MALO para la divisa.)
{INSTRUCCION_AFECTADOS}
Si hay varios datos a la vez, céntrate en el que más mueve el mercado y di cómo combinarlos si se contradicen.""",
        max_tokens=900,
    )
    cabecera = f"⚠️ <b>ALERTA MACRO · {hora_local} (en {minutos} min)</b>\n\n{esc(datos)}"
    return montar(cabecera, analisis, bloque_mercado(mercado) if mercado else "")


def mensaje_reaccion(r, mercado):
    hora_evento = datetime.fromisoformat(r["hora"])
    hora_local = hora_evento.astimezone(TZ).strftime("%H:%M")
    minutos = round((ahora_utc() - hora_evento).total_seconds() / 60)
    eventos = "\n".join(r["eventos"])
    movimiento = "\n".join(lineas_reaccion(r["foto"], mercado))

    analisis = preguntar_claude(
        f"""Ahora: {reloj()}.
A las {hora_local} (Madrid) se publicó:
{eventos}
NO conoces la cifra publicada. Han pasado {minutos} minutos.

Movimiento desde justo antes de la publicación hasta ahora:
{movimiento}

Mercado ahora:
{bloque_claude(mercado)}

Escala para movimientos en estos minutos: bonos EE. UU. menos de 2 pb irrelevante, 2-5 pb moderado, más de 5 pb
fuerte; divisas y DXY menos de 0,1 % irrelevante, 0,1-0,3 % moderado, más de 0,3 % fuerte; DAX y futuros menos
de 0,2 % irrelevante, 0,2-0,6 % moderado, más de 0,6 % fuerte.

Escribe (máximo 200 palabras; si la reacción es irrelevante, dilo en 2-3 frases y para):
Lectura del mercado: según los movimientos, ¿el mercado lo ha leído como positivo o negativo para la divisa
del dato? Cita las cifras. No digas si el dato salió por encima o por debajo de lo previsto: no lo sabes.
Intensidad y coherencia: fuerte, moderada o irrelevante según la escala; ¿bonos, DXY y pares van en la misma
dirección o hay contradicción?
Qué vigilar ahora: si la reacción va a favor o en contra de cómo venía el día (cambio respecto al cierre
anterior) y qué implicaría para EUR/USD, USD/JPY, GER 40 y futuros USA.""",
        max_tokens=700,
    )
    cabecera = f"🔁 <b>Reacción al dato · {hora_local} (+{minutos} min)</b>\n\n{esc(eventos)}"
    datos = f"Movimiento desde antes del dato:\n{movimiento}\n\nMercado ahora:\n{bloque_mercado(mercado)}"
    return montar(cabecera, analisis, datos, "Datos")


def mensaje_resumen(eventos_hoy, mercado):
    local = ahora_utc().astimezone(TZ)
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    fecha = f"{dias[local.weekday()]} {local:%d/%m}"
    agenda = lista_eventos(eventos_hoy, f"Hoy no hay eventos de impacto alto ni medio de {', '.join(DIVISAS)}.")
    analisis = preguntar_claude(
        f"""Ahora: {reloj()}.
Agenda de hoy (horas de Madrid). 🔴 = impacto alto, 🟠 = impacto medio:
{agenda}

Mercado ahora:
{bloque_claude(mercado) if mercado else 'sin datos'}

Escribe el briefing matinal (máximo 250 palabras; si el día da para poco, escribe poco):
1) Evento o eventos clave del día y por qué (los de impacto medio solo si pueden mover algo, y di cuál).
2) Qué dicen los bonos (EE. UU., Alemania y Japón) y el sentimiento de riesgo sobre el USD, el EUR y el JPY hoy,
   con cifras.
3) Franjas horarias a vigilar para EUR/USD, USD/JPY, GER 40 e índices USA, ligadas a la agenda.""",
        max_tokens=900,
    )
    cabecera = f"☀️ <b>Agenda macro · {fecha}</b>\n\n{esc(agenda)}"
    return montar(cabecera, analisis, bloque_mercado(mercado) if mercado else "")


def mensaje_pre_fra(mercado, eventos_resto):
    agenda = lista_eventos(eventos_resto, "Sin eventos de impacto alto ni medio el resto del día.")
    analisis = preguntar_claude(
        f"""Ahora: {reloj()}. Faltan unos 15 minutos para la apertura de Xetra (09:00 Madrid).
Mercado:
{bloque_claude(mercado)}

Eventos que quedan hoy (🔴 alto, 🟠 medio):
{agenda}

Escribe (máximo 250 palabras; si hay poco que decir, escribe poco): qué sesgo sugieren el Bund y el diferencial
EE. UU.-Alemania para el EUR y el GER 40 en la sesión europea, qué dice el sentimiento de riesgo (futuros USA,
VIX), si EUR/USD y DAX apuntan en la misma dirección o no, y qué eventos del día pueden romper ese sesgo.
Si el GER 40 aparece como "cierre anterior", di que aún no hay precio de hoy y no deduzcas un gap.""",
        max_tokens=900,
    )
    cabecera = (f"🇩🇪 <b>Antes de Fráncfort · {ahora_utc().astimezone(TZ):%H:%M}</b>\n\n"
                f"<b>Resto del día</b>\n{esc(agenda)}")
    return montar(cabecera, analisis, bloque_mercado(mercado))


def mensaje_pre_ny(mercado, eventos_tarde):
    agenda = lista_eventos(eventos_tarde, "Sin eventos de impacto alto ni medio el resto del día.")
    analisis = preguntar_claude(
        f"""Ahora: {reloj()}. Faltan 30 minutos para la apertura de Wall Street.
Mercado:
{bloque_claude(mercado)}

Eventos que quedan hoy (🔴 alto, 🟠 medio):
{agenda}

Escribe (máximo 250 palabras; si hay poco que decir, escribe poco): qué sesgo sugieren los bonos para USD y JPY
en la sesión de Nueva York, si el DXY confirma o contradice a los bonos, qué dice el sentimiento de riesgo
(futuros, VIX, petróleo, oro) y qué implica para EUR/USD, USD/JPY, futuros USA y el GER 40 (que cierra a las
17:30 Madrid). Relaciónalo con los eventos que quedan.""",
        max_tokens=900,
    )
    cabecera = (f"🗽 <b>Antes de Nueva York · {ahora_utc().astimezone(TZ):%H:%M}</b>\n\n"
                f"<b>Resto del día</b>\n{esc(agenda)}")
    return montar(cabecera, analisis, bloque_mercado(mercado))


def mensaje_alerta_mercado(alertas, mercado):
    analisis = preguntar_claude(
        f"""Ahora: {reloj()}.
Alertas de mercado ahora mismo:
{chr(10).join(alertas)}

Mercado:
{bloque_claude(mercado)}

Escribe (máximo 120 palabras): qué implica para USD, EUR, JPY, EUR/USD, USD/JPY, GER 40 y futuros USA, y si el
resto del mercado acompaña o no al movimiento (con cifras). No inventes la causa.""",
        max_tokens=450,
    )
    cabecera = "📈 <b>ALERTA DE MERCADO</b>\n\n" + esc("\n".join(alertas))
    return montar(cabecera, analisis, bloque_mercado(mercado))


def enviar_telegram(texto):
    silencioso = en_silencio(ahora_utc().astimezone(TZ))
    if DRY_RUN:
        print(f"----- [SIMULACIÓN] mensaje a Telegram{' (silencioso)' if silencioso else ''} -----\n"
              + texto + "\n-------------------------------------------")
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    base = {"chat_id": TELEGRAM_CHAT_ID, "disable_web_page_preview": True, "disable_notification": silencioso}
    r = requests.post(url, json={**base, "text": texto, "parse_mode": "HTML"}, timeout=20)
    if r.ok:
        return True
    print(f"ERROR Telegram: {r.status_code} {r.text}")
    if r.status_code == 400:                       # formato rechazado: reintento en texto plano
        plano = html.unescape(re.sub(r"<[^>]+>", "", texto))[:4000]
        r = requests.post(url, json={**base, "text": plano}, timeout=20)
        if r.ok:
            print("Enviado en texto plano tras el error de formato.")
            return True
        print(f"ERROR Telegram (texto plano): {r.status_code} {r.text}")
    return False


# ----------------------------------------------------------------- principal
def main():
    if "--prueba" in sys.argv:
        datos = obtener_mercado()
        print("Datos de mercado leídos:")
        for k, d in datos.items():
            print(f"  {k}: {d}")
        faltan = [INSTRUMENTOS[k]["nombre"] for k in INSTRUMENTOS if k not in datos]
        texto = "✅ Tu agente de alertas macro está conectado correctamente."
        if datos:
            fuentes = sorted({d["fuente"] for d in datos.values()})
            texto += f"\n\n<b>Prueba de datos de mercado</b>\n{esc(bloque_mercado(datos))}"
            texto += f"\n\nFuentes: {esc(', '.join(fuentes))}"
        if faltan:
            texto += f"\n\n⚠️ No se pudieron leer: {esc(', '.join(faltan))}"
        ok = enviar_telegram(texto)
        sys.exit(0 if ok else 1)

    if not DRY_RUN and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en los Secrets de GitHub.")

    estado = cargar_json(ESTADO_FILE, {})
    estado.setdefault("avisados", [])
    reacciones = estado.setdefault("reacciones", {})
    eventos = eventos_relevantes(obtener_calendario())
    agenda_ev = [e for e in eventos if e["impacto"] in IMPACTOS_AGENDA]
    ahora = ahora_utc()
    local = ahora.astimezone(TZ)
    ny = ahora.astimezone(TZ_NY)                     # hora de Nueva York (para el resumen pre-NY)
    hoy = local.date().isoformat()
    laborable = local.weekday() < 5

    # Datos de mercado (solo entre semana; el fin de semana el mercado está cerrado)
    mercado = obtener_mercado() if laborable else {}

    def restantes_hoy():
        return [e for e in agenda_ev if e["hora"] > ahora and e["hora"].astimezone(TZ).date() == local.date()]

    # 1) Agenda matinal
    if laborable and estado.get("resumen_enviado") != hoy and \
            (local.hour, local.minute) >= hora_hhmm(HORA_RESUMEN) and local.hour < 12:
        de_hoy = [e for e in agenda_ev if e["hora"].astimezone(TZ).date() == local.date()]
        if enviar_telegram(mensaje_resumen(de_hoy, mercado)):
            estado["resumen_enviado"] = hoy
            print("Resumen matinal enviado")

    # 2) Resumen antes de Fráncfort (apertura de Xetra)
    if laborable and mercado and estado.get("prefra_enviado") != hoy and \
            (local.hour, local.minute) >= hora_hhmm(HORA_PRE_FRA) and local.hour < 11:
        if enviar_telegram(mensaje_pre_fra(mercado, restantes_hoy())):
            estado["prefra_enviado"] = hoy
            print("Resumen pre-Fráncfort enviado")

    # 3) Resumen antes de Nueva York: se mide en hora de NY, así siempre sale 30 min antes
    #    de la apertura aunque EE. UU. y Europa cambien la hora en fechas distintas.
    if laborable and mercado and estado.get("preny_enviado") != hoy and \
            (ny.hour, ny.minute) >= hora_hhmm(HORA_PRE_NY) and ny.hour < 12:
        if enviar_telegram(mensaje_pre_ny(mercado, restantes_hoy())):
            estado["preny_enviado"] = hoy
            print("Resumen pre-NY enviado")

    # 4) Reacción al dato: actualizar la foto previa de los datos que aún no han salido
    #    (así la referencia es la última lectura antes de la publicación) y enviar las que tocan.
    foto = foto_mercado(mercado) if mercado else {}
    for clave, r in list(reacciones.items()):
        hora_ev = datetime.fromisoformat(r["hora"])
        if r.get("enviada"):
            if ahora - hora_ev > timedelta(hours=6):
                del reacciones[clave]
            continue
        if ahora < hora_ev:
            if foto:
                r["foto"] = foto
            continue
        if ahora - hora_ev > timedelta(minutes=MINUTOS_REACCION + 60):
            r["enviada"] = True                      # demasiado tarde: se descarta
            print(f"Reacción descartada por antigua: {r['eventos']}")
            continue
        if ahora - hora_ev >= timedelta(minutes=MINUTOS_REACCION) and mercado:
            if not lineas_reaccion(r["foto"], mercado):
                r["enviada"] = True
                print("Reacción sin datos comparables: no se envía")
                continue
            if enviar_telegram(mensaje_reaccion(r, mercado)):
                r["enviada"] = True
                print(f"Reacción al dato enviada: {r['eventos']}")

    # 5) Alertas 15 min antes de cada evento de alto impacto (agrupa los que salen a la misma hora)
    pendientes = [e for e in eventos
                  if e["impacto"] in IMPACTOS_ALERTA
                  and not e["sin_hora"]
                  and e["id"] not in estado["avisados"]
                  and timedelta(0) < e["hora"] - ahora <= timedelta(minutes=MINUTOS_ANTES + 1)]
    grupos = {}
    for e in pendientes:
        grupos.setdefault(e["hora"], []).append(e)
    for hora_grupo, grupo in sorted(grupos.items()):
        if enviar_telegram(mensaje_alerta(grupo, mercado)):
            estado["avisados"].extend(e["id"] for e in grupo)
            print(f"Alerta enviada: {[e['titulo'] for e in grupo]}")
            if foto:
                reacciones[hora_grupo.isoformat()] = {
                    "hora": hora_grupo.isoformat(),
                    "eventos": [linea_evento(e) for e in grupo],
                    "foto": foto,
                    "enviada": False,
                }

    # 6) Alertas de mercado (bonos, GER 40 y niveles)
    if mercado:
        avisos = alertas_mercado(mercado, estado)
        if avisos and enviar_telegram(mensaje_alerta_mercado(avisos, mercado)):
            print(f"Alerta de mercado enviada: {avisos}")

    if not pendientes:
        print(f"{local:%Y-%m-%d %H:%M} Madrid · sin eventos en los próximos {MINUTOS_ANTES} min")
    if mercado:
        print(bloque_mercado(mercado))

    estado["avisados"] = estado["avisados"][-300:]   # que el archivo no crezca sin límite
    guardar_json(ESTADO_FILE, estado)


if __name__ == "__main__":
    main()
