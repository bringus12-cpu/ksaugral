from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import load_settings
from .mt5_gateway import (
    Mt5Credentials,
    account_info,
    connect,
    ensure_symbol,
    get_tick,
    orders_by_magic,
    positions_by_magic,
    shutdown,
    symbol_info,
    mt5,
)
from .risk import normalize_volume


CONFIG_FIELDS: list[dict[str, str]] = [
    {"key": "MT5_LOGIN", "label": "MT5 Login", "group": "mt5", "type": "number"},
    {"key": "MT5_PASSWORD", "label": "MT5 Password", "group": "mt5", "type": "password"},
    {"key": "MT5_SERVER", "label": "MT5 Server", "group": "mt5", "type": "text"},
    {"key": "MT5_PATH", "label": "Terminal MT5", "group": "mt5", "type": "text"},
    {"key": "MT5_SYMBOL", "label": "Symbol", "group": "mt5", "type": "text"},
    {"key": "TELEGRAM_API_ID", "label": "Telegram API ID", "group": "telegram", "type": "number"},
    {"key": "TELEGRAM_API_HASH", "label": "Telegram API Hash", "group": "telegram", "type": "password"},
    {"key": "TELEGRAM_PHONE", "label": "Telefon Telegram", "group": "telegram", "type": "text"},
    {"key": "TELEGRAM_2FA_PASSWORD", "label": "Telegram 2FA", "group": "telegram", "type": "password"},
    {"key": "TELEGRAM_SESSION_NAME", "label": "Nazwa sesji", "group": "telegram", "type": "text"},
    {"key": "SIGNAL_LOT_MODE", "label": "Tryb lota", "group": "risk", "type": "select:fixed,profit_dynamic,funded_safe,equity_safe,risk_pct"},
    {"key": "SIGNAL_RISK_PCT", "label": "Ryzyko sygnalu %", "group": "risk", "type": "number"},
    {"key": "SIGNAL_FIXED_LOT", "label": "Bazowy lot", "group": "risk", "type": "number"},
    {"key": "SIGNAL_DYNAMIC_LOT_ENABLED", "label": "Dynamiczny lot", "group": "risk", "type": "bool"},
    {"key": "SIGNAL_DYNAMIC_LOT_STEP_USD", "label": "Co ile USD", "group": "risk", "type": "number"},
    {"key": "SIGNAL_DYNAMIC_LOT_ADD", "label": "Dodaj lot", "group": "risk", "type": "number"},
    {"key": "SIGNAL_DYNAMIC_LOT_MAX", "label": "Max dynamiczny lot", "group": "risk", "type": "number"},
    {"key": "SIGNAL_FUNDED_ACCOUNT_BALANCE", "label": "Saldo konta funded", "group": "risk", "type": "number"},
    {"key": "SIGNAL_FUNDED_SAFE_SIGNAL_LOT_PER_100K", "label": "Lot/100k funded", "group": "risk", "type": "number"},
    {"key": "SIGNAL_FUNDED_SKIP_BELOW_MIN", "label": "Pomijaj ponizej min", "group": "risk", "type": "bool"},
    {"key": "SIGNAL_ENTRY_MODE", "label": "Tryb wejsc", "group": "strategy", "type": "select:single,all3"},
    {"key": "SIGNAL_PENDING_EXPIRY_MINUTES", "label": "Wygaszenie pendingow min", "group": "strategy", "type": "number"},
    {"key": "SIGNAL_PROTECT_TP1_ENABLED", "label": "Ochrona po TP1", "group": "strategy", "type": "bool"},
    {"key": "SIGNAL_PROTECT_TP1_TRIGGER_PCT", "label": "Trigger TP1 %", "group": "strategy", "type": "number"},
    {"key": "SIGNAL_SL_ATR_MULT", "label": "ATR SL mult", "group": "strategy", "type": "number"},
    {"key": "SIGNAL_SL_MIN_POINTS", "label": "Min SL points", "group": "strategy", "type": "number"},
    {"key": "MAX_SPREAD_POINTS", "label": "Max spread", "group": "strategy", "type": "number"},
    {"key": "MAX_OPEN_POSITIONS", "label": "Max pozycji", "group": "risk", "type": "number"},
    {"key": "MAX_TOTAL_LOT", "label": "Max laczny lot", "group": "risk", "type": "number"},
    {"key": "XAU_SCALP_RISK_PCT", "label": "Ryzyko scalpera %", "group": "risk", "type": "number"},
    {"key": "MAX_DAILY_DRAWDOWN_PCT", "label": "Max DD dzienny %", "group": "risk", "type": "number"},
    {"key": "MAX_TOTAL_DRAWDOWN_PCT", "label": "Max DD calkowity %", "group": "risk", "type": "number"},
    {"key": "SIGNAL_SESSION_NET_PROFIT_STOP_USD", "label": "Stop zysk sesji", "group": "risk", "type": "number"},
    {"key": "SIGNAL_SESSION_NET_LOSS_STOP_USD", "label": "Stop strata sesji", "group": "risk", "type": "number"},
    {"key": "SIGNAL_ADAPTIVE_LEARNING_ENABLED", "label": "Tryb nauki", "group": "strategy", "type": "bool"},
    {"key": "XAU_SCALP_ENABLED", "label": "XAU Scalp wlaczony", "group": "xau_scalp", "type": "bool"},
    {"key": "XAU_SCALP_LOT", "label": "Lot sygnalu", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_MAX_POSITIONS", "label": "Max pozycji", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_MAX_SPREAD_POINTS", "label": "Max spread", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_SL_USD", "label": "SL USD", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_TP1_USD", "label": "TP1 USD", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_TP2_USD", "label": "TP2 USD", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_BE_TRIGGER_USD", "label": "BE trigger USD", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_BE_BUFFER_USD", "label": "BE bufor USD", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_COOLDOWN_SECONDS", "label": "Cooldown sek.", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_SAME_DIRECTION_CHASE_FILTER_ENABLED", "label": "Filtr gonienia ruchu", "group": "xau_scalp", "type": "bool"},
    {"key": "XAU_SCALP_MAX_SAME_DIRECTION_M1_MOVE_USD", "label": "Max ruch M1 USD", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_MAX_SAME_DIRECTION_M5_MOVE_USD", "label": "Max ruch M5 USD", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_DIRECTION_LOSS_BLOCK_ENABLED", "label": "Blokada kierunku po SL", "group": "xau_scalp", "type": "bool"},
    {"key": "XAU_SCALP_DIRECTION_LOSS_BLOCK_SECONDS", "label": "Blokada kierunku sek.", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_DAILY_DD_PCT", "label": "DD dzienny %", "group": "xau_scalp", "type": "number"},
    {"key": "XAU_SCALP_TOTAL_DD_PCT", "label": "DD calk. %", "group": "xau_scalp", "type": "number"},
    {"key": "SIGNAL_MIN_MARKET_TP1_RR", "label": "Min RR market TP1", "group": "signals", "type": "number"},
    {"key": "XAU_CONTR_SCALP_ENABLED", "label": "Kontrscalper wlaczony", "group": "xau_contr", "type": "bool"},
    {"key": "XAU_CONTR_SCALP_MODE", "label": "Tryb", "group": "xau_contr", "type": "select:safe,normal,aggressive,mega"},
    {"key": "XAU_CONTR_SCALP_LOT_FACTOR", "label": "Mnoznik lota", "group": "xau_contr", "type": "number"},
    {"key": "XAU_CONTR_SCALP_MAX_POSITIONS", "label": "Max pozycji", "group": "xau_contr", "type": "number"},
    {"key": "XAU_CONTR_SCALP_MAX_SPREAD_POINTS", "label": "Max spread", "group": "xau_contr", "type": "number"},
    {"key": "XAU_CONTR_SCALP_TP_USD", "label": "TP USD", "group": "xau_contr", "type": "number"},
    {"key": "XAU_CONTR_SCALP_SL_USD", "label": "SL USD", "group": "xau_contr", "type": "number"},
    {"key": "XAU_CONTR_SCALP_TIMEOUT_MINUTES", "label": "Timeout min", "group": "xau_contr", "type": "number"},
    {"key": "XAU_CONTR_SCALP_DAILY_DD_PCT", "label": "DD dzienny %", "group": "xau_contr", "type": "number"},
    {"key": "XAU_CONTR_SCALP_TOTAL_DD_PCT", "label": "DD calk. %", "group": "xau_contr", "type": "number"},
    {"key": "DASHBOARD_PORT", "label": "Port dashboardu", "group": "system", "type": "number"},
]

CONFIG_KEYS = {field["key"] for field in CONFIG_FIELDS}


HTML = """<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>xao Graal Dashboard</title>
  <style>
    :root {
      --bg: #101216;
      --band: #171a20;
      --panel: #20242c;
      --panel2: #252b35;
      --line: #3a414d;
      --text: #f2f4f7;
      --muted: #a9b1bd;
      --gold: #d6b75d;
      --green: #4fc47b;
      --red: #e76969;
      --blue: #69a8ff;
      --input: #14171d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      background: var(--bg);
      font-family: "Segoe UI", Tahoma, Arial, sans-serif;
      font-size: 14px;
    }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 5;
      background: #12151a;
      border-bottom: 1px solid var(--line);
    }
    .top-inner {
      width: min(1480px, calc(100vw - 24px));
      margin: 0 auto;
      min-height: 64px;
      display: grid;
      grid-template-columns: minmax(240px, 1fr) auto;
      align-items: center;
      gap: 16px;
    }
    .brand { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
    .brand h1 { margin: 0; font-size: 22px; letter-spacing: 0; font-weight: 700; }
    .brand span { color: var(--muted); white-space: nowrap; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .wrap { width: min(1480px, calc(100vw - 24px)); margin: 16px auto 28px; }
    .tabs {
      display: flex;
      gap: 6px;
      padding: 8px 0 14px;
      overflow-x: auto;
    }
    .tab {
      border: 1px solid var(--line);
      color: var(--muted);
      background: var(--band);
      border-radius: 8px;
      padding: 9px 12px;
      cursor: pointer;
      white-space: nowrap;
    }
    .tab.active { color: var(--text); border-color: var(--gold); background: #24251e; }
    .view { display: none; }
    .view.active { display: block; }
    .grid { display: grid; gap: 12px; }
    .cols-2 { grid-template-columns: 1.15fr 0.85fr; }
    .cols-3 { grid-template-columns: repeat(3, 1fr); }
    .cols-4 { grid-template-columns: repeat(4, 1fr); }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    .section-title {
      margin: 0 0 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font-size: 16px;
      font-weight: 700;
    }
    .muted { color: var(--muted); }
    .small { font-size: 12px; }
    .stat { min-height: 94px; }
    .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
    .value { font-size: 24px; line-height: 1.15; margin-top: 8px; font-weight: 700; overflow-wrap: anywhere; }
    .good { color: var(--green); }
    .bad { color: var(--red); }
    .blue { color: var(--blue); }
    .gold { color: var(--gold); }
    button, input, select, textarea {
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      background: var(--panel2);
      color: var(--text);
      border-radius: 8px;
      padding: 9px 12px;
      cursor: pointer;
    }
    button:hover { border-color: var(--gold); }
    button.primary { background: #3d3520; border-color: #6f5a24; }
    button.danger { background: #3a2022; border-color: #754046; }
    button.ghost { background: transparent; }
    input, select, textarea {
      width: 100%;
      color: var(--text);
      background: var(--input);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      outline: none;
    }
    input:focus, select:focus, textarea:focus { border-color: var(--gold); }
    textarea { min-height: 96px; resize: vertical; }
    .form-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .field label { display: block; color: var(--muted); font-size: 12px; margin-bottom: 5px; }
    .switch-row {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      gap: 10px;
      padding: 8px 0;
      border-bottom: 1px solid rgba(255,255,255,.05);
    }
    .switch-row:last-child { border-bottom: 0; }
    .toggle {
      width: 48px;
      height: 26px;
      border-radius: 999px;
      position: relative;
      border: 1px solid var(--line);
      background: #11151c;
      cursor: pointer;
    }
    .toggle::after {
      content: "";
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: var(--muted);
      position: absolute;
      top: 2px;
      left: 3px;
      transition: .15s;
    }
    .toggle.on { background: #193524; border-color: #34734b; }
    .toggle.on::after { left: 23px; background: var(--green); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 9px 7px; border-bottom: 1px solid rgba(255,255,255,.06); vertical-align: top; }
    th { color: var(--muted); font-weight: 600; }
    .table-wrap { overflow-x: auto; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 8px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: var(--panel2);
      color: var(--muted);
      margin: 0 6px 6px 0;
      max-width: 100%;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); display: inline-block; }
    .dot.good { background: var(--green); }
    .dot.bad { background: var(--red); }
    .dot.gold { background: var(--gold); }
    .channel-list { display: grid; gap: 8px; }
    .channel-item {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 120px auto auto auto;
      gap: 8px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--band);
    }
    .channel-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .channel-lot input { min-width: 0; }
    .channel-lot-label { color: var(--muted); font-size: 11px; margin-bottom: 4px; }
    .logbox {
      max-height: 460px;
      overflow: auto;
      display: grid;
      gap: 8px;
    }
    .log {
      padding: 10px;
      border: 1px solid rgba(255,255,255,.06);
      border-radius: 8px;
      background: #151920;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: Consolas, monospace;
      font-size: 12px;
    }
    .notice {
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #171b22;
      color: var(--muted);
      margin-bottom: 12px;
    }
    .status-line { min-height: 20px; color: var(--muted); margin-top: 8px; }
    @media (max-width: 1100px) {
      .cols-2, .cols-3, .cols-4 { grid-template-columns: 1fr 1fr; }
      .form-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      .top-inner { grid-template-columns: 1fr; padding: 10px 0; }
      .actions { justify-content: flex-start; }
      .cols-2, .cols-3, .cols-4 { grid-template-columns: 1fr; }
      .channel-item { grid-template-columns: 1fr; }
      .brand { display: block; }
      .brand span { display: block; margin-top: 4px; }
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="top-inner">
      <div class="brand">
        <h1>xao Graal</h1>
        <span id="subtitle">dashboard bota sygnalowego</span>
      </div>
      <div class="actions">
        <button class="primary" id="startBot">Start bot</button>
        <button class="danger" id="stopBot">Stop bot</button>
        <button id="refreshNow">Odśwież</button>
      </div>
    </div>
  </div>

  <main class="wrap">
    <div class="tabs">
      <button class="tab active" data-view="overview">Przegląd</button>
      <button class="tab" data-view="channels">Kanały</button>
      <button class="tab" data-view="analyzer">Analizator</button>
      <button class="tab" data-view="config">Konfiguracja</button>
      <button class="tab" data-view="trades">Pozycje</button>
      <button class="tab" data-view="xauscalp">XAU Scalp</button>
      <button class="tab" data-view="contrscalp">Kontr Scalper</button>
      <button class="tab" data-view="agentteams">Agent Teams</button>
      <button class="tab" data-view="signals">Sygnały</button>
      <button class="tab" data-view="logs">Logi</button>
    </div>

    <section class="view active" id="view-overview">
      <div class="notice" id="systemNotice">Ładowanie statusu...</div>
      <div class="grid cols-4" id="stats"></div>
      <div class="grid cols-2" style="margin-top:12px;">
        <div class="panel">
          <div class="section-title">Bot i konto <span class="small muted" id="heartbeat">-</span></div>
          <div id="botPills"></div>
          <div class="table-wrap" style="margin-top:10px;"><table id="accountTable"></table></div>
        </div>
        <div class="panel">
          <div class="section-title">Lot i ryzyko</div>
          <div class="table-wrap"><table id="lotTable"></table></div>
        </div>
      </div>
    </section>

    <section class="view" id="view-channels">
      <div class="grid cols-2">
        <div class="panel">
          <div class="section-title">Obserwowane kanały</div>
          <div class="field">
            <label>Dodaj kanał, link lub identyfikator</label>
            <div style="display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;">
              <input id="channelInput" placeholder="https://t.me/... albo -100..." autocomplete="off">
              <button class="primary" id="addChannel">Dodaj</button>
            </div>
          </div>
          <div class="status-line" id="channelStatus"></div>
          <div class="channel-list" id="channelList"></div>
        </div>
        <div class="panel">
          <div class="section-title">Szybkie ustawienia kanałów</div>
          <div class="notice">Po zmianie kanałów zrestartuj bota, żeby zaczął słuchać nowej listy.</div>
          <textarea id="channelsBulk" spellcheck="false"></textarea>
          <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;">
            <button class="primary" id="saveBulkChannels">Zapisz listę</button>
            <button id="reloadChannels">Wczytaj ponownie</button>
          </div>
        </div>
      </div>
    </section>

    <section class="view" id="view-analyzer">
      <div class="grid cols-4" id="channelAnalyzerStats"></div>
      <div class="panel" style="margin-top:12px;">
        <div class="section-title">Analiza wszystkich obserwowanych kanałów <span class="small muted" id="channelAnalyzerTime">-</span></div>
        <div class="table-wrap"><table id="channelAnalyzerTable"></table></div>
      </div>
    </section>

    <section class="view" id="view-config">
      <div class="grid cols-2">
        <div class="panel">
          <div class="section-title">MT5</div>
          <div class="form-grid" id="config-mt5"></div>
        </div>
        <div class="panel">
          <div class="section-title">Telegram API</div>
          <div class="form-grid" id="config-telegram"></div>
        </div>
        <div class="panel">
          <div class="section-title">Loty i ryzyko</div>
          <div class="form-grid" id="config-risk"></div>
        </div>
        <div class="panel">
          <div class="section-title">Strategia</div>
          <div class="form-grid" id="config-strategy"></div>
        </div>
        <div class="panel">
          <div class="section-title">XAU Scalp</div>
          <div class="form-grid" id="config-xau_scalp"></div>
        </div>
        <div class="panel">
          <div class="section-title">Kontr Scalper</div>
          <div class="form-grid" id="config-xau_contr"></div>
        </div>
        <div class="panel">
          <div class="section-title">System</div>
          <div class="form-grid" id="config-system"></div>
        </div>
        <div class="panel">
          <div class="section-title">Akcje</div>
          <div class="notice">Zmiany zapisują się do pliku .env w folderze xao Graal. Bot musi być zrestartowany, żeby wziął nową konfigurację.</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <button class="primary" id="saveConfig">Zapisz konfigurację</button>
            <button id="reloadConfig">Odczytaj z .env</button>
          </div>
          <div class="status-line" id="configStatus"></div>
        </div>
      </div>
    </section>

    <section class="view" id="view-trades">
      <div class="grid cols-2">
        <div class="panel">
          <div class="section-title">Otwarte pozycje</div>
          <div class="table-wrap"><table id="positionsTable"></table></div>
        </div>
        <div class="panel">
          <div class="section-title">Zlecenia oczekujące</div>
          <div class="table-wrap"><table id="ordersTable"></table></div>
        </div>
        <div class="panel">
          <div class="section-title">Ostatnie zamknięcia</div>
          <div class="table-wrap"><table id="tradesTable"></table></div>
        </div>
        <div class="panel">
          <div class="section-title">Dzisiejsze podsumowanie</div>
          <div class="table-wrap"><table id="todayTable"></table></div>
        </div>
      </div>
    </section>

    <section class="view" id="view-xauscalp">
      <div class="grid cols-4" id="xauScalpStats"></div>
      <div class="grid cols-2" style="margin-top:12px;">
        <div class="panel">
          <div class="section-title">XAU Scalp status <span class="small muted" id="xauScalpHeartbeat">-</span></div>
          <div id="xauScalpPills"></div>
          <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;">
            <button class="primary" id="startXauScalp">Start XAU Scalp</button>
            <button class="danger" id="stopXauScalp">Stop XAU Scalp</button>
          </div>
        </div>
        <div class="panel">
          <div class="section-title">Ryzyko i parametry</div>
          <div class="table-wrap"><table id="xauScalpRiskTable"></table></div>
        </div>
        <div class="panel">
          <div class="section-title">Otwarte pozycje XAU Scalp</div>
          <div class="table-wrap"><table id="xauScalpPositionsTable"></table></div>
        </div>
        <div class="panel">
          <div class="section-title">Ostatnie zamknięcia XAU Scalp</div>
          <div class="table-wrap"><table id="xauScalpTradesTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Log XAU Scalp</div>
          <div class="logbox" id="xauScalpEvents"></div>
        </div>
      </div>
    </section>

    <section class="view" id="view-contrscalp">
      <div class="grid cols-4" id="contrScalpStats"></div>
      <div class="grid cols-2" style="margin-top:12px;">
        <div class="panel">
          <div class="section-title">Kontr Scalper status <span class="small muted" id="contrScalpHeartbeat">-</span></div>
          <div id="contrScalpPills"></div>
          <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;">
            <button class="primary" id="startContrScalp">Start Kontr Scalper</button>
            <button class="danger" id="stopContrScalp">Stop Kontr Scalper</button>
          </div>
        </div>
        <div class="panel">
          <div class="section-title">Ryzyko i parametry</div>
          <div class="table-wrap"><table id="contrScalpRiskTable"></table></div>
        </div>
        <div class="panel">
          <div class="section-title">Otwarte pozycje Kontr Scalper</div>
          <div class="table-wrap"><table id="contrScalpPositionsTable"></table></div>
        </div>
        <div class="panel">
          <div class="section-title">Ostatnie zamkniecia Kontr Scalper</div>
          <div class="table-wrap"><table id="contrScalpTradesTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Log Kontr Scalper</div>
          <div class="logbox" id="contrScalpEvents"></div>
        </div>
      </div>
    </section>

    <section class="view" id="view-agentteams">
      <div class="grid cols-4" id="agentTeamStats"></div>
      <div class="grid cols-2" style="margin-top:12px;">
        <div class="panel">
          <div class="section-title">Agent Teams <span class="small muted" id="agentTeamHeartbeat">-</span></div>
          <div id="agentTeamPills"></div>
          <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;">
            <button class="primary" id="startAgentTeams">Start Agent Teams</button>
            <button class="danger" id="stopAgentTeams">Stop Agent Teams</button>
          </div>
        </div>
        <div class="panel">
          <div class="section-title">Zasada decyzji</div>
          <div class="table-wrap"><table id="agentTeamRulesTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Market Machine Analytics <span class="small muted" id="marketAnalyticsUpdate">-</span></div>
          <div id="marketAnalyticsPills"></div>
          <div class="section-title" style="margin-top:14px;">Stabilne pary instrument / strategia</div>
          <div class="table-wrap"><table id="marketAnalyticsTable"></table></div>
          <div class="section-title" style="margin-top:14px;">Dynamiczny lot i warianty odrabiania</div>
          <div class="table-wrap"><table id="marketSizingTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">GitHub Strategy Scout <span class="small muted" id="githubScoutUpdate">-</span></div>
          <div id="githubScoutPills"></div>
          <div class="notice" style="margin-top:10px;">Repozytoria są analizowane statycznie. Obcy kod nie jest uruchamiany; pomysły przechodzą przez lokalny backtester i walk-forward.</div>
          <div class="section-title" style="margin-top:14px;">Najwyżej ocenione repozytoria</div>
          <div class="table-wrap"><table id="githubScoutRepositoriesTable"></table></div>
          <div class="section-title" style="margin-top:14px;">Wyniki lokalnych testów</div>
          <div class="table-wrap"><table id="githubScoutTestsTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Meta Learning Agent <span class="small muted" id="agentLearningUpdate">-</span></div>
          <div id="agentLearningPills"></div>
          <div class="table-wrap" style="margin-top:10px;"><table id="agentLearningTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Instrument Scout <span class="small muted" id="instrumentScoutUpdate">-</span></div>
          <div id="instrumentScoutPills"></div>
          <div class="table-wrap" style="margin-top:10px;"><table id="instrumentScoutTeamsTable"></table></div>
          <div class="section-title" style="margin-top:14px;">Najlepsi kandydaci</div>
          <div class="table-wrap"><table id="instrumentScoutCandidatesTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Rada Kontroli Agentow <span class="small muted" id="controlTeamUpdate">-</span></div>
          <div id="controlTeamPills"></div>
          <div class="table-wrap" style="margin-top:10px;"><table id="controlTeamMembersTable"></table></div>
          <div class="section-title" style="margin-top:14px;">Audyt zamknietych trade'ow</div>
          <div class="table-wrap"><table id="controlTradeReviewsTable"></table></div>
          <div class="section-title" style="margin-top:14px;">Wspolna komunikacja ekspertow i kierownikow</div>
          <div class="logbox" id="controlTeamMessages"></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Brygada Long Term</div>
          <div id="longTermPills"></div>
          <div class="table-wrap" style="margin-top:10px;"><table id="longTermTeamsTable"></table></div>
          <div class="section-title" style="margin-top:14px;">Eksperci H1 / H4 / D1</div>
          <div class="table-wrap"><table id="longTermAgentsTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Ekipy i decyzje Supervisora</div>
          <div class="table-wrap"><table id="agentTeamsTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Głosy agentów</div>
          <div class="table-wrap"><table id="agentVotesTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Ostatnie transakcje i symulacje</div>
          <div class="table-wrap"><table id="agentTradesTable"></table></div>
        </div>
        <div class="panel" style="grid-column:1/-1;">
          <div class="section-title">Zdarzenia ekip</div>
          <div class="logbox" id="agentTeamEvents"></div>
        </div>
      </div>
    </section>

    <section class="view" id="view-signals">
      <div class="grid cols-2">
        <div class="panel">
          <div class="section-title">Ostatnie sygnały</div>
          <div class="logbox" id="signalEvents"></div>
        </div>
        <div class="panel">
          <div class="section-title">Ostatnie zlecenia z sygnałów</div>
          <div class="logbox" id="orderEvents"></div>
        </div>
      </div>
    </section>

    <section class="view" id="view-logs">
      <div class="grid cols-2">
        <div class="panel">
          <div class="section-title">Log bota</div>
          <div class="logbox" id="botLog"></div>
        </div>
        <div class="panel">
          <div class="section-title">Błędy / diagnostyka</div>
          <div class="logbox" id="errLog"></div>
        </div>
      </div>
    </section>
  </main>

  <script>
    const state = { channels: [], channelLots: {}, configFields: [], configValues: {}, overview: null, refreshing: false };
    const qs = (s) => document.querySelector(s);
    const qsa = (s) => Array.from(document.querySelectorAll(s));
    const esc = (v) => String(v ?? "-").replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
    const fmt = (n, d=2) => n === null || n === undefined || Number.isNaN(Number(n)) ? "-" : Number(n).toFixed(d);

    function table(headers, rows) {
      return `<tr>${headers.map(h => `<th>${esc(h)}</th>`).join("")}</tr>` + rows.join("");
    }
    function row(cells) {
      return `<tr>${cells.map(c => `<td>${c}</td>`).join("")}</tr>`;
    }
    async function api(path, options={}) {
      const res = await fetch(path, options);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      return data;
    }

    qsa(".tab").forEach(btn => btn.addEventListener("click", () => {
      qsa(".tab").forEach(x => x.classList.remove("active"));
      qsa(".view").forEach(x => x.classList.remove("active"));
      btn.classList.add("active");
      qs(`#view-${btn.dataset.view}`).classList.add("active");
    }));

    function renderStats(data) {
      const s = data.status || {};
      const account = s.account || {};
      const risk = s.risk || {};
      const lot = s.lot || {};
      const bot = data.bot || {};
      const pnl = Number(account.profit || 0);
      const stats = [
        ["Balance", fmt(account.balance), "gold"],
        ["Equity", fmt(account.equity), "blue"],
        ["Aktualny PnL", fmt(pnl), pnl >= 0 ? "good" : "bad"],
        ["Spread", fmt(risk.spread_points), "gold"],
        ["DD dzienny max", `${fmt(risk.max_daily_drawdown_pct)}%`, "blue"],
        ["DD calk. max", `${fmt(risk.max_total_drawdown_pct)}%`, "blue"],
        ["Pozycje", (s.positions || []).length, "blue"],
        ["Pendingi", (s.orders || []).length, "blue"],
        ["Lot / pozycja", fmt(lot.position_lot), "gold"],
        ["Bot", bot.running ? "RUNNING" : "STOP", bot.running ? "good" : "bad"],
      ];
      qs("#stats").innerHTML = stats.map(([label, value, cls]) => `
        <div class="panel stat"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div></div>
      `).join("");
      qs("#heartbeat").textContent = s.heartbeat_utc || "-";
      qs("#systemNotice").textContent = data.status.dashboard_error || `Konto ${account.login || "-"} / ${account.server || "-"} | Symbol ${s.symbol || "-"} | Folder ${data.project_dir || "-"}`;
      qs("#botPills").innerHTML = [
        ["Status", bot.running ? "działa" : "wyłączony", bot.running ? "good" : "bad"],
        ["PID", (bot.pids || []).join(", ") || "-", "gold"],
        ["Tryb lota", lot.mode || "-", "blue"],
        ["Magic", s.runtime?.magic || "-", "gold"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#accountTable").innerHTML = table(["Pole", "Wartość"], [
        row(["Login", esc(account.login)]),
        row(["Server", esc(account.server)]),
        row(["Free margin", esc(fmt(account.margin_free))]),
        row(["Symbol", esc(s.symbol)]),
      ]);
      qs("#lotTable").innerHTML = table(["Pole", "Wartość"], [
        row(["Tryb", esc(lot.mode)]),
        row(["Lot sygnału", esc(fmt(lot.signal_lot))]),
        row(["Lot pozycji", esc(fmt(lot.position_lot))]),
        row(["3 wejścia razem", esc(fmt(lot.three_leg_total_lot))]),
        row(["Krok USD", esc(fmt(lot.step_usd))]),
        row(["Kroki", esc(lot.steps)]),
        row(["Dodawany lot", esc(fmt(lot.add_lot))]),
      ]);
    }

    function renderTrades(data) {
      const positions = data.status?.positions || [];
      qs("#positionsTable").innerHTML = table(["Ticket", "Symbol", "Side", "Lot", "Open", "SL", "TP", "PnL", "Komentarz"], positions.map(p =>
        row([esc(p.ticket), esc(p.symbol), p.type === 0 ? "BUY" : "SELL", esc(fmt(p.volume)), esc(fmt(p.price_open)), esc(fmt(p.sl)), esc(fmt(p.tp)), `<span class="${Number(p.profit)>=0?'good':'bad'}">${esc(fmt(p.profit))}</span>`, esc(p.comment)])
      ));
      const orders = data.status?.orders || [];
      qs("#ordersTable").innerHTML = table(["Ticket", "Symbol", "Typ", "Lot", "Cena", "SL", "TP", "Komentarz"], orders.map(o =>
        row([esc(o.ticket), esc(o.symbol), esc(o.type_label), esc(fmt(o.volume_initial)), esc(fmt(o.price_open)), esc(fmt(o.sl)), esc(fmt(o.tp)), esc(o.comment)])
      ));
      const trades = data.trades || [];
      qs("#tradesTable").innerHTML = table(["Czas", "Side", "Lot", "Profit", "Powód"], trades.map(t =>
        row([esc(t.closed_at_utc), esc(t.side), esc(t.volume), `<span class="${Number(t.profit)>=0?'good':'bad'}">${esc(t.profit)}</span>`, esc(t.reason)])
      ));
      const today = data.today || {};
      qs("#todayTable").innerHTML = table(["Metryka", "Wartość"], [
        row(["Sygnały", esc(today.signals)]),
        row(["Zlecenia", esc(today.orders)]),
        row(["TP", esc(today.tp)]),
        row(["SL/BE", esc(today.sl_or_be)]),
        row(["Wynik zamknięć", `<span class="${Number(today.closed_profit)>=0?'good':'bad'}">${esc(fmt(today.closed_profit))}</span>`]),
      ]);
    }

    function renderEvents(data) {
      const signals = data.signal_events || [];
      qs("#signalEvents").innerHTML = signals.length ? signals.map(item => `<div class="log">${esc(JSON.stringify(item, null, 2))}</div>`).join("") : "<div class='muted'>Brak sygnałów</div>";
      const orders = data.order_events || [];
      qs("#orderEvents").innerHTML = orders.length ? orders.map(item => `<div class="log">${esc(JSON.stringify(item, null, 2))}</div>`).join("") : "<div class='muted'>Brak zleceń</div>";
      qs("#botLog").innerHTML = (data.logs?.bot || []).map(line => `<div class="log">${esc(line)}</div>`).join("") || "<div class='muted'>Brak logu</div>";
      qs("#errLog").innerHTML = (data.logs?.err || []).map(line => `<div class="log">${esc(line)}</div>`).join("") || "<div class='muted'>Brak błędów</div>";
    }

    function renderChannelAnalyzer(data) {
      const report = data.channel_analyzer || {};
      const totals = report.totals || {};
      const recognized = Number(totals.recognized || 0);
      const unparsed = Number(totals.unparsed || 0);
      const executed = Number(totals.executed_signals || 0);
      const pnl = Number(totals.pnl || 0);
      const parseRate = 100 * recognized / Math.max(1, recognized + unparsed);
      const executionRate = 100 * executed / Math.max(1, recognized);
      const stats = [
        ["Obserwowane", report.configured_count || 0, "blue"],
        ["Rozpoznane", recognized, "gold"],
        ["Parser", `${fmt(parseRate)}%`, parseRate >= 90 ? "good" : "bad"],
        ["Zagrane sygnały", executed, "blue"],
        ["Wykonanie", `${fmt(executionRate)}%`, executionRate >= 75 ? "good" : "bad"],
        ["Wygrane", totals.wins || 0, "good"],
        ["Straty", totals.losses || 0, Number(totals.losses || 0) ? "bad" : "good"],
        ["PnL", fmt(pnl), pnl >= 0 ? "good" : "bad"],
      ];
      qs("#channelAnalyzerStats").innerHTML = stats.map(([label, value, cls]) => `
        <div class="panel stat"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div></div>
      `).join("");
      qs("#channelAnalyzerTime").textContent = report.generated_utc ? `${report.window_days || 7} dni | ${report.generated_utc}` : "oczekuje na raport";
      const channels = report.channels || [];
      qs("#channelAnalyzerTable").innerHTML = table(
        ["Konto", "Kanał", "Rozpoznane", "Nieodczytane", "Zagrane", "Wykonanie", "Zlecenia", "W/L/BE", "Bez straty", "PnL", "Najczęstszy filtr"],
        channels.map(item => {
          const reasons = Object.entries(item.skip_reasons || {});
          const topReason = reasons.length ? `${reasons[0][0]} (${reasons[0][1]})` : "-";
          return row([
            esc(item.account_profile || "-"),
            esc(item.channel || item.chat_id || item.key),
            esc(item.recognized || 0),
            esc(item.unparsed || 0),
            esc(item.executed_signals || 0),
            esc(`${fmt(item.execution_rate_pct || 0)}%`),
            esc(item.successful_orders || 0),
            esc(`${item.wins || 0}/${item.losses || 0}/${item.be || 0}`),
            esc(`${fmt(item.non_loss_rate_pct || 0)}%`),
            `<span class="${Number(item.pnl || 0) >= 0 ? 'good' : 'bad'}">${esc(fmt(item.pnl || 0))}</span>`,
            esc(topReason),
          ]);
        })
      );
    }

    function renderXauScalp(data) {
      const scalp = data.xau_scalp || {};
      const bot = scalp.bot || {};
      const status = scalp.status || {};
      const account = status.account || {};
      const risk = status.risk || {};
      const pnl = Number(status.open_profit || 0);
      const totalRealized = Number(status.total_realized_profit || 0);
      const todayRealized = Number(status.today_realized_profit ?? status.day_realized_profit ?? 0);
      const stats = [
        ["Status", bot.running ? "RUNNING" : "STOP", bot.running ? "good" : "bad"],
        ["Open PnL", fmt(pnl), pnl >= 0 ? "good" : "bad"],
        ["Total PnL", fmt(totalRealized), totalRealized >= 0 ? "good" : "bad"],
        ["Dzisiejszy PnL", fmt(todayRealized), todayRealized >= 0 ? "good" : "bad"],
        ["Pozycje", status.positions_count || 0, "blue"],
        ["DD dzienny", `${fmt(risk.daily_dd_pct || 0)}%`, Number(risk.daily_dd_pct || 0) <= Number(risk.max_daily_dd_pct || 0) ? "good" : "bad"],
        ["DD calk.", `${fmt(risk.total_dd_pct || 0)}%`, Number(risk.total_dd_pct || 0) <= Number(risk.max_total_dd_pct || 0) ? "good" : "bad"],
        ["Lot sygnału", fmt(status.total_lot || 0), "gold"],
        ["Sygnał", status.last_signal_side || "-", "gold"],
      ];
      qs("#xauScalpStats").innerHTML = stats.map(([label, value, cls]) => `
        <div class="panel stat"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div></div>
      `).join("");
      qs("#xauScalpHeartbeat").textContent = status.heartbeat_utc || "-";
      qs("#xauScalpPills").innerHTML = [
        ["PID", (bot.pids || []).join(", ") || "-", "gold"],
        ["Konto", account.login || "-", "blue"],
        ["Server", account.server || "-", "blue"],
        ["Symbol", status.symbol || "-", "gold"],
        ["Magic", status.magic || "-", "gold"],
        ["Ostatni powód", status.last_reason || "-", "blue"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#xauScalpRiskTable").innerHTML = table(["Pole", "Wartość"], [
        row(["Balance", esc(fmt(account.balance))]),
        row(["Equity", esc(fmt(account.equity))]),
        row(["Max pozycji", esc(status.max_positions)]),
        row(["Spread", esc(fmt(risk.spread_points))]),
        row(["Max spread", esc(fmt(risk.max_spread_points))]),
        row(["SL USD", esc(fmt(status.sl_usd))]),
        row(["TP1 USD", esc(fmt(status.tp1_usd))]),
        row(["TP2 USD", esc(fmt(status.tp2_usd))]),
        row(["Cooldown sek.", esc(status.cooldown_seconds)]),
      ]);
      const positions = status.positions || [];
      qs("#xauScalpPositionsTable").innerHTML = table(["Ticket", "Noga", "Side", "Lot", "Open", "SL", "TP", "PnL"], positions.map(p =>
        row([esc(p.ticket), esc(p.leg || "-"), esc(p.side || "-"), esc(fmt(p.volume)), esc(fmt(p.price_open)), esc(fmt(p.sl)), esc(fmt(p.tp)), `<span class="${Number(p.profit)>=0?'good':'bad'}">${esc(fmt(p.profit))}</span>`])
      ));
      const trades = scalp.trades || [];
      qs("#xauScalpTradesTable").innerHTML = table(["Czas", "Ticket", "Noga", "Side", "Lot", "Profit"], trades.map(t =>
        row([esc(t.closed_at_utc || t.time || "-"), esc(t.ticket || "-"), esc(t.leg || "-"), esc(t.side || "-"), esc(t.volume || "-"), `<span class="${Number(t.profit)>=0?'good':'bad'}">${esc(t.profit || 0)}</span>`])
      ));
      const events = scalp.events || [];
      qs("#xauScalpEvents").innerHTML = events.length ? events.map(item => `<div class="log">${esc(JSON.stringify(item, null, 2))}</div>`).join("") : "<div class='muted'>Brak zdarzeń</div>";
    }

    function renderContrScalp(data) {
      const scalp = data.xau_contr_scalp || {};
      const bot = scalp.bot || {};
      const status = scalp.status || {};
      const account = status.account || {};
      const risk = status.risk || {};
      const pnl = Number(status.open_profit || 0);
      const totalRealized = Number(status.total_realized_profit || status.csv_total_realized_profit || 0);
      const todayRealized = Number(status.today_realized_profit ?? status.day_realized_profit ?? 0);
      const stats = [
        ["Status", bot.running ? "RUNNING" : "STOP", bot.running ? "good" : "bad"],
        ["Open PnL", fmt(pnl), pnl >= 0 ? "good" : "bad"],
        ["Total PnL", fmt(totalRealized), totalRealized >= 0 ? "good" : "bad"],
        ["Dzisiejszy PnL", fmt(todayRealized), todayRealized >= 0 ? "good" : "bad"],
        ["Pozycje", status.positions_count || 0, "blue"],
        ["DD dzienny", `${fmt(risk.daily_dd_pct || 0)}%`, Number(risk.daily_dd_pct || 0) <= Number(risk.max_daily_dd_pct || 0) ? "good" : "bad"],
        ["DD calk.", `${fmt(risk.total_dd_pct || 0)}%`, Number(risk.total_dd_pct || 0) <= Number(risk.max_total_dd_pct || 0) ? "good" : "bad"],
        ["Tryb", status.mode || "-", "gold"],
        ["TP / SL", `${fmt(status.tp_usd || 0)} / ${fmt(status.sl_usd || 0)}`, "gold"],
      ];
      qs("#contrScalpStats").innerHTML = stats.map(([label, value, cls]) => `
        <div class="panel stat"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div></div>
      `).join("");
      qs("#contrScalpHeartbeat").textContent = status.heartbeat_utc || "-";
      qs("#contrScalpPills").innerHTML = [
        ["PID", (bot.pids || []).join(", ") || "-", "gold"],
        ["Konto", account.login || "-", "blue"],
        ["Server", account.server || "-", "blue"],
        ["Symbol", status.symbol || "-", "gold"],
        ["Magic", status.magic || "-", "gold"],
        ["Powod", status.last_reason || "-", "blue"],
        ["Przetworzone SL", status.processed_source_tickets || 0, "gold"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#contrScalpRiskTable").innerHTML = table(["Pole", "Wartosc"], [
        row(["Balance", esc(fmt(account.balance))]),
        row(["Equity", esc(fmt(account.equity))]),
        row(["Tryb", esc(status.mode)]),
        row(["Mnoznik lota", esc(fmt(status.lot_factor))]),
        row(["Max pozycji", esc(status.max_positions)]),
        row(["Spread", esc(fmt(risk.spread_points))]),
        row(["Max spread", esc(fmt(status.max_spread_points))]),
        row(["TP USD", esc(fmt(status.tp_usd))]),
        row(["SL USD", esc(fmt(status.sl_usd))]),
        row(["Timeout min", esc(status.timeout_minutes)]),
      ]);
      const positions = status.positions || [];
      qs("#contrScalpPositionsTable").innerHTML = table(["Ticket", "Side", "Lot", "Open", "SL", "TP", "PnL", "Komentarz"], positions.map(p =>
        row([esc(p.ticket), esc(p.side || "-"), esc(fmt(p.volume)), esc(fmt(p.price_open)), esc(fmt(p.sl)), esc(fmt(p.tp)), `<span class="${Number(p.profit)>=0?'good':'bad'}">${esc(fmt(p.profit))}</span>`, esc(p.comment || "-")])
      ));
      const trades = scalp.trades || [];
      qs("#contrScalpTradesTable").innerHTML = table(["Czas", "Ticket", "Source", "Side", "Lot", "Profit"], trades.map(t =>
        row([esc(t.closed_at_utc || "-"), esc(t.ticket || "-"), esc(t.source_ticket || "-"), esc(t.side || "-"), esc(t.volume || "-"), `<span class="${Number(t.profit)>=0?'good':'bad'}">${esc(t.profit || 0)}</span>`])
      ));
      const events = scalp.events || [];
      qs("#contrScalpEvents").innerHTML = events.length ? events.map(item => `<div class="log">${esc(JSON.stringify(item, null, 2))}</div>`).join("") : "<div class='muted'>Brak zdarzen</div>";
    }

    function renderAgentTeams(data) {
      const bot = data.bot || {};
      const status = data.status || {};
      const summary = status.summary || {};
      const learning = status.learning || {};
      const scout = status.instrument_scout || {};
      const control = status.control_team || {};
      const longTerm = status.long_term_brigade || {};
      const analytics = data.analytics || {};
      const sizing = data.sizing || {};
      const githubScout = data.github_scout || {};
      const teams = status.teams || [];
      const lotPolicy = status.lot_policy || {};
      const lotPolicyLabel = typeof lotPolicy === "object"
        ? `${lotPolicy.mode || "-"} | risk ${fmt(lotPolicy.risk_per_trade_pct || 0)}% | max ${lotPolicy.dynamic_max_lot || "-"}`
        : String(lotPolicy || "-");
      const tradesCount = Number(summary.trades || 0);
      const wins = Number(summary.wins || 0);
      const winRate = 100 * wins / Math.max(1, tradesCount);
      const pnl = Number(summary.pnl || 0);
      const stats = [
        ["Status", bot.running ? "RUNNING" : "STOP", bot.running ? "good" : "bad"],
        ["Ekipy", summary.teams || teams.length || 0, "blue"],
        ["Live demo", summary.live_demo || 0, "good"],
        ["Long term", summary.long_term || 0, "gold"],
        ["Shadow", summary.shadow || 0, "gold"],
        ["Otwarte", summary.open_positions || 0, "blue"],
        ["Transakcje", tradesCount, "blue"],
        ["Win rate", `${fmt(winRate)}%`, winRate >= 55 ? "good" : "bad"],
        ["PnL", fmt(pnl), pnl >= 0 ? "good" : "bad"],
      ];
      qs("#agentTeamStats").innerHTML = stats.map(([label, value, cls]) => `
        <div class="panel stat"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div></div>
      `).join("");
      qs("#agentTeamHeartbeat").textContent = status.heartbeat_utc || "-";
      qs("#agentTeamPills").innerHTML = [
        ["PID", (bot.pids || []).join(", ") || "-", "gold"],
        ["Konto", status.account?.login || "-", "blue"],
        ["Server", status.account?.server || "-", "blue"],
        ["Loty", lotPolicyLabel, "gold"],
        ["Demo guard", status.account?.demo_guard ? "ON" : "OFF", status.account?.demo_guard ? "good" : "bad"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#agentTeamRulesTable").innerHTML = table(["Pole", "Wartość"], [
        row(["Supervisor threshold", esc(fmt(status.supervisor_threshold || 0))]),
        row(["Min. głosów kierunkowych", esc(status.min_directional_votes || "-")]),
        row(["Równoległe pozycje / ekipa", esc(status.concurrent_position_policy || "-")]),
        row(["Zadane loty", esc(lotPolicyLabel)]),
        row(["Egzekucja", "Jeden Supervisor na ekipę"]),
        row(["Live keys", esc((status.live_keys || []).join(", ") || "brak - wszystko shadow")]),
        row(["Nowe strategie", "shadow do czasu zaliczenia walk-forward"]),
      ]);
      const stablePairs = analytics.stable_pairs || [];
      const diversifiedPairs = analytics.diversified_pairs || [];
      const stablePortfolio = analytics.stable_portfolio || {};
      qs("#marketAnalyticsUpdate").textContent = analytics.generated_utc || "oczekuje na raport";
      qs("#marketAnalyticsPills").innerHTML = [
        ["Stabilne pary", stablePairs.length, stablePairs.length ? "good" : "gold"],
        ["Portfel po korelacji", diversifiedPairs.length, diversifiedPairs.length ? "good" : "gold"],
        ["Transakcje", stablePortfolio.trades || 0, "blue"],
        ["PnL", fmt(stablePortfolio.pnl || 0), Number(stablePortfolio.pnl || 0) >= 0 ? "good" : "bad"],
        ["Profit factor", stablePortfolio.profit_factor || "-", "blue"],
        ["Max DD", fmt(stablePortfolio.max_closed_drawdown || 0), "gold"],
        ["Dodatnie dni", `${fmt(stablePortfolio.positive_days_pct || 0)}%`, "blue"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#marketAnalyticsTable").innerHTML = table(
        ["Para", "Transakcje", "Dni", "PnL", "WR", "PF", "Dodatnie foldy", "Ryzyko straty bootstrap"],
        stablePairs.map(key => {
          const item = (analytics.pairs || {})[key] || {};
          return row([
            `${diversifiedPairs.includes(key) ? '<span class="good">PORTFEL</span> ' : ''}${esc(key)}`, esc(item.trades || 0), esc(item.trading_days || 0),
            `<span class="${Number(item.pnl || 0) >= 0 ? 'good' : 'bad'}">${esc(fmt(item.pnl || 0))}</span>`,
            esc(`${fmt(item.win_rate_pct || 0)}%`), esc(item.profit_factor || "-"),
            esc(`${item.walk_forward?.positive_folds || 0}/4`),
            esc(`${fmt(item.bootstrap?.probability_of_loss_pct || 0)}%`),
          ]);
        })
      );
      const sizingResults = sizing.results || {};
      qs("#marketSizingTable").innerHTML = table(
        ["Wariant", "Saldo końcowe", "PnL", "WR", "PF", "Max DD", "DD / start", "Ocena"],
        Object.entries(sizingResults).map(([name, item]) => row([
          esc(name), esc(fmt(item.ending_balance || 0)), esc(fmt(item.pnl || 0)),
          esc(`${fmt(item.win_rate_pct || 0)}%`), esc(item.profit_factor || "-"),
          esc(fmt(item.max_closed_drawdown || 0)), esc(`${fmt(item.max_closed_drawdown_pct_start || 0)}%`),
          `<span class="${name === sizing.recommendation ? 'good' : name.includes('classic') ? 'bad' : 'muted'}">${esc(name === sizing.recommendation ? 'REKOMENDOWANY' : 'BADAWCZY')}</span>`,
        ]))
      );
      const scoutDiscovery = githubScout.discovery || {};
      const scoutBacktest = githubScout.backtest || {};
      const scoutAnalytics = githubScout.analytics || {};
      const scoutPortfolio = scoutBacktest.portfolio || {};
      qs("#githubScoutUpdate").textContent = githubScout.generated_utc || "oczekuje na pierwszy skan";
      qs("#githubScoutPills").innerHTML = [
        ["Znalezione", scoutDiscovery.repositories_found || 0, "blue"],
        ["Sprawdzone", scoutDiscovery.repositories_inspected || 0, "blue"],
        ["Do badań", scoutDiscovery.approved_for_concept_research || 0, "gold"],
        ["Strategie lokalne", (scoutDiscovery.mapped_local_strategies || []).length, "gold"],
        ["Transakcje", scoutPortfolio.trades || 0, "blue"],
        ["PnL testu", fmt(scoutPortfolio.pnl || 0), Number(scoutPortfolio.pnl || 0) >= 0 ? "good" : "bad"],
        ["Stabilne pary", (scoutAnalytics.stable_pairs || []).length, (scoutAnalytics.stable_pairs || []).length ? "good" : "gold"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#githubScoutRepositoriesTable").innerHTML = table(
        ["Repozytorium", "Ocena", "Stars", "Licencja", "Koncepcje", "Status"],
        (scoutDiscovery.repositories || []).slice(0, 15).map(item => row([
          item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noreferrer">${esc(item.full_name || "-")}</a>` : esc(item.full_name || "-"),
          esc(item.score || 0), esc(item.stars || 0), esc(item.license || "-"),
          esc((item.concepts || []).join(", ") || "-"),
          `<span class="${item.safe_for_review ? "good" : "bad"}">${item.safe_for_review ? "REVIEW" : "ODRZUCONE"}</span>`,
        ]))
      );
      qs("#githubScoutTestsTable").innerHTML = table(
        ["Para", "Transakcje", "PnL", "WR", "PF", "Foldy", "Ryzyko straty"],
        Object.entries(scoutAnalytics.pairs || {})
          .sort((a, b) => Number(b[1].pnl || 0) - Number(a[1].pnl || 0))
          .slice(0, 20)
          .map(([key, item]) => row([
            esc(key), esc(item.trades || 0),
            `<span class="${Number(item.pnl || 0) >= 0 ? "good" : "bad"}">${esc(fmt(item.pnl || 0))}</span>`,
            esc(`${fmt(item.win_rate_pct || 0)}%`), esc(item.profit_factor || "-"),
            esc(`${item.walk_forward?.positive_folds || 0}/4`),
            esc(`${fmt(item.bootstrap?.probability_of_loss_pct || 0)}%`),
          ]))
      );
      qs("#agentLearningUpdate").textContent = learning.last_update_utc || "oczekuje na zamknięte transakcje";
      qs("#agentLearningPills").innerHTML = [
        ["Status", learning.enabled ? "LEARNING" : "STOP", learning.enabled ? "good" : "bad"],
        ["Nauczone transakcje", learning.closed_trades_learned || 0, "blue"],
        ["Aktywni agenci", learning.active_agents || 0, "gold"],
        ["Kandydaci shadow", learning.shadow_candidates || 0, "blue"],
        ["Awanse", learning.promotions || 0, "good"],
        ["Degradacje", learning.demotions || 0, learning.demotions ? "bad" : "good"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#agentLearningTable").innerHTML = table(
        ["Agent", "Typ", "Status", "Oceny", "Trafność", "Waga", "Zadanie"],
        (learning.agents || []).map(agent => row([
          esc(agent.agent || "-"),
          esc(agent.kind || "-"),
          `<span class="${agent.status === 'active' ? 'good' : 'gold'}">${esc(String(agent.status || "-").toUpperCase())}</span>`,
          esc(agent.observations || 0),
          esc(`${fmt(agent.accuracy || 0)}%`),
          esc(agent.effective_weight || 0),
          esc(agent.description || "-"),
        ]))
      );
      const recruited = scout.recruited || [];
      const scoutCandidates = scout.candidates || [];
      qs("#instrumentScoutUpdate").textContent = scout.last_scan_utc || "oczekuje na pierwszy skan";
      qs("#instrumentScoutPills").innerHTML = [
        ["Status", scout.enabled ? "SCANNING" : "STOP", scout.enabled ? "good" : "bad"],
        ["Katalog MT5", scout.catalog_count || 0, "blue"],
        ["Przeskanowane", scout.scanned_count || 0, "blue"],
        ["Zatrudnione ekipy", recruited.length, "gold"],
        ["Zwolnione ekipy", (scout.retired || []).length, (scout.retired || []).length ? "bad" : "good"],
        ["Prog oceny", scout.minimum_score || "-", "gold"],
        ["Lot testowy", scout.test_lot || 1, "good"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#instrumentScoutTeamsTable").innerHTML = table(
        ["Ekipa", "Symbol", "Score teraz", "Slabe skany", "Lot", "Status", "Zatrudnieni specjalisci"],
        recruited.map(item => row([
          esc(item.name || item.key || "-"),
          esc(item.symbol || "-"),
          esc(fmt(item.last_score ?? item.score ?? 0)),
          `<span class="${Number(item.weak_scans || 0) ? 'bad' : 'good'}">${esc(item.weak_scans || 0)}/3</span>`,
          esc(item.lot || 1),
          `<span class="good">${esc(String(item.status || "live_demo").toUpperCase())}</span>`,
          esc((item.agents || []).join(", ") || "-"),
        ]))
      );
      qs("#instrumentScoutCandidatesTable").innerHTML = table(
        ["Symbol", "Score", "ATR %", "Spread/ATR", "Efektywnosc", "Tick age", "Proponowani agenci"],
        scoutCandidates.map(item => row([
          esc(item.symbol || "-"),
          esc(fmt(item.score || 0)),
          esc(fmt(100 * Number(item.atr_pct || 0))),
          esc(fmt(item.spread_atr || 0)),
          esc(fmt(item.efficiency || 0)),
          esc(item.tick_age_seconds || 0),
          esc((item.agents || []).join(", ") || "-"),
        ]))
      );
      qs("#controlTeamUpdate").textContent = control.last_update_utc || "oczekuje na propozycje";
      qs("#controlTeamPills").innerHTML = [
        ["Status", control.enabled ? "ACTIVE" : "STOP", control.enabled ? "good" : "bad"],
        ["Zatwierdzone", control.approved_entries || 0, "good"],
        ["Zablokowane", control.blocked_entries || 0, control.blocked_entries ? "gold" : "good"],
        ["Ocenione trade'y", control.reviewed_trades || 0, "blue"],
        ["Dobre", control.good_trades || 0, "good"],
        ["Slabe", control.bad_trades || 0, control.bad_trades ? "bad" : "good"],
        ["Min confidence", control.minimum_confidence || "-", "gold"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#controlTeamMembersTable").innerHTML = table(
        ["Agent kontrolny", "Funkcja"],
        (control.members || []).map(item => row([
          esc(item.agent || "-"),
          esc(item.description || "-"),
        ]))
      );
      qs("#controlTradeReviewsTable").innerHTML = table(
        ["Czas", "Ekipa", "Symbol", "Side", "PnL", "Ocena", "Diagnoza"],
        (control.trade_reviews || []).slice().reverse().map(item => row([
          esc(item.closed_at_utc || "-"),
          esc(item.team || "-"),
          esc(item.symbol || "-"),
          esc(item.side || "-"),
          `<span class="${Number(item.profit || 0) >= 0 ? 'good' : 'bad'}">${esc(fmt(item.profit || 0))}</span>`,
          `<span class="${String(item.grade || "").startsWith("GOOD") ? 'good' : String(item.grade || "").startsWith("BAD") ? 'bad' : 'gold'}">${esc(item.grade || "-")}</span>`,
          esc(item.diagnosis || "-"),
        ]))
      );
      const controlMessages = (control.messages || []).slice().reverse();
      qs("#controlTeamMessages").innerHTML = controlMessages.length ? controlMessages.map(item => `
        <div class="log"><span class="gold">${esc(item.sender || "-")}</span> [${esc(item.role || "-")}] ${esc(item.message || "-")}
        <div class="small muted">${esc(item.timestamp_utc || "-")} | ${esc(item.team || "-")} | ${esc(item.symbol || "-")}</div></div>
      `).join("") : "<div class='muted'>Brak wiadomosci</div>";
      const longTermTeams = teams.filter(team => team.strategy_profile === "long_term");
      qs("#longTermPills").innerHTML = [
        ["Status", longTerm.enabled ? "ACTIVE" : "STOP", longTerm.enabled ? "good" : "bad"],
        ["Ekipy", longTerm.teams || longTermTeams.length || 0, "gold"],
        ["Interwaly", longTerm.timeframes || "-", "blue"],
        ["Threshold", longTerm.threshold || "-", "gold"],
        ["Min glosow", longTerm.min_votes || "-", "blue"],
        ["Cooldown", `${Number(longTerm.cooldown_seconds || 0) / 3600}h`, "blue"],
        ["Lot XAU", longTerm.xau_lot || "-", "good"],
        ["Lot pozostale", longTerm.default_lot || "-", "good"],
      ].map(([a,b,c]) => `<span class="pill"><span class="dot ${c}"></span>${esc(a)}: ${esc(b)}</span>`).join("");
      qs("#longTermTeamsTable").innerHTML = table(
        ["Ekipa", "Symbol", "Decyzja", "Confidence", "Pozycje", "PnL", "WR"],
        longTermTeams.map(team => row([
          esc(team.name || team.key || "-"),
          esc(team.symbol || "-"),
          `<span class="${team.supervisor?.decision === 'buy' ? 'good' : team.supervisor?.decision === 'sell' ? 'bad' : 'muted'}">${esc(String(team.supervisor?.decision || "hold").toUpperCase())}</span><div class="small muted">${esc(team.supervisor?.reason || "")}</div>`,
          esc(`${fmt(100 * Number(team.supervisor?.confidence || 0))}%`),
          esc(team.open_positions_count || 0),
          `<span class="${Number(team.stats?.pnl || 0) >= 0 ? 'good' : 'bad'}">${esc(fmt(team.stats?.pnl || 0))}</span>`,
          esc(`${fmt(team.stats?.win_rate || 0)}%`),
        ]))
      );
      qs("#longTermAgentsTable").innerHTML = table(
        ["Agent", "Funkcja"],
        (longTerm.agents || []).map(item => row([
          esc(item.agent || "-"),
          esc(item.description || "-"),
        ]))
      );
      qs("#agentTeamsTable").innerHTML = table(
        ["Ekipa", "Symbol", "Zrodlo / agenci", "Rada", "Tryb", "Lot", "Decyzja", "Confidence", "B/S/H", "Min lot", "Pozycja", "PnL", "WR"],
        teams.map(team => {
          const supervisor = team.supervisor || {};
          const counts = supervisor.counts || {};
          const decision = String(supervisor.decision || "hold").toUpperCase();
          const decisionClass = decision === "BUY" ? "good" : decision === "SELL" ? "bad" : "muted";
          const position = team.open_position;
          const positions = team.open_positions || (position ? [position] : []);
          const positionsProfit = positions.reduce((sum, item) => sum + Number(item.profit || 0), 0);
          return row([
            esc(team.name || team.key),
            esc(team.symbol || "-"),
            `<span class="${team.source === 'instrument_scout' ? 'gold' : team.source === 'long_term_brigade' ? 'blue' : 'muted'}">${esc(team.source || "configured")}</span><div class="small muted">${esc((team.recruited_agents || []).join(", "))}</div>`,
            `<span class="${team.control?.approved ? 'good' : team.control?.verdict === 'blocked' ? 'bad' : 'muted'}">${esc(String(team.control?.verdict || "waiting").toUpperCase())}</span><div class="small muted">${esc(team.control?.reason || "")}</div>`,
            esc(team.mode || "-"),
            esc(team.requested_lot || "-"),
            `<span class="${decisionClass}">${esc(decision)}</span><div class="small muted">${esc(supervisor.reason || "")}</div>`,
            esc(`${fmt(100 * Number(supervisor.confidence || 0))}%`),
            esc(`${counts.buy || 0}/${counts.sell || 0}/${counts.hold || 0}`),
            esc(team.broker_min_lot || "-"),
            positions.length ? esc(`${positions.length} pozycji | PnL ${fmt(positionsProfit)} | ostatnia ${String(position?.side || "").toUpperCase()} ${position?.volume || 0} @ ${fmt(position?.entry || 0)}`) : "-",
            `<span class="${Number(team.stats?.pnl || 0) >= 0 ? 'good' : 'bad'}">${esc(fmt(team.stats?.pnl || 0))}</span>`,
            esc(`${fmt(team.stats?.win_rate || 0)}%`),
          ]);
        })
      );
      const voteRows = [];
      teams.forEach(team => (team.agents || []).forEach(vote => {
        const side = String(vote.side || "hold").toUpperCase();
        const cls = side === "BUY" ? "good" : side === "SELL" ? "bad" : "muted";
        voteRows.push(row([
          esc(team.name || team.key),
          esc(vote.agent || "-"),
          esc(String(vote.status || "active").toUpperCase()),
          `<span class="${cls}">${esc(side)}</span>`,
          esc(`${fmt(100 * Number(vote.confidence || 0))}%`),
          esc(`${fmt(vote.learned_accuracy || 0)}%`),
          esc(vote.weight || 0),
          esc(vote.reason || "-"),
        ]));
      }));
      qs("#agentVotesTable").innerHTML = table(["Ekipa", "Agent", "Status", "Głos", "Confidence", "Trafność", "Waga", "Uzasadnienie"], voteRows);
      const closed = data.trades || [];
      qs("#agentTradesTable").innerHTML = table(
        ["Czas", "Ekipa", "Symbol", "Tryb", "Side", "Lot", "Entry", "Exit", "PnL", "Powód"],
        closed.map(item => row([
          esc(item.closed_at_utc || "-"),
          esc(item.team || "-"),
          esc(item.symbol || "-"),
          esc(item.mode || "-"),
          esc(item.side || "-"),
          esc(item.volume || "-"),
          esc(fmt(item.entry || 0)),
          esc(fmt(item.exit || 0)),
          `<span class="${Number(item.profit || 0) >= 0 ? 'good' : 'bad'}">${esc(fmt(item.profit || 0))}</span>`,
          esc(item.reason || "-"),
        ]))
      );
      const events = data.events || [];
      qs("#agentTeamEvents").innerHTML = events.length ? events.map(item => `<div class="log">${esc(JSON.stringify(item, null, 2))}</div>`).join("") : "<div class='muted'>Brak zdarzeń</div>";
    }

    function renderChannels() {
      qs("#channelsBulk").value = state.channels.join("\\n");
      qs("#channelList").innerHTML = state.channels.length ? state.channels.map((ch, idx) => {
        const lot = state.channelLots[ch] ?? "";
        return `
          <div class="channel-item">
            <div class="channel-name" title="${esc(ch)}">${esc(ch)}</div>
            <div class="channel-lot">
              <div class="channel-lot-label">Lot / pozycję</div>
              <input type="number" min="0.01" max="100" step="0.01" placeholder="globalny" data-channel-lot="${esc(ch)}" value="${esc(lot)}">
            </div>
            <button data-channel-lot-save="${idx}">Zapisz lot</button>
            <button class="ghost" data-channel-open="${esc(ch)}">Otwórz</button>
            <button class="danger" data-channel-remove="${idx}">Usuń</button>
          </div>`;
      }).join("") : "<div class='muted'>Brak kanałów</div>";
    }

    function collectChannelLots() {
      const lots = {};
      qsa("[data-channel-lot]").forEach(input => {
        const value = input.value.trim();
        if (value !== "") lots[input.dataset.channelLot] = Number(value);
      });
      return lots;
    }

    async function loadChannels() {
      const data = await api("/api/channels");
      state.channels = data.channels || [];
      state.channelLots = data.lot_sizes || {};
      renderChannels();
    }
    async function saveChannels(channels, lotSizes=state.channelLots) {
      const data = await api("/api/channels", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({channels, lot_sizes:lotSizes})});
      state.channels = data.channels || [];
      state.channelLots = data.lot_sizes || {};
      renderChannels();
      qs("#channelStatus").textContent = "Zapisano kanały i loty. Zrestartuj bota.";
    }

    function inputFor(field, value) {
      const type = field.type || "text";
      if (type === "bool") {
        const on = String(value).toLowerCase() === "true";
        return `<div class="switch-row"><span>${esc(field.label)}</span><button class="toggle ${on?'on':''}" data-bool="${esc(field.key)}" type="button"></button><input type="hidden" data-config="${esc(field.key)}" value="${on ? 'true' : 'false'}"></div>`;
      }
      if (type.startsWith("select:")) {
        const options = type.split(":", 2)[1].split(",");
        return `<div class="field"><label>${esc(field.label)}</label><select data-config="${esc(field.key)}">${options.map(opt => `<option value="${esc(opt)}" ${String(value)===opt?'selected':''}>${esc(opt)}</option>`).join("")}</select></div>`;
      }
      const htmlType = type === "password" ? "password" : type === "number" ? "number" : "text";
      const step = htmlType === "number" ? " step='any'" : "";
      return `<div class="field"><label>${esc(field.label)}</label><input type="${htmlType}"${step} data-config="${esc(field.key)}" value="${esc(value)}"></div>`;
    }

    function renderConfig(data) {
      state.configFields = data.fields || [];
      state.configValues = data.values || {};
      ["mt5","telegram","risk","strategy","xau_scalp","xau_contr","system"].forEach(group => {
        qs(`#config-${group}`).innerHTML = state.configFields.filter(f => f.group === group).map(f => inputFor(f, state.configValues[f.key] ?? "")).join("");
      });
    }

    async function loadConfig() {
      renderConfig(await api("/api/config"));
    }
    async function saveConfig() {
      const values = {};
      qsa("[data-config]").forEach(el => values[el.dataset.config] = el.value);
      await api("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({values})});
      qs("#configStatus").textContent = "Zapisano .env. Zrestartuj bota, żeby użył nowych ustawień.";
      await loadConfig();
      await refresh();
    }

    async function refresh() {
      if (state.refreshing) return;
      state.refreshing = true;
      try {
        const [data, agentData] = await Promise.all([api("/api/overview"), api("/api/agent-teams")]);
        state.overview = data;
        renderStats(data);
        renderTrades(data);
        renderEvents(data);
        renderChannelAnalyzer(data);
        renderXauScalp(data);
        renderContrScalp(data);
        renderAgentTeams(agentData);
      } catch (err) {
        qs("#systemNotice").textContent = err.message;
      } finally {
        state.refreshing = false;
      }
    }

    qs("#addChannel").addEventListener("click", async () => {
      const value = qs("#channelInput").value.trim();
      if (!value) return;
      await saveChannels([...state.channels, value]);
      qs("#channelInput").value = "";
    });
    qs("#channelList").addEventListener("click", async (event) => {
      const remove = event.target.closest("[data-channel-remove]");
      const open = event.target.closest("[data-channel-open]");
      const saveLot = event.target.closest("[data-channel-lot-save]");
      if (remove) await saveChannels(state.channels.filter((_, i) => i !== Number(remove.dataset.channelRemove)), collectChannelLots());
      if (saveLot) await saveChannels(state.channels, collectChannelLots());
      if (open) window.open(`https://t.me/${open.dataset.channelOpen}`, "_blank");
    });
    qs("#saveBulkChannels").addEventListener("click", async () => {
      await saveChannels(qs("#channelsBulk").value.split(/\\r?\\n|,/).map(x => x.trim()).filter(Boolean));
    });
    qs("#reloadChannels").addEventListener("click", loadChannels);
    qs("#saveConfig").addEventListener("click", saveConfig);
    qs("#reloadConfig").addEventListener("click", loadConfig);
    qs("#refreshNow").addEventListener("click", refresh);
    qs("#startBot").addEventListener("click", async () => {
      qs("#systemNotice").textContent = "Uruchamianie bota...";
      await api("/api/bot", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"start"})});
      await refresh();
    });
    qs("#stopBot").addEventListener("click", async () => {
      qs("#systemNotice").textContent = "Zatrzymywanie bota...";
      await api("/api/bot", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"stop"})});
      await refresh();
    });
    qs("#startXauScalp").addEventListener("click", async () => {
      qs("#systemNotice").textContent = "Uruchamianie XAU Scalp...";
      await api("/api/xau-scalp", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"start"})});
      await refresh();
    });
    qs("#stopXauScalp").addEventListener("click", async () => {
      qs("#systemNotice").textContent = "Zatrzymywanie XAU Scalp...";
      await api("/api/xau-scalp", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"stop"})});
      await refresh();
    });
    qs("#startContrScalp").addEventListener("click", async () => {
      qs("#systemNotice").textContent = "Uruchamianie Kontr Scalper...";
      await api("/api/xau-contr-scalp", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"start"})});
      await refresh();
    });
    qs("#stopContrScalp").addEventListener("click", async () => {
      qs("#systemNotice").textContent = "Zatrzymywanie Kontr Scalper...";
      await api("/api/xau-contr-scalp", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"stop"})});
      await refresh();
    });
    qs("#startAgentTeams").addEventListener("click", async () => {
      qs("#systemNotice").textContent = "Uruchamianie Agent Teams...";
      await api("/api/agent-teams", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"start"})});
      await refresh();
    });
    qs("#stopAgentTeams").addEventListener("click", async () => {
      qs("#systemNotice").textContent = "Zatrzymywanie Agent Teams...";
      await api("/api/agent-teams", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action:"stop"})});
      await refresh();
    });
    document.addEventListener("click", event => {
      const toggle = event.target.closest("[data-bool]");
      if (!toggle) return;
      toggle.classList.toggle("on");
      const hidden = document.querySelector(`input[data-config="${toggle.dataset.bool}"]`);
      hidden.value = toggle.classList.contains("on") ? "true" : "false";
    });

    loadChannels();
    loadConfig();
    refresh();
    setInterval(refresh, 1000);
    setInterval(() => {
      const active = document.activeElement;
      if (active && (active.id === "channelsBulk" || active.id === "channelInput" || active.matches("[data-channel-lot]"))) return;
      loadChannels().catch(() => {});
    }, 1000);
  </script>
</body>
</html>
"""


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_channel_analysis(base_dir: Path, data_dir: Path) -> dict:
    sources = [
        ("Vantage", base_dir / "data_vantage" / "channel_analysis.json"),
    ]
    current_path = data_dir / "channel_analysis.json"
    if current_path not in {path for _, path in sources}:
        sources.append(("Bieżące", current_path))

    reports: list[tuple[str, dict]] = []
    for profile, path in sources:
        report = _read_json(path, {})
        if isinstance(report, dict) and report.get("channels") is not None:
            reports.append((profile, report))
    if not reports:
        return {}

    total_keys = ("recognized", "unparsed", "executed_signals", "successful_orders", "wins", "losses", "be", "pnl")
    totals = {key: 0.0 if key == "pnl" else 0 for key in total_keys}
    channels: list[dict] = []
    configured: set[str] = set()
    for profile, report in reports:
        configured.update(str(item) for item in report.get("configured_channels", []) if str(item))
        for key in total_keys:
            totals[key] += report.get("totals", {}).get(key, 0) or 0
        for item in report.get("channels", []):
            if isinstance(item, dict):
                channels.append({**item, "account_profile": profile})
    totals["pnl"] = round(float(totals["pnl"]), 2)
    return {
        "generated_utc": max((str(report.get("generated_utc", "")) for _, report in reports), default=""),
        "window_days": max((int(report.get("window_days", 7) or 7) for _, report in reports), default=7),
        "configured_count": len(configured) or max((int(report.get("configured_count", 0) or 0) for _, report in reports), default=0),
        "configured_channels": sorted(configured),
        "profiles": [profile for profile, _ in reports],
        "channels": channels,
        "totals": totals,
    }


def _read_last_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:][::-1]
    except Exception:
        return []


def _read_last_jsonl(path: Path, limit: int, *, event_type: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in reversed(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
        if len(out) >= limit:
            break
        try:
            item = json.loads(line)
        except Exception:
            continue
        if event_type and item.get("type") != event_type:
            continue
        out.append(item)
    return out


def _read_last_trades(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return list(reversed(rows[-limit:]))
    except Exception:
        return []


def _sum_trade_profit(path: Path) -> float:
    if not path.exists():
        return 0.0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            total = 0.0
            for row in csv.DictReader(handle):
                try:
                    total += float(row.get("profit", 0.0) or 0.0)
                except Exception:
                    continue
            return round(total, 2)
    except Exception:
        return 0.0


def _magic_mt5_pnl(cfg, magic: int, comment_marker: str = "") -> dict:
    try:
        start = datetime.now(UTC) - timedelta(days=7)
        deals = mt5.history_deals_get(start, datetime.now(UTC)) or []
    except Exception:
        return {}
    total_profit = 0.0
    total_commission = 0.0
    total_swap = 0.0
    today_profit = 0.0
    today_commission = 0.0
    today_swap = 0.0
    today_key = datetime.now().date().isoformat()
    deal_count = 0
    for deal in deals:
        comment = str(getattr(deal, "comment", "") or "").lower()
        if int(getattr(deal, "magic", 0) or 0) != int(magic) and (not comment_marker or comment_marker.lower() not in comment):
            continue
        profit = float(getattr(deal, "profit", 0.0) or 0.0)
        commission = float(getattr(deal, "commission", 0.0) or 0.0)
        swap = float(getattr(deal, "swap", 0.0) or 0.0)
        total_profit += profit
        total_commission += commission
        total_swap += swap
        deal_count += 1
        try:
            deal_day = datetime.fromtimestamp(int(getattr(deal, "time", 0) or 0)).date().isoformat()
        except Exception:
            deal_day = ""
        if deal_day == today_key:
            today_profit += profit
            today_commission += commission
            today_swap += swap
    return {
        "mt5_deals_count": deal_count,
        "total_realized_profit": round(total_profit + total_commission + total_swap, 2),
        "total_realized_gross": round(total_profit, 2),
        "total_commission": round(total_commission, 2),
        "today_realized_profit": round(today_profit + today_commission + today_swap, 2),
        "today_realized_gross": round(today_profit, 2),
        "today_commission": round(today_commission, 2),
    }


def _xau_scalp_mt5_pnl(cfg) -> dict:
    return _magic_mt5_pnl(cfg, int(getattr(cfg, "xau_scalp_magic", 994242) or 994242), "xau scalp")


def _xau_contr_scalp_mt5_pnl(cfg) -> dict:
    return _magic_mt5_pnl(cfg, int(getattr(cfg, "xau_contr_scalp_magic", 994244) or 994244), "kontrscalper")


def _clean_channel(value: object) -> str:
    text = str(value or "").strip().strip("'\"")
    text = re.sub(r"^https?://", "", text, flags=re.I)
    text = re.sub(r"^(t\.me|telegram\.me)/", "", text, flags=re.I)
    text = text.strip().strip("/")
    if text.startswith("@"):
        text = text[1:]
    if "/" in text and not text.startswith("+"):
        text = text.split("/", 1)[0].strip()
    return text


def _split_channels(raw: str) -> list[str]:
    channels: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[\n,]+", raw):
        channel = _clean_channel(item)
        if not channel:
            continue
        key = channel.lower()
        if key in seen:
            continue
        seen.add(key)
        channels.append(channel)
    return channels


def _parse_channel_lot_sizes(raw: str) -> dict[str, float]:
    try:
        payload = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, float] = {}
    for channel, raw_lot in payload.items():
        name = _clean_channel(channel)
        try:
            lot = round(float(raw_lot), 2)
        except Exception:
            continue
        if name and 0.01 <= lot <= 100.0:
            result[name] = lot
    return result


def _clean_channel_lot_sizes(raw_lots: object, channels: list[str]) -> dict[str, float]:
    if not isinstance(raw_lots, dict):
        raise ValueError("lot_sizes must be an object")
    canonical = {channel.lower(): channel for channel in channels}
    result: dict[str, float] = {}
    for raw_channel, raw_lot in raw_lots.items():
        channel = canonical.get(_clean_channel(raw_channel).lower())
        if not channel or raw_lot in {None, ""}:
            continue
        try:
            lot = round(float(raw_lot), 2)
        except Exception as exc:
            raise ValueError(f"Nieprawidlowy lot dla {channel}") from exc
        if lot < 0.01 or lot > 100.0:
            raise ValueError(f"Lot dla {channel} musi byc w zakresie 0.01-100.00")
        result[channel] = lot
    return result


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _write_env_values(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    existing: dict[str, int] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing[stripped.split("=", 1)[0].strip()] = index
    for key, value in values.items():
        if key not in CONFIG_KEYS and key not in {"WATCH_CHANNELS", "CHANNEL_LOT_SIZES"}:
            continue
        if key in existing:
            lines[existing[key]] = f"{key}={value}"
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _send_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return


def _serialize_position(position) -> dict:
    return {
        "ticket": int(getattr(position, "ticket", 0) or 0),
        "type": int(getattr(position, "type", 0) or 0),
        "symbol": str(getattr(position, "symbol", "")),
        "volume": float(getattr(position, "volume", 0.0) or 0.0),
        "price_open": float(getattr(position, "price_open", 0.0) or 0.0),
        "price_current": float(getattr(position, "price_current", 0.0) or 0.0),
        "sl": float(getattr(position, "sl", 0.0) or 0.0),
        "tp": float(getattr(position, "tp", 0.0) or 0.0),
        "profit": float(getattr(position, "profit", 0.0) or 0.0),
        "comment": str(getattr(position, "comment", "") or ""),
    }


def _order_type_label(order_type: int) -> str:
    return {
        2: "BUY LIMIT",
        3: "SELL LIMIT",
        4: "BUY STOP",
        5: "SELL STOP",
    }.get(int(order_type), str(order_type))


def _serialize_order(order) -> dict:
    return {
        "ticket": int(getattr(order, "ticket", 0) or 0),
        "type": int(getattr(order, "type", 0) or 0),
        "type_label": _order_type_label(int(getattr(order, "type", 0) or 0)),
        "symbol": str(getattr(order, "symbol", "")),
        "volume_initial": float(getattr(order, "volume_initial", 0.0) or 0.0),
        "price_open": float(getattr(order, "price_open", 0.0) or 0.0),
        "sl": float(getattr(order, "sl", 0.0) or 0.0),
        "tp": float(getattr(order, "tp", 0.0) or 0.0),
        "comment": str(getattr(order, "comment", "") or ""),
    }


_MT5_STATUS_CACHE: dict[str, object] = {"expires_at": 0.0, "value": None}


def _safe_compound_base(balance: float, equity: float) -> float:
    if balance <= 0.0:
        return max(0.0, equity)
    if equity <= 0.0:
        return balance
    return min(balance, equity)


def _lot_status(cfg, symbol: str, balance: float, equity: float) -> dict:
    mode = str(getattr(cfg, "signal_lot_mode", "fixed") or "fixed")
    base_balance = float(_read_json(cfg.data_dir / "telegram_signal_state.json", {}).get("dynamic_lot_base_balance", 0.0) or balance)
    signal_lot = float(cfg.signal_fixed_lot)
    steps = 0
    net_profit = 0.0
    compound_base = None
    if mode == "profit_dynamic" or (bool(cfg.signal_dynamic_lot_enabled) and mode not in {"funded_safe", "equity_safe"}):
        net_profit = max(0.0, float(balance))
        steps = int(net_profit // float(cfg.signal_dynamic_lot_step_usd))
        signal_lot = min(float(cfg.signal_dynamic_lot_max), max(float(cfg.signal_fixed_lot), steps * float(cfg.signal_dynamic_lot_add)))
    elif mode == "funded_safe":
        compound_base = float(cfg.signal_funded_account_balance or 0.0) or float(balance or 0.0)
        signal_lot = (compound_base / 100000.0) * float(cfg.signal_funded_safe_signal_lot_per_100k)
    elif mode == "equity_safe":
        compound_base = _safe_compound_base(balance, equity)
        signal_lot = (compound_base / 100000.0) * float(cfg.signal_funded_safe_signal_lot_per_100k)
    max_lot = max(float(cfg.max_lot), float(cfg.signal_dynamic_lot_max))
    normalized_signal_lot = normalize_volume(symbol, signal_lot, float(cfg.min_lot), max_lot)
    lot_per_position = mode == "profit_dynamic" or (bool(cfg.signal_dynamic_lot_enabled) and mode not in {"funded_safe", "equity_safe"})
    position_lot = normalized_signal_lot if lot_per_position else normalize_volume(symbol, normalized_signal_lot / 3.0, float(cfg.min_lot), max_lot)
    return {
        "mode": mode,
        "signal_lot": normalized_signal_lot,
        "position_lot": position_lot,
        "three_leg_total_lot": round(float(position_lot) * 3.0, 4),
        "lot_per_position": lot_per_position,
        "raw_signal_lot": signal_lot,
        "base_balance": base_balance,
        "net_profit_from_base": net_profit,
        "steps": steps,
        "step_usd": float(cfg.signal_dynamic_lot_step_usd),
        "add_lot": float(cfg.signal_dynamic_lot_add),
        "compound_base": compound_base,
    }


def _live_mt5_status(cfg) -> dict:
    now = time.monotonic()
    cached = _MT5_STATUS_CACHE.get("value")
    if cached is not None and now < float(_MT5_STATUS_CACHE.get("expires_at", 0.0)):
        return cached
    connect(Mt5Credentials(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server, path=cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        account = account_info()
        positions = positions_by_magic(symbol, cfg.magic)
        orders = orders_by_magic(symbol, cfg.magic)
        tick = get_tick(symbol)
        try:
            info = symbol_info(symbol)
            point = float(getattr(info, "point", 0.01) or 0.01)
            spread_points = abs(float(tick.ask) - float(tick.bid)) / point
        except Exception:
            spread_points = 0.0
        status = {
            "heartbeat_utc": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "account": {
                "login": int(account.login),
                "server": str(account.server),
                "balance": float(account.balance),
                "equity": float(account.equity),
                "margin_free": float(account.margin_free),
                "profit": float(account.profit),
            },
            "risk": {
                "spread_points": spread_points,
                "max_daily_drawdown_pct": float(cfg.max_daily_drawdown_pct),
                "max_total_drawdown_pct": float(cfg.max_total_drawdown_pct),
            },
            "lot": _lot_status(cfg, symbol, float(account.balance), float(account.equity)),
            "positions": [_serialize_position(position) for position in positions],
            "orders": [_serialize_order(order) for order in orders],
            "runtime": {"magic": cfg.magic, "entry_timeframe": cfg.entry_timeframe, "regime_timeframe": cfg.regime_timeframe},
        }
        _MT5_STATUS_CACHE["value"] = status
        _MT5_STATUS_CACHE["expires_at"] = time.monotonic() + 1.0
        return status
    finally:
        shutdown()


def _bot_pids(project_dir: Path) -> list[int]:
    escaped = str(project_dir)
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -match 'run_signal_bot.py' "
        f"-and $_.CommandLine -match [regex]::Escape('{escaped}') }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=8)
        return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
    except Exception:
        return []


def _bot_status(project_dir: Path) -> dict:
    pids = _bot_pids(project_dir)
    return {"running": bool(pids), "pids": pids}


def _process_pids(project_dir: Path, script_name: str) -> list[int]:
    escaped = str(project_dir)
    safe_script = re.escape(script_name)
    command = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -like 'python*.exe' -and $_.CommandLine -match '{safe_script}' "
        f"-and $_.CommandLine -match [regex]::Escape('{escaped}') }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=8)
        return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
    except Exception:
        return []


def _xau_scalp_status(project_dir: Path) -> dict:
    pids = _process_pids(project_dir, "run_xau_scalp_bot.py")
    return {"running": bool(pids), "pids": pids}


def _xau_contr_scalp_status(project_dir: Path) -> dict:
    pids = _process_pids(project_dir, "run_xau_contr_scalp_bot.py")
    return {"running": bool(pids), "pids": pids}


def _agent_teams_status(project_dir: Path) -> dict:
    pids = _process_pids(project_dir, "run_agent_teams_profile.py")
    return {"running": bool(pids), "pids": pids}


def _agent_teams_data_dir(project_dir: Path) -> Path:
    values = _read_env(project_dir / ".env.vantage.agent_teams")
    raw = str(values.get("AGENT_TEAM_DATA_DIR", "data_vantage_agent_teams") or "data_vantage_agent_teams")
    path = Path(raw)
    return path if path.is_absolute() else project_dir / path


def _start_bot(project_dir: Path) -> dict:
    if _bot_pids(project_dir):
        return {"message": "Bot już działa", **_bot_status(project_dir)}
    python = project_dir / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    out = open(project_dir / "signal_bot.out.log", "a", encoding="utf-8")
    err = open(project_dir / "signal_bot.err.log", "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen([str(python), "-u", "run_signal_bot.py"], cwd=str(project_dir), stdout=out, stderr=err, creationflags=flags)
    return {"message": f"Uruchomiono bota PID {process.pid}", **_bot_status(project_dir)}


def _start_xau_scalp(project_dir: Path) -> dict:
    if _process_pids(project_dir, "run_xau_scalp_bot.py"):
        return {"message": "XAU Scalp juz dziala", **_xau_scalp_status(project_dir)}
    python = project_dir / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    out = open(project_dir / "xau_scalp_bot.out.log", "a", encoding="utf-8")
    err = open(project_dir / "xau_scalp_bot.err.log", "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen([str(python), "-u", "run_xau_scalp_bot.py"], cwd=str(project_dir), stdout=out, stderr=err, creationflags=flags)
    return {"message": f"Uruchomiono XAU Scalp PID {process.pid}", **_xau_scalp_status(project_dir)}


def _start_xau_contr_scalp(project_dir: Path) -> dict:
    if _process_pids(project_dir, "run_xau_contr_scalp_bot.py"):
        return {"message": "Kontr Scalper juz dziala", **_xau_contr_scalp_status(project_dir)}
    python = project_dir / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    out = open(project_dir / "xau_contr_scalp_bot.out.log", "a", encoding="utf-8")
    err = open(project_dir / "xau_contr_scalp_bot.err.log", "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen([str(python), "-u", "run_xau_contr_scalp_bot.py"], cwd=str(project_dir), stdout=out, stderr=err, creationflags=flags)
    return {"message": f"Uruchomiono Kontr Scalper PID {process.pid}", **_xau_contr_scalp_status(project_dir)}


def _start_agent_teams(project_dir: Path) -> dict:
    if _process_pids(project_dir, "run_agent_teams_profile.py"):
        return {"message": "Agent Teams już działa", **_agent_teams_status(project_dir)}
    python = project_dir / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    out = open(project_dir / "agent_teams.vantage.out.log", "a", encoding="utf-8")
    err = open(project_dir / "agent_teams.vantage.err.log", "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            str(python),
            "-u",
            "run_agent_teams_profile.py",
            ".env.vantage",
            ".env.vantage.signal",
            ".env.vantage.agent_teams",
        ],
        cwd=str(project_dir),
        stdout=out,
        stderr=err,
        creationflags=flags,
    )
    return {"message": f"Uruchomiono Agent Teams PID {process.pid}", **_agent_teams_status(project_dir)}


def _stop_bot(project_dir: Path) -> dict:
    pids = _bot_pids(project_dir)
    stopped: list[int] = []
    for pid in pids:
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"], timeout=8)
            stopped.append(pid)
        except Exception:
            continue
    return {"message": f"Zatrzymano: {stopped or '-'}", **_bot_status(project_dir)}


def _stop_xau_scalp(project_dir: Path) -> dict:
    pids = _process_pids(project_dir, "run_xau_scalp_bot.py")
    stopped: list[int] = []
    for pid in pids:
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"], timeout=8)
            stopped.append(pid)
        except Exception:
            continue
    return {"message": f"Zatrzymano XAU Scalp: {stopped or '-'}", **_xau_scalp_status(project_dir)}


def _stop_xau_contr_scalp(project_dir: Path) -> dict:
    pids = _process_pids(project_dir, "run_xau_contr_scalp_bot.py")
    stopped: list[int] = []
    for pid in pids:
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"], timeout=8)
            stopped.append(pid)
        except Exception:
            continue
    return {"message": f"Zatrzymano Kontr Scalper: {stopped or '-'}", **_xau_contr_scalp_status(project_dir)}


def _stop_agent_teams(project_dir: Path) -> dict:
    pids = _process_pids(project_dir, "run_agent_teams_profile.py")
    stopped: list[int] = []
    for pid in pids:
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"], timeout=8)
            stopped.append(pid)
        except Exception:
            continue
    return {"message": f"Zatrzymano Agent Teams: {stopped or '-'}", **_agent_teams_status(project_dir)}


def _today_summary(events: list[dict], order_events: list[dict]) -> dict:
    today = datetime.now(UTC).date().isoformat()
    todays_events = [event for event in events if str(event.get("timestamp_utc", "")).startswith(today)]
    todays_orders = [event for event in order_events if str(event.get("timestamp_utc", "")).startswith(today)]
    return {
        "signals": sum(1 for event in todays_events if event.get("type") == "signal"),
        "orders": len(todays_orders),
        "tp": 0,
        "sl_or_be": 0,
        "closed_profit": 0.0,
    }


def serve() -> None:
    cfg = load_settings()
    data_dir = cfg.data_dir
    env_path = cfg.base_dir / ".env"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/config":
                _send_json(self, HTTPStatus.OK, {"fields": CONFIG_FIELDS, "values": _read_env(env_path)})
                return
            if parsed.path == "/api/channels":
                env_values = _read_env(env_path)
                _send_json(
                    self,
                    HTTPStatus.OK,
                    {
                        "channels": _split_channels(env_values.get("WATCH_CHANNELS", "")),
                        "lot_sizes": _parse_channel_lot_sizes(env_values.get("CHANNEL_LOT_SIZES", "{}")),
                    },
                )
                return
            if parsed.path == "/api/agent-teams":
                project_dir = load_settings().base_dir
                team_data_dir = _agent_teams_data_dir(project_dir)
                _send_json(
                    self,
                    HTTPStatus.OK,
                    {
                        "bot": _agent_teams_status(project_dir),
                        "status": _read_json(team_data_dir / "agent_teams_status.json", {}),
                        "events": _read_last_jsonl(team_data_dir / "agent_teams_events.jsonl", 40),
                        "trades": _read_last_trades(team_data_dir / "agent_teams_trades.csv", 30),
                        "analytics": _read_json(project_dir / "data_vantage" / "market_machine_analytics.json", {}),
                        "sizing": _read_json(project_dir / "data_vantage" / "market_machine_sizing_analysis.json", {}),
                        "github_scout": _read_json(project_dir / "data_vantage" / "github_strategy_scout_report.json", {}),
                    },
                )
                return
            if parsed.path == "/api/overview":
                current_cfg = load_settings()
                status = _read_json(data_dir / "status.json", {})
                try:
                    status = {**status, **_live_mt5_status(current_cfg)}
                except Exception as exc:
                    status = {
                        **status,
                        "heartbeat_utc": datetime.now(UTC).isoformat(),
                        "symbol": current_cfg.symbol,
                        "account": {"login": current_cfg.mt5_login, "server": current_cfg.mt5_server, "balance": 0.0, "equity": 0.0, "margin_free": 0.0, "profit": 0.0},
                        "positions": [],
                        "orders": [],
                        "risk": {
                            "spread_points": 0.0,
                            "max_daily_drawdown_pct": float(getattr(current_cfg, "max_daily_drawdown_pct", 0.0) or 0.0),
                            "max_total_drawdown_pct": float(getattr(current_cfg, "max_total_drawdown_pct", 0.0) or 0.0),
                        },
                        "dashboard_error": f"MT5 status error: {type(exc).__name__}: {exc}",
                    }
                signal_events = _read_last_jsonl(data_dir / "telegram_signal_events.jsonl", 40, event_type="signal")
                order_events = _read_last_jsonl(data_dir / "telegram_signal_orders.jsonl", 40)
                xau_scalp_status = _read_json(data_dir / "xau_scalp_status.json", {})
                xau_scalp_status = {
                    **xau_scalp_status,
                    "csv_total_realized_profit": _sum_trade_profit(data_dir / "xau_scalp_trades.csv"),
                    **_xau_scalp_mt5_pnl(current_cfg),
                }
                xau_contr_scalp_status = _read_json(data_dir / "xau_contr_scalp_status.json", {})
                xau_contr_scalp_status = {
                    **xau_contr_scalp_status,
                    "csv_total_realized_profit": _sum_trade_profit(data_dir / "xau_contr_scalp_trades.csv"),
                    **_xau_contr_scalp_mt5_pnl(current_cfg),
                }
                payload = {
                    "project_dir": str(current_cfg.base_dir),
                    "status": status,
                    "bot": _bot_status(current_cfg.base_dir),
                    "xau_scalp": {
                        "bot": _xau_scalp_status(current_cfg.base_dir),
                        "status": xau_scalp_status,
                        "events": _read_last_jsonl(data_dir / "xau_scalp_events.jsonl", 40),
                        "trades": _read_last_trades(data_dir / "xau_scalp_trades.csv", 30),
                    },
                    "xau_contr_scalp": {
                        "bot": _xau_contr_scalp_status(current_cfg.base_dir),
                        "status": xau_contr_scalp_status,
                        "events": _read_last_jsonl(data_dir / "xau_contr_scalp_events.jsonl", 40),
                        "trades": _read_last_trades(data_dir / "xau_contr_scalp_trades.csv", 30),
                    },
                    "trades": _read_last_trades(data_dir / "trades.csv", 30),
                    "signal_events": signal_events,
                    "order_events": order_events,
                    "channel_analyzer": _read_channel_analysis(current_cfg.base_dir, data_dir),
                    "today": _today_summary(signal_events, order_events),
                    "logs": {
                        "bot": _read_last_lines(current_cfg.base_dir / "logs" / "telegram_signal_bot.log", 80),
                        "err": _read_last_lines(current_cfg.base_dir / "signal_bot.err.log", 80),
                    },
                }
                _send_json(self, HTTPStatus.OK, payload)
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if parsed.path == "/api/config":
                    values = payload.get("values", {})
                    if not isinstance(values, dict):
                        raise ValueError("values must be an object")
                    clean = {str(key): str(value).strip() for key, value in values.items() if str(key) in CONFIG_KEYS}
                    _write_env_values(env_path, clean)
                    _send_json(self, HTTPStatus.OK, {"ok": True, "values": _read_env(env_path)})
                    return
                if parsed.path == "/api/channels":
                    raw_channels = payload.get("channels", [])
                    if not isinstance(raw_channels, list):
                        raise ValueError("channels must be a list")
                    channels = _split_channels("\n".join(str(item) for item in raw_channels))
                    if not channels:
                        raise ValueError("Dodaj przynajmniej jeden kanał")
                    lot_sizes = _clean_channel_lot_sizes(payload.get("lot_sizes", {}), channels)
                    _write_env_values(
                        env_path,
                        {
                            "WATCH_CHANNELS": ",".join(channels),
                            "CHANNEL_LOT_SIZES": json.dumps(lot_sizes, separators=(",", ":"), ensure_ascii=True),
                        },
                    )
                    _send_json(self, HTTPStatus.OK, {"channels": channels, "lot_sizes": lot_sizes})
                    return
                if parsed.path == "/api/bot":
                    action = str(payload.get("action", "")).lower()
                    if action == "start":
                        _send_json(self, HTTPStatus.OK, _start_bot(load_settings().base_dir))
                        return
                    if action == "stop":
                        _send_json(self, HTTPStatus.OK, _stop_bot(load_settings().base_dir))
                        return
                    raise ValueError("Unknown bot action")
                if parsed.path == "/api/xau-scalp":
                    action = str(payload.get("action", "")).lower()
                    if action == "start":
                        _send_json(self, HTTPStatus.OK, _start_xau_scalp(load_settings().base_dir))
                        return
                    if action == "stop":
                        _send_json(self, HTTPStatus.OK, _stop_xau_scalp(load_settings().base_dir))
                        return
                    raise ValueError("Unknown XAU Scalp action")
                if parsed.path == "/api/xau-contr-scalp":
                    action = str(payload.get("action", "")).lower()
                    if action == "start":
                        _send_json(self, HTTPStatus.OK, _start_xau_contr_scalp(load_settings().base_dir))
                        return
                    if action == "stop":
                        _send_json(self, HTTPStatus.OK, _stop_xau_contr_scalp(load_settings().base_dir))
                        return
                    raise ValueError("Unknown Kontr Scalper action")
                if parsed.path == "/api/agent-teams":
                    action = str(payload.get("action", "")).lower()
                    if action == "start":
                        _send_json(self, HTTPStatus.OK, _start_agent_teams(load_settings().base_dir))
                        return
                    if action == "stop":
                        _send_json(self, HTTPStatus.OK, _stop_agent_teams(load_settings().base_dir))
                        return
                    raise ValueError("Unknown Agent Teams action")
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
            except Exception as exc:
                _send_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", cfg.dashboard_port), Handler)
    print(f"xao Graal dashboard listening on http://127.0.0.1:{cfg.dashboard_port}")
    server.serve_forever()
