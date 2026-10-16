"""
Agenda macro semanal de Edwin  ·  genera docs/datos.json para la página web (GitHub Pages).

Se ejecuta desde GitHub Actions (.github/workflows/agenda.yml) en tres modos (variable MODO):
  - "completo" (domingo por la tarde, a mano o al cambiar el script): calendario, precios, sesgo y,
    si falta, la tesis y los escenarios de la semana                                       -> Claude Sonnet
  - "rapido" (L-V cada 15 minutos en horario de datos): SOLO si hay un dato de impacto alto ya
    publicado y sin resultado, lo busca y actualiza la agenda y el sesgo. Si no, sale sin tocar nada
    (no descarga nada, no llama a la API y no hace commit)                                  -> Claude Haiku
  - "cierre" (L-V tras el cierre de Nueva York, en verano y en invierno): precios, resultados que
    falten y resumen del día                                                                -> Claude Haiku

Fuentes:
  Forex Factory (calendario USD y EUR) · CNBC / Yahoo / Fed de St. Louis (precios)
  API de Claude con búsqueda web (resultados reales, contexto Fed y redacción)

No hace falta tocar este archivo. Lo configurable está en Settings → Secrets and variables → Actions:
  ANTHROPIC_API_KEY (secret, el mismo que usan las alertas)
  AGENDA_MODELO_SEMANAL (variable opcional; por defecto claude-sonnet-5-5)
  AGENDA_MODELO_DIARIO  (variable opcional; por defecto claude-haiku-5-5)
"""

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

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"             # Reserva Federal de St. Louis
# Históricos diarios de la Fed (Yahoo bloquea a GitHub con el error 429). Cierres del día hábil anterior.
FRED = {"US2Y": "DGS2", "US10Y": "DGS10", "SPX": "SP500", "NDX": "NASDAQ100", "VIX": "VIXCLS",
        "EURUSD": "DEXUSEU", "USDJPY": "DEXJPUS", "WTI": "DCOILWTICO"}
# El DXY no está en la Fed: se reconstruye con su fórmula oficial a partir de los tipos de cambio diarios
DXY_CESTA = {"DEXUSEU": -0.576, "DEXJPUS": 0.136, "DEXUSUK": -0.119, "DEXCAUS": 0.091,
             "DEXSDUS": 0.042, "DEXSZUS": 0.036}
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

DIVISAS = ["USD", "EUR"]
IMPACTOS = ["High", "Medium"]

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
ORDEN = ["DXY", "EURUSD", "USDJPY", "US2Y", "US10Y", "DE10Y", "SPX", "NDX", "VIX", "ORO", "WTI"]

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
    """Filtra USD/EUR alto y medio impacto. Conserva resultados reales ya obtenidos."""
    previos_por_id = {e["id"]: e for e in previos}
    salida = []
    for ev in crudos:
        if ev.get("country") not in DIVISAS:
            continue
        impacto = ev.get("impact")
        if impacto not in IMPACTOS and not (impacto == "Holiday" and ev.get("country") == "USD"):
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
        datos["DIF_US_DE"] = calcular_diferencial(historico, datos, lunes)
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


