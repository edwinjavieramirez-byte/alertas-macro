"""
Agenda macro semanal de Edwin  ·  genera docs/datos.json para la página web (GitHub Pages).

Se ejecuta desde GitHub Actions (.github/workflows/agenda.yml):
  - domingo por la tarde: monta la semana nueva (calendario, COT, sesgo, tesis y escenarios)  -> Claude Sonnet
  - de lunes a viernes, tras el cierre de Nueva York: precios y COT (gratis) y, SOLO si ese día hubo
    datos de impacto alto, sus resultados reales y el resumen del día                          -> Claude Haiku

Fuentes:
  Forex Factory (calendario USD y EUR) · CNBC / Yahoo (precios) · CFTC (COT, informe TFF)
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
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
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

TZ = ZoneInfo("Europe/Madrid")
NY = ZoneInfo("America/New_York")
RAIZ = Path(__file__).resolve().parent.parent
DOCS = RAIZ / "docs"
DATOS_FILE = DOCS / "datos.json"
SEMANAS_DIR = DOCS / "semanas"
HISTORICO_FILE = Path(__file__).resolve().parent / "historico.json"

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CFTC_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"   # Traders in Financial Futures
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

COT_MERCADOS = {
    "EUR": {"codigo": "099741", "nombre": "Euro FX (CME)"},
    "DXY": {"codigo": "098662", "nombre": "Índice dólar (ICE)"},
    "JPY": {"codigo": "097741", "nombre": "Yen japonés (CME)"},
    "ES":  {"codigo": "13874A", "nombre": "E-mini S&P 500"},
    "NQ":  {"codigo": "209742", "nombre": "Nasdaq 100 mini"},
}

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


def aviso(texto):
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
    Path(path).write_text(texto, encoding="utf-8")


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


def _yahoo_serie(simbolo, rango, intervalo):
    """Devuelve (lista de (datetime_utc, fecha_de_mercado_iso, cierre), meta)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(simbolo)}"
    r = requests.get(url, params={"range": rango, "interval": intervalo}, headers=UA, timeout=25)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    meta = res.get("meta", {})
    offset = int(meta.get("gmtoffset") or 0)
    ts = res.get("timestamp") or []
    cierres = res["indicators"]["quote"][0].get("close") or []
    puntos = []
    for t, c in zip(ts, cierres):
        c = a_float(c)
        if c is None:
            continue
        if simbolo == "^TNX" and c > 20:
            c = c / 10
        fecha_mercado = datetime.fromtimestamp(t + offset, timezone.utc).date().isoformat()
        puntos.append((datetime.fromtimestamp(t, timezone.utc), fecha_mercado, c))
    return puntos, meta


