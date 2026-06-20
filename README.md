# Scanner SMA CEDEARs

Escanea automaticamente el universo completo de activos con CEDEAR en Argentina
(375 acciones/ETFs de USA) mas las 20 acciones principales que cotizan
directo en BYMA, y avisa por Telegram cuando alguno cumple o esta cerca
de cumplir la condicion de entrada de tu estrategia.

## Parametros de analisis

1. SMA300(1h) > SMA1000(1h)        -> filtro de tendencia de fondo
2. Cruce alcista precio/SMA300(1h)  -> trigger de entrada confirmada
3. Distancia % del precio a SMA300  -> deteccion de "cerca de entrar"
4. ATR(14, 1h)                      -> ajusta automaticamente el umbral
                                        de cercania segun la volatilidad
                                        propia de cada activo (un activo
                                        volatil como TSLA tiene un umbral
                                        mas amplio que uno tranquilo como KO)
5. RSI(14, 1h)                      -> INFORMATIVO, aparece en el mensaje,
                                        marca con advertencia si supera 80
6. VIX                              -> INFORMATIVO, aparece en cada mensaje
                                        como contexto general de mercado

Todos los calculos se hacen sobre el precio NATIVO de cada activo
(USD para CEDEARs de EEUU, ARS para acciones que cotizan directo en BYMA).

## Universo de activos

- 375 CEDEARs disponibles en BYMA, con subyacente en NASDAQ/NYSE
  (extraidos del PDF oficial de BYMA)
- 20 acciones del Panel Lider argentino que cotizan directo en BYMA
  (sufijo .BA en Yahoo Finance): GGAL, YPFD, PAMP, BMA, ALUA, CEPU,
  TXAR, TGNO4, TGSU2, LOMA, SUPV, BYMA, CRES, EDN, TRAN, VALO, COME,
  MIRG, CVH, BBAR

## Como activarlo (una sola vez)

### 1. Subir esta carpeta a GitHub
1. Crea una cuenta en https://github.com si no tenes
2. Crea un repositorio nuevo, PRIVADO
3. Sube todo el contenido de esta carpeta, incluyendo la carpeta oculta
   .github/workflows/

### 2. Crear el Bot de Telegram
1. En Telegram, busca @BotFather y mandale /newbot
2. Te da un TOKEN (algo como 123456:ABC...)
3. Mandale cualquier mensaje a tu bot recien creado
4. Abri en el navegador:
   https://api.telegram.org/bot<TU_TOKEN>/getUpdates
5. Copia el numero de "chat":{"id": XXXXXXX} -> ese es tu CHAT_ID

### 3. Configurar los Secrets en GitHub
1. En tu repositorio: Settings > Secrets and variables > Actions
2. New repository secret, crear DOS:
   - TELEGRAM_BOT_TOKEN -> el token de BotFather
   - TELEGRAM_CHAT_ID   -> tu chat_id

### 4. Listo
El workflow corre automaticamente cada 30 minutos, de lunes a viernes,
en horario de mercado de EEUU. No necesitas dejar nada prendido.

Para probarlo manualmente: pestana "Actions" en tu repo > "Scanner SMA
CEDEARs" > "Run workflow"

## Archivos
- scanner_final.py            -> script principal
- universo_completo.json      -> los 395 tickers (375 CEDEARs + 20 BYMA)
- requirements.txt            -> dependencias de Python
- .github/workflows/scanner.yml -> configuracion de automatizacion (cron)

## Personalizar
- UMBRAL_CERCANIA_BASE_PCT en scanner_final.py: umbral base antes del
  ajuste por ATR (default 1.0%)
- RSI_ALERTA: nivel de RSI que dispara la advertencia (default 80)
- TICKERS_CARTERA: tu lista de activos en cartera, se reportan aparte
  siempre en cada mensaje
- El cron en scanner.yml: horario y frecuencia (formato UTC)
