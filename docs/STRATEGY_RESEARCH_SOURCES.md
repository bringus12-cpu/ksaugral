# Codzienne badania strategii

Zrodla: GitHub oraz GitLab, dodatkowo siedem bibliotek:

| Zrodlo | Adres | Zakres |
| --- | --- | --- |
| MQL5 Code Base | https://www.mql5.com/en/code/mt5 | MQ5, wskazniki, Expert Advisors |
| TradingView | https://www.tradingview.com/scripts/ | Otwarte Pine Script; wymagaja adaptacji |
| QuantConnect | https://www.quantconnect.com/forum/ | Przyklady algorytmow i dolaczone backtesty |
| ProRealCode | https://www.prorealcode.com/prorealtime-trading-strategies/ | Strategie ProRealTime; wymagaja adaptacji |
| SourceForge | https://sourceforge.net/directory/financial/ | Narzedzia analityczne, biblioteki i starsze projekty |
| Hugging Face | https://huggingface.co/ | Modele, datasety i Spaces do badan ML oraz szeregow czasowych |
| Kaggle | https://www.kaggle.com/search?q=xauusd | Datasety i notebooki; wymagaja kontroli jakosci i licencji |

Kazdy kandydat otrzymuje URL, rewizje, date pobrania, licencje,
opis regul wejscia/wyjscia oraz status: discovered, reviewed, adapted,
tested albo rejected. Brak licencji nie oznacza zgody na dystrybucje.
Wynik lokalnego odpowiednika nie jest wynikiem cudzego bota.

Nowe pliki od uzytkownika analizujemy statycznie. Plik EX5 bez MQ5
nie pozwala sprawdzic regul z kodu zrodlowego. Pakiety do badan trafiaja
do osobnego katalogu tmp/research_inbox, bez danych logowania.

Protokol: 60 zakonczonych sesji, zakres dat i pokrycie dla kazdego
instrumentu, wejscie po dostepnym sygnale, koszty brokera, konserwatywna
kolejnosc SL/TP. Ostatnie 20 sesji pozostaje poza doborem parametrow.
Raport: PnL, PF, WR, liczba setupow, DD zamkniety i equity (jesli
dostepne), koszty, stabilnosc okresow i korelacje. Braki tickow,
swapow albo stop-outu musza byc jawnie oznaczone.

Codzienny skan nie instaluje automatycznie obcego kodu przy terminalu live.
Adaptacje wykonujemy w silniku badawczym i porownujemy z baza.
Duza liczba testow zwieksza ryzyko dopasowania do historii.

Na Hugging Face skaner czyta metadane modeli, datasetow i Spaces.
Datasety sa oznaczane jako kandydaci do walidacji; modeli, wag i kodu
nie uruchamia automatycznie.

Nowe implementacje badawcze: DeMarker, Vortex, Chandelier Exit
w app/research_indicators.py. Nie sa aktywnymi strategiami handlowymi.