def obtener_mercado(historico, lunes):
    """Precio actual, cambio del día, de la semana, serie horaria de la semana y datos diarios.
    Para cada activo se usa UNA sola fuente para precio e histórico (Yahoo si existe; si no, CNBC),
    para que los cambios semanales sean coherentes."""
    datos = {}
    try:
        cnbc = _cnbc([i["cnbc"] for i in INSTRUMENTOS.values() if i["cnbc"]])
    except Exception as e:
        aviso(f"CNBC no disponible ({e})")
        cnbc = {}

    inicio_semana = datetime.combine(lunes, datetime.min.time(), TZ).astimezone(timezone.utc)
    hoy_ny = ahora_utc().astimezone(NY)
    for k, i in INSTRUMENTOS.items():
        d = {"nombre": i["nombre"], "grupo": i["grupo"], "tipo": i["tipo"], "dec": i["dec"]}
        diarios, horaria = [], []
        if i["yahoo"]:
            try:
                diarios, meta = _yahoo_serie(i["yahoo"], "5y", "1d")
                horaria, _ = _yahoo_serie(i["yahoo"], "5d", "60m")
            except Exception as e:
                aviso(f"Yahoo sin datos para {k} ({e})")
        h = historico.setdefault(k, {})
        q = cnbc.get(i["cnbc"]) if i["cnbc"] else None
        if diarios:
            ultimo = horaria[-1][2] if horaria and horaria[-1][0] > diarios[-1][0] else diarios[-1][2]
            d["fuente"] = "Yahoo"
            for _, f, c in diarios:
                h[f] = c
            h[diarios[-1][1]] = ultimo
        elif q:
            ultimo = q["ultimo"]
            d["fuente"] = "CNBC"
            if hoy_ny.weekday() < 5 or not h:
                # cierre aproximado: la última lectura del día (en fin de semana, el último día hábil)
                dia = hoy_ny.date()
                while dia.weekday() >= 5:
                    dia -= timedelta(days=1)
                h[dia.isoformat()] = ultimo
        else:
            aviso(f"Sin precio para {k}")
            continue
        for vieja in sorted(h)[:-1300]:
            del h[vieja]
        fechas = sorted(h)
        cierres = [h[f] for f in fechas]

        d["ultimo"] = ultimo
        d["anterior"] = cierres[-2] if len(cierres) > 1 else None
        if d["fuente"] == "CNBC" and q.get("anterior") is not None:
            d["anterior"] = q["anterior"]                   # CNBC trae el cierre oficial de ayer
        d["fecha_dato"] = fechas[-1]
        d["cambio_dia"] = cambio(i["tipo"], ultimo, d["anterior"])
        # cierre de referencia: último cierre ANTES del lunes de la semana mostrada
        ref = [h[f] for f in fechas if f < lunes.isoformat()]
        d["cierre_semana_anterior"] = ref[-1] if ref else None
        d["cambio_semana"] = cambio(i["tipo"], ultimo, d["cierre_semana_anterior"])
        # semana anterior completa (viernes a viernes)
        ref2 = [h[f] for f in fechas if f < (lunes - timedelta(days=7)).isoformat()]
        d["cambio_semana_anterior"] = cambio(i["tipo"], d["cierre_semana_anterior"], ref2[-1] if ref2 else None)
        d["sma20"] = redondear(sma(cierres, 20), 5)
        d["sma50"] = redondear(sma(cierres, 50), 5)
        d["sma200"] = redondear(sma(cierres, 200), 5)
        if diarios and i["grupo"] == "indices" and k != "VIX":
            maximo = max(c for _, _, c in diarios)
            d["maximo_5a"] = maximo
            d["dist_maximo_pct"] = round((ultimo / maximo - 1) * 100, 2)
        # serie horaria de la semana mostrada (si aún no ha empezado: los últimos 5 días)
        serie = [(t, c) for t, _, c in horaria if t >= inicio_semana]
        d["serie_semana_actual"] = bool(serie)
        if not serie:
            serie = [(t, c) for t, _, c in horaria]
        d["serie"] = [redondear(c, 5) for _, c in serie][-140:]
        d["diario"] = [[f, redondear(h[f], 5)] for f in fechas[-30:]]
        d["ultimo"] = redondear(ultimo, 5)
        d["anterior"] = redondear(d["anterior"], 5)
        d["cierre_semana_anterior"] = redondear(d["cierre_semana_anterior"], 5)
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


# ----------------------------------------------------------------- COT
def obtener_cot():
    salida = {}
    for clave, m in COT_MERCADOS.items():
        try:
            r = requests.get(CFTC_URL, params={
                "cftc_contract_market_code": m["codigo"],
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": 60,
            }, headers=UA, timeout=30)
            r.raise_for_status()
            filas = r.json()
            if not filas:
                aviso(f"COT sin filas para {clave} ({m['codigo']})")
                continue
            filas = list(reversed(filas))   # de más antigua a más reciente
            serie = []
            for f in filas:
                serie.append({
                    "fecha": f["report_date_as_yyyy_mm_dd"][:10],
                    "lev": int(a_float(f.get("lev_money_positions_long")) or 0) - int(a_float(f.get("lev_money_positions_short")) or 0),
                    "am": int(a_float(f.get("asset_mgr_positions_long")) or 0) - int(a_float(f.get("asset_mgr_positions_short")) or 0),
                    "dealer": int(a_float(f.get("dealer_positions_long_all")) or 0) - int(a_float(f.get("dealer_positions_short_all")) or 0),
                    "oi": int(a_float(f.get("open_interest_all")) or 0),
                })
            ult = serie[-1]
            ventana = serie[-52:]

            def indice(campo):
                vals = [s[campo] for s in ventana]
                lo, hi = min(vals), max(vals)
                return None if hi == lo else round((ult[campo] - lo) / (hi - lo) * 100)

            def cambio_n(campo, n):
                return ult[campo] - serie[-1 - n][campo] if len(serie) > n else None

            salida[clave] = {
                "nombre": m["nombre"], "fecha": ult["fecha"],
                "lev": ult["lev"], "am": ult["am"], "dealer": ult["dealer"], "oi": ult["oi"],
                "lev_cambio_1s": cambio_n("lev", 1), "lev_cambio_4s": cambio_n("lev", 4),
                "am_cambio_1s": cambio_n("am", 1), "am_cambio_4s": cambio_n("am", 4),
                "lev_indice_52s": indice("lev"), "am_indice_52s": indice("am"),
                "serie_lev": [s["lev"] for s in serie[-26:]],
                "serie_am": [s["am"] for s in serie[-26:]],
            }
        except Exception as e:
            aviso(f"COT {clave} no disponible ({e})")
    return salida


