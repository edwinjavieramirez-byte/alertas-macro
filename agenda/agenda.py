"""
Agenda macro semanal de Edwin  ·  genera docs/datos.json para la página web (GitHub Pages).

Se ejecuta desde GitHub Actions (.github/workflows/agenda.yml) en tres modos (variable MODO):
  - "completo" (domingo por la tarde, a mano o al cambiar el script): calendario, precios, sesgo y,
    si falta, la tesis y los escenarios de la semana                                       -> Claude Sonnet
  - "rapido" (L-V cada 15 minutos en horario de datos): fotos del mercado a horas fijas de Nueva York y
    alrededor de cada dato de impacto alto (sin coste), el sesgo pre-NY una vez al día antes de los datos
    de las 8:30 NY (-> Claude Sonnet) y el resultado real de los datos ya publicados (-> Claude Haiku).
    Si no le toca nada, sale sin tocar nada (no llama a la API y no hace commit).
  - "cierre" (L-V tras el cierre de Nueva York, en verano y en invierno): precios, resultados que
    falten y resumen del día                                                                -> Claude Haiku

Además, en los modos completo y cierre (sin coste de API):
  - Validación del sesgo: las mismas reglas aplicadas a los 5 años de histórico (¿cuántas veces acierta?) y
    un registro en vivo (agenda/validacion.json) con el sesgo del domingo y el de cada cierre.
  - Correlaciones móviles de 20 y 60 días entre los activos y sus factores.
  - Sesión de Nueva York (agenda/intradia.json): rango esperado, cierre de la sesión, reacción a cada dato y
    validación del sesgo pre-NY frente a referencias sencillas (reglas, seguir a Londres, ir contra Londres).

Fuentes:
  Forex Factory (calendario USD y EUR; JPY solo impacto alto) · CNBC / Yahoo / Fed de St. Louis /
  Ministerio de Finanzas de Japón (precios) · API de Claude con búsqueda web (resultados, Fed/BCE/BoJ y redacción)

No hace falta tocar este archivo. Lo configurable está en Settings → Secrets and variables → Actions:
  ANTHROPIC_API_KEY (secret, el mismo que usan las alertas)
  AGENDA_MODELO_SEMANAL (variable opcional; por defecto claude-sonnet-5-5)
  AGENDA_MODELO_DIARIO  (variable opcional; por defecto claude-haiku-5-5)
  AGENDA_JPY_INTERVENCION (variable opcional; por defecto 158): nivel de USD/JPY a partir del cual se avisa
    del riesgo de intervención de Japón
"""

import bisect
import json
import math
import os
import re
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------- configuración
# .strip(): si la clave se pegó con un espacio o un salto de línea al final, la API la rechaza
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
MODELO_SEMANAL = os.getenv("AGENDA_MODELO_SEMANAL") or "claude-sonnet-5-5"   # redacta la tesis del domingo
MODELO_DIARIO = os.getenv("AGENDA_MODELO_DIARIO") or "claude-haiku-5-5"       # resultados y resumen del día
MODELO_RESPALDO = "claude-haiku-4-5"                                           # si el anterior falla
# Precios de la API en dólares por millón de tokens (entrada, salida). Búsqueda web: 10 $ cada 1.000.
PRECIOS = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50), "claude-haiku-4-5": (1.0, 5.0),
           "claude-opus-5-5": (4.0, 20.0)}
PRECIO_BUSQUEDA = 0.01
GASTOS = []          # lo que cuesta cada llamada de esta ejecución
FORZAR_SEMANAL = os.getenv("FORZAR_SEMANAL") == "1"      # 1 = rehace tesis y escenarios aunque ya existan
SIN_CLAUDE = os.getenv("SIN_CLAUDE") == "1"              # 1 = no llama a la API (pruebas)
MODO = (os.getenv("MODO") or "completo").strip().lower()  # completo | rapido | cierre


def _num_env(nombre, defecto):
    try:
        return float(str(os.getenv(nombre) or "").replace(",", "."))
    except ValueError:
        return defecto


# USD/JPY: nivel y subida semanal a partir de los que se avisa del riesgo de intervención del Ministerio de
# Finanzas de Japón. Es un aviso, no vota en el sesgo: nadie sabe cuándo intervienen. Referencias: 2022
# (~146 y ~152) y 2024 (~160-162).
JPY_INTERVENCION = _num_env("AGENDA_JPY_INTERVENCION", 158.0)
JPY_SUBIDA_RAPIDA = 2.5          # % en la semana

# Búsqueda del dato real: el intento n se hace pasados ESPERAS_MIN[n] minutos desde la publicación
# (la cifra tarda unos minutos en aparecer en la web) y nunca dos intentos con menos de 12 minutos.
ESPERAS_MIN = [5, 20, 45, 90, 180]
MIN_ENTRE_INTENTOS = 12

TZ = ZoneInfo("Europe/Madrid")
NY = ZoneInfo("America/New_York")
RAIZ = Path(__file__).resolve().parent.parent
DOCS = RAIZ / "docs"
DATOS_FILE = DOCS / "datos.json"
SEMANAS_DIR = DOCS / "semanas"
HISTORICO_FILE = Path(__file__).resolve().parent / "historico.json"
VALIDACION_FILE = Path(__file__).resolve().parent / "validacion.json"     # registro en vivo del sesgo

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"             # Reserva Federal de St. Louis
# Históricos diarios de la Fed (Yahoo bloquea a GitHub con el error 429). Cierres del día hábil anterior.
FRED = {"US2Y": "DGS2", "US10Y": "DGS10", "SPX": "SP500", "NDX": "NASDAQ100", "VIX": "VIXCLS",
        "EURUSD": "DEXUSEU", "USDJPY": "DEXJPUS", "WTI": "DCOILWTICO"}
# El DXY no está en la Fed: se reconstruye con su fórmula oficial a partir de los tipos de cambio diarios
DXY_CESTA = {"DEXUSEU": -0.576, "DEXJPUS": 0.136, "DEXUSUK": -0.119, "DEXCAUS": 0.091,
             "DEXSDUS": 0.042, "DEXSZUS": 0.036}
# Curva oficial del Ministerio de Finanzas de Japón (la Fed solo tiene el bono japonés en mensual):
# el histórico completo y el mes en curso. Solo se usa para rellenar el histórico del JGB 10A.
MOF_URLS = ["https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv",
            "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"]
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

DIVISAS = ["USD", "EUR", "JPY"]
# Impactos por divisa. Japón solo alto: sus datos medios salen de madrugada y no mueven la sesión de Nueva York.
IMPACTOS = {"USD": ["High", "Medium"], "EUR": ["High", "Medium"], "JPY": ["High"]}

# Instrumentos. tipo "yield" = rentabilidad en % (cambios en pb); "precio" = cambios en %.
INSTRUMENTOS = {
    "DXY":    {"nombre": "DXY", "grupo": "dolar", "tipo": "precio", "dec": 2,
               "cnbc": ".DXY", "yahoo": "DX-Y.NYB"},
    "EURUSD": {"nombre": "EUR/USD", "grupo": "dolar", "tipo": "precio", "dec": 4,
               "cnbc": "EUR=", "yahoo": "EURUSD=X"},
    "USDJPY": {"nombre": "USD/JPY", "grupo": "dolar", "tipo": "precio", "dec": 2,
               "cnbc": "JPY=", "yahoo": "JPY=X"},
    "US2Y":   {"nombre": "Bono EE. UU. 2A", "grupo": "tipos", "tipo": "yield", "dec": 3,
               "cnbc": "US2Y", "yahoo": "2YY=F"},
    "US10Y":  {"nombre": "Bono EE. UU. 10A", "grupo": "tipos", "tipo": "yield", "dec": 3,
               "cnbc": "US10Y", "yahoo": "^TNX"},
    "DE10Y":  {"nombre": "Bund 10A", "grupo": "tipos", "tipo": "yield", "dec": 3,
               "cnbc": "DE10Y-DE", "yahoo": None},
    # Mismo símbolo que usan las alertas de Telegram (comprobado en producción)
    "JP10Y":  {"nombre": "JGB 10A (Japón)", "grupo": "tipos", "tipo": "yield", "dec": 3,
               "cnbc": "JP10Y", "yahoo": None},
    "SPX":    {"nombre": "S&P 500", "grupo": "indices", "tipo": "precio", "dec": 2,
               "cnbc": ".SPX", "yahoo": "^GSPC"},
    "NDX":    {"nombre": "Nasdaq 100", "grupo": "indices", "tipo": "precio", "dec": 2,
               "cnbc": ".NDX", "yahoo": "^NDX"},
    "VIX":    {"nombre": "VIX", "grupo": "indices", "tipo": "precio", "dec": 2,
               "cnbc": ".VIX", "yahoo": "^VIX"},
    "ORO":    {"nombre": "Oro", "grupo": "materias", "tipo": "precio", "dec": 1,
               "cnbc": "@GC.1", "yahoo": "GC=F"},
    "WTI":    {"nombre": "Crudo WTI", "grupo": "materias", "tipo": "precio", "dec": 2,
               "cnbc": "@CL.1", "yahoo": "CL=F"},
}
ORDEN = ["DXY", "EURUSD", "USDJPY", "US2Y", "US10Y", "DE10Y", "JP10Y", "SPX", "NDX", "VIX", "ORO", "WTI"]

# Traducciones fijas de los títulos más habituales de Forex Factory (Claude completa el resto).
TITULOS_ES = {
    "CPI m/m": "IPC m/m", "CPI y/y": "IPC a/a", "Core CPI m/m": "IPC subyacente m/m",
    "Core CPI y/y": "IPC subyacente a/a", "PPI m/m": "IPP m/m", "Core PPI m/m": "IPP subyacente m/m",
    "Retail Sales m/m": "Ventas minoristas m/m", "Core Retail Sales m/m": "Ventas minoristas subyacentes m/m",
    "Unemployment Claims": "Peticiones de subsidio por desempleo", "Non-Farm Employment Change": "Nóminas no agrícolas (NFP)",
    "Unemployment Rate": "Tasa de paro", "Average Hourly Earnings m/m": "Salario medio por hora m/m",
    "Federal Funds Rate": "Decisión de tipos de la Fed", "FOMC Statement": "Comunicado del FOMC",
    "FOMC Press Conference": "Rueda de prensa del FOMC", "FOMC Meeting Minutes": "Actas del FOMC",
    "Main Refinancing Rate": "Decisión de tipos del BCE", "Monetary Policy Statement": "Comunicado de política monetaria",
    "ECB Press Conference": "Rueda de prensa del BCE", "Advance GDP q/q": "PIB adelantado t/t",
    "Prelim GDP q/q": "PIB preliminar t/t", "Final GDP q/q": "PIB final t/t",
    "Core PCE Price Index m/m": "PCE subyacente m/m", "ISM Manufacturing PMI": "ISM manufacturero",
    "ISM Services PMI": "ISM servicios", "Flash Manufacturing PMI": "PMI manufacturero preliminar",
    "Flash Services PMI": "PMI servicios preliminar", "Empire State Manufacturing Index": "Índice manufacturero Empire State",
    "Philly Fed Manufacturing Index": "Índice manufacturero Fed de Filadelfia",
    "Prelim UoM Consumer Sentiment": "Confianza del consumidor U. Michigan (prel.)",
    "Prelim UoM Inflation Expectations": "Expectativas de inflación U. Michigan (prel.)",
    "CB Consumer Confidence": "Confianza del consumidor Conference Board", "JOLTS Job Openings": "Ofertas de empleo JOLTS",
    "ADP Non-Farm Employment Change": "Empleo privado ADP", "Building Permits": "Permisos de construcción",
    "Housing Starts": "Viviendas iniciadas", "Existing Home Sales": "Ventas de viviendas de segunda mano",
    "New Home Sales": "Ventas de viviendas nuevas", "Durable Goods Orders m/m": "Pedidos de bienes duraderos m/m",
    "Core Durable Goods Orders m/m": "Pedidos de bienes duraderos subyacentes m/m",
    "Industrial Production m/m": "Producción industrial m/m", "Import Prices m/m": "Precios de importación m/m",
    "Crude Oil Inventories": "Inventarios de crudo (EIA)", "Beige Book": "Libro Beige de la Fed",
    "Bank Holiday": "Festivo", "German ZEW Economic Sentiment": "ZEW Alemania (sentimiento económico)",
    "ZEW Economic Sentiment": "ZEW eurozona (sentimiento económico)", "German Ifo Business Climate": "Ifo Alemania (clima empresarial)",
    "German Prelim CPI m/m": "IPC preliminar Alemania m/m", "Final CPI y/y": "IPC final a/a",
    "Core CPI Flash Estimate y/y": "IPC subyacente preliminar a/a", "CPI Flash Estimate y/y": "IPC preliminar a/a",
    "German Flash Manufacturing PMI": "PMI manufacturero preliminar Alemania",
    "German Flash Services PMI": "PMI servicios preliminar Alemania",
    "Trade Balance": "Balanza comercial", "Industrial Production m/m ": "Producción industrial m/m",
    "Treasury Currency Report": "Informe de divisas del Tesoro", "Federal Budget Balance": "Saldo presupuestario federal",
    "NFIB Small Business Index": "Índice NFIB de pequeñas empresas",
    # Japón (solo los de impacto alto)
    "BOJ Policy Rate": "Decisión de tipos del BoJ", "BOJ Press Conference": "Rueda de prensa del BoJ",
    "BOJ Outlook Report": "Informe de perspectivas del BoJ", "BOJ Gov Ueda Speaks": "Discurso del gobernador del BoJ (Ueda)",
    "Tokyo Core CPI y/y": "IPC subyacente de Tokio a/a", "National Core CPI y/y": "IPC subyacente nacional a/a",
    "Tankan Manufacturing Index": "Tankan manufacturero", "Tankan Non-Manufacturing Index": "Tankan no manufacturero",
}

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

DIAGNOSTICO = []


def ocultar(texto):
    """Quita cualquier clave de la API de un texto antes de imprimirlo o guardarlo en datos.json,
    que es público. Así un error nunca puede filtrar la clave."""
    texto = str(texto)
    if ANTHROPIC_API_KEY:
        texto = texto.replace(ANTHROPIC_API_KEY, "[clave oculta]")
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]{8,}", "[clave oculta]", texto)


def aviso(texto):
    texto = ocultar(texto)
    print("AVISO:", texto)
    DIAGNOSTICO.append(texto)


