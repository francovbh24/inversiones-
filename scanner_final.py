"""
SCANNER DE ENTRADAS - Estrategia SMA300/SMA1000
Version final con notificacion por Telegram.

Variables de entorno necesarias (se configuran como GitHub Secrets):
  TELEGRAM_BOT_TOKEN  -> el token que te dio @BotFather
  TELEGRAM_CHAT_ID    -> tu chat_id personal

PARAMETROS DE ANALISIS:
  1) SMA300(1h) > SMA1000(1h)        -> filtro de tendencia de fondo
  2) Cruce alcista precio/SMA300(1h)  -> trigger de entrada
  3) Distancia % a SMA300             -> detecta "cerca de entrar"
  4) ATR(14, 1h)                      -> ajusta el umbral de cercania
                                          segun la volatilidad propia
                                          de cada activo (en vez de un
                                          % fijo para todos)
  5) RSI(14, 1d)                      -> INFORMATIVO en el mensaje,
                                          calculado sobre velas DIARIAS
                                          (no 1h), marca si esta sobre 80
  6) VIX                              -> INFORMATIVO en el mensaje,
                                          contexto general de mercado
  7) Pendiente SMA300 (20 velas)      -> mide la fuerza de la tendencia
                                          de fondo, se usa para ORDENAR
                                          cada grupo del mensaje

GRUPOS DEL MENSAJE (en este orden):
  1) YA CRUZARON, todavia a <5% sobre la SMA300
     (el cruce ya paso, pero el precio no se alejo mucho - todavia se
     puede entrar sin haber perdido el arranque del movimiento)
  2) ENTRADA CONFIRMADA en esta misma vela
     (el cruce ocurrio justo ahora)
  3) CERCA DE ENTRAR (todavia no cruzo, pero esta dentro del umbral
     ajustado por ATR)
  4) RSI - tendencia alcista (SMA300>SMA1000) + RSI(1h) <= 40
     (posible sobreventa de corto plazo dentro de una tendencia de
     fondo alcista; sin distincion por distancia a la SMA300; sin
     subgrupos, ordenado de RSI mas bajo a mas alto)

Los primeros 3 grupos, dentro de cada uno, se ordenan asi: primero los
de pendiente de SMA300 mas fuerte (tendencia de fondo mas pronunciada
= mayor potencial de captura historico), despues el resto.

UNIVERSO DE ACTIVOS:
  - 375 CEDEARs disponibles en BYMA con subyacente en NASDAQ/NYSE
  - 20 acciones del Panel Lider que cotizan directo en BYMA (sufijo .BA)
  - Los calculos siempre se hacen sobre el precio NATIVO del activo
    (USD para CEDEARs, ARS para acciones de BYMA), nunca sobre el
    precio del CEDEAR en pesos.
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import time
import requests
from datetime import datetime

# ──────────────────────────────────────────────────────────
# CONFIGURACION
# ──────────────────────────────────────────────────────────
UMBRAL_CERCANIA_BASE_PCT = 1.0   # umbral base, se ajusta con ATR
ATR_PERIODO = 14
RSI_PERIODO = 14
RSI_ALERTA = 80.0                 # solo informativo, no filtra
RSI_SOBREVENTA_1H = 40.0          # umbral del grupo RSI(1h) sobreventa
UMBRAL_YA_CRUZO_PCT = 5.0         # techo del grupo "ya cruzo, todavia cerca"
PENDIENTE_VELAS = 20              # ventana para medir la pendiente de SMA300

UNIVERSO_JSON = os.path.join(os.path.dirname(__file__), "universo_completo.json")
BATCH_SIZE = 15
VIX_TICKER = "^VIX"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ──────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────
def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ATENCION: faltan credenciales de Telegram, no se envia mensaje.")
        print(mensaje)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i in range(0, len(mensaje), 4000):
        chunk = mensaje[i:i+4000]
        try:
            r = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML"
            }, timeout=15)
            if r.status_code != 200:
                print(f"Error enviando a Telegram: {r.text}")
        except Exception as e:
            print(f"Error de conexion con Telegram: {e}")
        time.sleep(1)

# ──────────────────────────────────────────────────────────
# CARGA DE TICKERS
# ──────────────────────────────────────────────────────────
def cargar_tickers():
    with open(UNIVERSO_JSON, "r") as f:
        records = json.load(f)
    tickers = [r["ticker"] for r in records]
    name_map = {r["ticker"]: r["name"] for r in records}
    return tickers, name_map

# ──────────────────────────────────────────────────────────
# DESCARGA DE DATOS
# ──────────────────────────────────────────────────────────
def descargar_datos_1h(tickers):
    """Devuelve dict ticker -> DataFrame con columnas Close, High, Low (velas 1h)"""
    data_dict = {}
    batches = [tickers[i:i+BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    for i, batch in enumerate(batches):
        print(f"  Lote {i+1}/{len(batches)} ({len(batch)} tickers)...")
        try:
            raw = yf.download(batch, period="2y", interval="1h",
                               auto_adjust=True, progress=False, threads=True)
            if len(raw) == 0:
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                for t in batch:
                    try:
                        sub = raw.xs(t, axis=1, level=1, drop_level=True)
                        sub = sub.dropna(subset=["Close"])
                        if len(sub) > 500:
                            data_dict[t] = sub[["Close", "High", "Low"]]
                    except KeyError:
                        continue
            else:
                sub = raw.dropna(subset=["Close"])
                if len(sub) > 500:
                    data_dict[batch[0]] = sub[["Close", "High", "Low"]]
        except Exception as e:
            print(f"    Error en lote: {e}")
        time.sleep(0.5)
    return data_dict

def descargar_datos_1d(tickers):
    """Devuelve dict ticker -> Serie de cierre diario, usada SOLO para el RSI"""
    close_dict = {}
    batches = [tickers[i:i+BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    for i, batch in enumerate(batches):
        print(f"  Lote diario {i+1}/{len(batches)} ({len(batch)} tickers)...")
        try:
            raw = yf.download(batch, period="2y", interval="1d",
                               auto_adjust=True, progress=False, threads=True)
            if len(raw) == 0:
                continue
            close = raw["Close"]
            if isinstance(close, pd.DataFrame):
                for t in batch:
                    if t in close.columns:
                        s = close[t].dropna()
                        if len(s) > RSI_PERIODO + 5:
                            close_dict[t] = s
            else:
                s = close.dropna()
                if len(s) > RSI_PERIODO + 5:
                    close_dict[batch[0]] = s
        except Exception as e:
            print(f"    Error en lote diario: {e}")
        time.sleep(0.5)
    return close_dict

def descargar_vix():
    try:
        raw = yf.download(VIX_TICKER, period="5d", interval="1h",
                           auto_adjust=True, progress=False)
        if len(raw) == 0:
            return None
        close = raw["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        return round(float(close.dropna().iloc[-1]), 2)
    except Exception as e:
        print(f"Error descargando VIX: {e}")
        return None

# ──────────────────────────────────────────────────────────
# INDICADORES
# ──────────────────────────────────────────────────────────
def calcular_rsi(close, periodo=14):
    delta = close.diff()
    ganancia = delta.clip(lower=0)
    perdida = -delta.clip(upper=0)
    avg_gain = ganancia.rolling(periodo).mean()
    avg_loss = perdida.rolling(periodo).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calcular_atr(high, low, close, periodo=14):
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(periodo).mean()
    return atr

# ──────────────────────────────────────────────────────────
# LOGICA DE ESCANEO
# ──────────────────────────────────────────────────────────
def escanear_ticker(ticker, df, rsi_diario_serie):
    c = df["Close"].dropna()
    h = df["High"]
    l = df["Low"]
    if len(c) < 1100 + PENDIENTE_VELAS:
        return None

    sma300 = c.rolling(300).mean()
    sma1000 = c.rolling(1000).mean()
    atr = calcular_atr(h, l, c, ATR_PERIODO)
    rsi_1h_serie = calcular_rsi(c, RSI_PERIODO)
    ultimo_rsi_1h = rsi_1h_serie.iloc[-1] if not pd.isna(rsi_1h_serie.iloc[-1]) else None

    # RSI calculado sobre velas DIARIAS (1d), informativo en los otros grupos
    ultimo_rsi = None
    if rsi_diario_serie is not None and len(rsi_diario_serie) > RSI_PERIODO:
        rsi_diario = calcular_rsi(rsi_diario_serie, RSI_PERIODO)
        if len(rsi_diario.dropna()):
            ultimo_rsi = rsi_diario.dropna().iloc[-1]

    # Performance ultimos 12 meses (usando datos diarios ya descargados)
    perf_1y = None
    if rsi_diario_serie is not None and len(rsi_diario_serie) >= 252:
        precio_actual_d = float(rsi_diario_serie.dropna().iloc[-1])
        precio_hace_1y = float(rsi_diario_serie.dropna().iloc[-252])
        if precio_hace_1y > 0:
            perf_1y = round((precio_actual_d - precio_hace_1y) / precio_hace_1y * 100, 1)

    ultimo_precio = c.iloc[-1]
    ultimo_sma300 = sma300.iloc[-1]
    ultimo_sma1000 = sma1000.iloc[-1]
    penultimo_precio = c.iloc[-2]
    penultimo_sma300 = sma300.iloc[-2]
    ultimo_atr = atr.iloc[-1]
    sma300_hace_n = sma300.iloc[-1 - PENDIENTE_VELAS]

    if pd.isna(ultimo_sma300) or pd.isna(ultimo_sma1000) or pd.isna(sma300_hace_n):
        return None

    cond1_tendencia = ultimo_sma300 > ultimo_sma1000
    cruce_confirmado = (penultimo_precio < penultimo_sma300) and (ultimo_precio >= ultimo_sma300)
    distancia_pct = (ultimo_precio - ultimo_sma300) / ultimo_sma300 * 100

    pendiente_sma300 = (ultimo_sma300 - sma300_hace_n) / sma300_hace_n * 100

    if not pd.isna(ultimo_atr) and ultimo_precio > 0:
        atr_pct = (ultimo_atr / ultimo_precio) * 100
        umbral_ajustado = max(UMBRAL_CERCANIA_BASE_PCT, atr_pct * 0.5)
    else:
        atr_pct = None
        umbral_ajustado = UMBRAL_CERCANIA_BASE_PCT

    cerca_de_entrada = (
        cond1_tendencia
        and ultimo_precio < ultimo_sma300
        and abs(distancia_pct) <= umbral_ajustado
    )

    ya_cruzo_cerca = (
        cond1_tendencia
        and ultimo_precio >= ultimo_sma300
        and distancia_pct <= UMBRAL_YA_CRUZO_PCT
    )

    # Grupo independiente: tendencia alcista + RSI(1d) en sobreventa.
    # No excluye a un activo de los otros grupos, es una etiqueta aparte.
    rsi1h_sobreventa = (
        cond1_tendencia
        and ultimo_rsi is not None
        and ultimo_rsi <= RSI_SOBREVENTA_1H
    )

    if cond1_tendencia and cruce_confirmado:
        estado = "ENTRADA_CONFIRMADA"
    elif ya_cruzo_cerca:
        estado = "YA_CRUZO_CERCA"
    elif cerca_de_entrada:
        estado = "CERCA_DE_ENTRADA"
    elif cond1_tendencia:
        estado = "TENDENCIA_OK_LEJOS_DE_ENTRADA"
    else:
        estado = "SIN_TENDENCIA"

    return {
        "ticker": ticker,
        "estado": estado,
        "precio_actual": round(float(ultimo_precio), 2),
        "sma300": round(float(ultimo_sma300), 2),
        "sma1000": round(float(ultimo_sma1000), 2),
        "distancia_pct_a_sma300": round(float(distancia_pct), 2),
        "umbral_ajustado_pct": round(float(umbral_ajustado), 2),
        "atr_pct": round(float(atr_pct), 2) if atr_pct is not None else None,
        "rsi": round(float(ultimo_rsi), 1) if ultimo_rsi is not None and not pd.isna(ultimo_rsi) else None,
        "rsi_sobre_80": bool(ultimo_rsi > RSI_ALERTA) if ultimo_rsi is not None and not pd.isna(ultimo_rsi) else False,
        "tendencia_alcista": bool(cond1_tendencia),
        "pendiente_sma300_pct": round(float(pendiente_sma300), 2),
        "rsi_1h": round(float(ultimo_rsi_1h), 1) if ultimo_rsi_1h is not None else None,
        "rsi1h_sobreventa": bool(rsi1h_sobreventa),
        "perf_1y": perf_1y,
    }

# ──────────────────────────────────────────────────────────
# FORMATEO DEL MENSAJE
# ──────────────────────────────────────────────────────────
def fmt_rsi(r):
    if r['rsi'] is None:
        return ""
    return " ⚠️RSI&gt;80" if r['rsi_sobre_80'] else f" RSI {r['rsi']}"

def fmt_1y(r):
    if r['perf_1y'] is None:
        return ""
    signo = "+" if r['perf_1y'] >= 0 else ""
    return f" | 1Y {signo}{r['perf_1y']}%"

def ordenar_grupo(grupo_df, umbral_fuerte=2.0):
    """
    Divide en dos subgrupos por pendiente de SMA300, y dentro de cada
    subgrupo ordena de mayor a menor performance de los ultimos 12 meses.
    """
    fuertes = grupo_df[grupo_df['pendiente_sma300_pct'] >= umbral_fuerte].sort_values(
        'perf_1y', ascending=False)
    resto = grupo_df[grupo_df['pendiente_sma300_pct'] < umbral_fuerte].sort_values(
        'perf_1y', ascending=False)
    return fuertes, resto

def construir_bloque_grupo(titulo, emoji, grupo_df, name_map, max_items=15):
    lineas = []
    if len(grupo_df) == 0:
        return lineas

    fuertes, resto = ordenar_grupo(grupo_df)

    lineas.append(f"{emoji} <b>{titulo} ({len(grupo_df)})</b>")

    if len(fuertes):
        lineas.append("  📈 <i>Tendencia mas fuerte</i>")
        for _, r in fuertes.head(max_items).iterrows():
            nombre = name_map.get(r['ticker'], r['ticker'])
            lineas.append(
                f"  • {r['ticker']} ({nombre[:22]}) — ${r['precio_actual']} | "
                f"dist {r['distancia_pct_a_sma300']}% | pend.SMA300 {r['pendiente_sma300_pct']}%"
                f"{fmt_1y(r)}{fmt_rsi(r)}"
            )

    if len(resto):
        lineas.append("  ▫️ <i>Resto</i>")
        for _, r in resto.head(max_items).iterrows():
            nombre = name_map.get(r['ticker'], r['ticker'])
            lineas.append(
                f"  • {r['ticker']} ({nombre[:22]}) — ${r['precio_actual']} | "
                f"dist {r['distancia_pct_a_sma300']}% | pend.SMA300 {r['pendiente_sma300_pct']}%"
                f"{fmt_1y(r)}{fmt_rsi(r)}"
            )

    lineas.append("")
    return lineas

def construir_bloque_simple(titulo, emoji, grupo_df, name_map, max_items=20):
    """Bloque sin subgrupos, ordenado de mayor a menor performance 1Y."""
    lineas = []
    if len(grupo_df) == 0:
        return lineas
    ordenado = grupo_df.sort_values('perf_1y', ascending=False)
    lineas.append(f"{emoji} <b>{titulo} ({len(grupo_df)})</b>")
    for _, r in ordenado.head(max_items).iterrows():
        nombre = name_map.get(r['ticker'], r['ticker'])
        rsi_val = r['rsi'] if r['rsi'] is not None else "N/D"
        lineas.append(
            f"• {r['ticker']} ({nombre[:22]}) — ${r['precio_actual']} | "
            f"RSI(1d) {rsi_val} | dist SMA300 {r['distancia_pct_a_sma300']}%"
            f"{fmt_1y(r)}"
        )
    lineas.append("")
    return lineas

def formatear_mensaje(df, name_map, vix_valor):
    ahora = datetime.now().strftime('%Y-%m-%d %H:%M')

    ya_cruzo = df[df['estado'] == 'YA_CRUZO_CERCA']
    confirmadas = df[df['estado'] == 'ENTRADA_CONFIRMADA']
    cercanas = df[df['estado'] == 'CERCA_DE_ENTRADA']
    sobreventa = df[df['rsi1h_sobreventa'] == True]

    lineas = [f"<b>SCANNER SMA — {ahora}</b>"]
    vix_txt = f"{vix_valor}" if vix_valor is not None else "N/D"
    lineas.append(f"VIX actual: {vix_txt}")
    lineas.append("")

    lineas += construir_bloque_grupo(
        f"YA CRUZARON, a &lt;{UMBRAL_YA_CRUZO_PCT}% sobre SMA300", "🔵", ya_cruzo, name_map)

    if len(confirmadas):
        lineas += construir_bloque_grupo("ENTRADA CONFIRMADA ESTA VELA", "🟢", confirmadas, name_map)
    else:
        lineas.append("🟢 Sin entradas nuevas confirmadas esta hora")
        lineas.append("")

    lineas += construir_bloque_grupo(
        "CERCA DE ENTRAR (umbral ajustado por ATR)", "🟡", cercanas, name_map)

    lineas += construir_bloque_simple(
        f"RSI — tendencia alcista + RSI(1d) ≤ {RSI_SOBREVENTA_1H:.0f}", "🟣", sobreventa, name_map)

    return "\n".join(lineas)

# ──────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────
def main():
    print(f"=== SCANNER DE ENTRADAS — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    tickers, name_map = cargar_tickers()
    print(f"Total tickers a escanear: {len(tickers)}\n")

    print("Descargando VIX...")
    vix_valor = descargar_vix()
    print(f"VIX: {vix_valor}\n")

    print("Descargando datos 1h de Yahoo Finance (SMA300/SMA1000/ATR)...")
    data_dict = descargar_datos_1h(tickers)
    print(f"\nTickers con datos 1h suficientes: {len(data_dict)}\n")

    print("Descargando datos diarios de Yahoo Finance (solo para RSI)...")
    rsi_dict = descargar_datos_1d(tickers)
    print(f"\nTickers con datos diarios suficientes: {len(rsi_dict)}\n")

    print("Analizando condiciones de entrada...")
    resultados = []
    for t, df_t in data_dict.items():
        rsi_serie = rsi_dict.get(t)
        r = escanear_ticker(t, df_t, rsi_serie)
        if r:
            resultados.append(r)

    df = pd.DataFrame(resultados)
    df.to_csv("scanner_resultados.csv", index=False)

    mensaje = formatear_mensaje(df, name_map, vix_valor)
    print("\n" + mensaje)

    enviar_telegram(mensaje)

if __name__ == "__main__":
    main()