# ----------------------------------------------------------------- sesgo (reglas fijas)
def miles(n, signo=True):
    t = f"{n:+,.0f}" if signo else f"{n:,.0f}"
    return t.replace(",", ".")


def factor(nombre, voto, detalle):
    return {"factor": nombre, "voto": voto, "detalle": detalle}


def sesgo_dolar(m, cot, eventos):
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
    eur = cot.get("EUR")
    if eur and eur.get("lev_cambio_4s") is not None:
        c = eur["lev_cambio_4s"]
        v = -1 if c >= 10000 else 1 if c <= -10000 else 0
        f.append(factor("COT Euro FX (Leveraged Funds)", v,
                        f"neto {miles(eur['lev'])} contratos, {miles(c)} en 4 semanas (umbral ±10.000). "
                        f"Datos del {eur['fecha']}: el COT va con varios días de retraso"))
    usd = [e for e in eventos if e["divisa"] == "USD" and e.get("sorpresa") in ("positiva", "negativa")]
    if usd:
        pos = sum(1 for e in usd if e["sorpresa"] == "positiva")
        neg = sum(1 for e in usd if e["sorpresa"] == "negativa")
        v = 1 if pos - neg >= 2 else -1 if neg - pos >= 2 else 0
        f.append(factor("Datos USD de la semana", v, f"{pos} mejores de lo previsto · {neg} peores"))
    return resumir(f, "USD")


def sesgo_indice(clave, m, cot):
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
    c = cot.get("ES" if clave == "SPX" else "NQ")
    if c and c.get("am_cambio_4s") is not None:
        ch = c["am_cambio_4s"]
        umbral = 15000 if clave == "SPX" else 3000
        v = 1 if ch >= umbral else -1 if ch <= -umbral else 0
        f.append(factor("COT Asset Managers", v, f"neto {miles(c['am'])}, {miles(ch)} en 4 semanas (datos del {c['fecha']})"))
    r = resumir(f, nombre)
    return r


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


# ----------------------------------------------------------------- Claude
PROMPT_SISTEMA = """Eres el analista macro de Edwin, trader de EUR/USD que opera en la sesión de Nueva York (tarde en España)
y sigue el S&P 500, el Nasdaq 100 y el VIX para medir el sentimiento de riesgo. Escribes en español de España,
directo, sin relleno, sin emojis y sin inventar cifras, fechas ni noticias. Si no tienes un dato, dilo.
Nunca das órdenes de compra o venta: describes escenarios, sesgos y riesgos. El sesgo numérico lo calculan reglas
fijas que se te dan; explícalo y señala si el contexto lo contradice, pero no cambies los números.
Horas siempre en hora de Madrid."""