def calcular_diferencial(historico, datos, lunes):
    us, de = historico.get("US10Y", {}), historico.get("DE10Y", {})
    comunes = sorted(set(us) & set(de))
    actual = (datos["US10Y"]["ultimo"] - datos["DE10Y"]["ultimo"]) * 100
    ref = [(us[f] - de[f]) * 100 for f in comunes if f < lunes.isoformat()]
    return {"nombre": "Diferencial 10A EE. UU.-Alemania", "grupo": "tipos", "tipo": "pb",
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
    usd = [e for e in eventos if e["divisa"] == "USD" and e.get("sorpresa") in ("positiva", "negativa")]
    if usd:
        pos = sum(1 for e in usd if e["sorpresa"] == "positiva")
        neg = sum(1 for e in usd if e["sorpresa"] == "negativa")
        v = 1 if pos - neg >= 2 else -1 if neg - pos >= 2 else 0
        f.append(factor("Datos USD de la semana", v, f"{pos} mejores de lo previsto · {neg} peores"))
    return resumir(f, "USD")


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
        nivel, cs = vix["ultimo"], vix.get("cambio_semana")
        if nivel >= 22 or (cs is not None and cs >= 20):
            v, t = -1, "miedo alto o subiendo con fuerza"
        elif nivel <= 16 and (cs is None or cs < 10):
            v, t = 1, "calma"
        else:
            v, t = 0, "zona intermedia"
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
    return s


# ----------------------------------------------------------------- Claude
PROMPT_SISTEMA = """Eres el analista macro de Edwin, trader de EUR/USD que opera en la sesión de Nueva York (tarde en España)
y sigue el S&P 500, el Nasdaq 100 y el VIX para medir el sentimiento de riesgo. Escribes en español de España,
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


def linea_evento(e):
    hora = e["hora"] or "sin hora"
    return (f'{e["dia"]} {e["fecha"][8:10]}/{e["fecha"][5:7]} {hora} · {e["divisa"]} · {e["titulo_en"]} '
            f'[{e["impacto"]}] prev. {e["prevision"] or "—"} · ant. {e["anterior"] or "—"}'
            + (f' · REAL {e["real"]} ({e["sorpresa"]})' if e.get("real") else ""))


def texto_mercado(m):
    lineas = []
    for k in ORDEN + ["DIF_US_DE", "PENDIENTE"]:
        x = m.get(k)
        if not x:
            continue
        u = "pb" if x.get("tipo") in ("yield", "pb") else "%"
        partes = [f'{x["nombre"]}: {x["ultimo"]}']
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
                     "; ".join(f'{f["factor"]} {f["voto"]:+}: {f["detalle"]}' for f in s[k]["factores"])
                     for k in s)


def narrativa_semanal(datos, archivo_prev):
    eventos = datos["calendario"]
    prev_eventos = (archivo_prev or {}).get("calendario", [])
    lunes = datos["semana"]["lunes"]
    prompt = f"""Hoy es {ahora_utc().astimezone(TZ):%A %d/%m/%Y %H:%M} (Madrid). Prepara la agenda de la semana del {lunes}.

DATOS DE MERCADO (último · cambio de esta semana · cambio de la semana anterior completa):
{texto_mercado(datos["mercado"])}

SESGO CALCULADO POR REGLAS FIJAS:
{texto_sesgo(datos["sesgo"])}

CALENDARIO DE LA SEMANA PASADA (con resultados reales si los hay):
{chr(10).join(linea_evento(e) for e in prev_eventos) or "no disponible: búscalo"}

CALENDARIO DE ESTA SEMANA (USD y EUR, impacto alto y medio, hora de Madrid):
{chr(10).join(linea_evento(e) for e in eventos) or "no disponible"}

Usa la búsqueda web para: (1) qué movió el dólar, el S&P 500 y el Nasdaq la semana pasada (datos, Fed, BCE,
geopolítica, resultados empresariales); (2) probabilidades actuales de CME FedWatch para la próxima reunión de la Fed
y la próxima decisión del BCE; (3) los discursos de miembros de la Fed y del BCE previstos esta semana y los resultados
empresariales grandes que puedan mover los índices.

Responde SOLO con un JSON válido (sin texto antes ni después) con esta estructura:
{{
 "semana_anterior": {{"titular": "máx. 12 palabras", "resumen": "60-90 palabras: qué pasó y por qué",
                      "claves": [{{"titulo": "dato o noticia", "detalle": "máx. 30 palabras: resultado y efecto en USD/índices"}}]}},
 "tesis": {{"titular": "máx. 14 palabras: la idea central de esta semana", "texto": "70-110 palabras"}},
 "bancos_centrales": {{"fed": "estado actual y probabilidades FedWatch con fecha de la próxima reunión (máx. 45 palabras)",
                       "bce": "estado actual y próxima decisión (máx. 35 palabras)"}},
 "escenarios": [{{"id": "id exacto del evento del calendario", "por_que": "máx. 20 palabras",
                  "si_mayor": "si sale por encima de lo previsto: efecto en USD, EUR/USD y S&P/Nasdaq (máx. 25 palabras)",
                  "si_menor": "si sale por debajo (máx. 25 palabras)"}}],
 "otros_eventos": [{{"dia": "Lunes..Viernes", "hora": "HH:MM o null", "texto": "discurso, subasta, resultados... (máx. 15 palabras)"}}],
 "lectura_usd": "máx. 60 palabras: explica el sesgo del dólar calculado y si el contexto lo confirma o contradice; implicación para EUR/USD",
 "lectura_indices": "máx. 60 palabras: igual para S&P 500 y Nasdaq (¿pueden buscar nuevo máximo o hay riesgo de retroceso?)",
 "ventana_ny": "máx. 50 palabras: qué días y horas de la sesión de Nueva York (14:00-22:00 Madrid) concentran el riesgo",
 "riesgos": ["máx. 4 riesgos no programados, 12 palabras cada uno"],
 "titulos_es": {{"titulo en inglés del calendario": "traducción breve al español"}}
}}
En "escenarios" incluye solo los 4-7 eventos de impacto alto más importantes. En "si_mayor"/"si_menor" ten en cuenta
que en paro y peticiones de subsidio un número mayor es PEOR para la economía. Si hay un discurso o evento sin
previsión numérica, usa "si_mayor" para el tono duro (hawkish) y "si_menor" para el blando (dovish)."""
    texto = llamar_claude(prompt, max_tokens=16000, busquedas=5, modelo=MODELO_SEMANAL, tarea="tesis semanal",
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
    """Datos de impacto alto ya publicados, sin resultado real, a los que les toca un intento de búsqueda."""
    salida = []
    for e in eventos:
        if e.get("real") is not None or e.get("impacto") != "alto" or not e.get("hora"):
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
    res = extraer_json(llamar_claude(prompt, max_tokens=2500, busquedas=min(6, len(pendientes) + 1),
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


def resumen_del_dia(datos, de_hoy):
    prompt = f"""Hoy es {ahora_utc().astimezone(TZ):%A %d/%m/%Y %H:%M} (Madrid), tras el cierre de Nueva York.
Datos de impacto alto de hoy (con el resultado real si lo hay):
{chr(10).join(linea_evento(e) for e in de_hoy)}

Mercado al cierre (último · cambio en la semana):
{texto_mercado(datos["mercado"])}

Sesgo calculado ahora:
{texto_sesgo(datos["sesgo"])}

Tesis de la semana: {((datos.get("narrativa") or {}).get("tesis") or {}).get("texto", "—")}

Escribe el resumen del día en máx. 90 palabras, texto plano: qué salió (mejor o peor de lo previsto), cómo
reaccionaron el dólar, EUR/USD y los índices según los datos de mercado, si eso refuerza o debilita la tesis de la
semana y qué toca vigilar mañana. Sin saludos. No inventes la causa de un movimiento si no está en los datos."""
    # 2500 y no 500: los modelos 5.x piensan antes de escribir y ese pensamiento cuenta dentro de max_tokens
    return llamar_claude(prompt, max_tokens=2500, modelo=MODELO_DIARIO, tarea="resumen del día")


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


# ----------------------------------------------------------------- modos rápido y cierre
def modo_rapido(datos_prev, ahora, lunes):
    """Cada 15 minutos: si hay un dato de impacto alto recién publicado, busca su resultado y lo refleja
    en la agenda y en el sesgo. Si no hay nada pendiente, no escribe nada (y por tanto no hay commit)."""
    if (datos_prev.get("semana") or {}).get("lunes") != lunes.isoformat():
        print("La semana aún no está montada: lo hará la ejecución completa")
        return
    calendario = datos_prev.get("calendario") or []
    pendientes = pendientes_resultado(calendario, ahora)
    if not pendientes:
        print("Ningún dato de impacto alto pendiente de resultado: no se toca nada")
        return
    print("Pendientes: " + " · ".join(e["titulo_en"] for e in pendientes))
    if SIN_CLAUDE:
        return
    try:
        obtener_resultados(calendario)
    except Exception as e:
        aviso(f"Error obteniendo resultados ({e})")
    local = ahora.astimezone(TZ)
    datos = datos_prev
    datos["sesgo"] = calcular_sesgo(datos.get("mercado") or {}, calendario)
    datos["generado"] = ahora.isoformat()
    datos["generado_local"] = local.strftime("%d/%m/%Y %H:%M")
    control = datos.setdefault("_control", {})
    datos["coste"] = registrar_coste(control, local.date(), lunes)
    # Se conservan los avisos del último cierre y se añaden los de esta ejecución
    datos["diagnostico"] = list(dict.fromkeys((datos.get("diagnostico") or []) + DIAGNOSTICO))[-12:]
    guardar_json(DATOS_FILE, datos, compacto=True)        # se guarda aunque no encuentre nada: lleva los intentos
    print("datos.json actualizado con los resultados")


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
    de_hoy = [e for e in calendario if e["fecha"] == hoy.isoformat() and e["impacto"] == "alto" and e.get("hora")]
    hoy_ya_publicado = bool(de_hoy) and all(datetime.fromisoformat(e["utc"]) < ahora for e in de_hoy)

    # 3) Resultados reales de lo ya publicado (busca solo si hay pendientes)
    if not SIN_CLAUDE:
        try:
            obtener_resultados(calendario)
        except Exception as e:
            aviso(f"Error obteniendo resultados ({e})")

    # 4) Sesgo por reglas fijas
    datos["sesgo"] = calcular_sesgo(mercado, calendario)

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

    # 7) Resumen del día: solo los días con datos de impacto alto, una vez que ya han salido todos
    if not SIN_CLAUDE and hoy_ya_publicado and control.get("nota_dia") != hoy.isoformat():
        texto = resumen_del_dia(datos, de_hoy)
        if texto:
            datos["nota_dia"] = {"texto": limpiar(texto), "generada": local.strftime("%d/%m %H:%M"), "fecha": hoy.isoformat()}
            control["nota_dia"] = hoy.isoformat()

    # 8) Coste real de la API (se acumula por semana y en total)
    datos["coste"] = registrar_coste(control, hoy, lunes)

    if MODO == "cierre":
        control["cierre"] = ahora.astimezone(NY).date().isoformat()
    datos["diagnostico"] = DIAGNOSTICO
    datos["_control"] = control
    guardar_json(DATOS_FILE, datos, compacto=True)
    guardar_json(HISTORICO_FILE, historico, compacto=True)
    print(f"datos.json guardado ({DATOS_FILE.stat().st_size // 1024} KB) · avisos: {len(DIAGNOSTICO)}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
