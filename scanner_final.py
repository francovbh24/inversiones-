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
  5) RSI(14, 1h)                      -> INFORMATIVO en el mensaje,
                                          marca si esta sobre 80
  6) VIX                              -> INFORMATIVO en el mensaje,
                                          contexto general de mercado

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

UNIVERSO_JSON = os.path.join(os.path.dirname(__file__), "universo_completo.json")
BATCH_SIZE = 15
VIX_TICKER = "^VIX"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Tus activos en cartera - siempre se reportan, sin importar el estado
TICKERS_CARTERA = ["MU","INTC","AMD","LRCX","AMAT","ASML","TSM","ARM",
                    "ANET","GOOGL","NVDA","MELI","AVGO"]

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
    """Devuelve dict ticker -> DataFrame con columnas Close, High, Low"""
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
def escanear_ticker(ticker, df):
    c = df["Close"].dropna()
    h = df["High"]
    l = df["Low"]
    if len(c) < 1100:
        return None

    sma300 = c.rolling(300).mean()
    sma1000 = c.rolling(1000).mean()
    rsi = calcular_rsi(c, RSI_PERIODO)
    atr = calcular_atr(h, l, c, ATR_PERIODO)

    ultimo_precio = c.iloc[-1]
    ultimo_sma300 = sma300.iloc[-1]
    ultimo_sma1000 = sma1000.iloc[-1]
    penultimo_precio = c.iloc[-2]
    penultimo_sma300 = sma300.iloc[-2]
    ultimo_rsi = rsi.iloc[-1]
    ultimo_atr = atr.iloc[-1]

    if pd.isna(ultimo_sma300) or pd.isna(ultimo_sma1000):
        return None

    cond1_tendencia = ultimo_sma300 > ultimo_sma1000
    cruce_confirmado = (penultimo_precio < penultimo_sma300) and (ultimo_precio >= ultimo_sma300)
    distancia_pct = (ultimo_precio - ultimo_sma300) / ultimo_sma300 * 100

    # Umbral de cercania ajustado por volatilidad (ATR como % del precio)
    if not pd.isna(ultimo_atr) and ultimo_precio > 0:
        atr_pct = (ultimo_atr / ultimo_precio) * 100
        # El umbral nunca baja de la base, pero crece si el activo es mas volatil
        umbral_ajustado = max(UMBRAL_CERCANIA_BASE_PCT, atr_pct * 0.5)
    else:
        atr_pct = None
        umbral_ajustado = UMBRAL_CERCANIA_BASE_PCT

    cerca_de_entrada = (
        cond1_tendencia
        and ultimo_precio < ultimo_sma300
        and abs(distancia_pct) <= umbral_ajustado
    )

    if cond1_tendencia and cruce_confirmado:
        estado = "ENTRADA_CONFIRMADA"
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
        "rsi": round(float(ultimo_rsi), 1) if not pd.isna(ultimo_rsi) else None,
        "rsi_sobre_80": bool(ultimo_rsi > RSI_ALERTA) if not pd.isna(ultimo_rsi) else False,
        "tendencia_alcista": bool(cond1_tendencia),
    }

# ──────────────────────────────────────────────────────────
# FORMATEO DEL MENSAJE
# ──────────────────────────────────────────────────────────
def formatear_mensaje(df, name_map, vix_valor):
    ahora = datetime.now().strftime('%Y-%m-%d %H:%M')
    confirmadas = df[df['estado'] == 'ENTRADA_CONFIRMADA'].sort_values('distancia_pct_a_sma300')
    cercanas = df[df['estado'] == 'CERCA_DE_ENTRADA'].sort_values('distancia_pct_a_sma300', ascending=False)

    lineas = [f"<b>SCANNER SMA — {ahora}</b>"]
    vix_txt = f"{vix_valor}" if vix_valor is not None else "N/D"
    lineas.append(f"VIX actual: {vix_txt}")
    lineas.append("")

    def fmt_rsi(r):
        if r['rsi'] is None:
            return ""
        flag = " ⚠️RSI&gt;80" if r['rsi_sobre_80'] else f" RSI {r['rsi']}"
        return flag

    if len(confirmadas):
        lineas.append(f"🟢 <b>ENTRADAS CONFIRMADAS ({len(confirmadas)})</b>")
        for _, r in confirmadas.iterrows():
            nombre = name_map.get(r['ticker'], r['ticker'])
            lineas.append(f"• {r['ticker']} ({nombre[:25]}) — ${r['precio_actual']} | +{r['distancia_pct_a_sma300']}%{fmt_rsi(r)}")
        lineas.append("")
    else:
        lineas.append("🟢 Sin entradas confirmadas esta hora")
        lineas.append("")

    if len(cercanas):
        lineas.append(f"🟡 <b>CERCA DE ENTRADA (umbral ajustado por ATR, {len(cercanas)})</b>")
        for _, r in cercanas.head(15).iterrows():
            nombre = name_map.get(r['ticker'], r['ticker'])
            lineas.append(f"• {r['ticker']} ({nombre[:25]}) — ${r['precio_actual']} | dist {r['distancia_pct_a_sma300']}% (umbral {r['umbral_ajustado_pct']}%){fmt_rsi(r)}")

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

    print("Descargando datos 1h de Yahoo Finance...")
    data_dict = descargar_datos_1h(tickers)
    print(f"\nTickers con datos suficientes: {len(data_dict)}\n")

    print("Analizando condiciones de entrada...")
    resultados = []
    for t, df_t in data_dict.items():
        r = escanear_ticker(t, df_t)
        if r:
            resultados.append(r)

    df = pd.DataFrame(resultados)
    df.to_csv("scanner_resultados.csv", index=False)

    mensaje = formatear_mensaje(df, name_map, vix_valor)
    print("\n" + mensaje)

    enviar_telegram(mensaje)

if __name__ == "__main__":
    main()
