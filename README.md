# XAU Autonomous Bot COM

Bot do automatycznego handlu XAUUSD przez MetaTrader 5 oraz do kopiowania sygnalow z wybranych kanalow Telegram. Projekt jest przygotowany tak, zeby po sklonowaniu z GitHuba uzytkownik mogl przejsc przez konfigurator i wpisac swoje dane MT5, dane Telegram API, lot oraz kanaly.

Domyslne kanaly Telegram sa juz wpisane w konfiguracji:

```env
WATCH_CHANNELS=https://t.me/Gold_Pro_Trader_Forex_Signal,https://t.me/cryptoalertyt
```

## Wazne

To narzedzie moze skladac realne zlecenia na rachunku MT5. Najpierw uruchom je na koncie demo albo na minimalnym locie. Autor projektu nie odpowiada za wynik transakcji ani za bledna konfiguracje brokera, symbolu lub ryzyka.

## Wymagania

- Windows.
- Python 3.10 lub nowszy.
- Zainstalowany MetaTrader 5.
- Konto MT5 u brokera.
- Konto Telegram.
- `TELEGRAM_API_ID` i `TELEGRAM_API_HASH` z https://my.telegram.org/apps.

## Instalacja

```powershell
git clone <adres-twojego-repo>
cd xau_autonomus_bot_com
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Pakiet `MetaTrader5` dziala tylko wtedy, gdy terminal MT5 jest zainstalowany i dostepny na komputerze.

## Pierwsza konfiguracja

Mozesz uruchomic konfigurator osobno:

```powershell
python configure.py
```

Ponowna pelna konfiguracja:

```powershell
python configure.py --force
```

Albo po prostu uruchomic bota:

```powershell
python -u run_signal_bot.py
```

Jesli plik `.env` nie istnieje albo brakuje wymaganych danych, kreator zapyta o:

- login MT5,
- haslo MT5,
- serwer MT5,
- opcjonalna sciezke do `terminal64.exe`,
- lot dla sygnalow Telegram,
- minimalny, maksymalny i laczny lot,
- Telegram API ID,
- Telegram API HASH,
- telefon Telegram,
- opcjonalne haslo 2FA Telegram,
- kanaly Telegram do obserwowania.

Konfiguracja zapisuje sie lokalnie do `.env`. Ten plik jest ignorowany przez Git i nie powinien byc publikowany.

## Uruchomienie bota Telegram

```powershell
python -u run_signal_bot.py
```

Przy pierwszym logowaniu Telethon moze poprosic o kod z Telegrama. Sesja zostanie zapisana lokalnie w katalogu `data`.

Bot sygnalowy:

- slucha kanalow z `WATCH_CHANNELS`,
- szuka sygnalow `BUY`, `SELL`, `BUY LIMIT`, `SELL LIMIT`, `BUY STOP` albo `SELL STOP`,
- wymaga przynajmniej jednej linii `TP`,
- dla sygnalow market otwiera pozycje market na XAUUSD,
- dla sygnalow limit/stop sklada zlecenie oczekujace z ceny `Entry`,
- uzywa stalego lota z `SIGNAL_FIXED_LOT`,
- uzywa `SL` z sygnalu, a jesli go nie ma, liczy stop loss automatycznie z ATR i minimalnego bufora,
- domyslnie ustawia TP z drugiego poziomu sygnalu, jesli jest dostepny, a TP1 traktuje jako poziom ochrony,
- gdy cena przejdzie za TP1 o `SIGNAL_PROTECT_TP1_TRIGGER_PCT`, przesuwa SL na TP1,
- pomija sygnal, jezeli na symbolu jest juz aktywna pozycja albo aktywne zlecenie.

## Uruchomienie bota autonomicznego

```powershell
python -u run_bot.py
```

Ten tryb nie czyta Telegrama. Korzysta z lokalnych strategii z `app/strategies.py`, zarzadzania ryzykiem z `app/risk.py` i wykonuje zlecenia przez MT5.

## Dashboard

```powershell
python -u run_dashboard.py
```

Domyslny adres:

```text
http://127.0.0.1:8787
```

Port zmienisz w `.env`:

```env
DASHBOARD_PORT=8787
```

## Skrypty PowerShell

```powershell
.\start_signal_bot.ps1
.\stop_signal_bot.ps1
.\start_bot.ps1
.\start_dashboard.ps1
.\start_all.ps1
.\stop_all.ps1
```

`start_signal_bot.ps1` uruchamia bota sygnalowego w tle. Do pracy w konsoli i pierwszej autoryzacji Telegram wygodniejsze jest:

```powershell
python -u run_signal_bot.py
```

## Najwazniejsze ustawienia `.env`

```env
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=
MT5_PATH=

