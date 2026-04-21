import os
import json
import logging
import asyncio
import threading
import subprocess
import signal
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import nest_asyncio
import requests
import yfinance as yf
import numpy as np
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

nest_asyncio.apply()

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(**name**)

# ── Config ─────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = "8699916911:AAEVUvgQZjhk0L4t_kza8Nax0RaRXi4Lycw"
ADMIN_ID       = 6848492273
METAL_API_KEY  = "53861da1c05ef5f93e3455735195d771"
GROQ_API_KEY   = "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"
GROQ_MODEL     = "llama-3.3-70b-versatile"

PARAMS_FILE = "params.json"
SUBS_FILE   = "subscribers.json"

DEFAULT_PARAMS = {
"capital":       50.0,
"objectif":      10000.0,
"risk_percent":  2.0,
"rr_minimum":    2.0,
"actif":         True,
"trades_gagnes": 0,
"trades_perdus": 0,
}

groq_client = Groq(api_key=GROQ_API_KEY)

active_trade = {
"open":      False,
"direction": None,
"entry":     None,
"sl":        None,
"tp":        None,
"lot":       None,
"be_moved":  False,
}

tg_app = None
signal_history = []
last_signal = {}

# ── Params ─────────────────────────────────────────────────────────────────────

def load_params() -> dict:
if os.path.exists(PARAMS_FILE):
with open(PARAMS_FILE) as f:
return {**DEFAULT_PARAMS, **json.load(f)}
return DEFAULT_PARAMS.copy()

def save_params(p: dict):
with open(PARAMS_FILE, "w") as f:
json.dump(p, f, indent=2)

# ── Subscriber management ──────────────────────────────────────────────────────

def load_subs() -> dict:
if os.path.exists(SUBS_FILE):
with open(SUBS_FILE) as f:
return json.load(f)
return {}

def save_subs(subs: dict):
with open(SUBS_FILE, "w") as f:
json.dump(subs, f, indent=2)