# ----------------------------------------------------------------- utilidades
def cargar_json(path, defecto):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return defecto


def guardar_json(path, data, compacto=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    texto = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compacto else \
        json.dumps(data, ensure_ascii=False, indent=1)
    Path(path).write_text(ocultar(texto), encoding="utf-8")      # última barrera: nunca guardar una clave


def ahora_utc():
    return datetime.now(timezone.utc)


def a_float(valor):
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return None if (isinstance(valor, float) and math.isnan(valor)) else float(valor)
    s = str(valor).strip().replace("%", "").replace("+", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def redondear(x, dec=4):
    return None if x is None else round(x, dec)


def lunes_de_la_semana(hoy):
    """Semana que muestra la página: L-V en curso; el domingo, la que empieza al día siguiente."""
    if hoy.weekday() == 6:
        return hoy + timedelta(days=1)
    return hoy - timedelta(days=hoy.weekday())


def sma(valores, n):
    if len(valores) < n:
        return None
    return sum(valores[-n:]) / n


# ----------------------------------------------------------------- calendario
def obtener_calendario():
    try:
        r = requests.get(FF_URL, timeout=25, headers=UA)
        r.raise_for_status()
        eventos = r.json()
        print(f"Calendario: {len(eventos)} eventos en el feed")
        return eventos
    except Exception as e:
        aviso(f"No se pudo descargar el calendario de Forex Factory ({e})")
        return None


def normalizar_eventos(crudos, previos):
    """Filtra USD/EUR de impacto alto y medio y JPY de impacto alto. Conserva resultados reales ya obtenidos."""
    previos_por_id = {e["id"]: e for e in previos}
    salida = []
    for ev in crudos:
        if ev.get("country") not in DIVISAS:
            continue
        impacto = ev.get("impact")
        if impacto not in IMPACTOS[ev["country"]] and not (impacto == "Holiday" and ev.get("country") == "USD"):
            continue
        fecha = datetime.fromisoformat(ev["date"])
        sin_hora = fecha.astimezone(NY).hour == 0 and fecha.astimezone(NY).minute == 0
        local = fecha.astimezone(TZ)
        ident = f'{ev["country"]}|{ev["title"]}|{fecha.astimezone(timezone.utc):%Y-%m-%dT%H:%M}'
        previo = previos_por_id.get(ident, {})
        titulo = ev["title"].strip()
        hora_ny = fecha.astimezone(NY)
        salida.append({
            "id": ident,
            "divisa": ev["country"],
            "titulo_en": titulo,
            "titulo": previo.get("titulo") or TITULOS_ES.get(titulo, titulo),
            "impacto": {"High": "alto", "Medium": "medio", "Holiday": "festivo"}.get(impacto, impacto),
            "utc": fecha.astimezone(timezone.utc).isoformat(),
            "fecha": local.date().isoformat(),
            "dia": DIAS[local.weekday()],
            "hora": None if sin_hora else local.strftime("%H:%M"),
            "hora_ny": None if sin_hora else hora_ny.strftime("%H:%M"),
            # Ventana operativa de Edwin: sesión de Nueva York (14:00-22:00 Madrid)
            "ventana_ny": (not sin_hora) and 14 <= local.hour < 22,
            "prevision": ev.get("forecast") or None,
            "anterior": ev.get("previous") or None,
            "real": previo.get("real"),
            "sorpresa": previo.get("sorpresa"),
            "comentario": previo.get("comentario"),
            "fuente": previo.get("fuente"),
        })
    return sorted(salida, key=lambda e: e["utc"])


# ----------------------------------------------------------------- precios
def _cnbc(simbolos):
    url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
           f"?symbols={'|'.join(simbolos)}&requestMethod=itv&noform=1&partnerId=2"
           "&fund=1&exthrs=1&output=json")
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    lista = r.json().get("FormattedQuoteResult", {}).get("FormattedQuote", [])
    if isinstance(lista, dict):
        lista = [lista]
    salida = {}
    for q in lista:
        ultimo = a_float(q.get("last"))
        anterior = a_float(q.get("previous_day_closing"))
        if anterior is None and ultimo is not None and a_float(q.get("change")) is not None:
            anterior = ultimo - a_float(q.get("change"))
        if ultimo is not None:
            salida[q.get("symbol")] = {"ultimo": ultimo, "anterior": anterior}
    return salida


def _yahoo_horaria(simbolo):
    """Serie horaria de los últimos 5 días: lista de (datetime_utc, cierre)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(simbolo)}"
    r = requests.get(url, params={"range": "5d", "interval": "60m"}, headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    puntos = []
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        c = a_float(c)
        if c is not None:
            puntos.append((datetime.fromtimestamp(t, timezone.utc), c / 10 if simbolo == "^TNX" and c > 20 else c))
    return puntos


def _fred(serie, desde):
    """Serie diaria de la Fed de St. Louis: {fecha_iso: valor}."""
    r = requests.get(FRED_URL, params={"id": serie, "cosd": desde}, headers=UA, timeout=12)
    r.raise_for_status()
    salida = {}
    for linea in r.text.strip().splitlines()[1:]:
        partes = linea.split(",")
        if len(partes) >= 2:
            v = a_float(partes[1])
            if v is not None:
                salida[partes[0][:10]] = v
    if not salida:
        raise ValueError("serie vacía")
    return salida


def _cnbc_barras(simbolo, desde):
    """Cierres diarios de CNBC (la misma fuente que el precio actual): {fecha_iso: cierre}."""
    url = (f"https://ts-api.cnbc.com/harmony/app/bars/{requests.utils.quote(simbolo, safe='')}/1D/"
           f"{desde:%Y%m%d}000000/{ahora_utc():%Y%m%d}235959/adjusted/EST5EDT.json")
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    barras = ((r.json() or {}).get("barData") or {}).get("priceBars") or []
    salida = {}
    for b in barras:
        t, c = str(b.get("tradeTime") or ""), a_float(b.get("close"))
        if len(t) >= 8 and c is not None:
            salida[f"{t[:4]}-{t[4:6]}-{t[6:8]}"] = c
    if not salida:
        raise ValueError("sin barras")
    return salida


def _yahoo_diario(sesion, simbolo, crumb):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(simbolo)}"
    r = sesion.get(url, params={"range": "5y", "interval": "1d", "crumb": crumb}, timeout=15)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    off = int(res.get("meta", {}).get("gmtoffset") or 0)
    salida = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        c = a_float(c)
        if c is not None:
            salida[datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()] = \
                c / 10 if simbolo == "^TNX" and c > 20 else c
    if not salida:
        raise ValueError("sin datos")
    return salida


def _mof_jgb10(desde):
    """JGB 10A del Ministerio de Finanzas de Japón: {fecha_iso: rentabilidad}. Las fechas antiguas vienen en
    calendario japonés y se ignoran; solo se leen las del tipo AAAA/M/D."""
    salida = {}
    for url in MOF_URLS:
        r = requests.get(url, headers=UA, timeout=25)
        r.raise_for_status()
        col = None
        for linea in r.content.decode("utf-8", "ignore").splitlines():
            celdas = [c.strip() for c in linea.split(",")]
            if "10Y" in celdas:
                col = celdas.index("10Y")
                continue
            m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", celdas[0]) if celdas else None
            if col is None or not m or len(celdas) <= col:
                continue
            f = f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
            v = a_float(celdas[col])
            if v is not None and f >= desde:
                salida[f] = v
    if not salida:
        raise ValueError("sin datos")
    return salida


def rellenar_historico(historico):
    """Solo hace falta UNA vez: si a un activo le faltan días de histórico, se intenta descargar de
    CNBC, después de Yahoo y por último de la Fed. Cada fuente se abandona al primer fallo para no
    perder tiempo. Lo descargado se guarda en historico.json y a partir de ahí basta con el cierre
    propio de cada día."""
    faltan = [k for k in INSTRUMENTOS if len(historico.get(k, {})) < 210]
    if not faltan:
        return
    hoy = ahora_utc().date()
    desde = hoy - timedelta(days=5 * 366)

    def guardar(k, serie, fuente):
        h = historico.setdefault(k, {})
        nuevos = 0
        for f, v in serie.items():
            if f not in h:
                h[f] = v
                nuevos += 1
        print(f"Histórico {k}: {nuevos} días de {fuente}")

    # 1) CNBC
    for k in list(faltan):
        try:
            guardar(k, _cnbc_barras(INSTRUMENTOS[k]["cnbc"], desde), "CNBC")
            faltan.remove(k)
        except Exception as e:
            print(f"CNBC sin histórico ({k}: {e}); paso a Yahoo")
            break
    # 2) Yahoo, con cookie y "crumb" como un navegador
    if any(INSTRUMENTOS[k].get("yahoo") for k in faltan):
        try:
            ses = requests.Session()
            ses.headers.update(UA)
            ses.get("https://fc.yahoo.com", timeout=10)
            crumb = ses.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10).text.strip()
            for k in list(faltan):
                if INSTRUMENTOS[k].get("yahoo"):
                    guardar(k, _yahoo_diario(ses, INSTRUMENTOS[k]["yahoo"], crumb), "Yahoo")
                    faltan.remove(k)
        except Exception as e:
            print(f"Yahoo sin histórico ({e}); paso a la Fed")
    # 3) Fed de St. Louis (y DXY reconstruido con su fórmula)
    if faltan:
        try:
            cache = {}
            for k in list(faltan):
                if k in FRED:
                    cache[FRED[k]] = _fred(FRED[k], desde.isoformat())
                    guardar(k, cache[FRED[k]], "la Fed")
                    faltan.remove(k)
            if "DXY" in faltan:
                cesta = {sid: cache.get(sid) or _fred(sid, (hoy - timedelta(days=420)).isoformat()) for sid in DXY_CESTA}
                comunes = set.intersection(*(set(v) for v in cesta.values()))
                guardar("DXY", {f: 50.14348112 * math.prod(cesta[sid][f] ** w for sid, w in DXY_CESTA.items())
                                for f in comunes}, "la Fed (DXY reconstruido)")
                faltan.remove("DXY")
        except Exception as e:
            print(f"La Fed sin histórico ({e})")
    # 4) Bono japonés: Ministerio de Finanzas de Japón
    if "JP10Y" in faltan:
        try:
            guardar("JP10Y", _mof_jgb10(desde.isoformat()), "el Ministerio de Finanzas de Japón")
            faltan.remove("JP10Y")
        except Exception as e:
            print(f"Ministerio de Finanzas de Japón sin histórico ({e})")
    if faltan:
        print("Aún sin histórico suficiente para: " + ", ".join(faltan) +
              ". Se irá completando con el cierre de cada día; mientras, esos factores del sesgo no cuentan.")


def obtener_mercado(historico, lunes):
    """Precio actual (CNBC) + histórico diario (cierres propios guardados cada día y, para rellenar,
    los de la Fed). Calcula cambio del día, de la semana, medias, distancia al máximo y la serie del gráfico."""
    datos = {}
    try:
        cnbc = _cnbc([i["cnbc"] for i in INSTRUMENTOS.values() if i["cnbc"]])
    except Exception as e:
        aviso(f"CNBC no disponible ({e})")
        cnbc = {}
    rellenar_historico(historico)

    # Serie horaria de Yahoo para los gráficos: si Yahoo bloquea la primera petición, no se insiste
    horarias, yahoo_ok = {}, True
    for k, i in INSTRUMENTOS.items():
        if not (yahoo_ok and i.get("yahoo")):
            continue
        try:
            horarias[k] = _yahoo_horaria(i["yahoo"])
        except Exception as e:
            yahoo_ok = False
            print(f"Yahoo no disponible ({e}): los gráficos usarán cierres diarios")

    inicio_semana = datetime.combine(lunes, datetime.min.time(), TZ).astimezone(timezone.utc)
    ahora_ny = ahora_utc().astimezone(NY)
    dia_habil = ahora_ny.date()
    while dia_habil.weekday() >= 5:
        dia_habil -= timedelta(days=1)

    for k, i in INSTRUMENTOS.items():
        d = {"nombre": i["nombre"], "grupo": i["grupo"], "tipo": i["tipo"], "dec": i["dec"]}
        propio = historico.setdefault(k, {})
        q = cnbc.get(i["cnbc"]) if i["cnbc"] else None
        f = {}
        if q:
            ultimo, anterior = q["ultimo"], q["anterior"]
            propio[dia_habil.isoformat()] = ultimo        # cierre propio (la última lectura del día)
            d["fuente"] = "CNBC"
        elif len(propio) > 1:
            fs = sorted(propio)
            ultimo, anterior = propio[fs[-1]], propio[fs[-2]]
            d["fuente"] = "histórico (cierre anterior)"
        else:
            aviso(f"Sin precio para {k}")
            continue
        for vieja in sorted(propio)[:-1300]:
            del propio[vieja]
        combinado = dict(f)
        combinado.update(propio)                           # lo propio manda sobre la Fed
        fechas = sorted(combinado)
        cierres = [combinado[x] for x in fechas]

        d["ultimo"] = redondear(ultimo, 5)
        d["anterior"] = redondear(anterior, 5)
        d["cambio_dia"] = cambio(i["tipo"], ultimo, anterior)
        ref = [combinado[x] for x in fechas if x < lunes.isoformat()]
        d["cierre_semana_anterior"] = redondear(ref[-1], 5) if ref else None
        d["cambio_semana"] = cambio(i["tipo"], ultimo, d["cierre_semana_anterior"])
        ref2 = [combinado[x] for x in fechas if x < (lunes - timedelta(days=7)).isoformat()]
        d["cambio_semana_anterior"] = cambio(i["tipo"], d["cierre_semana_anterior"], ref2[-1] if ref2 else None)
        d["sma20"] = redondear(sma(cierres, 20), 5)
        d["sma50"] = redondear(sma(cierres, 50), 5)
        d["sma200"] = redondear(sma(cierres, 200), 5)
        if i["grupo"] == "indices" and k != "VIX" and len(cierres) > 200:
            maximo = max(cierres + [ultimo])
            d["maximo_5a"] = maximo
            d["dist_maximo_pct"] = round((ultimo / maximo - 1) * 100, 2)
        h = horarias.get(k) or []
        serie = [c for t, c in h if t >= inicio_semana] or [c for _, c in h]
        if serie:
            d["serie_tipo"] = "horas"
        else:
            serie = cierres[-30:]
            d["serie_tipo"] = "dias"
        d["serie"] = [redondear(c, 5) for c in serie][-140:]
        d["diario"] = [[x, redondear(combinado[x], 5)] for x in fechas[-30:]]
        datos[k] = d

    if "US10Y" in datos and "DE10Y" in datos:
        datos["DIF_US_DE"] = calcular_diferencial(historico, datos, lunes, "DE10Y", "Diferencial 10A EE. UU.-Alemania")
    if "US10Y" in datos and "JP10Y" in datos:
        datos["DIF_US_JP"] = calcular_diferencial(historico, datos, lunes, "JP10Y", "Diferencial 10A EE. UU.-Japón")
    if "US10Y" in datos and "US2Y" in datos:
        datos["PENDIENTE"] = {"nombre": "Pendiente 2A-10A EE. UU.", "grupo": "tipos", "tipo": "pb",
                              "ultimo": round((datos["US10Y"]["ultimo"] - datos["US2Y"]["ultimo"]) * 100, 1)}
    return datos


def cambio(tipo, ultimo, referencia):
    if ultimo is None or referencia in (None, 0):
        return None
    if tipo == "yield":
        return round((ultimo - referencia) * 100, 1)          # puntos básicos
    return round((ultimo / referencia - 1) * 100, 2)          # %


def calcular_diferencial(historico, datos, lunes, otro, nombre):
    """Diferencial en pb entre el 10A de EE. UU. y el de otro país (DE10Y o JP10Y)."""
    us, de = historico.get("US10Y", {}), historico.get(otro, {})
    comunes = sorted(set(us) & set(de))
    actual = (datos["US10Y"]["ultimo"] - datos[otro]["ultimo"]) * 100
    ref = [(us[f] - de[f]) * 100 for f in comunes if f < lunes.isoformat()]
    return {"nombre": nombre, "grupo": "tipos", "tipo": "pb",
            "ultimo": round(actual, 1),
            "cambio_semana": round(actual - ref[-1], 1) if ref else None,
            "serie": [round((us[f] - de[f]) * 100, 1) for f in comunes[-30:]]}


# ----------------------------------------------------------------- sesgo (reglas fijas)
def miles(n, signo=True):
    t = f"{n:+,.0f}" if signo else f"{n:,.0f}"
    return t.replace(",", ".")


def factor(nombre, voto, detalle):
    return {"factor": nombre, "voto": voto, "detalle": detalle}


def sesgo_dolar(m, eventos):
    f = []
    dxy = m.get("DXY")
    if dxy and dxy.get("sma20") and dxy.get("sma50"):
        p = dxy["ultimo"]
        if p > dxy["sma20"] and p > dxy["sma50"]:
            v, t = 1, "por encima de sus medias de 20 y 50 días"
        elif p < dxy["sma20"] and p < dxy["sma50"]:
            v, t = -1, "por debajo de sus medias de 20 y 50 días"
        else:
            v, t = 0, "entre sus medias de 20 y 50 días (sin tendencia clara)"
        f.append(factor("Tendencia DXY", v, f"DXY {p:.2f}, {t} (MM20 {dxy['sma20']:.2f} · MM50 {dxy['sma50']:.2f})"))
    if dxy and dxy.get("cambio_semana") is not None:
        c = dxy["cambio_semana"]
        v = 1 if c >= 0.5 else -1 if c <= -0.5 else 0
        f.append(factor("DXY en la semana", v, f"{c:+.2f} % desde el cierre del viernes (umbral ±0,5 %)"))
    us2 = m.get("US2Y")
    if us2 and us2.get("cambio_semana") is not None:
        c = us2["cambio_semana"]
        v = 1 if c >= 8 else -1 if c <= -8 else 0
        f.append(factor("Bono 2A EE. UU.", v, f"{us2['ultimo']:.3f} %, {c:+.1f} pb en la semana (umbral ±8 pb). "
                                             "Es el plazo que más refleja lo que espera el mercado de la Fed"))
    dif = m.get("DIF_US_DE")
    if dif and dif.get("cambio_semana") is not None:
        c = dif["cambio_semana"]
        v = 1 if c >= 5 else -1 if c <= -5 else 0
        f.append(factor("Diferencial 10A EE. UU.-Alemania", v,
                        f"{dif['ultimo']:.0f} pb, {c:+.1f} pb en la semana (umbral ±5 pb). Si se amplía, favorece al USD frente al EUR"))
    # Solo impacto alto: los de impacto medio tienen resultado desde v5.1, pero no cambian el sesgo
    usd = [e for e in eventos if e["divisa"] == "USD" and e.get("impacto") == "alto"
           and e.get("sorpresa") in ("positiva", "negativa")]
    if usd:
        pos = sum(1 for e in usd if e["sorpresa"] == "positiva")
        neg = sum(1 for e in usd if e["sorpresa"] == "negativa")
        v = 1 if pos - neg >= 2 else -1 if neg - pos >= 2 else 0
        f.append(factor("Datos USD de la semana", v, f"{pos} mejores de lo previsto · {neg} peores"))
    return resumir(f, "USD")


def sorpresas(eventos, divisa):
    """(mejores, peores) de lo previsto entre los datos de esa divisa con resultado."""
    ev = [e for e in eventos if e["divisa"] == divisa and e.get("impacto") == "alto"
          and e.get("sorpresa") in ("positiva", "negativa")]
    return sum(1 for e in ev if e["sorpresa"] == "positiva"), sum(1 for e in ev if e["sorpresa"] == "negativa")


def voto_vix(vix):
    """VIX alto o subiendo con fuerza = miedo (-1); bajo y estable = calma (+1)."""
    nivel, cs = vix["ultimo"], vix.get("cambio_semana")
    if nivel >= 22 or (cs is not None and cs >= 20):
        return -1, "miedo alto o subiendo con fuerza", nivel, cs
    if nivel <= 16 and (cs is None or cs < 10):
        return 1, "calma", nivel, cs
    return 0, "zona intermedia", nivel, cs


def sesgo_yen(m, eventos):
    """USD/JPY NO es el espejo del dólar: el yen pesa solo un 13,6 % del DXY y tiene motores propios
    (diferencial de tipos con Japón, BoJ, refugio en momentos de miedo, intervenciones)."""
    f = []
    x = m.get("USDJPY")
    if x and x.get("sma20") and x.get("sma50"):
        p = x["ultimo"]
        if p > x["sma20"] and p > x["sma50"]:
            v, t = 1, "por encima de sus medias de 20 y 50 días"
        elif p < x["sma20"] and p < x["sma50"]:
            v, t = -1, "por debajo de sus medias de 20 y 50 días"
        else:
            v, t = 0, "entre sus medias de 20 y 50 días (sin tendencia clara)"
        f.append(factor("Tendencia USD/JPY", v, f"{p:.2f}, {t} (MM20 {x['sma20']:.2f} · MM50 {x['sma50']:.2f})"))
    dif = m.get("DIF_US_JP")
    if dif and dif.get("cambio_semana") is not None:
        c = dif["cambio_semana"]
        v = 1 if c >= 5 else -1 if c <= -5 else 0
        f.append(factor("Diferencial 10A EE. UU.-Japón", v,
                        f"{dif['ultimo']:.0f} pb, {c:+.1f} pb en la semana (umbral ±5 pb). "
                        "Es el motor histórico del par: si se amplía, el carry favorece al USD"))
    us2 = m.get("US2Y")
    if us2 and us2.get("cambio_semana") is not None:
        c = us2["cambio_semana"]
        v = 1 if c >= 8 else -1 if c <= -8 else 0
        f.append(factor("Bono 2A EE. UU.", v, f"{us2['ultimo']:.3f} %, {c:+.1f} pb en la semana (umbral ±8 pb)"))
    vix = m.get("VIX")
    if vix:
        v, t, nivel, cs = voto_vix(vix)
        extra = f", {cs:+.1f} % en la semana" if cs is not None else ""
        f.append(factor("Apetito por el riesgo (VIX)", v,
                        f"{nivel:.2f}{extra}: {t}. Con miedo se compra yen (refugio) y USD/JPY cae; con calma "
                        "vuelve el carry trade"))
    pu, nu = sorpresas(eventos, "USD")
    pj, nj = sorpresas(eventos, "JPY")
    if pu + nu + pj + nj:
        neto = (pu - nu) - (pj - nj)
        v = 1 if neto >= 2 else -1 if neto <= -2 else 0
        f.append(factor("Datos de la semana", v, f"EE. UU.: {pu} mejores · {nu} peores; Japón: {pj} mejores · {nj} peores"))
    s = resumir(f, "USD/JPY")
    s["aviso"] = aviso_intervencion(x)
    return s


def aviso_intervencion(x):
    """Texto de aviso (o None). No vota: avisa de que el techo del par lo puede poner Japón, no el mercado."""
    if not x:
        return None
    p, cs = x["ultimo"], x.get("cambio_semana")
    motivos = []
    if p >= JPY_INTERVENCION:
        motivos.append(f"USD/JPY en {p:.2f}, por encima de {JPY_INTERVENCION:.0f}")
    if cs is not None and cs >= JPY_SUBIDA_RAPIDA:
        motivos.append(f"sube un {cs:.1f} % en la semana")
    if not motivos:
        return None
    return ("Riesgo de intervención de Japón: " + " y ".join(motivos) +
            ". En 2022 y 2024 cada intervención bajó el par entre 4 y 6 yenes en pocas horas, sin previo aviso.")


def sesgo_indice(clave, m):
    f = []
    x = m.get(clave)
    nombre = INSTRUMENTOS[clave]["nombre"]
    if x and x.get("sma50") and x.get("sma200"):
        p = x["ultimo"]
        if p > x["sma50"] and p > x["sma200"]:
            v, t = 1, "por encima de sus medias de 50 y 200 días"
        elif p < x["sma50"] and p < x["sma200"]:
            v, t = -1, "por debajo de sus medias de 50 y 200 días"
        else:
            v, t = 0, "entre sus medias de 50 y 200 días"
        f.append(factor("Tendencia", v, f"{miles(p, False)}, {t}"))
    if x and x.get("dist_maximo_pct") is not None:
        d = x["dist_maximo_pct"]
        v = 1 if d >= -2 else -1 if d <= -10 else 0
        f.append(factor("Distancia al máximo", v, f"{d:+.1f} % respecto al máximo de 5 años ({miles(x['maximo_5a'], False)})"))
    vix = m.get("VIX")
    if vix:
        v, t, nivel, cs = voto_vix(vix)
        extra = f", {cs:+.1f} % en la semana" if cs is not None else ""
        f.append(factor("VIX", v, f"{nivel:.2f}{extra}: {t}"))
    us10 = m.get("US10Y")
    if us10 and us10.get("cambio_semana") is not None:
        c = us10["cambio_semana"]
        umbral = 8 if clave == "NDX" else 10
        v = -1 if c >= umbral else 1 if c <= -umbral else 0
        f.append(factor("Bono 10A EE. UU.", v, f"{us10['ultimo']:.3f} %, {c:+.1f} pb en la semana (umbral ±{umbral} pb). "
                                              "Tipos al alza presionan las valoraciones, sobre todo de la tecnología"))
    return resumir(f, nombre)


def etiqueta(total, n):
    if n == 0:
        return "Sin datos suficientes"
    if total >= 3:
        return "Alcista fuerte"
    if total >= 1:
        return "Alcista"
    if total <= -3:
        return "Bajista fuerte"
    if total <= -1:
        return "Bajista"
    return "Neutral"


def resumir(factores, sujeto):
    total = sum(x["voto"] for x in factores)
    return {"sujeto": sujeto, "puntuacion": total, "maximo": len(factores),
            "etiqueta": etiqueta(total, len(factores)), "factores": factores}


def calcular_sesgo(mercado, calendario):
    s = {"usd": sesgo_dolar(mercado, calendario),
         "spx": sesgo_indice("SPX", mercado),
         "ndx": sesgo_indice("NDX", mercado)}
    # EUR/USD es la inversa del dólar
    u = s["usd"]
    s["eurusd"] = {"sujeto": "EUR/USD", "puntuacion": -u["puntuacion"], "maximo": u["maximo"],
                   "etiqueta": etiqueta(-u["puntuacion"], u["maximo"]), "factores": []}
    # USD/JPY tiene su propio cálculo (no es el espejo del dólar)
    s["usdjpy"] = sesgo_yen(mercado, calendario)
    return s


# ----------------------------------------------------------------- validación del sesgo
# Qué instrumento mide si acertó cada sesgo
SUJETOS_VAL = {"usd": "DXY", "eurusd": "EURUSD", "usdjpy": "USDJPY", "spx": "SPX", "ndx": "NDX"}
MAX_REG_SEMANAL, MAX_REG_DIARIO = 260, 600


def preparar_series(historico):
    """{clave: (fechas ordenadas, valores)} para los instrumentos y los dos diferenciales."""
    out = {}
    for k in INSTRUMENTOS:
        h = historico.get(k) or {}
        if h:
            fs = sorted(h)
            out[k] = (fs, [h[f] for f in fs])
    for clave, otro in (("DIF_US_DE", "DE10Y"), ("DIF_US_JP", "JP10Y")):
        us, ot = historico.get("US10Y") or {}, historico.get(otro) or {}
        fs = sorted(set(us) & set(ot))
        if fs:
            out[clave] = (fs, [(us[f] - ot[f]) * 100 for f in fs])
    return out


def mercado_en_fecha(series, corte, lunes):
    """Lo que obtener_mercado habría calculado con los cierres hasta 'corte' (incluido), para una semana que
    empieza el 'lunes' dado. Así el histórico se evalúa con las MISMAS reglas que ves en la página."""
    lun = lunes.isoformat()
    m = {}
    claves = [(k, i["tipo"], i["grupo"]) for k, i in INSTRUMENTOS.items()] + \
             [("DIF_US_DE", "pb", "tipos"), ("DIF_US_JP", "pb", "tipos")]
    for k, tipo, grupo in claves:
        s = series.get(k)
        if not s:
            continue
        n = bisect.bisect_right(s[0], corte)
        if n < 2:
            continue
        cierres = s[1][max(0, n - 1300):n]
        ultimo = cierres[-1]
        j = min(bisect.bisect_left(s[0], lun), n)
        csa = s[1][j - 1] if j > 0 else None
        if tipo == "pb":
            m[k] = {"ultimo": ultimo, "cambio_semana": None if csa is None else round(ultimo - csa, 1)}
            continue
        d = {"ultimo": ultimo, "cambio_semana": cambio(tipo, ultimo, csa),
             "sma20": sma(cierres, 20), "sma50": sma(cierres, 50), "sma200": sma(cierres, 200)}
        if grupo == "indices" and k != "VIX" and len(cierres) > 200:
            d["maximo_5a"] = max(cierres)
            d["dist_maximo_pct"] = round((ultimo / d["maximo_5a"] - 1) * 100, 2)
        m[k] = d
    return m


def foto_sesgo(sesgo):
    """Versión compacta del sesgo para guardarla: puntuación y voto de cada factor. EUR/USD hereda los
    factores del dólar con el signo cambiado."""
    foto = {}
    for k in SUJETOS_VAL:
        x = sesgo.get(k)
        if not x:
            continue
        fuente = x["factores"] if k != "eurusd" else [dict(f, voto=-f["voto"]) for f in sesgo["usd"]["factores"]]
        foto[k] = {"p": x["puntuacion"], "f": {f["factor"]: f["voto"] for f in fuente}}
    return foto


def sesgo_sin_eventos(m):
    return calcular_sesgo(m, [])


def mov_pct(series, k, f_desde, f_hasta):
    """% entre el último cierre <= f_desde y el último cierre <= f_hasta (None si falta alguno o coinciden)."""
    s = series.get(k)
    if not s:
        return None
    i, j = bisect.bisect_right(s[0], f_desde), bisect.bisect_right(s[0], f_hasta)
    if i == 0 or j <= i:
        return None
    return (s[1][j - 1] / s[1][i - 1] - 1) * 100


def siguiente_fecha(series, k, fecha):
    s = series.get(k)
    if not s:
        return None
    i = bisect.bisect_right(s[0], fecha)
    return s[0][i] if i < len(s[0]) else None


def estadistica(registros, sujeto):
    """registros: [{"p": puntuación, "f": {factor: voto}, "mov": % del periodo}].
    Acierto = el signo del sesgo coincide con el del movimiento. Se compara con lo que acertaría el azar
    apostando en la misma dirección con la frecuencia real de subidas y bajadas del activo."""
    regs = [r for r in registros if r.get("mov") is not None and r["mov"] != 0]
    if not regs:
        return None
    p_sube = sum(1 for r in regs if r["mov"] > 0) / len(regs)

    def medir(pares):
        pares = [(v, mov) for v, mov in pares if v]
        if not pares:
            return {"senales": 0}
        ac = sum(1 for v, mov in pares if (v > 0) == (mov > 0))
        azar = sum(p_sube if v > 0 else 1 - p_sube for v, _ in pares) / len(pares)
        return {"senales": len(pares), "aciertos": ac, "pct": round(ac / len(pares) * 100, 1),
                "azar": round(azar * 100, 1), "ventaja": round((ac / len(pares) - azar) * 100, 1)}

    total = medir([(r["p"], r["mov"]) for r in regs])
    alc = [r["mov"] for r in regs if r["p"] > 0]
    baj = [r["mov"] for r in regs if r["p"] < 0]
    total.update({
        "periodos": len(regs), "sube_pct": round(p_sube * 100, 1),
        "mov_alcista": round(sum(alc) / len(alc), 3) if alc else None, "n_alcista": len(alc),
        "mov_bajista": round(sum(baj) / len(baj), 3) if baj else None, "n_bajista": len(baj),
        "fuerte": medir([(r["p"], r["mov"]) for r in regs if abs(r["p"]) >= 3]),
    })
    nombres = []
    for r in regs:
        for nombre in r["f"]:
            if nombre not in nombres:
                nombres.append(nombre)
    total["factores"] = [dict(factor=n, **medir([(r["f"].get(n, 0), r["mov"]) for r in regs])) for n in nombres]
    total["sujeto"] = sujeto
    return total


def nombres_sujetos():
    return {"usd": "Dólar (DXY)", "eurusd": "EUR/USD", "usdjpy": "USD/JPY", "spx": "S&P 500", "ndx": "Nasdaq 100"}


def backtest(historico):
    """Aplica las reglas de precios al histórico de 5 años. No incluye el factor «datos de la semana»
    (no hay histórico de sorpresas), así que mide solo la parte de mercado del sesgo."""
    series = preparar_series(historico)
    base = series.get("DXY") or series.get("EURUSD")
    if not base or len(base[0]) < 260:
        return None
    fechas = base[0]
    inicio = fechas[210]                           # hacen falta 200 días para la media de 200
    diario = {k: [] for k in SUJETOS_VAL}
    semanal = {k: [] for k in SUJETOS_VAL}
    con_cierre = {inst: set(series[inst][0]) for inst in SUJETOS_VAL.values() if inst in series}
    # Diario: sesgo al cierre del día D -> movimiento del día siguiente
    for d in fechas:
        if d < inicio:
            continue
        fd = date.fromisoformat(d)
        foto = foto_sesgo(sesgo_sin_eventos(mercado_en_fecha(series, d, fd - timedelta(days=fd.weekday()))))
        for k, inst in SUJETOS_VAL.items():
            if k not in foto or d not in con_cierre.get(inst, ()):
                continue
            sig = siguiente_fecha(series, inst, d)
            if sig:
                diario[k].append(dict(foto[k], mov=mov_pct(series, inst, d, sig)))
    # Semanal: sesgo del domingo (cierres hasta el viernes) -> movimiento de lunes a viernes
    lunes = date.fromisoformat(inicio) + timedelta(days=7 - date.fromisoformat(inicio).weekday())
    ultimo = date.fromisoformat(fechas[-1])
    while lunes + timedelta(days=4) <= ultimo:
        corte = (lunes - timedelta(days=1)).isoformat()
        foto = foto_sesgo(sesgo_sin_eventos(mercado_en_fecha(series, corte, lunes)))
        for k, inst in SUJETOS_VAL.items():
            if k in foto:
                semanal[k].append(dict(foto[k], mov=mov_pct(series, inst, corte, (lunes + timedelta(days=4)).isoformat())))
        lunes += timedelta(days=7)
    nom = nombres_sujetos()
    return {"desde": inicio, "hasta": fechas[-1],
            "diario": {k: estadistica(v, nom[k]) for k, v in diario.items()},
            "semanal": {k: estadistica(v, nom[k]) for k, v in semanal.items()}}


def registrar_validacion(registro, sesgo, historico, ahora, lunes, hoy_ny):
    """Registro en vivo: guarda el sesgo del domingo (semanal) y el de cada cierre (diario) y, cuando ya
    se conoce el resultado, apunta el movimiento real. Devuelve las estadísticas para la página."""
    series = preparar_series(historico)
    local = ahora.astimezone(TZ)
    sem = registro.setdefault("semanal", [])
    dia = registro.setdefault("diario", [])
    # Foto semanal: solo antes de que abra la semana (domingo o lunes de madrugada), si no está ya
    if local < datetime.combine(lunes, datetime.min.time(), TZ) + timedelta(hours=8) and \
            not any(r["lunes"] == lunes.isoformat() for r in sem):
        sem.append({"lunes": lunes.isoformat(), "creado": local.strftime("%d/%m %H:%M"), "sesgo": foto_sesgo(sesgo)})
        print(f"Validación: guardado el sesgo de apertura de la semana del {lunes}")
    # Foto diaria: en el cierre de Nueva York
    if MODO == "cierre" and not any(r["fecha"] == hoy_ny.isoformat() for r in dia):
        dia.append({"fecha": hoy_ny.isoformat(), "sesgo": foto_sesgo(sesgo)})
        print(f"Validación: guardado el sesgo del cierre del {hoy_ny}")
    # Resultados: un cierre solo cuenta como definitivo si es de un día anterior a hoy (o de hoy, en el cierre)
    limite = hoy_ny.isoformat() if MODO == "cierre" else (hoy_ny - timedelta(days=1)).isoformat()
    for r in sem:
        viernes = (date.fromisoformat(r["lunes"]) + timedelta(days=4)).isoformat()
        if "resultado" not in r and viernes <= limite:
            corte = (date.fromisoformat(r["lunes"]) - timedelta(days=1)).isoformat()
            r["resultado"] = {k: redondear(mov_pct(series, inst, corte, viernes), 3) for k, inst in SUJETOS_VAL.items()}
    for r in dia:
        if "resultado" in r:
            continue
        res = {}
        for k, inst in SUJETOS_VAL.items():
            sig = siguiente_fecha(series, inst, r["fecha"])
            if sig and sig <= limite:
                res[k] = redondear(mov_pct(series, inst, r["fecha"], sig), 3)
        if len(res) == len(SUJETOS_VAL) or (res and r["fecha"] < (hoy_ny - timedelta(days=5)).isoformat()):
            r["resultado"] = res
    registro["semanal"], registro["diario"] = sem[-MAX_REG_SEMANAL:], dia[-MAX_REG_DIARIO:]

    nom = nombres_sujetos()

    def stats(lista):
        return {k: estadistica([dict(r["sesgo"][k], mov=r["resultado"].get(k)) for r in lista
                                if "resultado" in r and k in r["sesgo"]], nom[k]) for k in SUJETOS_VAL}

    ultimas = [{"lunes": r["lunes"], "sesgo": {k: v["p"] for k, v in r["sesgo"].items()},
                "resultado": r.get("resultado")} for r in registro["semanal"][-8:]][::-1]
    return {"semanal": stats(registro["semanal"]), "diario": stats(registro["diario"]),
            "n_semanal": sum(1 for r in registro["semanal"] if "resultado" in r),
            "n_diario": sum(1 for r in registro["diario"] if "resultado" in r),
            "desde": min([r["lunes"] for r in registro["semanal"][:1]] + [r["fecha"] for r in registro["diario"][:1]],
                         default=None),
            "ultimas_semanas": ultimas}


# ----------------------------------------------------------------- correlaciones
# (activo, factor, signo habitual, nombre, qué significa)
PARES_CORR = [
    ("EURUSD", "DIF_US_DE", -1, "EUR/USD · diferencial EE. UU.-Alemania",
     "Lo habitual: si el diferencial se amplía, EUR/USD cae"),
    ("EURUSD", "SPX", 1, "EUR/USD · S&P 500", "Lo habitual: con apetito por el riesgo el dólar se debilita"),
    ("USDJPY", "DIF_US_JP", 1, "USD/JPY · diferencial EE. UU.-Japón", "El motor clásico del par (carry trade)"),
    ("USDJPY", "US10Y", 1, "USD/JPY · bono 10A EE. UU.", "Tipos de EE. UU. al alza empujan el par"),
    ("USDJPY", "VIX", -1, "USD/JPY · VIX", "Con miedo se compra yen y el par cae"),
    ("DXY", "US2Y", 1, "DXY · bono 2A EE. UU.", "El dólar sigue a lo que se espera de la Fed"),
    ("SPX", "US10Y", -1, "S&P 500 · bono 10A EE. UU.",
     "Negativa: los tipos pesan en la bolsa. Positiva: mandan las expectativas de crecimiento"),
    ("NDX", "US10Y", -1, "Nasdaq 100 · bono 10A EE. UU.", "La tecnología es lo más sensible a los tipos"),
]


def _corr(a, b):
    n = len(a)
    if n < 5:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def _cambios(series, k, fechas):
    """Cambio diario: rendimiento logarítmico en precios, diferencia en rentabilidades y diferenciales."""
    d = dict(zip(*series[k]))
    vals = [d[f] for f in fechas]
    es_precio = k in INSTRUMENTOS and INSTRUMENTOS[k]["tipo"] == "precio"
    return [math.log(b / a) if es_precio else b - a for a, b in zip(vals, vals[1:])]


def lectura_corr(c20, signo, previo):
    if c20 is None:
        return "sin datos", "mute"
    t, nivel = None, "mute"
    if abs(c20) < 0.3:
        t = "Débil: ahora apenas se mueven juntos"
    elif (c20 > 0) == (signo > 0):
        if abs(c20) >= 0.6:
            t, nivel = "Fuerte y en el sentido habitual: este factor manda ahora", "good"
        else:
            t, nivel = "Moderada, en el sentido habitual", "info"
    else:
        t, nivel = "Al revés de lo habitual: el mercado se rige por otra cosa", "warn"
    if previo is not None and abs(c20 - previo) >= 0.4:
        t += " · ha cambiado mucho en un mes"
    return t, nivel


def calcular_correlaciones(historico):
    series = preparar_series(historico)
    pares = []
    for a, b, signo, nombre, nota in PARES_CORR:
        x = {"activo": a, "factor": b, "nombre": nombre, "nota": nota, "signo": signo}
        if a in series and b in series:
            comunes = sorted(set(series[a][0]) & set(series[b][0]))[-260:]
            ca, cb = _cambios(series, a, comunes), _cambios(series, b, comunes)
            if len(ca) >= 60:
                x["c20"] = redondear(_corr(ca[-20:], cb[-20:]), 2)
                x["c60"] = redondear(_corr(ca[-60:], cb[-60:]), 2)
                x["c20_hace_1m"] = redondear(_corr(ca[-40:-20], cb[-40:-20]), 2)
                x["serie"] = [redondear(_corr(ca[i - 20:i], cb[i - 20:i]), 2)
                              for i in range(max(20, len(ca) - 119), len(ca) + 1)]
                x["hasta"] = comunes[-1]
        x["lectura"], x["nivel"] = lectura_corr(x.get("c20"), signo, x.get("c20_hace_1m"))
        pares.append(x)
    return {"pares": pares}


# ----------------------------------------------------------------- Claude
PROMPT_SISTEMA = """Eres el analista macro de Edwin, trader de EUR/USD que opera en la sesión de Nueva York (tarde en España),
analiza también USD/JPY y sigue el S&P 500, el Nasdaq 100 y el VIX para medir el sentimiento de riesgo. Escribes en español de España,
directo, sin relleno, sin emojis y sin inventar cifras, fechas ni noticias. Si no tienes un dato, dilo.
Nunca das órdenes de compra o venta: describes escenarios, sesgos y riesgos. El sesgo numérico lo calculan reglas
fijas que se te dan; explícalo y señala si el contexto lo contradice, pero no cambies los números.
Horas siempre en hora de Madrid."""


def llamar_claude(prompt, max_tokens=1500, busquedas=0, modelo=None, tarea="", esfuerzo="low", _reintento=False):
    """Devuelve el texto final o None. Con busquedas>0 activa la búsqueda web del servidor.
    Apunta en GASTOS los tokens y búsquedas consumidos para calcular el coste real.
    Si el modelo gasta todo max_tokens pensando y no escribe nada, se reintenta UNA vez con el triple."""
    if SIN_CLAUDE or not ANTHROPIC_API_KEY:
        return None
    modelos = [modelo or MODELO_DIARIO]
    if MODELO_RESPALDO not in modelos:
        modelos.append(MODELO_RESPALDO)
    for mod in modelos:
        cuerpo = {"model": mod, "max_tokens": max_tokens, "system": PROMPT_SISTEMA,
                  "messages": [{"role": "user", "content": prompt}]}
        if not mod.startswith(("claude-haiku-4", "claude-sonnet-4", "claude-opus-4")):
            # Los modelos 5.x "piensan" antes de responder y ese pensamiento gasta max_tokens:
            # con esfuerzo bajo piensan menos y dejan sitio a la respuesta
            cuerpo["output_config"] = {"effort": esfuerzo}
        if busquedas:
            cuerpo["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": busquedas}]
        uso = {"entrada": 0, "salida": 0, "busquedas": 0}
        partes, motivo = [], None
        try:
            for _ in range(5):        # pause_turn: una búsqueda larga puede pausarse y hay que reanudarla
                r = requests.post("https://api.anthropic.com/v1/messages", headers={
                    "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                    "content-type": "application/json"}, json=cuerpo, timeout=240)
                if r.status_code == 400 and "tools" in cuerpo and "web search" in r.text.lower():
                    aviso("La búsqueda web de la API no está habilitada: sigo sin ella")
                    cuerpo.pop("tools", None)
                    continue
                if r.status_code == 400 and "output_config" in cuerpo and ("effort" in r.text or "output_config" in r.text):
                    cuerpo.pop("output_config", None)          # modelo que no admite "effort": se reintenta sin él
                    continue
                if r.status_code in (400, 404) and "model" in r.text.lower():
                    raise ValueError(f"modelo {mod} no disponible: {r.text[:200]}")
                r.raise_for_status()
                res = r.json()
                u = res.get("usage") or {}
                uso["entrada"] += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) \
                    + (u.get("cache_read_input_tokens") or 0)
                uso["salida"] += u.get("output_tokens") or 0
                uso["busquedas"] += (u.get("server_tool_use") or {}).get("web_search_requests") or 0
                partes.append("".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text"))
                motivo = res.get("stop_reason")
                if motivo == "pause_turn":
                    cuerpo["messages"] = cuerpo["messages"] + [{"role": "assistant", "content": res["content"]}]
                    continue
                break
            pe, ps = PRECIOS.get(mod, (2.0, 10.0))
            usd = uso["entrada"] * pe / 1e6 + uso["salida"] * ps / 1e6 + uso["busquedas"] * PRECIO_BUSQUEDA
            GASTOS.append({"tarea": tarea, "modelo": mod, **uso, "usd": round(usd, 4)})
            texto = "".join(partes).strip()
            print(f"Claude ({mod}) · {tarea}: {uso['entrada']} tokens de entrada, {uso['salida']} de salida, "
                  f"{uso['busquedas']} búsquedas = {usd:.4f} $ · fin: {motivo} · {len(texto)} caracteres")
            if not texto and motivo == "max_tokens" and not _reintento:
                # Se le acabó el espacio pensando antes de escribir: un reintento con el triple de margen
                print(f"Reintento de {tarea} con {max_tokens * 3} tokens")
                return llamar_claude(prompt, max_tokens * 3, busquedas, mod, tarea, esfuerzo, _reintento=True)
            if motivo == "max_tokens":
                aviso(f"Claude se quedó sin espacio al escribir ({tarea}); puede salir incompleto")
            if not texto:
                aviso(f"Claude no devolvió texto ({tarea}, fin: {motivo})")
            return texto or None
        except Exception as e:
            aviso(f"Fallo al llamar a Claude con {mod} ({str(e)[:300]})")
            continue
    return None


def limpiar(obj):
    """Quita las marcas de cita (cite index="..."> que deja la búsqueda web dentro del texto."""
    if isinstance(obj, str):
        return re.sub(r"[ \t]{2,}", " ", re.sub(r"</?cite[^>]*>", "", obj)).strip()
    if isinstance(obj, list):
        return [limpiar(x) for x in obj]
    if isinstance(obj, dict):
        return {k: limpiar(v) for k, v in obj.items()}
    return obj


def extraer_json(texto):
    if not texto:
        return None
    texto = re.sub(r"```(?:json)?", "", texto)
    # Se prueba primero el que aparece antes: una lista con un solo objeto "[{...}]" debe salir como lista
    pares = sorted((("{", "}"), ("[", "]")), key=lambda p: texto.find(p[0]) if p[0] in texto else len(texto))
    for abre, cierra in pares:
        i, j = texto.find(abre), texto.rfind(cierra)
        if i != -1 and j > i:
            try:
                return limpiar(json.loads(texto[i:j + 1]))
            except json.JSONDecodeError:
                continue
    aviso("Claude no devolvió un JSON válido")
    return None


def fecha_es(t):
    """«jueves 22/10/2026 13:50» (strftime %A sale en inglés en GitHub)."""
    t = t.astimezone(TZ)
    return f"{DIAS[t.weekday()].lower()} {t:%d/%m/%Y %H:%M}"


def linea_evento(e):
    hora = e["hora"] or "sin hora"
    return (f'{e["dia"]} {e["fecha"][8:10]}/{e["fecha"][5:7]} {hora} · {e["divisa"]} · {e["titulo_en"]} '
            f'[{e["impacto"]}] prev. {e["prevision"] or "—"} · ant. {e["anterior"] or "—"}'
            + (f' · REAL {e["real"]} ({e["sorpresa"]})' if e.get("real") else ""))


def texto_mercado(m):
    lineas = []
    for k in ORDEN + ["DIF_US_DE", "DIF_US_JP", "PENDIENTE"]:
        x = m.get(k)
        if not x:
            continue
        u = "pb" if x.get("tipo") in ("yield", "pb") else "%"
        partes = [f'{x["nombre"]}: {x["ultimo"]}']
        if x.get("cambio_dia") is not None:
            partes.append(f'día {x["cambio_dia"]:+} {u}')
        if x.get("cambio_semana") is not None:
            partes.append(f'semana {x["cambio_semana"]:+} {u}')
        if x.get("cambio_semana_anterior") is not None:
            partes.append(f'semana anterior {x["cambio_semana_anterior"]:+} {u}')
        if x.get("dist_maximo_pct") is not None:
            partes.append(f'a {x["dist_maximo_pct"]:+} % del máximo')
        lineas.append(" · ".join(partes))
    return "\n".join(lineas)


def texto_sesgo(s):
    return "\n".join(f'{s[k]["sujeto"]}: {s[k]["etiqueta"]} ({s[k]["puntuacion"]:+}/{s[k]["maximo"]}) — ' +
                     "; ".join(f'{f["factor"]} {f["voto"]:+}: {f["detalle"]}' for f in s[k]["factores"]) +
                     (f' · AVISO: {s[k]["aviso"]}' if s[k].get("aviso") else "")
                     for k in s)


def texto_correlaciones(c):
    return "\n".join(f'{x["nombre"]}: 20 días {x["c20"]:+.2f} · 60 días {x["c60"]:+.2f} ({x["lectura"]})'
                      for x in (c or {}).get("pares", []) if x.get("c20") is not None) or "no disponibles"


def texto_fiabilidad(v):
    """Una línea por sesgo con su acierto histórico frente al azar, para que la IA no lo venda como señal fiable."""
    h = (v or {}).get("historico") or {}
    lineas = []
    for hz, t in (("semanal", "domingo → semana"), ("diario", "cierre → día siguiente")):
        for k, x in (h.get(hz) or {}).items():
            if x and x.get("senales"):
                margen = 100 / math.sqrt(x["senales"])
                lineas.append(f'{x["sujeto"]} ({t}): acierta {x["pct"]} % frente a {x["azar"]} % del azar '
                              f'en {x["senales"]} señales ({"ventaja real" if abs(x["ventaja"]) > margen else "sin ventaja demostrada"})')
    return "\n".join(lineas) or "no disponible"


def narrativa_semanal(datos, archivo_prev):
    eventos = datos["calendario"]
    prev_eventos = (archivo_prev or {}).get("calendario", [])
    lunes = datos["semana"]["lunes"]
    prompt = f"""Hoy es {fecha_es(ahora_utc())} (Madrid). Prepara la agenda de la semana del {lunes}.

DATOS DE MERCADO (último · cambio de esta semana · cambio de la semana anterior completa):
{texto_mercado(datos["mercado"])}

SESGO CALCULADO POR REGLAS FIJAS:
{texto_sesgo(datos["sesgo"])}

CORRELACIONES DE LOS CAMBIOS DIARIOS (qué factor está mandando ahora):
{texto_correlaciones(datos.get("correlaciones"))}

FIABILIDAD HISTÓRICA DEL SESGO (mismas reglas aplicadas a los últimos años):
{texto_fiabilidad(datos.get("validacion"))}
Si un sesgo no tiene ventaja demostrada, no lo presentes como señal: úsalo solo como descripción del contexto.

CALENDARIO DE LA SEMANA PASADA (con resultados reales si los hay):
{chr(10).join(linea_evento(e) for e in prev_eventos) or "no disponible: búscalo"}

CALENDARIO DE ESTA SEMANA (USD y EUR de impacto alto y medio, JPY de impacto alto, hora de Madrid):
{chr(10).join(linea_evento(e) for e in eventos) or "no disponible"}

Usa la búsqueda web para: (1) qué movió el dólar, el yen, el S&P 500 y el Nasdaq la semana pasada (datos, Fed, BCE,
BoJ, geopolítica, resultados empresariales, avisos o intervenciones del Ministerio de Finanzas de Japón);
(2) probabilidades actuales de CME FedWatch para la próxima reunión de la Fed y la próxima decisión del BCE y del BoJ;
(3) los discursos de miembros de la Fed y del BCE previstos esta semana y los resultados
empresariales grandes que puedan mover los índices.

Responde SOLO con un JSON válido (sin texto antes ni después) con esta estructura:
{{
 "semana_anterior": {{"titular": "máx. 12 palabras", "resumen": "60-90 palabras: qué pasó y por qué",
                      "claves": [{{"titulo": "dato o noticia", "detalle": "máx. 30 palabras: resultado y efecto en USD/índices"}}]}},
 "tesis": {{"titular": "máx. 14 palabras: la idea central de esta semana", "texto": "70-110 palabras"}},
 "bancos_centrales": {{"fed": "estado actual y probabilidades FedWatch con fecha de la próxima reunión (máx. 45 palabras)",
                       "bce": "estado actual y próxima decisión (máx. 35 palabras)",
                       "boj": "estado actual, próxima decisión y si hay avisos verbales de intervención (máx. 35 palabras)"}},
 "escenarios": [{{"id": "id exacto del evento del calendario", "por_que": "máx. 20 palabras",
                  "si_mayor": "si sale por encima de lo previsto: efecto en USD, EUR/USD, USD/JPY y S&P/Nasdaq (máx. 25 palabras)",
                  "si_menor": "si sale por debajo (máx. 25 palabras)"}}],
 "otros_eventos": [{{"dia": "Lunes..Viernes", "hora": "HH:MM o null", "texto": "discurso, subasta, resultados... (máx. 15 palabras)"}}],
 "lectura_usd": "máx. 60 palabras: explica el sesgo del dólar calculado y si el contexto lo confirma o contradice; implicación para EUR/USD",
 "lectura_jpy": "máx. 60 palabras: explica el sesgo de USD/JPY calculado, si el contexto (BoJ, diferencial, riesgo de intervención) lo confirma o contradice",
 "lectura_indices": "máx. 60 palabras: igual para S&P 500 y Nasdaq (¿pueden buscar nuevo máximo o hay riesgo de retroceso?)",
 "ventana_ny": "máx. 50 palabras: qué días y horas de la sesión de Nueva York (14:00-22:00 Madrid) concentran el riesgo",
 "riesgos": ["máx. 4 riesgos no programados, 12 palabras cada uno"],
 "titulos_es": {{"titulo en inglés del calendario": "traducción breve al español"}}
}}
En "escenarios" incluye solo los 4-7 eventos de impacto alto más importantes. En "si_mayor"/"si_menor" ten en cuenta
que en paro y peticiones de subsidio un número mayor es PEOR para la economía. Si hay un discurso o evento sin
previsión numérica, usa "si_mayor" para el tono duro (hawkish) y "si_menor" para el blando (dovish)."""
    texto = llamar_claude(prompt, max_tokens=16000, busquedas=6, modelo=MODELO_SEMANAL, tarea="tesis semanal",
                          esfuerzo="medium")
    n = extraer_json(texto)
    if isinstance(n, dict) and n.get("tesis"):
        return n
    if texto:
        # Si el JSON salió roto o incompleto, un modelo barato lo recompone con lo ya investigado (sin buscar otra vez)
        aviso("Recomponiendo la tesis con el texto ya investigado")
        pedido = prompt[prompt.index("Responde SOLO con un JSON"):]
        n = extraer_json(llamar_claude(f"Información ya investigada para la semana del {lunes}:\n{texto[-30000:]}\n\n"
                                       f"Con esa información y estos datos:\n{texto_sesgo(datos['sesgo'])}\n\n{pedido}",
                                       max_tokens=6000, modelo=MODELO_DIARIO, tarea="recomponer tesis"))
        if isinstance(n, dict) and n.get("tesis"):
            return n
    return None


def pendientes_resultado(eventos, ahora):
    """Datos de impacto alto y medio ya publicados, sin resultado real, a los que les toca un intento de búsqueda."""
    salida = []
    for e in eventos:
        if e.get("real") is not None or e.get("impacto") not in ("alto", "medio") or not e.get("hora"):
            continue
        if not (e.get("prevision") or e.get("anterior")):       # discursos: no hay cifra que buscar
            continue
        n = e.get("intentos", 0)
        if n >= len(ESPERAS_MIN):
            continue
        publicado = datetime.fromisoformat(e["utc"])
        if publicado < ahora - timedelta(days=6) or ahora - publicado < timedelta(minutes=ESPERAS_MIN[n]):
            continue
        ultimo = e.get("ultimo_intento")
        if ultimo and ahora - datetime.fromisoformat(ultimo) < timedelta(minutes=MIN_ENTRE_INTENTOS):
            continue
        salida.append(e)
    return salida[:14]


def obtener_resultados(eventos):
    """Busca el dato real de los eventos ya publicados que aún no lo tienen."""
    ahora = ahora_utc()
    pendientes = pendientes_resultado(eventos, ahora)
    if not pendientes:
        return 0
    lista = "\n".join(f'{e["id"]} :: {linea_evento(e)}' for e in pendientes)
    prompt = f"""Estos datos macro ya se han publicado (hora de Madrid). Busca en la web el valor REAL publicado de cada uno.
{lista}

Responde SOLO con un JSON: una lista de objetos
{{"id": "id exacto (lo que va antes de ' :: ')", "real": "valor con el mismo formato que la previsión (p. ej. 0.4% o 225K) o null si no lo encuentras",
  "sorpresa": "positiva | negativa | en_linea", "comentario": "máx. 18 palabras: qué implica para la divisa y los índices",
  "fuente": "URL de donde sacaste el dato"}}
"sorpresa" es desde el punto de vista de la economía de esa divisa: mejor de lo previsto = positiva (en paro y
peticiones de subsidio, un número MAYOR es negativa; en inflación, más alta de lo previsto se considera positiva para
la divisa porque aleja bajadas de tipos). Si no hay previsión, compara con el anterior. No inventes: si no lo
encuentras, "real": null. Si el dato se publicó hace pocos minutos, busca en la web de la fuente oficial
o en agencias de noticias, no en calendarios que aún no lo hayan actualizado."""
    # 5000: con muchos datos a la vez (8 PMI el 23/10) 2500 se quedaba justo y el JSON podía salir cortado
    res = extraer_json(llamar_claude(prompt, max_tokens=5000, busquedas=min(6, len(pendientes) + 1),
                                     modelo=MODELO_DIARIO, tarea="resultados reales"))
    if isinstance(res, dict):
        res = [res]
    por_id = {e["id"]: e for e in eventos}
    for e in pendientes:
        e["intentos"] = e.get("intentos", 0) + 1
        e["ultimo_intento"] = ahora.isoformat()
    n = 0
    if isinstance(res, list):
        for r in res:
            e = por_id.get(r.get("id"))
            if e and r.get("real") and r.get("fuente"):
                e["real"] = str(r["real"])
                e["sorpresa"] = r.get("sorpresa") if r.get("sorpresa") in ("positiva", "negativa", "en_linea") else None
                e["comentario"] = r.get("comentario")
                e["fuente"] = r.get("fuente")
                n += 1
    print(f"Resultados reales añadidos: {n} de {len(pendientes)} pendientes")
    return n


def texto_sesion_ny(intr):
    """La sesión de Nueva York de hoy (desde la foto pre-NY) y la reacción a cada dato, para el resumen."""
    if not intr or not intr.get("resultado"):
        return "no disponible"
    r, lineas = intr["resultado"], []
    if r.get("EURUSD") is not None:
        lineas.append(f'Desde las 14:00 hasta el cierre: EUR/USD {r["EURUSD"]:+} % ({r.get("EURUSD_pips"):+} pips)'
                      + (f', USD/JPY {r["USDJPY"]:+} %' if r.get("USDJPY") is not None else "")
                      + (f', S&P 500 {r["SPX"]:+} %' if r.get("SPX") is not None else ""))
    for x in intr.get("reacciones") or []:
        if x.get("reaccion_pips") is not None:
            lineas.append(f'{x["hora"]} {", ".join(x.get("eventos") or [])}: EUR/USD {x["reaccion_pips"]:+} pips a los '
                          f'{x.get("min_despues")} min' + (f', después {x["hasta_cierre_pips"]:+} pips hasta el cierre'
                                                            if x.get("hasta_cierre_pips") is not None else ""))
    p = (intr.get("preny") or {}).get("eurusd")
    if isinstance(p, dict):
        lineas.append(f'Sesgo pre-NY de EUR/USD: {p.get("sesgo")} (convicción {p.get("conviccion")})')
    return "\n".join(lineas) or "no disponible"


def resumen_del_dia(datos, de_hoy):
    prompt = f"""Hoy es {fecha_es(ahora_utc())} (Madrid), tras el cierre de Nueva York.
Datos de hoy de impacto alto y medio (con el resultado real si lo hay):
{chr(10).join(linea_evento(e) for e in de_hoy)}

Mercado al cierre (último · cambio del día · cambio en la semana):
{texto_mercado(datos["mercado"])}

Sesión de Nueva York de hoy (fotos propias):
{texto_sesion_ny(datos.get("intradia"))}

Sesgo calculado ahora:
{texto_sesgo(datos["sesgo"])}

Tesis de la semana: {((datos.get("narrativa") or {}).get("tesis") or {}).get("texto", "—")}

Escribe el resumen del día en máx. 90 palabras, texto plano: qué salió (mejor o peor de lo previsto), cómo
reaccionaron el dólar, EUR/USD, USD/JPY y los índices según los datos de mercado, si eso refuerza o debilita la tesis de la
semana y qué toca vigilar mañana. Sin saludos. No inventes la causa de un movimiento si no está en los datos.
No repitas todas las cifras: cita solo las sorpresas que importan."""
    # 6000: los modelos 5.x piensan antes de escribir y ese pensamiento cuenta dentro de max_tokens. Con 2500 se
    # cortaba los días con muchos datos (23/10: 8 PMI). Con Haiku cuesta menos de medio céntimo.
    return llamar_claude(prompt, max_tokens=6000, modelo=MODELO_DIARIO, tarea="resumen del día")


# ----------------------------------------------------------------- coste
def registrar_coste(control, hoy, lunes):
    """Acumula el coste real de la API por semana y en total. Devuelve lo que se muestra en la página."""
    c = control.setdefault("coste", {"desde": hoy.isoformat(), "total_usd": 0.0, "semana": lunes.isoformat(),
                                     "semana_usd": 0.0, "llamadas": 0})
    if c.get("semana") != lunes.isoformat():
        c["semana_anterior_usd"] = round(c.get("semana_usd", 0.0), 4)
        c["semana"], c["semana_usd"] = lunes.isoformat(), 0.0
    gasto = sum(g["usd"] for g in GASTOS)
    c["total_usd"] = round(c.get("total_usd", 0.0) + gasto, 4)
    c["semana_usd"] = round(c.get("semana_usd", 0.0) + gasto, 4)
    c["llamadas"] = c.get("llamadas", 0) + len(GASTOS)
    c["ultima_ejecucion"] = GASTOS
    print(f"Coste de esta ejecución: {gasto:.4f} $ · semana: {c['semana_usd']:.4f} $ · total: {c['total_usd']:.4f} $")
    return {k: v for k, v in c.items()}


# ----------------------------------------------------------------- intradía: tu sesión de Nueva York
INTRADIA_FILE = Path(__file__).resolve().parent / "intradia.json"
# Qué se fotografía. SNAP_EXTRA solo se usa aquí (sin histórico ni ficha en «Mercado»). Sus símbolos de CNBC
# no se han podido comprobar: si CNBC no los devuelve, simplemente no aparecen.
SNAP_INST = ["EURUSD", "DXY", "USDJPY", "US2Y", "US10Y", "DE10Y", "JP10Y", "SPX", "NDX", "VIX"]
SNAP_EXTRA = {"DE2Y": "DE2Y-DE", "ES": "@SP.1", "NQ": "@ND.1"}
NOMBRES_SNAP = {"DE2Y": "Bund 2A", "ES": "Futuro S&P 500", "NQ": "Futuro Nasdaq 100"}
# Marcas horarias en hora de NUEVA YORK (los datos de EE. UU. salen a las 8:30 NY todo el año; Europa y EE. UU.
# cambian de hora en semanas distintas). Cada marca se toma en la primera ejecución que cae dentro de su ventana.
MARCAS = [("europa", (3, 0), (3, 50), "Apertura de Londres"),
          ("pre_ny", (7, 40), (8, 25), "Antes de los datos de Nueva York"),
          ("apertura_ny", (9, 30), (10, 15), "Apertura de Wall Street"),
          ("londres", (11, 0), (11, 50), "Cierre de Londres")]
PRENY_LIMITE = (9, 30)     # si la marca pre-NY se pierde, el sesgo se genera igual hasta esta hora (marcado «tardío»)
UMBRAL_MOV = 0.05          # % mínimo para dar por hecho que Londres o un dato han movido el par (≈5-6 pips en EUR/USD)
PIP = {"EURUSD": 10000, "USDJPY": 100}
MAX_SESIONES, DIAS_CON_FOTOS = 400, 30


def _hm(ny):
    return (ny.hour, ny.minute)


def foto_mercado(ahora, marca=None):
    """Una consulta a CNBC con todo lo que interesa en la sesión."""
    simbolos = {INSTRUMENTOS[k]["cnbc"]: k for k in SNAP_INST}
    simbolos.update({v: k for k, v in SNAP_EXTRA.items()})
    q = _cnbc(list(simbolos))
    p = {simbolos[s]: v["ultimo"] for s, v in q.items() if s in simbolos}
    if "EURUSD" not in p:
        raise ValueError("CNBC no devolvió EUR/USD")
    ant = {simbolos[s]: v["anterior"] for s, v in q.items() if s in simbolos and v.get("anterior") is not None}
    f = {"t": ahora.isoformat(), "hora": ahora.astimezone(TZ).strftime("%H:%M"), "p": p, "ant": ant}
    if marca:
        f["marca"] = marca
    return f


def es_rentabilidad(k):
    return k in ("DE2Y",) or (k in INSTRUMENTOS and INSTRUMENTOS[k]["tipo"] == "yield")


def delta(k, a, b):
    """Cambio entre dos valores: pb en bonos, % en precios."""
    if a is None or b is None or a == 0:
        return None
    return round((b - a) * 100, 1) if es_rentabilidad(k) else round((b / a - 1) * 100, 3)


def pips(k, a, b):
    return None if a is None or b is None or k not in PIP else round((b - a) * PIP[k], 1)


def sesion_de(intr, fecha_ny):
    return intr.setdefault("sesiones", {}).setdefault(fecha_ny, {"snaps": [], "reacciones": {}})


def marcas_de(sesion):
    return {s["marca"]: s for s in sesion.get("snaps", []) if s.get("marca")}


def eventos_reaccion(calendario):
    """Datos de EE. UU. y la eurozona con hora: los de impacto alto a cualquier hora y los de impacto medio
    dentro de la sesión de Nueva York (los de Japón salen de madrugada)."""
    return [e for e in calendario if e.get("hora") and e["divisa"] in ("USD", "EUR") and
            (e.get("impacto") == "alto" or (e.get("impacto") == "medio" and e.get("ventana_ny")))]


def tareas_intradia(intr, calendario, ahora):
    """Qué toca hacer en esta ejecución rápida: (marca a tomar, foto previa a un dato, reacciones a medir)."""
    ny = ahora.astimezone(NY)
    sesion = intr.get("sesiones", {}).get(ny.date().isoformat(), {"snaps": [], "reacciones": {}})
    hechas = marcas_de(sesion)
    marca = next((n for n, ini, fin, _ in MARCAS if ini <= _hm(ny) < fin and n not in hechas), None)
    eventos = eventos_reaccion(calendario)
    ultima = max((datetime.fromisoformat(s["t"]) for s in sesion["snaps"]), default=None)
    previa = any(timedelta(0) < datetime.fromisoformat(e["utc"]) - ahora <= timedelta(minutes=25) for e in eventos) \
        and (ultima is None or ahora - ultima > timedelta(minutes=10))
    reacciones = []
    for hora_utc in sorted({e["utc"] for e in eventos}):
        t = datetime.fromisoformat(hora_utc)
        r = sesion["reacciones"].get(hora_utc, {})
        if t.astimezone(NY).date() != ny.date() or "despues" in r or r.get("perdida"):
            continue
        if timedelta(minutes=12) <= ahora - t <= timedelta(minutes=75):
            reacciones.append(hora_utc)
    return marca, previa, reacciones


def registrar_fotos(intr, calendario, ahora, marca, previa, reacciones):
    """Toma la foto (una sola consulta) y la asigna a la marca, a la previa y a las reacciones que tocan."""
    ny = ahora.astimezone(NY)
    sesion = sesion_de(intr, ny.date().isoformat())
    foto = foto_mercado(ahora, marca)
    sesion["snaps"].append(foto)
    eventos = eventos_reaccion(calendario)
    for hora_utc in reacciones:
        t = datetime.fromisoformat(hora_utc)
        antes = [s for s in sesion["snaps"][:-1] if datetime.fromisoformat(s["t"]) < t]
        r = sesion["reacciones"].setdefault(hora_utc, {})
        r["hora"] = t.astimezone(TZ).strftime("%H:%M")
        r["eventos"] = [e["titulo"] for e in eventos if e["utc"] == hora_utc]
        if not antes or t - datetime.fromisoformat(antes[-1]["t"]) > timedelta(minutes=45):
            r["perdida"] = True            # no hay foto de justo antes del dato: no se puede medir
            continue
        r["antes"], r["despues"] = antes[-1]["t"], foto["t"]
        r["min_despues"] = int((ahora - t).total_seconds() // 60)
    print("Foto intradía" + (f" · marca {marca}" if marca else "") + (" · previa a un dato" if previa else "") +
          (f" · {len(reacciones)} reacción(es)" if reacciones else ""))
    return sesion, foto


def foto_por_t(sesion, t):
    return next((s for s in sesion.get("snaps", []) if s["t"] == t), None)


def resumen_sesion(sesion):
    """Lo que ha pasado antes de la sesión de NY: desde la apertura de Londres (o el cierre de ayer) hasta ahora."""
    m = marcas_de(sesion)
    fin = m.get("pre_ny") or (sesion["snaps"][-1] if sesion.get("snaps") else None)
    if not fin:
        return None
    ini = m.get("europa")
    desde = "desde la apertura de Londres" if ini else "desde el cierre de ayer"
    a = ini["p"] if ini else fin.get("ant", {})
    b = fin["p"]
    out = []
    for k, n in (("EURUSD", "EUR/USD"), ("DXY", "DXY"), ("USDJPY", "USD/JPY"), ("US2Y", "Bono 2A EE. UU."),
                 ("DE2Y", "Bund 2A"), ("US10Y", "Bono 10A EE. UU."), ("ES", "Futuro S&P 500"),
                 ("NQ", "Futuro Nasdaq 100"), ("VIX", "VIX")):
        d = delta(k, a.get(k), b.get(k))
        if d is None:
            continue
        x = {"clave": k, "nombre": n, "cambio": d, "unidad": "pb" if es_rentabilidad(k) else "%", "ultimo": b[k]}
        if k in PIP:
            x["pips"] = pips(k, a.get(k), b.get(k))
        out.append(x)
    # Diferencial a 2 años EE. UU.-Alemania: el que más mueve EUR/USD en el día a día
    if all(a.get(k) is not None and b.get(k) is not None for k in ("US2Y", "DE2Y")):
        d = round(((b["US2Y"] - b["DE2Y"]) - (a["US2Y"] - a["DE2Y"])) * 100, 1)
        out.insert(4, {"clave": "DIF2", "nombre": "Diferencial 2A EE. UU.-Alemania", "cambio": d, "unidad": "pb",
                       "ultimo": round((b["US2Y"] - b["DE2Y"]) * 100, 1)})
    return {"desde": desde, "hora": fin["hora"], "items": out}


def texto_resumen_sesion(r):
    if not r:
        return "no disponible"
    return f"({r['desde']} hasta las {r['hora']} de Madrid)\n" + "\n".join(
        f"{x['nombre']}: {x['cambio']:+} {x['unidad']}" + (f" ({x['pips']:+} pips)" if x.get("pips") is not None else "")
        for x in r["items"])


# ----------------------------------------------------------------- rango esperado (sin IA)
def contexto_volatilidad(historico, fecha):
    """Movimiento esperado de cierre a cierre para la sesión 'fecha' según el régimen de volatilidad de 20 días
    (en el histórico, con volatilidad alta el día siguiente se mueve ~1,7 veces más que con volatilidad baja)
    y el día de la semana. No es el rango máximo-mínimo, que suele ser bastante mayor."""
    out = {}
    for k in ("EURUSD", "USDJPY"):
        h = historico.get(k) or {}
        fs = [f for f in sorted(h) if f < fecha.isoformat() and date.fromisoformat(f).weekday() < 5]
        if len(fs) < 320:
            continue
        v = [h[f] for f in fs]
        r = [(v[i] / v[i - 1] - 1) * 100 for i in range(1, len(v))]
        fr = fs[1:]
        vol = [None] * len(r)
        for i in range(19, len(r)):
            m = sum(r[i - 19:i + 1]) / 20
            vol[i] = math.sqrt(sum((x - m) ** 2 for x in r[i - 19:i + 1]) / 20)

        def tercil(i):
            ventana = sorted(x for x in vol[max(19, i - 249):i + 1] if x is not None)
            pos = bisect.bisect_left(ventana, vol[i]) / max(len(ventana) - 1, 1)
            return 0 if pos < 1 / 3 else 2 if pos > 2 / 3 else 1

        grupos = {0: [], 1: [], 2: []}
        for i in range(269, len(r) - 1):
            grupos[tercil(i)].append(abs(r[i + 1]))
        actual = tercil(len(r) - 1)
        media = sum(abs(x) for x in r[-500:]) / len(r[-500:])
        por_dia = {}
        for f, x in zip(fr[-750:], r[-750:]):
            por_dia.setdefault(date.fromisoformat(f).weekday(), []).append(abs(x))
        mult = (sum(por_dia.get(fecha.weekday(), [media])) / len(por_dia.get(fecha.weekday(), [1]))) / media
        esperado = (sum(grupos[actual]) / len(grupos[actual])) * mult if grupos[actual] else media
        out[k] = {"regimen": ["bajo", "normal", "alto"][actual], "esperado_pct": round(esperado, 3),
                  "normal_pct": round(media, 3), "esperado_pips": round(esperado / 100 * v[-1] * PIP[k]),
                  "normal_pips": round(media / 100 * v[-1] * PIP[k]), "dia_mult": round(mult, 2),
                  "vol20": round(vol[-1], 3)}
    return {"fecha": fecha.isoformat(), **out} if out else None


def proxima_sesion(ahora):
    """Fecha (NY) de la sesión que interesa: hoy hasta el cierre de NY; después, el siguiente día laborable."""
    ny = ahora.astimezone(NY)
    d = ny.date()
    if ny.hour >= 16 or d.weekday() >= 5:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# ----------------------------------------------------------------- sesgo pre-NY con IA
CONVICCION_P = {"baja": 1, "media": 1, "alta": 3}


def sesgo_preny(datos, sesion, ahora, tardio):
    """Claude Sonnet con búsqueda web, justo antes de los datos de Nueva York. Devuelve el JSON o None."""
    hoy_ny = ahora.astimezone(NY).date().isoformat()
    de_hoy = [e for e in datos.get("calendario") or [] if e.get("utc") and
              datetime.fromisoformat(e["utc"]).astimezone(NY).date().isoformat() == hoy_ny and e["impacto"] != "festivo"]
    vol = sesion.get("volatilidad") or {}
    v_txt = "; ".join(f'{k}: régimen {x["regimen"]}, movimiento esperado {x["esperado_pips"]} pips (normal {x["normal_pips"]})'
                      for k, x in vol.items() if isinstance(x, dict)) or "no disponible"
    prompt = f"""Es {fecha_es(ahora)} (hora de Madrid). Edwin opera EUR/USD en la sesión de
Nueva York (14:00-22:00 Madrid) con metodología ICT. Prepara su sesgo para la sesión de HOY, antes de los datos de las 14:30.

QUÉ HA PASADO ESTA MAÑANA (Londres):
{texto_resumen_sesion(resumen_sesion(sesion))}

DATOS Y EVENTOS DE HOY (hora de Madrid; impacto alto y medio):
{chr(10).join(linea_evento(e) for e in de_hoy) or "ninguno"}

MERCADO AL CIERRE DE AYER Y SESGO POR REGLAS:
{texto_mercado(datos.get("mercado") or {})}
{texto_sesgo(datos.get("sesgo") or {})}

TESIS DE LA SEMANA: {((datos.get("narrativa") or {}).get("tesis") or {}).get("texto", "—")}

RANGO ESPERADO HOY (calculado con el histórico, cierre a cierre): {v_txt}

FIABILIDAD COMPROBADA: los sesgos por reglas no tienen ventaja demostrada a un día. No los repitas como si fueran señal.

Usa la búsqueda web (máx. 4 búsquedas) para: noticias de esta mañana que muevan el dólar o el euro (Fed, BCE, aranceles,
geopolítica, datos europeos ya publicados), discursos de la Fed/BCE de hoy y el consenso de los datos de las 14:30.

Responde SOLO con un JSON válido:
{{
 "eurusd": {{"sesgo": "alcista | bajista | neutral", "conviccion": "baja | media | alta",
             "por_que": "máx. 45 palabras: los 2-3 motivos concretos",
             "invalida": "máx. 25 palabras: qué tendría que pasar para que el sesgo deje de valer",
             "claves": ["máx. 4: hora y qué vigilar, 12 palabras cada una"]}},
 "usdjpy": {{"sesgo": "alcista | bajista | neutral", "conviccion": "baja | media | alta", "por_que": "máx. 30 palabras"}},
 "indices": {{"sesgo": "alcista | bajista | neutral", "conviccion": "baja | media | alta", "por_que": "máx. 30 palabras (S&P 500 y Nasdaq)"}},
 "volatilidad": "máx. 25 palabras: ¿sesión movida o tranquila y por qué?",
 "noticias": ["máx. 3 titulares de esta mañana, 15 palabras cada uno"]
}}
Reglas: «neutral» es una respuesta válida y preferible a forzar una dirección. «alta» solo si Londres, los diferenciales
y las noticias apuntan en el mismo sentido. No inventes cifras ni noticias. Sin órdenes de compra o venta."""
    n = extraer_json(llamar_claude(prompt, max_tokens=8000, busquedas=4, modelo=MODELO_SEMANAL,
                                   tarea="sesgo pre-NY", esfuerzo="low"))
    if not isinstance(n, dict) or not isinstance(n.get("eurusd"), dict):
        return None
    for k in ("eurusd", "usdjpy", "indices"):
        x = n.get(k)
        if isinstance(x, dict):
            x["sesgo"] = x.get("sesgo") if x.get("sesgo") in ("alcista", "bajista", "neutral") else "neutral"
            x["conviccion"] = x.get("conviccion") if x.get("conviccion") in CONVICCION_P else "baja"
    n["generado"] = ahora.astimezone(TZ).strftime("%H:%M")
    n["tardio"] = tardio
    return n


def p_de(x):
    """Sesgo de texto -> número para validarlo (alta convicción = ±3, como el «sesgo fuerte»)."""
    if not isinstance(x, dict) or x.get("sesgo") not in ("alcista", "bajista"):
        return 0
    return (1 if x["sesgo"] == "alcista" else -1) * CONVICCION_P.get(x.get("conviccion"), 1)


def metodos_sesion(sesion, sesgo_reglas):
    """Lo que se compara al cierre: la IA frente a tres referencias sencillas."""
    m = marcas_de(sesion)
    ia = sesion.get("preny") or {}
    met = {}
    if ia and not ia.get("tardio"):
        met["ia_eurusd"] = p_de(ia.get("eurusd"))
        met["ia_usdjpy"] = p_de(ia.get("usdjpy"))
        met["ia_spx"] = p_de(ia.get("indices"))
    for k, s in (("eurusd", "eurusd"), ("usdjpy", "usdjpy"), ("spx", "spx")):
        if (sesgo_reglas or {}).get(s):
            met[f"reglas_{k}"] = sesgo_reglas[s]["puntuacion"]
    e, pn = m.get("europa"), m.get("pre_ny")
    if e and pn:
        d = delta("EURUSD", e["p"].get("EURUSD"), pn["p"].get("EURUSD"))
        if d is not None:
            s = 0 if abs(d) < UMBRAL_MOV else (1 if d > 0 else -1)
            met["londres_eurusd"], met["contra_londres_eurusd"] = s, -s
    return met


def cerrar_sesion(intr, foto_cierre, ahora):
    """Al cierre de NY: movimiento de la sesión (desde la foto pre-NY) y lo que pasó tras cada dato."""
    sesion = sesion_de(intr, ahora.astimezone(NY).date().isoformat())
    if not any(s.get("marca") == "cierre" for s in sesion["snaps"]):
        foto_cierre["marca"] = "cierre"
        sesion["snaps"].append(foto_cierre)
    m = marcas_de(sesion)
    ini = m.get("pre_ny")
    fin = m["cierre"]["p"]
    if ini:
        sesion["resultado"] = {k: delta(k, ini["p"].get(k), fin.get(k)) for k in ("EURUSD", "USDJPY", "SPX", "ES")}
        sesion["resultado"]["EURUSD_pips"] = pips("EURUSD", ini["p"].get("EURUSD"), fin.get("EURUSD"))
    for r in sesion["reacciones"].values():
        a, d = foto_por_t(sesion, r.get("antes")), foto_por_t(sesion, r.get("despues"))
        if a and d:
            r["reaccion"] = delta("EURUSD", a["p"].get("EURUSD"), d["p"].get("EURUSD"))
            r["reaccion_pips"] = pips("EURUSD", a["p"].get("EURUSD"), d["p"].get("EURUSD"))
            r["reaccion_2a"] = delta("US2Y", a["p"].get("US2Y"), d["p"].get("US2Y"))
            r["hasta_cierre"] = delta("EURUSD", d["p"].get("EURUSD"), fin.get("EURUSD"))
            r["hasta_cierre_pips"] = pips("EURUSD", d["p"].get("EURUSD"), fin.get("EURUSD"))
    return sesion


def podar_intradia(intr, hoy_ny):
    ses = intr.get("sesiones", {})
    for f in sorted(ses)[:-MAX_SESIONES]:
        del ses[f]
    corte = (hoy_ny - timedelta(days=DIAS_CON_FOTOS)).isoformat()
    for f, s in ses.items():
        if f < corte and s.get("snaps"):
            s["snaps"] = [x for x in s["snaps"] if x.get("marca") in ("pre_ny", "cierre")]


METODOS_NOMBRE = {
    "ia_eurusd": "IA pre-NY · EUR/USD", "reglas_eurusd": "Reglas · EUR/USD", "londres_eurusd": "Seguir a Londres · EUR/USD",
    "contra_londres_eurusd": "Ir contra Londres · EUR/USD", "reaccion": "Seguir la reacción al dato · EUR/USD",
    "ia_usdjpy": "IA pre-NY · USD/JPY", "reglas_usdjpy": "Reglas · USD/JPY",
    "ia_spx": "IA pre-NY · S&P 500", "reglas_spx": "Reglas · S&P 500"}
METODO_INST = {"eurusd": "EURUSD", "usdjpy": "USDJPY", "spx": "SPX"}


def stats_sesion(intr):
    """Validación de la sesión de NY (solo en vivo: no hay histórico intradía)."""
    filas = {k: [] for k in METODOS_NOMBRE}
    for f, s in sorted(intr.get("sesiones", {}).items()):
        res = s.get("resultado")
        if res:
            for met, p in (s.get("metodos") or {}).items():
                inst = METODO_INST[met.rsplit("_", 1)[1]]
                if met in filas:
                    filas[met].append({"p": p, "f": {}, "mov": res.get(inst)})
        for r in (s.get("reacciones") or {}).values():
            if r.get("reaccion") is not None and r.get("hasta_cierre") is not None:
                p = 0 if abs(r["reaccion"]) < UMBRAL_MOV else (1 if r["reaccion"] > 0 else -1)
                filas["reaccion"].append({"p": p, "f": {}, "mov": r["hasta_cierre"]})
    return {k: estadistica(v, METODOS_NOMBRE[k]) for k, v in filas.items()}


def intradia_para_pagina(intr, ahora):
    """Lo que necesita el panel «Tu sesión de Nueva York» (sesión de hoy o, tras el cierre, la de hoy ya cerrada
    más el rango esperado de la siguiente)."""
    hoy = ahora.astimezone(NY).date()
    ses = intr.get("sesiones", {})
    actual = ses.get(hoy.isoformat()) if hoy.weekday() < 5 else None
    sig = proxima_sesion(ahora).isoformat()
    s = actual or ses.get(sig) or {}
    reac = []
    for hora_utc, r in sorted((s.get("reacciones") or {}).items()):
        reac.append({k: r.get(k) for k in ("hora", "eventos", "min_despues", "reaccion", "reaccion_pips", "reaccion_2a",
                                          "hasta_cierre", "hasta_cierre_pips", "perdida")})
    marcas = [{"marca": m["marca"], "hora": m["hora"]} for m in s.get("snaps", []) if m.get("marca")]
    out = {"fecha": (hoy.isoformat() if actual else sig), "resumen": resumen_sesion(s) if s.get("snaps") else None,
           "preny": s.get("preny"), "reacciones": reac, "resultado": s.get("resultado"), "marcas": marcas,
           "volatilidad": s.get("volatilidad"), "cerrada": bool(s.get("resultado"))}
    if actual and actual.get("resultado") and sig != hoy.isoformat():
        out["siguiente"] = {"fecha": sig, "volatilidad": (ses.get(sig) or {}).get("volatilidad")}
    return out


# ----------------------------------------------------------------- modos rápido y cierre
def modo_rapido(datos_prev, ahora, lunes):
    """Cada 15 minutos (L-V). Solo escribe (y por tanto solo hay commit) si ha hecho algo:
      - fotos del mercado a las horas fijas y alrededor de cada dato de impacto alto (sin coste de API);
      - el sesgo pre-NY con IA, una vez al día, justo antes de los datos de Nueva York;
      - el resultado real de los datos de impacto alto ya publicados."""
    if (datos_prev.get("semana") or {}).get("lunes") != lunes.isoformat():
        print("La semana aún no está montada: lo hará la ejecución completa")
        return
    calendario = datos_prev.get("calendario") or []
    intr = cargar_json(INTRADIA_FILE, {})
    ny = ahora.astimezone(NY)
    cambios = False

    # 1) Fotos intradía
    marca, previa, reacciones = tareas_intradia(intr, calendario, ahora)
    sesion = None
    if marca or previa or reacciones:
        try:
            sesion, _ = registrar_fotos(intr, calendario, ahora, marca, previa, reacciones)
            if marca == "pre_ny":       # referencias sencillas para comparar con la IA: reglas y Londres a esta hora
                sesion["metodos"] = metodos_sesion(sesion, datos_prev.get("sesgo"))
            cambios = True
        except Exception as e:
            aviso(f"No se pudo tomar la foto intradía ({e})")

    # 2) Sesgo pre-NY con IA: en cuanto exista la foto pre-NY (o, si se perdió, hasta las 9:30 NY, marcado «tardío»)
    sesion = sesion or intr.get("sesiones", {}).get(ny.date().isoformat())
    previo = (sesion or {}).get("preny") or {}
    falta = sesion is not None and (not previo or (previo.get("error") and previo.get("intentos", 1) < 2))
    if falta and _hm(ny) < PRENY_LIMITE and not SIN_CLAUDE and \
            ("pre_ny" in marcas_de(sesion) or _hm(ny) >= MARCAS[1][2]):
        tardio = "pre_ny" not in marcas_de(sesion)
        try:
            n = sesgo_preny(datos_prev, sesion, ahora, tardio)
        except Exception as e:
            n = None
            aviso(f"Error en el sesgo pre-NY ({e})")
        sesion["preny"] = n or {"error": True, "intentos": previo.get("intentos", 0) + 1,
                                "generado": ahora.astimezone(TZ).strftime("%H:%M")}
        # Si es tardío (ya han salido los datos) no se valida nada de esta sesión: sería hacer trampa
        nuevos = {} if tardio else metodos_sesion(sesion, datos_prev.get("sesgo"))
        sesion["metodos"] = {**nuevos, **{k: v for k, v in (sesion.get("metodos") or {}).items() if not k.startswith("ia_")}}
        cambios = True

    # 3) Resultado real de los datos de impacto alto ya publicados
    pendientes = pendientes_resultado(calendario, ahora)
    if pendientes and not SIN_CLAUDE:
        print("Pendientes: " + " · ".join(e["titulo_en"] for e in pendientes))
        try:
            obtener_resultados(calendario)
        except Exception as e:
            aviso(f"Error obteniendo resultados ({e})")
        cambios = True

    if not cambios:
        print("Nada que hacer en esta ejecución: no se toca nada")
        return
    local = ahora.astimezone(TZ)
    datos = datos_prev
    datos["sesgo"] = calcular_sesgo(datos.get("mercado") or {}, calendario)
    datos["intradia"] = intradia_para_pagina(intr, ahora)
    datos["generado"] = ahora.isoformat()
    datos["generado_local"] = local.strftime("%d/%m/%Y %H:%M")
    control = datos.setdefault("_control", {})
    datos["coste"] = registrar_coste(control, local.date(), lunes)
    # Se conservan los avisos del último cierre y se añaden los de esta ejecución
    datos["diagnostico"] = list(dict.fromkeys((datos.get("diagnostico") or []) + DIAGNOSTICO))[-12:]
    guardar_json(DATOS_FILE, datos, compacto=True)        # se guarda aunque no encuentre nada: lleva los intentos
    guardar_json(INTRADIA_FILE, intr, compacto=True)
    print("datos.json actualizado")


def toca_cierre(control, ahora):
    """GitHub no ajusta el cambio de hora, así que el cierre se programa a las 20:15 y a las 21:15 UTC y
    solo se ejecuta la primera que cae después de las 16:00 de Nueva York (en verano y en invierno)."""
    ny = ahora.astimezone(NY)
    if ny.hour < 16:
        print(f"Son las {ny:%H:%M} en Nueva York: aún no ha cerrado, esta ejecución no hace nada")
        return False
    if control.get("cierre") == ny.date().isoformat():
        print("El cierre de hoy ya está hecho")
        return False
    return True


# ----------------------------------------------------------------- principal
def main():
    ahora = ahora_utc()
    local = ahora.astimezone(TZ)
    hoy = local.date()
    lunes = lunes_de_la_semana(hoy)
    print(f"Modo: {MODO}")

    datos_prev = cargar_json(DATOS_FILE, {})
    if MODO == "rapido":
        modo_rapido(datos_prev, ahora, lunes)
        return
    control = datos_prev.get("_control", {})
    if MODO == "cierre" and not toca_cierre(control, ahora):
        return
    control.pop("cot_descargado", None)          # restos de la versión con COT
    historico = cargar_json(HISTORICO_FILE, {})

    # ¿Cambio de semana? -> archivar la anterior
    semana_prev = (datos_prev.get("semana") or {}).get("lunes")
    nueva_semana = semana_prev != lunes.isoformat()
    archivo_prev = None
    if semana_prev and nueva_semana:
        archivo_prev = {k: v for k, v in datos_prev.items() if not k.startswith("_")}
        guardar_json(SEMANAS_DIR / f"{semana_prev}.json", archivo_prev, compacto=True)
        print(f"Semana {semana_prev} archivada")
    elif semana_prev:
        archivo_prev = cargar_json(SEMANAS_DIR / f"{(lunes - timedelta(days=7)).isoformat()}.json", None)

    # 1) Calendario
    previos = [] if nueva_semana else datos_prev.get("calendario", [])
    crudos = obtener_calendario()
    if crudos is not None:
        calendario = normalizar_eventos(crudos, previos)
        # El feed "thisweek" de Forex Factory va de domingo a sábado: si aún no cubre la semana mostrada, avisar
        if calendario and not any(e["fecha"] >= lunes.isoformat() for e in calendario):
            aviso("El calendario de Forex Factory aún no cubre la semana nueva")
            calendario = previos
    else:
        calendario = previos
    for e in calendario:            # conserva los contadores de intentos de búsqueda
        p = next((x for x in previos if x["id"] == e["id"]), None)
        if p:
            for campo in ("intentos", "ultimo_intento"):
                if p.get(campo):
                    e[campo] = p[campo]

    # 2) Mercado
    mercado = obtener_mercado(historico, lunes)
    if mercado:
        precios_local = local.strftime("%d/%m/%Y %H:%M")
    else:
        mercado = datos_prev.get("mercado", {})
        precios_local = datos_prev.get("precios_local")

    datos = {
        "generado": ahora.isoformat(),
        "generado_local": local.strftime("%d/%m/%Y %H:%M"),
        "precios_local": precios_local,
        "semana": {"lunes": lunes.isoformat(), "viernes": (lunes + timedelta(days=4)).isoformat()},
        "calendario": calendario,
        "mercado": mercado,
        "narrativa": None if nueva_semana else datos_prev.get("narrativa"),
        "nota_dia": None if nueva_semana else datos_prev.get("nota_dia"),
    }
    de_hoy = [e for e in calendario if e["fecha"] == hoy.isoformat() and e["impacto"] in ("alto", "medio")
              and e.get("hora") and e["divisa"] in ("USD", "EUR")]
    hoy_ya_publicado = bool(de_hoy) and all(datetime.fromisoformat(e["utc"]) < ahora for e in de_hoy)

    # 3) Resultados reales de lo ya publicado (busca solo si hay pendientes)
    if not SIN_CLAUDE:
        try:
            obtener_resultados(calendario)
        except Exception as e:
            aviso(f"Error obteniendo resultados ({e})")

    # 4) Sesgo por reglas fijas
    datos["sesgo"] = calcular_sesgo(mercado, calendario)

    # 4b) Correlaciones y validación del sesgo (cálculos propios, sin coste de API)
    try:
        datos["correlaciones"] = calcular_correlaciones(historico)
        datos["correlaciones"]["calculado"] = local.strftime("%d/%m/%Y %H:%M")
    except Exception as e:
        aviso(f"No se pudieron calcular las correlaciones ({e})")
    registro = cargar_json(VALIDACION_FILE, {})
    validacion = {}
    try:
        validacion["historico"] = backtest(historico)
    except Exception as e:
        aviso(f"No se pudo calcular la validación histórica ({e})")
    try:
        validacion["vivo"] = registrar_validacion(registro, datos["sesgo"], historico, ahora, lunes,
                                                  ahora.astimezone(NY).date())
    except Exception as e:
        aviso(f"No se pudo actualizar el registro de validación ({e})")
    datos["validacion"] = validacion

    # 4c) Intradía: rango esperado de la próxima sesión, cierre de la de hoy y su validación
    intr = cargar_json(INTRADIA_FILE, {})
    try:
        objetivo = proxima_sesion(ahora)
        vol = contexto_volatilidad(historico, objetivo)
        if vol:
            sesion_de(intr, objetivo.isoformat())["volatilidad"] = vol
        hoy_sesion = ahora.astimezone(NY).date()
        if MODO == "cierre" and hoy_sesion.weekday() < 5:
            cerrar_sesion(intr, foto_mercado(ahora), ahora)
        podar_intradia(intr, hoy_sesion)
        datos["intradia"] = intradia_para_pagina(intr, ahora)
        if isinstance(validacion.get("vivo"), dict):
            validacion["vivo"]["sesion"] = stats_sesion(intr)
            validacion["vivo"]["n_sesion"] = sum(1 for x in intr.get("sesiones", {}).values() if x.get("resultado"))
    except Exception as e:
        aviso(f"Error en la parte intradía ({e})")

    # 5) Semana anterior (archivo)
    if archivo_prev:
        datos["semana_anterior"] = {
            "lunes": archivo_prev.get("semana", {}).get("lunes"),
            "viernes": archivo_prev.get("semana", {}).get("viernes"),
            "calendario": [e for e in archivo_prev.get("calendario", []) if e["impacto"] == "alto"],
            "sesgo": archivo_prev.get("sesgo"),
        }
    else:
        datos["semana_anterior"] = datos_prev.get("semana_anterior")

    # 6) Narrativa semanal (una vez por semana, o si falta / se fuerza)
    intentos = control.get("tesis_intentos", {})
    n_int = intentos.get(lunes.isoformat(), 0)
    if not SIN_CLAUDE and ((datos["narrativa"] is None and n_int < 2) or FORZAR_SEMANAL):
        control["tesis_intentos"] = {lunes.isoformat(): n_int + 1}     # máx. 2 intentos automáticos por semana
        n = narrativa_semanal(datos, archivo_prev)
        if n:
            datos["narrativa"] = n
            datos["narrativa"]["generada"] = local.strftime("%d/%m %H:%M")
            # Claude a veces devuelve el título del evento en vez de su id: se enlaza por el título
            por_titulo = {e["titulo_en"]: e["id"] for e in calendario}
            ids = {e["id"] for e in calendario}
            for esc_ in n.get("escenarios") or []:
                if esc_.get("id") not in ids and esc_.get("id") in por_titulo:
                    esc_["id"] = por_titulo[esc_["id"]]
            for e in calendario:
                es = (n.get("titulos_es") or {}).get(e["titulo_en"])
                if es and e["titulo"] == e["titulo_en"]:
                    e["titulo"] = es

    # 7) Resumen del día: los días con datos de impacto alto o medio, solo tras el cierre de Nueva York
    #    («nota_dia_cierre»: clave nueva en v5.2, así se rehace el resumen del 23/10, que salió a media sesión)
    tras_cierre = ahora.astimezone(NY).hour >= 16 and ahora.astimezone(NY).date() == hoy
    if not SIN_CLAUDE and hoy_ya_publicado and tras_cierre and control.get("nota_dia_cierre") != hoy.isoformat():
        texto = resumen_del_dia(datos, de_hoy)
        if texto:
            datos["nota_dia"] = {"texto": limpiar(texto), "generada": local.strftime("%d/%m %H:%M"), "fecha": hoy.isoformat()}
            control["nota_dia_cierre"] = hoy.isoformat()

    # 8) Coste real de la API (se acumula por semana y en total)
    datos["coste"] = registrar_coste(control, hoy, lunes)

    if MODO == "cierre":
        control["cierre"] = ahora.astimezone(NY).date().isoformat()
    datos["diagnostico"] = DIAGNOSTICO
    datos["_control"] = control
    guardar_json(DATOS_FILE, datos, compacto=True)
    guardar_json(HISTORICO_FILE, historico, compacto=True)
    if registro:
        guardar_json(VALIDACION_FILE, registro, compacto=True)
    if intr:
        guardar_json(INTRADIA_FILE, intr, compacto=True)
    print(f"datos.json guardado ({DATOS_FILE.stat().st_size // 1024} KB) · avisos: {len(DIAGNOSTICO)}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