def llamar_claude(prompt, max_tokens=1500, busquedas=0, modelo=None, tarea=""):
    """Devuelve el texto final o None. Con busquedas>0 activa la búsqueda web del servidor.
    Apunta en GASTOS los tokens y búsquedas consumidos para calcular el coste real."""
    if SIN_CLAUDE or not ANTHROPIC_API_KEY:
        return None
    modelos = [modelo or MODELO_DIARIO]
    if MODELO_RESPALDO not in modelos:
        modelos.append(MODELO_RESPALDO)
    for mod in modelos:
        cuerpo = {"model": mod, "max_tokens": max_tokens, "system": PROMPT_SISTEMA,
                  "messages": [{"role": "user", "content": prompt}]}
        if busquedas:
            cuerpo["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": busquedas}]
        uso = {"entrada": 0, "salida": 0, "busquedas": 0}
        try:
            for _ in range(4):        # pause_turn: una búsqueda larga puede pausarse y hay que reanudarla
                r = requests.post("https://api.anthropic.com/v1/messages", headers={
                    "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                    "content-type": "application/json"}, json=cuerpo, timeout=180)
                if r.status_code == 400 and "tools" in cuerpo and "web search" in r.text.lower():
                    aviso("La búsqueda web de la API no está habilitada: sigo sin ella")
                    cuerpo.pop("tools", None)
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
                if res.get("stop_reason") == "pause_turn":
                    cuerpo["messages"] = cuerpo["messages"] + [{"role": "assistant", "content": res["content"]}]
                    continue
                texto = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
                pe, ps = PRECIOS.get(mod, (2.0, 10.0))
                usd = uso["entrada"] * pe / 1e6 + uso["salida"] * ps / 1e6 + uso["busquedas"] * PRECIO_BUSQUEDA
                GASTOS.append({"tarea": tarea, "modelo": mod, **uso, "usd": round(usd, 4)})
                print(f"Claude ({mod}) · {tarea}: {uso['entrada']} tokens de entrada, {uso['salida']} de salida, "
                      f"{uso['busquedas']} búsquedas = {usd:.4f} $")
                return texto.strip()
        except Exception as e:
            aviso(f"Fallo al llamar a Claude con {mod} ({str(e)[:300]})")
            continue
    return None


def extraer_json(texto):
    if not texto:
        return None
    texto = re.sub(r"```(?:json)?", "", texto)
    for abre, cierra in (("{", "}"), ("[", "]")):
        i, j = texto.find(abre), texto.rfind(cierra)
        if i != -1 and j > i:
            try:
                return json.loads(texto[i:j + 1])
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


def texto_cot(cot):
    return "\n".join(f'{c["nombre"]} ({c["fecha"]}): Leveraged Funds neto {c["lev"]:+} (4s {c["lev_cambio_4s"]:+}, índice 52s {c["lev_indice_52s"]}) · '
                     f'Asset Managers neto {c["am"]:+} (4s {c["am_cambio_4s"]:+}, índice 52s {c["am_indice_52s"]})'
                     for c in cot.values() if c.get("lev_cambio_4s") is not None and c.get("am_cambio_4s") is not None)


def narrativa_semanal(datos, archivo_prev):
    eventos = datos["calendario"]
    prev_eventos = (archivo_prev or {}).get("calendario", [])
    lunes = datos["semana"]["lunes"]
    prompt = f"""Hoy es {ahora_utc().astimezone(TZ):%A %d/%m/%Y %H:%M} (Madrid). Prepara la agenda de la semana del {lunes}.

DATOS DE MERCADO (último · cambio de esta semana · cambio de la semana anterior completa):
{texto_mercado(datos["mercado"])}

SESGO CALCULADO POR REGLAS FIJAS:
{texto_sesgo(datos["sesgo"])}

COT (CFTC, informe TFF):
{texto_cot(datos["cot"]) or "sin datos"}

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
    return extraer_json(llamar_claude(prompt, max_tokens=4000, busquedas=7, modelo=MODELO_SEMANAL,
                                      tarea="tesis semanal"))


def obtener_resultados(eventos):
    """Busca el dato real de los eventos ya publicados que aún no lo tienen."""
    ahora = ahora_utc()
    # Solo datos con cifra (los discursos no tienen "resultado" que buscar)
    pendientes = [e for e in eventos if e.get("real") is None and e["impacto"] == "alto"
                  and (e.get("prevision") or e.get("anterior"))
                  and e.get("hora") and datetime.fromisoformat(e["utc"]) < ahora - timedelta(minutes=10)
                  and datetime.fromisoformat(e["utc"]) > ahora - timedelta(days=6)
                  and e.get("intentos", 0) < 3]
    if not pendientes:
        return 0
    pendientes = pendientes[:14]
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
encuentras, "real": null."""
    res = extraer_json(llamar_claude(prompt, max_tokens=2500, busquedas=min(6, len(pendientes) + 1),
                                     modelo=MODELO_DIARIO, tarea="resultados reales"))
    por_id = {e["id"]: e for e in eventos}
    for e in pendientes:
        e["intentos"] = e.get("intentos", 0) + 1
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
    print(f"Resultados reales añadidos: {n}")
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
    return llamar_claude(prompt, max_tokens=500, modelo=MODELO_DIARIO, tarea="resumen del día")


# ----------------------------------------------------------------- principal
def main():
    ahora = ahora_utc()
    local = ahora.astimezone(TZ)
    hoy = local.date()
    lunes = lunes_de_la_semana(hoy)

    datos_prev = cargar_json(DATOS_FILE, {})
    historico = cargar_json(HISTORICO_FILE, {})
    control = datos_prev.get("_control", {})

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
    for e in calendario:            # conserva contadores de intentos
        p = next((x for x in previos if x["id"] == e["id"]), None)
        if p and p.get("intentos"):
            e["intentos"] = p["intentos"]

    # 2) Mercado
    mercado = obtener_mercado(historico, lunes) or datos_prev.get("mercado", {})

    # 3) COT (cada 12 h como mucho; se publica los viernes)
    cot = datos_prev.get("cot", {})
    ult_cot = control.get("cot_descargado")
    if not cot or not ult_cot or ahora - datetime.fromisoformat(ult_cot) > timedelta(hours=12):
        nuevo = obtener_cot()
        if nuevo:
            cot = nuevo
            control["cot_descargado"] = ahora.isoformat()

    datos = {
        "generado": ahora.isoformat(),
        "generado_local": local.strftime("%d/%m/%Y %H:%M"),
        "semana": {"lunes": lunes.isoformat(), "viernes": (lunes + timedelta(days=4)).isoformat()},
        "calendario": calendario,
        "mercado": mercado,
        "cot": cot,
        "narrativa": None if nueva_semana else datos_prev.get("narrativa"),
        "nota_dia": None if nueva_semana else datos_prev.get("nota_dia"),
    }
    de_hoy = [e for e in calendario if e["fecha"] == hoy.isoformat() and e["impacto"] == "alto" and e.get("hora")]
    hoy_ya_publicado = bool(de_hoy) and all(datetime.fromisoformat(e["utc"]) < ahora for e in de_hoy)

    # 4) Resultados reales de lo ya publicado (busca solo si hay pendientes)
    if not SIN_CLAUDE:
        try:
            obtener_resultados(calendario)
        except Exception as e:
            aviso(f"Error obteniendo resultados ({e})")

    # 5) Sesgo por reglas fijas
    datos["sesgo"] = {
        "usd": sesgo_dolar(mercado, cot, calendario),
        "spx": sesgo_indice("SPX", mercado, cot),
        "ndx": sesgo_indice("NDX", mercado, cot),
    }
    # EUR/USD es la inversa del dólar
    u = datos["sesgo"]["usd"]
    datos["sesgo"]["eurusd"] = {"sujeto": "EUR/USD", "puntuacion": -u["puntuacion"], "maximo": u["maximo"],
                                "etiqueta": etiqueta(-u["puntuacion"], u["maximo"]),
                                "factores": []}

    # 6) Semana anterior (archivo)
    if archivo_prev:
        datos["semana_anterior"] = {
            "lunes": archivo_prev.get("semana", {}).get("lunes"),
            "viernes": archivo_prev.get("semana", {}).get("viernes"),
            "calendario": [e for e in archivo_prev.get("calendario", []) if e["impacto"] == "alto"],
            "sesgo": archivo_prev.get("sesgo"),
        }
    else:
        datos["semana_anterior"] = datos_prev.get("semana_anterior")

    # 7) Narrativa semanal (una vez por semana, o si falta / se fuerza)
    if not SIN_CLAUDE and (datos["narrativa"] is None or FORZAR_SEMANAL):
        n = narrativa_semanal(datos, archivo_prev)
        if n:
            datos["narrativa"] = n
            datos["narrativa"]["generada"] = local.strftime("%d/%m %H:%M")
            for e in calendario:
                es = (n.get("titulos_es") or {}).get(e["titulo_en"])
                if es and e["titulo"] == e["titulo_en"]:
                    e["titulo"] = es

    # 8) Resumen del día: solo los días con datos de impacto alto, una vez que ya han salido todos
    if not SIN_CLAUDE and hoy_ya_publicado and control.get("nota_dia") != hoy.isoformat():
        texto = resumen_del_dia(datos, de_hoy)
        if texto:
            datos["nota_dia"] = {"texto": texto, "generada": local.strftime("%d/%m %H:%M"), "fecha": hoy.isoformat()}
            control["nota_dia"] = hoy.isoformat()

    # 9) Coste real de la API (se acumula por semana y en total)
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
    datos["coste"] = {k: v for k, v in c.items()}
    print(f"Coste de esta ejecución: {gasto:.4f} $ · semana: {c['semana_usd']:.4f} $ · total: {c['total_usd']:.4f} $")

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