def is_sub_active(sub: dict) -> bool:
try:
expiry = datetime.strptime(sub["expiry_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
return datetime.now(timezone.utc) < expiry
except Exception:
return False

def get_active_subs() -> dict:
return {uid: s for uid, s in load_subs().items() if is_sub_active(s)}

def get_expired_subs() -> dict:
return {uid: s for uid, s in load_subs().items() if not is_sub_active(s)}

# ── Access control ─────────────────────────────────────────────────────────────

def is_admin(update: Update) -> bool:
return update.effective_user.id == ADMIN_ID

def is_subscriber(update: Update) -> bool:
uid = str(update.effective_user.id)
subs = load_subs()
return uid in subs and is_sub_active(subs[uid])

def can_access(update: Update) -> bool:
return is_admin(update) or is_subscriber(update)

async def deny(update: Update):
await update.message.reply_text(
"🔒 Accès refusé.\n\n"
"Contactez @wfn40 pour souscrire à Gold Hunter."
)

async def deny_admin(update: Update):
await update.message.reply_text("⛔ Accès refusé. Commande réservée à l’administrateur.")

# ── Broadcast helpers ──────────────────────────────────────────────────────────

async def broadcast(bot, text: str, parse_mode: str = None):
recipients = [ADMIN_ID] + [int(uid) for uid in get_active_subs()]
for uid in recipients:
try:
await bot.send_message(chat_id=uid, text=text, parse_mode=parse_mode)
except Exception as e:
logger.warning(f"Broadcast échec uid {uid}: {e}")

# ── Market schedule ────────────────────────────────────────────────────────────

def is_market_open() -> bool:
now = datetime.now(timezone.utc)
wd, h = now.weekday(), now.hour
if wd == 5 and h >= 22: return False
if wd == 6 and h < 22:  return False
return True

# ── Lot size calculation ───────────────────────────────────────────────────────

def calc_lot(capital: float, risk_pct: float, sl_distance: float) -> dict:
risk_eur = capital * risk_pct / 100
lot      = max(0.01, round(risk_eur / (sl_distance * 10), 2))
max_loss = round(lot * sl_distance * 10, 2)
return {"lot": lot, "risk_eur": round(risk_eur, 2), "max_loss": max_loss}

# ── Technical analysis ─────────────────────────────────────────────────────────

def get_technical_data() -> dict:
"""Analyse sur 1h + détection spike sur 15min"""
t  = yf.Ticker("GC=F")
df = t.history(period="60d", interval="1h")
if df.empty or len(df) < 50:
return {}

```
close = df["Close"].values
high  = df["High"].values
low   = df["Low"].values
price = round(float(close[-1]), 2)

delta    = np.diff(close)
gain     = np.where(delta > 0, delta, 0.0)
loss     = np.where(delta < 0, -delta, 0.0)
ag       = np.convolve(gain, np.ones(14)/14, mode='valid')
al       = np.convolve(loss, np.ones(14)/14, mode='valid')
rsi_arr  = 100 - 100 / (1 + ag / (al + 1e-10))
rsi      = round(float(rsi_arr[-1]), 1)
rsi_prev = round(float(rsi_arr[-6]), 1)

def ema(arr, span):
    k, res = 2/(span+1), [arr[0]]
    for v in arr[1:]: res.append(v*k + res[-1]*(1-k))
    return np.array(res)

macd_line = ema(close, 12) - ema(close, 26)
sig_line  = ema(macd_line, 9)
hist_val  = round(float((macd_line - sig_line)[-1]), 2)
macd_val  = round(float(macd_line[-1]), 2)
sig_val   = round(float(sig_line[-1]), 2)

sma20  = np.array([close[i-20:i].mean() for i in range(20, len(close)+1)])
std20  = np.array([close[i-20:i].std()  for i in range(20, len(close)+1)])
bb_up  = round(float(sma20[-1] + 2*std20[-1]), 2)
bb_low = round(float(sma20[-1] - 2*std20[-1]), 2)
ma20   = round(float(sma20[-1]), 2)
ma50   = round(float(close[-50:].mean()),  2) if len(close) >= 50  else None
ma200  = round(float(close[-200:].mean()), 2) if len(close) >= 200 else None

support    = round(float(low[-20:].min()),  2)
resistance = round(float(high[-20:].max()), 2)

price_up    = close[-1] > close[-6]
price_down  = close[-1] < close[-6]
rsi_up      = rsi > rsi_prev
rsi_down    = rsi < rsi_prev
bearish_div = price_up and rsi_down
bullish_div = price_down and rsi_up
trend = "HAUSSIERE 📈" if (ma50 and price > ma50) else "BAISSIERE 📉"

score_short = 0
if rsi > 65:                                        score_short += 2
if hist_val < 0:                                    score_short += 1
if price > bb_up:                                   score_short += 2
if abs(price - resistance) < resistance * 0.002:    score_short += 1
if bearish_div:                                     score_short += 3
if ma50 and ma20 and price < ma20 and price < ma50: score_short += 1

score_long = 0
if rsi < 35:                                        score_long += 2
if hist_val > 0:                                    score_long += 1
if price < bb_low:                                  score_long += 2
if abs(price - support) < support * 0.002:          score_long += 1
if bullish_div:                                     score_long += 3
if ma50 and ma20 and price > ma20 and price > ma50: score_long += 1

# ── Détection spike 15min ──────────────────────────────────────────────────
spike_detected = False
spike_direction = None
spike_amplitude = 0.0
try:
    df15 = t.history(period="1d", interval="15m")
    if not df15.empty and len(df15) >= 4:
        c15 = df15["Close"].values
        h15 = df15["High"].values
        l15 = df15["Low"].values
        # Amplitude des 4 dernières bougies
        recent_range = float(h15[-4:].max() - l15[-4:].min())
        avg_range    = float(np.mean([h15[i] - l15[i] for i in range(-12, -4)]) if len(c15) >= 12 else recent_range)
        if avg_range > 0 and recent_range > avg_range * 2.0:
            spike_detected  = True
            spike_amplitude = round(recent_range, 2)
            spike_direction = "BUY" if c15[-1] > c15[-4] else "SELL"
            logger.info(f"Spike 15min détecté : {spike_direction} | amplitude {spike_amplitude}")
except Exception as e:
    logger.warning(f"Analyse 15min échouée : {e}")

return {
    "price": price, "rsi": rsi, "macd": macd_val, "macd_sig": sig_val,
    "macd_hist": hist_val, "bb_up": bb_up, "bb_low": bb_low,
    "ma20": ma20, "ma50": ma50, "ma200": ma200,
    "support": support, "resistance": resistance, "trend": trend,
    "score_short": score_short, "score_long": score_long,
    "bearish_div": bearish_div, "bullish_div": bullish_div,
    "spike_detected": spike_detected,
    "spike_direction": spike_direction,
    "spike_amplitude": spike_amplitude,
}
```

def get_gold_price() -> float | None:
try:
d = requests.get(
f"https://metals-api.com/api/latest?access_key={METAL_API_KEY}&base=USD&symbols=XAU",
timeout=10).json()
if d.get("success"): return round(1 / d["rates"]["XAU"], 2)
except Exception: pass
try:
h = yf.Ticker("GC=F").history(period="1d", interval="5m")
if not h.empty: return round(float(h["Close"].iloc[-1]), 2)
except Exception: pass
return None

# ── AI analysis ────────────────────────────────────────────────────────────────

def ai_analyse(tech: dict, params: dict) -> dict:
div_txt = ""
if tech.get("bearish_div"): div_txt = "⚠️ DIVERGENCE BAISSIÈRE RSI DÉTECTÉE"
elif tech.get("bullish_div"): div_txt = "⚠️ DIVERGENCE HAUSSIÈRE RSI DÉTECTÉE"

```
spike_txt = ""
if tech.get("spike_detected"):
    spike_txt = f"⚡ SPIKE 15MIN DÉTECTÉ : {tech['spike_direction']} | amplitude {tech['spike_amplitude']} pts"

prompt = f"""Tu es Gold Hunter, expert en trading XAU/USD.
```

Données techniques :

- Prix : {tech[‘price’]} | RSI(14) : {tech[‘rsi’]}
- MACD : {tech[‘macd’]} | Signal : {tech[‘macd_sig’]} | Histogramme : {tech[‘macd_hist’]}
- Bollinger : {tech[‘bb_low’]} -- {tech[‘bb_up’]}
- MA20 : {tech[‘ma20’]} | MA50 : {tech[‘ma50’]} | MA200 : {tech[‘ma200’]}
- Support : {tech[‘support’]} | Résistance : {tech[‘resistance’]}
- Tendance : {tech[‘trend’]}
- Score SHORT : {tech[‘score_short’]}/10 | Score LONG : {tech[‘score_long’]}/10
- {div_txt}
- {spike_txt}

Capital : {params[‘capital’]}€ | Risque : {params[‘risk_percent’]}% | RR min : {params[‘rr_minimum’]}

Analyse aussi : politique Fed, force du dollar, géopolitique, inflation, actualités or.

Réponds UNIQUEMENT en JSON valide, sans markdown :
{{
"direction": "BUY" ou "SELL" ou "WAIT",
"confiance": entier 0-100,
"entree": prix numérique,
"sl": prix stop loss numérique,
"tp": prix take profit numérique,
"rsi_comment": "interprétation RSI en français",
"macd_comment": "interprétation MACD en français",
"analyse_fondamentale": "analyse macro : Fed, dollar, inflation, géopolitique (3-4 phrases)",
"anticipation": "anticipation dans les prochaines heures (2-3 phrases)",
"risque": "ce qui invaliderait ce trade (1-2 phrases)"
}}

Règles absolues :

- Si confiance < 65 → direction = "WAIT"
- RR = abs(tp-entree)/abs(sl-entree) doit être >= {params[‘rr_minimum’]}, sinon WAIT
- Si un spike 15min est détecté et confirme la direction → tu peux monter la confiance de 5 points
- Si WAIT → entree/sl/tp = 0
  """
  try:
  resp = groq_client.chat.completions.create(
  model=GROQ_MODEL,
  messages=[{"role": "user", "content": prompt}],
  temperature=0.2, max_tokens=700,
  )
  text = resp.choices[0].message.content.strip()
  if text.startswith("`"): parts = text.split("`")
  text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
  return json.loads(text)
  except Exception as e:
  logger.error(f"AI error: {e}")
  return {"direction": "WAIT", "confiance": 0, "entree": 0, "sl": 0, "tp": 0,
  "rsi_comment": "-", "macd_comment": "-",
  "analyse_fondamentale": "Analyse indisponible.",
  "anticipation": "-", "risque": "-"}

# ── Message builders ───────────────────────────────────────────────────────────

def build_signal(tech: dict, ai: dict, params: dict) -> str:
direction = ai["direction"]
confiance = ai["confiance"]
entree, sl, tp = ai["entree"], ai["sl"], ai["tp"]
capital, risk_pct, objectif = params["capital"], params["risk_percent"], params["objectif"]

```
sl_dist = round(abs(entree - sl), 2) if entree and sl else 0
tp_dist = round(abs(tp - entree), 2) if entree and tp else 0
rr      = round(tp_dist / sl_dist, 1) if sl_dist else 0
rr_min  = params.get("rr_minimum", 2.0)

if direction in ("BUY", "SELL") and rr < rr_min:
    now = datetime.now(timezone.utc).strftime("🕐 %d/%m/%Y %H:%M")
    return (
        f"╔══════════════════════════╗\n"
        f"     🏆 GOLD HUNTER AGENT\n"
        f"     ⚡ SIGNAL XAU/USD\n"
        f"╚══════════════════════════╝\n\n"
        f"⏸ WAIT -- Signal ignoré\n\n"
        f"• Direction analysée : {direction}\n"
        f"• RR calculé : 1:{rr}\n"
        f"• RR minimum requis : 1:{rr_min}\n\n"
        f"❌ Ratio risque/rendement insuffisant.\n"
        f"Aucune position ouverte. L'agent attend une meilleure opportunité.\n\n"
        f"📐 Technique :\n"
        f"• Prix : {tech.get('price')} | RSI : {tech.get('rsi')}\n"
        f"• Score SHORT : {tech.get('score_short')}/10 | Score LONG : {tech.get('score_long')}/10\n\n"
        f"🔮 Anticipation :\n{ai.get('anticipation', '-')}\n\n"
        f"{now}"
    )

lot_data = calc_lot(capital, risk_pct, sl_dist) if sl_dist else {"lot":0,"risk_eur":0,"max_loss":0}
lot, risk_eur, max_loss = lot_data["lot"], lot_data["risk_eur"], lot_data["max_loss"]
gain = round(lot * tp_dist * 10, 2)

start_capital = DEFAULT_PARAMS["capital"]
journey_total = max(objectif - start_capital, 0.01)
progression   = round(max(capital - start_capital, 0) / journey_total * 100, 2)
trades_est    = int((objectif - capital) / max(risk_eur, 0.01))

dir_emoji = "🟢" if direction == "BUY" else ("🔴" if direction == "SELL" else "⚪")
sentiment = "🐂 BULLISH" if direction == "BUY" else ("🐻 BEARISH" if direction == "SELL" else "➖ NEUTRE")
sl_sign   = "-" if direction == "BUY" else "+"
tp_sign   = "+" if direction == "BUY" else "-"

div_line = ""
if tech.get("bullish_div"):  div_line = "• ⚠️ DIVERGENCE HAUSSIÈRE RSI DÉTECTÉE\n"
elif tech.get("bearish_div"): div_line = "• ⚠️ DIVERGENCE BAISSIÈRE RSI DÉTECTÉE\n"

spike_line = ""
if tech.get("spike_detected"):
    spike_line = f"• ⚡ SPIKE 15MIN : {tech['spike_direction']} | {tech['spike_amplitude']} pts\n"

ma200_str = f" | MA200 : {tech['ma200']}" if tech.get("ma200") else ""
now = datetime.now(timezone.utc).strftime("🕐 %d/%m/%Y %H:%M")

return (
    f"╔══════════════════════════╗\n"
    f"     🏆 GOLD HUNTER AGENT\n"
    f"     ⚡ SIGNAL XAU/USD\n"
    f"╚══════════════════════════╝\n\n"
    f"{dir_emoji} {direction} | Confiance : {confiance}% | {sentiment}\n\n"
    f"• Position d'entrée : {entree}\n"
    f"• Stop Loss : {sl} ({sl_sign}{sl_dist} pts)\n"
    f"• Take Profit : {tp} ({tp_sign}{tp_dist} pts)\n"
    f"• Taille de lot : {lot} lot\n"
    f"• Pourcentage risqué : {risk_pct}% ({risk_eur:.2f}€)\n"
    f"• Perte max si SL touché : -{max_loss:.2f}€\n"
    f"• Gain si TP atteint : +{gain:.2f}€\n"
    f"• RR : 1:{rr}\n\n"
    f"📐 Analyse Technique :\n"
    f"• RSI(14) : {tech['rsi']} → {ai['rsi_comment']}\n"
    f"• MACD : {tech['macd']} | Histogramme : {tech['macd_hist']} ({ai['macd_comment']})\n"
    f"• Tendance : {tech['trend']}\n"
    f"• MA20 : {tech['ma20']} | MA50 : {tech['ma50']}{ma200_str}\n"
    f"• Bollinger : {tech['bb_low']} -- {tech['bb_up']}\n"
    f"• Support clé : {tech['support']}\n"
    f"• Résistance clé : {tech['resistance']}\n"
    f"• Score SHORT : {tech['score_short']}/10 | Score LONG : {tech['score_long']}/10\n"
    f"{div_line}"
    f"{spike_line}"
    f"\n📊 Analyse Fondamentale :\n{ai['analyse_fondamentale']}\n\n"
    f"🔮 Anticipation :\n{ai['anticipation']}\n\n"
    f"⚠️ Risque principal :\n{ai['risque']}\n\n"
    f"💼 Gestion du capital :\n"
    f"• Capital actuel : {capital:.2f}€\n"
    f"• Objectif : {objectif:.2f}€\n"
    f"• Progression : {progression}%\n"
    f"• Trades restants estimés : {trades_est}\n\n"
    f"{now}"
)
```

def build_alert(tech: dict) -> str:
score_short, score_long = tech["score_short"], tech["score_long"]
direction = "SELL" if score_short >= score_long else "BUY"
score     = max(score_short, score_long)
signals   = []
if tech["rsi"] > 65:   signals.append(f"• RSI {tech[‘rsi’]} -- Zone surachat extrême")
elif tech["rsi"] < 35: signals.append(f"• RSI {tech[‘rsi’]} -- Zone survente extrême")
if tech.get("bearish_div"): signals.append("• Divergence baissière RSI confirmée")
if tech.get("bullish_div"): signals.append("• Divergence haussière RSI confirmée")
if abs(tech["price"] - tech["resistance"]) < tech["resistance"] * 0.003:
signals.append(f"• Prix sur résistance majeure {tech[‘resistance’]}")
if abs(tech["price"] - tech["support"]) < tech["support"] * 0.003:
signals.append(f"• Prix sur support majeur {tech[‘support’]}")
if tech["macd_hist"] < 0 and direction == "SELL": signals.append("• MACD histogramme négatif")
if tech["macd_hist"] > 0 and direction == "BUY":  signals.append("• MACD histogramme positif")
if tech.get("spike_detected"):
signals.append(f"• ⚡ Spike 15min détecté : {tech[‘spike_direction’]} | {tech[‘spike_amplitude’]} pts")
now = datetime.now(timezone.utc).strftime("🕐 %d/%m/%Y %H:%M")
label = "SHORT" if direction == "SELL" else "LONG"
return (
f"⚠️ ALERTE ANTICIPATOIRE {direction}\n\n"
f"Score {label} : {score}/10\n"
f"Signaux détectés :\n" + ("\n".join(signals) if signals else "• Indicateurs convergents") + "\n\n"
f"🔮 Signal {direction} potentiel dans les prochaines heures\n"
f"Restez attentif au prochain signal\n\n{now}"
)

def build_spike_alert(tech: dict) -> str:
"""Message d’alerte dédié aux spikes 15min"""
direction = tech["spike_direction"]
amplitude = tech["spike_amplitude"]
emoji     = "🟢" if direction == "BUY" else "🔴"
now       = datetime.now(timezone.utc).strftime("🕐 %d/%m/%Y %H:%M")
return (
f"╔══════════════════════════╗\n"
f"     🏆 GOLD HUNTER AGENT\n"
f"     ⚡ ALERTE SPIKE\n"
f"╚══════════════════════════╝\n\n"
f"{emoji} MOUVEMENT RAPIDE DÉTECTÉ : {direction}\n\n"
f"• Amplitude : {amplitude} pts sur 15min\n"
f"• Prix actuel : {tech[‘price’]}\n"
f"• RSI : {tech[‘rsi’]}\n"
f"• Tendance fond : {tech[‘trend’]}\n\n"
f"⚠️ Analyse complète en cours -- signal à venir.\n\n"
f"{now}"
)

def build_technique(tech: dict) -> str:
div_line = ""
if tech.get("bullish_div"):  div_line = "\n• ⚠️ DIVERGENCE HAUSSIÈRE RSI DÉTECTÉE"
elif tech.get("bearish_div"): div_line = "\n• ⚠️ DIVERGENCE BAISSIÈRE RSI DÉTECTÉE"
spike_line = ""
if tech.get("spike_detected"):
spike_line = f"\n• ⚡ SPIKE 15MIN : {tech[‘spike_direction’]} | {tech[‘spike_amplitude’]} pts"
ma200_str = f" | MA200 : {tech[‘ma200’]}" if tech.get("ma200") else ""
return (
f"📐 *Analyse Technique XAU/USD*\n\n"
f"💰 Prix : `{tech['price']}`\n"
f"• RSI(14) : `{tech['rsi']}`\n"
f"• MACD : `{tech['macd']}` | Histogramme : `{tech['macd_hist']}`\n"
f"• Tendance : {tech[‘trend’]}\n"
f"• MA20 : `{tech['ma20']}` | MA50 : `{tech['ma50']}`{ma200_str}\n"
f"• Bollinger : `{tech['bb_low']}` -- `{tech['bb_up']}`\n"
f"• Support clé : `{tech['support']}`\n"
f"• Résistance clé : `{tech['resistance']}`\n"
f"• Score SHORT : `{tech['score_short']}/10` | Score LONG : `{tech['score_long']}/10`"
f"{div_line}"
f"{spike_line}"
)

def build_close_message(direction: str, entry: float, close_price: float,
lot: float, pnl: float, params: dict) -> str:
dist      = round(abs(close_price - entry), 2)
sign      = "+" if pnl >= 0 else ""
new_cap   = round(params["capital"] + pnl, 2)
pct       = round(pnl / params["capital"] * 100, 1)
start_cap = DEFAULT_PARAMS["capital"]
journey_total = max(params["objectif"] - start_cap, 0.01)
progression   = round(max(new_cap - start_cap, 0) / journey_total * 100, 2)
gagnes, perdus = params["trades_gagnes"], params["trades_perdus"]
total = gagnes + perdus
wr    = round(gagnes / total * 100) if total > 0 else 0
dist_sign = "+" if (direction == "BUY" and close_price > entry) or   
(direction == "SELL" and close_price < entry) else "-"
now = datetime.now(timezone.utc).strftime("🕐 %d/%m/%Y %H:%M")
return (
f"╔══════════════════════════╗\n"
f"     ✅ TRADE FERMÉ\n"
f"╚══════════════════════════╝\n\n"
f"• Direction : {direction}\n"
f"• Entrée : {entry}\n"
f"• Fermeture : {close_price}\n"
f"• Lot : {lot}\n"
f"• Distance : {dist_sign}{dist} pts\n"
f"• Résultat : {sign}{pnl:.2f}€ ({sign}{pct}%)\n"
f"• Nouveau capital : {new_cap:.2f}€\n"
f"• Progression vers objectif : {progression}%\n"
f"• Trades gagnants : {gagnes} | Perdants : {perdus}\n"
f"• Win rate : {wr}%\n\n{now}"
)

# ── USER commands ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
uid = update.effective_user.id
if uid == ADMIN_ID:
await update.message.reply_text(
"╔══════════════════════════╗\n"
"     🏆 GOLD HUNTER AGENT\n"
"     👑 MODE ADMIN\n"
"╚══════════════════════════╝\n\n"
"📋 *Commandes utilisateur :*\n"
"/analyse -- Signal complet\n"
"/technique -- Indicateurs techniques\n"
"/anticipation -- Anticipation marché\n"
"/status -- Statut + capital\n\n"
"👑 *Commandes admin :*\n"
"/admin -- Panel admin\n"
"/adduser [id] [jours] -- Ajouter abonné\n"
"/removeuser [id] -- Supprimer abonné\n"
"/users -- Liste abonnés\n"
"/broadcast [message] -- Diffuser message\n"
"/stats -- Statistiques\n"
"/params -- Paramètres\n"
"/setcapital 50 | /setobjectif 10000\n"
"/setrisk 2 | /pause | /reprendre | /reset",
parse_mode="Markdown"
)
elif is_subscriber(update):
await update.message.reply_text(
"🏆 GOLD HUNTER AGENT\n"
"Signal XAU/USD professionnel\n\n"
"📋 Vos commandes :\n"
"/analyse -- Signal complet maintenant\n"
"/technique -- Analyse technique\n"
"/anticipation -- Anticipation marché\n"
"/status -- Prix actuel et statut"
)
else:
await update.message.reply_text(
"╔══════════════════════════╗\n"
"     🏆 GOLD HUNTER AGENT\n"
"╚══════════════════════════╝\n\n"
"🔒 Accès restreint.\n\n"
"Contactez @wfn40 pour souscrire à Gold Hunter et recevoir des signaux XAU/USD professionnels."
)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not can_access(update): return await deny(update)
params = load_params()
price  = get_gold_price()
price_str = f"{price}" if price else "Indisponible"
trade_txt = "Aucun trade actif."
if active_trade["open"]:
trade_txt = (
f"Direction : {active_trade[‘direction’]}\n"
f"Entrée : {active_trade[‘entry’]}\n"
f"SL : {active_trade[‘sl’]} | TP : {active_trade[‘tp’]}\n"
f"Lot : {active_trade[‘lot’]}\n"
f"BE déplacé : {‘Oui’ if active_trade[‘be_moved’] else ‘Non’}"
)
start_cap = DEFAULT_PARAMS["capital"]
progression = round(max(params["capital"] - start_cap, 0) / max(params["objectif"] - start_cap, 0.01) * 100, 2)
total = params["trades_gagnes"] + params["trades_perdus"]
wr    = round(params["trades_gagnes"] / total * 100) if total > 0 else 0
now   = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
await update.message.reply_text(
f"📊 *Statut Gold Hunter*\n\n"
f"💰 Prix actuel : `{price_str}`\n"
f"📈 Marché : {‘Ouvert ✅’ if is_market_open() else ‘Fermé ❌’}\n"
f"⏸ Bot : {‘Actif ✅’ if params[‘actif’] else ‘En pause ⏸’}\n"
f"🕐 {now}\n\n"
f"🔄 *Trade actif :*\n{trade_txt}\n\n"
f"💼 *Capital :*\n"
f"• Capital : `{params['capital']:.2f}€`\n"
f"• Objectif : `{params['objectif']:.2f}€`\n"
f"• Progression : `{progression}%`\n"
f"• Win rate : `{wr}%` ({params[‘trades_gagnes’]}W / {params[‘trades_perdus’]}L)",
parse_mode="Markdown"
)

async def cmd_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not can_access(update): return await deny(update)
await update.message.reply_text("⏳ Analyse complète en cours…")
params = load_params()
tech   = get_technical_data()
if not tech:
await update.message.reply_text("⚠️ Données de marché indisponibles.")
return
ai      = ai_analyse(tech, params)
msg     = build_signal(tech, ai, params)
sl_dist = abs(ai["entree"] - ai["sl"]) if ai["entree"] and ai["sl"] else 0
tp_dist = abs(ai["tp"] - ai["entree"]) if ai["tp"] and ai["entree"] else 0
rr      = round(tp_dist / sl_dist, 1) if sl_dist else 0
if ai["direction"] in ("BUY", "SELL") and rr >= params.get("rr_minimum", 2.0):
lot_d = calc_lot(params["capital"], params["risk_percent"], sl_dist)
active_trade.update(open=True, direction=ai["direction"],
entry=ai["entree"], sl=ai["sl"], tp=ai["tp"],
lot=lot_d["lot"], be_moved=False)
await update.message.reply_text(msg)

async def cmd_technique(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not can_access(update): return await deny(update)
tech = get_technical_data()
if not tech:
await update.message.reply_text("⚠️ Données indisponibles.")
return
await update.message.reply_text(build_technique(tech), parse_mode="Markdown")

async def cmd_anticipation(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not can_access(update): return await deny(update)
await update.message.reply_text("⏳ Calcul de l’anticipation…")
params = load_params()
tech   = get_technical_data()
if not tech:
await update.message.reply_text("⚠️ Données indisponibles.")
return
ai = ai_analyse(tech, params)
await update.message.reply_text(
f"🔮 *Anticipation Gold Hunter*\n\n{ai[‘anticipation’]}\n\n"
f"⚠️ Risque principal : {ai[‘risque’]}",
parse_mode="Markdown"
)

# ── ADMIN commands ─────────────────────────────────────────────────────────────

async def cmd_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
p     = load_params()
total = p["trades_gagnes"] + p["trades_perdus"]
wr    = round(p["trades_gagnes"] / total * 100, 1) if total > 0 else 0.0
await update.message.reply_text(
f"⚙️ *Paramètres*\n\n"
f"💰 Capital : `{p['capital']:.2f}€`\n"
f"🎯 Objectif : `{p['objectif']:.2f}€`\n"
f"⚠️ Risque : `{p['risk_percent']}%`\n"
f"📐 RR minimum : `{p['rr_minimum']}`\n"
f"🤖 Actif : `{'Oui' if p['actif'] else 'Non'}`\n\n"
f"📊 *Statistiques :*\n"
f"✅ Trades gagnants : `{p['trades_gagnes']}`\n"
f"❌ Trades perdants : `{p['trades_perdus']}`\n"
f"🏆 Win rate : `{wr}%` ({total} trades)",
parse_mode="Markdown"
)

async def cmd_setcapital(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
try:
val = float(context.args[0]); assert val > 0
p = load_params(); p["capital"] = val; save_params(p)
await update.message.reply_text(f"✅ Capital défini à `{val:.2f}€`", parse_mode="Markdown")
except Exception:
await update.message.reply_text("Usage : /setcapital <montant>")

async def cmd_setobjectif(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
try:
val = float(context.args[0]); assert val > 0
p = load_params(); p["objectif"] = val; save_params(p)
await update.message.reply_text(f"✅ Objectif défini à `{val:.2f}€`", parse_mode="Markdown")
except Exception:
await update.message.reply_text("Usage : /setobjectif <montant>")

async def cmd_setrisk(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
try:
val = float(context.args[0]); assert 0.1 <= val <= 5
p = load_params(); p["risk_percent"] = val; save_params(p)
await update.message.reply_text(f"✅ Risque défini à `{val}%`", parse_mode="Markdown")
except Exception:
await update.message.reply_text("Usage : /setrisk <0.1–5>")

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
p = load_params(); p["actif"] = False; save_params(p)
await update.message.reply_text("⏸ Bot mis en *pause*.", parse_mode="Markdown")

async def cmd_reprendre(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
p = load_params(); p["actif"] = True; save_params(p)
await update.message.reply_text("▶️ Bot *repris*.", parse_mode="Markdown")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
save_params(DEFAULT_PARAMS.copy())
active_trade.update(open=False, direction=None, entry=None, sl=None, tp=None, lot=None, be_moved=False)
await update.message.reply_text("🔄 Réinitialisé : capital 50€ | objectif 10 000€.", parse_mode="Markdown")

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
active  = get_active_subs()
expired = get_expired_subs()
await update.message.reply_text(
f"👑 PANEL ADMIN -- GOLD HUNTER\n\n"
f"👥 Abonnés actifs : {len(active)}\n"
f"⏰ Abonnés expirés : {len(expired)}\n\n"
f"📋 Commandes admin :\n"
f"/adduser [id] [jours] -- Ajouter abonné\n"
f"/removeuser [id] -- Supprimer abonné\n"
f"/users -- Liste abonnés\n"
f"/broadcast [message] -- Diffuser message\n"
f"/stats -- Statistiques"
)

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
try:
args   = context.args
uid    = str(int(args[0]))
days   = int(args[1])
plan   = args[2] if len(args) > 2 else "basic"
expiry = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
subs   = load_subs()
user   = await context.bot.get_chat(int(uid))
name   = f"@{user.username}" if user.username else user.first_name or uid
subs[uid] = {
"user_id":     int(uid),
"name":        name,
"expiry_date": expiry,
"plan":        plan,
"added_date":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
}
save_subs(subs)
await update.message.reply_text(
f"✅ Abonné ajouté !\n"
f"• ID : {uid}\n"
f"• Nom : {name}\n"
f"• Plan : {plan}\n"
f"• Expire : {expiry} ({days} jours)"
)
try:
await context.bot.send_message(
chat_id=int(uid),
text=(
f"🎉 Bienvenue sur *Gold Hunter* !\n\n"
f"Votre abonnement *{plan.upper()}* est actif jusqu’au {expiry}.\n"
f"Tapez /start pour voir vos commandes."
),
parse_mode="Markdown"
)
except Exception:
pass
except Exception as e:
await update.message.reply_text(f"Usage : /adduser [user_id] [jours] [plan]\nErreur : {e}")

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
try:
uid  = str(int(context.args[0]))
subs = load_subs()
if uid in subs:
name = subs[uid].get("name", uid)
del subs[uid]
save_subs(subs)
await update.message.reply_text(f"✅ Abonné {name} ({uid}) supprimé.")
else:
await update.message.reply_text(f"❌ Utilisateur {uid} introuvable.")
except Exception:
await update.message.reply_text("Usage : /removeuser [user_id]")

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
active  = get_active_subs()
expired = get_expired_subs()
lines   = ["👥 *ABONNÉS ACTIFS*\n"]
for i, (uid, s) in enumerate(active.items(), 1):
exp = datetime.strptime(s["expiry_date"], "%Y-%m-%d").strftime("%d/%m/%Y")
lines.append(f"{i}. {s[‘name’]} (`{uid}`)\n   Plan : {s[‘plan’].capitalize()} | Expire : {exp}")
if not active:
lines.append("*(aucun abonné actif)*")
if expired:
lines.append(f"\n⏰ *Abonnés expirés : {len(expired)}*")
for uid, s in expired.items():
lines.append(f"• {s[‘name’]} (`{uid}`) -- expiré le {s[‘expiry_date’]}")
await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
if not context.args:
await update.message.reply_text("Usage : /broadcast [message]")
return
msg     = " ".join(context.args)
active  = get_active_subs()
success, failed = 0, 0
for uid in [str(ADMIN_ID)] + list(active.keys()):
try:
await context.bot.send_message(chat_id=int(uid), text=f"📢 *Message Gold Hunter*\n\n{msg}", parse_mode="Markdown")
success += 1
except Exception:
failed += 1
await update.message.reply_text(f"✅ Broadcast envoyé : {success} reçu(s), {failed} échec(s).")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_admin(update): return await deny_admin(update)
subs    = load_subs()
active  = get_active_subs()
expired = get_expired_subs()
plans   = {"basic": 0, "pro": 0, "vip": 0}
for s in active.values():
plans[s.get("plan", "basic")] = plans.get(s.get("plan", "basic"), 0) + 1
params  = load_params()
total_trades = params["trades_gagnes"] + params["trades_perdus"]
wr = round(params["trades_gagnes"] / total_trades * 100, 1) if total_trades > 0 else 0
await update.message.reply_text(
f"📊 *Statistiques Gold Hunter*\n\n"
f"👥 Total abonnés : `{len(subs)}`\n"
f"✅ Actifs : `{len(active)}`\n"
f"⏰ Expirés : `{len(expired)}`\n\n"
f"📋 *Plans actifs :*\n"
f"• Basic : `{plans.get('basic', 0)}`\n"
f"• Pro : `{plans.get('pro', 0)}`\n"
f"• VIP : `{plans.get('vip', 0)}`\n\n"
f"📈 *Trading :*\n"
f"• Trades gagnants : `{params['trades_gagnes']}`\n"
f"• Trades perdants : `{params['trades_perdus']}`\n"
f"• Win rate : `{wr}%`\n"
f"• Capital : `{params['capital']:.2f}€`",
parse_mode="Markdown"
)

# ── Auto-analysis loop ─────────────────────────────────────────────────────────

async def auto_loop(bot):
"""
AMÉLIORATIONS :
- Analyse immédiate au démarrage (plus de 2h d’attente)
- Loop principale toutes les 2h
- Loop rapide toutes les 15min pour détecter les spikes
- Seuil d’alerte abaissé à score >= 4
- Confiance abaissée à 65% dans le prompt AI
"""
first_run = True

```
async def run_analysis():
    try:
        params = load_params()
        if not params.get("actif", True):
            logger.info("Auto-analyse ignorée (pause).")
            return
        if not is_market_open():
            logger.info("Auto-analyse ignorée (marché fermé).")
            return

        logger.info("Auto-analyse démarrée…")
        tech = get_technical_data()
        if not tech:
            logger.warning("Données indisponibles pour l'auto-analyse.")
            return

        score_max = max(tech["score_short"], tech["score_long"])

        # Alerte anticipatoire : score entre 4 et 7 (était 6-8)
        if 4 <= score_max < 8:
            alert = build_alert(tech)
            await broadcast(bot, alert)
            logger.info(f"Alerte broadcast (score {score_max}/10).")
            return

        ai      = ai_analyse(tech, params)
        msg     = build_signal(tech, ai, params)
        sl_dist = abs(ai["entree"] - ai["sl"]) if ai["entree"] and ai["sl"] else 0
        tp_dist = abs(ai["tp"] - ai["entree"]) if ai["tp"] and ai["entree"] else 0
        rr      = round(tp_dist / sl_dist, 1) if sl_dist else 0
        if ai["direction"] in ("BUY", "SELL") and rr >= params.get("rr_minimum", 2.0):
            lot_d = calc_lot(params["capital"], params["risk_percent"], sl_dist)
            active_trade.update(open=True, direction=ai["direction"],
                                entry=ai["entree"], sl=ai["sl"], tp=ai["tp"],
                                lot=lot_d["lot"], be_moved=False)

        await broadcast(bot, msg)
        logger.info(f"Signal broadcast : {ai['direction']} | RR: {rr}")

        signal_record = {
            "id":          int(datetime.now(timezone.utc).timestamp()),
            "pair":        "XAUUSD",
            "direction":   ai["direction"],
            "entry":       ai["entree"],
            "sl":          ai["sl"],
            "tp":          ai["tp"],
            "confiance":   ai["confiance"],
            "rr":          rr,
            "status":      "active",
            "time":        datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "date":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "rsi":         tech.get("rsi", 0),
            "trend":       tech.get("trend", ""),
            "anticipation": ai.get("anticipation", ""),
        }
        signal_history.insert(0, signal_record)
        if len(signal_history) > 20:
            signal_history.pop()

    except Exception as e:
        logger.error(f"Erreur auto-loop : {e}")

async def spike_watch():
    """Boucle rapide 15min dédiée à la détection des spikes"""
    while True:
        await asyncio.sleep(900)  # 15 minutes
        try:
            params = load_params()
            if not params.get("actif", True) or not is_market_open():
                continue
            tech = get_technical_data()
            if not tech:
                continue
            if tech.get("spike_detected"):
                logger.info(f"Spike détecté par spike_watch : {tech['spike_direction']}")
                # Alerte spike immédiate
                spike_msg = build_spike_alert(tech)
                await broadcast(bot, spike_msg)
                # Puis analyse complète
                await asyncio.sleep(30)
                await run_analysis()
        except Exception as e:
            logger.error(f"Erreur spike_watch : {e}")

# Lancer le spike_watch en parallèle
asyncio.ensure_future(spike_watch())

# Boucle principale 2h
while True:
    if first_run:
        # Première analyse immédiate au démarrage
        logger.info("Première analyse au démarrage…")
        await run_analysis()
        first_run = False
    else:
        await asyncio.sleep(7200)
        await run_analysis()
```

# ── Expiry check loop ──────────────────────────────────────────────────────────

async def expiry_loop(bot):
while True:
await asyncio.sleep(86400)
try:
subs    = load_subs()
now_utc = datetime.now(timezone.utc)
changed = False
for uid, s in list(subs.items()):
expiry    = datetime.strptime(s["expiry_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
days_left = (expiry - now_utc).days
if days_left < 0:
del subs[uid]
changed = True
logger.info(f"Abonné expiré supprimé : {uid}")
try:
await bot.send_message(
chat_id=int(uid),
text="⏰ Votre abonnement *Gold Hunter* a expiré.\n\nContactez @wfn40 pour renouveler.",
parse_mode="Markdown"
)
except Exception:
pass
await bot.send_message(
chat_id=ADMIN_ID,
text=f"⏰ Abonné expiré supprimé : {s[‘name’]} ({uid})"
)
elif days_left <= 3:
try:
await bot.send_message(
chat_id=int(uid),
text=f"⚠️ Votre abonnement *Gold Hunter* expire dans *{days_left} jour(s)* (le {s[‘expiry_date’]}).\n\nContactez @wfn40 pour renouveler.",
parse_mode="Markdown"
)
except Exception:
pass
if changed:
save_subs(subs)
except Exception as e:
logger.error(f"Erreur expiry-loop : {e}")

# ── Startup cleanup ────────────────────────────────────────────────────────────

def kill_stale_instances():
my_pid = os.getpid()
try:
result = subprocess.run(["pgrep", "-f", "python.*main.py"], capture_output=True, text=True)
for pid_str in result.stdout.strip().splitlines():
pid = int(pid_str)
if pid != my_pid:
try:
os.kill(pid, signal.SIGKILL)
logger.info(f"Instance fantôme tuée (PID {pid})")
except ProcessLookupError:
pass
except Exception as e:
logger.warning(f"kill_stale_instances: {e}")

```
def _kill_port_8080():
    import glob as _glob
    port_hex     = format(8080, '04X')
    inode_target = None
    for tcp_file in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(tcp_file, 'r') as f:
                for line in f.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) < 10:
                        continue
                    local_port_hex = parts[1].split(':')[-1]
                    if local_port_hex.upper() == port_hex:
                        inode_target = parts[9]
                        break
        except Exception:
            pass
        if inode_target:
            break
    if inode_target:
        for fd_path in _glob.glob('/proc/[0-9]*/fd/*'):
            try:
                link = os.readlink(fd_path)
                if f'socket:[{inode_target}]' in link:
                    pid = int(fd_path.split('/')[2])
                    if pid != os.getpid():
                        os.kill(pid, signal.SIGKILL)
                        logger.info(f"Port 8080 libéré (PID {pid})")
            except Exception:
                pass
    else:
        logger.info("Port 8080 déjà libre.")
try:
    _kill_port_8080()
except Exception as e:
    logger.warning(f"Libération port 8080 : {e}")
```

# ── HTTP uptime server ─────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):

```
def _send_json(self, data: dict, status: int = 200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

def do_OPTIONS(self):
    self.send_response(204)
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    self.end_headers()

def do_GET(self):
    if self.path == "/":
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Gold Hunter OK")
    elif self.path == "/api/signals":
        self._send_json({"signals": signal_history})
    elif self.path == "/api/status":
        params = load_params()
        total  = params["trades_gagnes"] + params["trades_perdus"]
        wr     = round(params["trades_gagnes"] / total * 100, 1) if total > 0 else 0
        self._send_json({
            "active":        params.get("actif", True),
            "market_open":   is_market_open(),
            "capital":       params["capital"],
            "objectif":      params["objectif"],
            "risk_percent":  params["risk_percent"],
            "trades_gagnes": params["trades_gagnes"],
            "trades_perdus": params["trades_perdus"],
            "win_rate":      wr,
            "active_trade":  active_trade,
        })
    elif self.path == "/api/price":
        price = get_gold_price()
        self._send_json({"price": price, "pair": "XAUUSD"})
    else:
        self._send_json({"error": "Not found"}, 404)

def log_message(self, *args):
    pass
```

def start_http_server():
import time
HTTPServer.allow_reuse_address = True
for attempt in range(6):
try:
server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
logger.info(f"HTTP uptime server sur le port 8080 (tentative {attempt+1})")
server.serve_forever()
return
except OSError:
logger.warning(f"Port 8080 occupé, nouvelle tentative dans 1s ({attempt+1}/6)…")
time.sleep(1)
logger.error("Impossible de démarrer le serveur HTTP sur 8080 après 6 tentatives.")

# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
global tg_app
kill_stale_instances()
threading.Thread(target=start_http_server, daemon=True).start()

```
tg_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

tg_app.add_handler(CommandHandler("start",        cmd_start))
tg_app.add_handler(CommandHandler("status",       cmd_status))
tg_app.add_handler(CommandHandler("analyse",      cmd_analyse))
tg_app.add_handler(CommandHandler("technique",    cmd_technique))
tg_app.add_handler(CommandHandler("anticipation", cmd_anticipation))
tg_app.add_handler(CommandHandler("params",       cmd_params))
tg_app.add_handler(CommandHandler("setcapital",   cmd_setcapital))
tg_app.add_handler(CommandHandler("setobjectif",  cmd_setobjectif))
tg_app.add_handler(CommandHandler("setrisk",      cmd_setrisk))
tg_app.add_handler(CommandHandler("pause",        cmd_pause))
tg_app.add_handler(CommandHandler("reprendre",    cmd_reprendre))
tg_app.add_handler(CommandHandler("reset",        cmd_reset))
tg_app.add_handler(CommandHandler("admin",        cmd_admin))
tg_app.add_handler(CommandHandler("adduser",      cmd_adduser))
tg_app.add_handler(CommandHandler("removeuser",   cmd_removeuser))
tg_app.add_handler(CommandHandler("users",        cmd_users))
tg_app.add_handler(CommandHandler("broadcast",    cmd_broadcast_msg))
tg_app.add_handler(CommandHandler("stats",        cmd_stats))

await tg_app.initialize()
await tg_app.start()
await tg_app.updater.start_polling(drop_pending_updates=True)
logger.info("🏆 Gold Hunter Agent démarré -- 24/7 actif.")

# Message de démarrage à l'admin
try:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    await tg_app.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"✅ *Gold Hunter Agent démarré*\n\n"
            f"🕐 {now}\n"
            f"🔍 Première analyse en cours…\n"
            f"⚡ Détection spike 15min : activée\n"
            f"📊 Seuil alerte : score ≥ 4/10\n"
            f"🎯 Confiance min : 65%"
        ),
        parse_mode="Markdown"
    )
except Exception as e:
    logger.warning(f"Message démarrage échoué : {e}")

await asyncio.gather(
    auto_loop(tg_app.bot),
    expiry_loop(tg_app.bot),
)
```

if **name** == "**main**":
asyncio.get_event_loop().run_until_complete(main())