SYMBOL=XAUUSD
MIN_LOT=0.01
MAX_LOT=0.01
MAX_TOTAL_LOT=0.01
MAX_OPEN_POSITIONS=1
MAX_DAILY_DRAWDOWN_PCT=2.5
MAX_SPREAD_POINTS=120

TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=
TELEGRAM_2FA_PASSWORD=
WATCH_CHANNELS=https://t.me/Gold_Pro_Trader_Forex_Signal,https://t.me/cryptoalertyt

SIGNAL_FIXED_LOT=0.01
SIGNAL_SL_ATR_MULT=1.5
SIGNAL_SL_MIN_POINTS=50
SIGNAL_TP_TARGET_INDEX=2
SIGNAL_PROTECT_TP1_ENABLED=true
SIGNAL_PROTECT_TP1_TRIGGER_PCT=0.20
```

## Dodawanie kanalow Telegram

W `.env` wpisz kanaly po przecinku:

```env
WATCH_CHANNELS=https://t.me/Gold_Pro_Trader_Forex_Signal,https://t.me/cryptoalertyt,https://t.me/nazwa_kolejnego_kanalu
```

Mozesz tez uzyc username bez linku:

```env
WATCH_CHANNELS=Gold_Pro_Trader_Forex_Signal,cryptoalertyt
```

Po zmianie kanalow zrestartuj bota.

## Format sygnalu

Parser oczekuje wiadomosci z kierunkiem i przynajmniej jednym `TP`.
Pierwsza linia `TP` jest traktowana jako TP1. Domyslnie zlecenie dostaje TP2, jesli istnieje, a przy ruchu ceny 20% za TP1 bot przesuwa SL na TP1.
Rozpoznawane sa tez warianty `Target`, `Targets`, `TGT`, kilka TP w jednej linii po ukosniku oraz wejscia wpisane w linii z kierunkiem.

Przyklady:

```text
BUY GOLD@ 4500
TP1: 4505
TP2: 4510++
SL : PREMIUM
```

```text
SELL XAUUSD
TP1: 4470
TP2: 4465
```

```text
Gold sell limit

Entry 4462
SL 4482

TP 4452
TP 4432
TP 4400
```

```text
Gold Buy
Entry 4462
SL 4450
TP 4472
TP 4480
```

```text
Gold Sell Now @ 3330 - 3333
Stop Loss 3342
Target 3320
Target 3310
```

```text
XAUUSD BUY
ENTRY:3330
TP1: 3340
TP2 3350
TP3. 3360
SL: 3320
```

```text
Gold buy now @3330/3332
SL 3320
TP 3340/3350/3360
```

Formaty typu `TP: 50/100 Pips` sa wykrywane jako niejednoznaczne i nie sa automatycznie skladane jako zlecenia. Dla XAU rozni brokerzy licza pip/point inaczej, wiec taki parser powinien miec osobne ustawienie przelicznika.

Wiadomosci typu `TP hit`, `close trade`, `breakeven`, `update` sa ignorowane.

## Struktura projektu

- `app/config.py` - ladowanie konfiguracji.
- `app/setup_wizard.py` - pierwszy kreator `.env`.
- `app/mt5_gateway.py` - polaczenie i zlecenia MT5.
- `app/telegram_signal_bot.py` - listener Telegram i wejscia market.
- `app/engine.py` - autonomiczny bot strategii.
- `app/risk.py` - lot, limity, SL/TP i zarzadzanie pozycja.
- `app/dashboard.py` - lokalny dashboard.
- `app/agent_teams.py` - wieloinstrumentowy silnik specjalistow, Supervisora, rady kontroli i uczenia shadow.
- `app/strategy_lab.py` - 20 dodatkowych strategii wskaznikowych.
- `app/indicators.py` - wspolna ramka ponad 30 wskaznikow bez look-ahead.
- `scripts/backtest_market_machine.py` - chronologiczny backtest wielorynkowy ze spreadem MT5.
- `scripts/analyze_market_machine.py` - walk-forward, bootstrap, korelacje i selekcja portfela.
- `scripts/analyze_sizing_variants.py` - dynamiczny lot i badawcze porownanie martingale.
- `configure.py` - reczne uruchomienie kreatora.

Pelny opis Market Machine i audyt repozytoriow znajduja sie w
`docs/MARKET_MACHINE.md` oraz `docs/OPEN_SOURCE_RESEARCH.md`. Nowe strategie i
instrumenty startuja w trybie shadow. Martingale jest domyslnie wylaczony.

## Pliki prywatne

Do GitHuba nie wrzucaj:

- `.env`,
- `data/`,
- `logs/`,
- plikow `*.session`,
- `__pycache__/`.

Sa juz wpisane w `.gitignore`.
