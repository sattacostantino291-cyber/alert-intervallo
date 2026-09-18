#!/usr/bin/env python3
"""
Alert intervallo - avvisa su Telegram quando una partita chiude il primo tempo
sullo 0-0 ma con statistiche molto offensive. Dati da Sofascore.

Gira su GitHub Actions ogni 5 minuti. Lo stato (iscritti, partite gia' avvisate)
viene conservato tra un giro e l'altro nella cartella "stato".
"""
import html
import json
import os
import time
from pathlib import Path

try:
    from curl_cffi import requests as http  # si presenta come un vero browser
    SOFA_EXTRA = {"impersonate": "chrome"}
except ImportError:  # piano B
    import requests as http
    SOFA_EXTRA = {}

# ------------------------------------------------------------------ SOGLIE --
TIRI_TOTALI = 10          # almeno una squadra, primo tempo
TIRI_IN_PORTA = 6         # stessa squadra, primo tempo
ANGOLI_TOTALI = 5         # somma delle due squadre, primo tempo
OCCASIONI_PER_SQUADRA = 1 # grandi occasioni: almeno 1 a testa (quindi almeno 2)
MINUTO_MASSIMO = 55       # se lo vediamo in ritardo, oltre questo minuto non avvisa

# Massime serie europee seguite (numero Sofascore -> nome mostrato)
CAMPIONATI = {
    23: "Serie A",
    17: "Premier League",
    8: "LaLiga",
    35: "Bundesliga",
    34: "Ligue 1",
    37: "Eredivisie",
    238: "Liga Portugal",
    38: "Pro League (Belgio)",
    52: "Süper Lig",
    36: "Premiership (Scozia)",
}

MAX_ISCRITTI = 5
# ---------------------------------------------------------------------------

TOKEN = os.environ["TELEGRAM_TOKEN"]
TG = f"https://api.telegram.org/bot{TOKEN}"
MANUALE = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
FILE_STATO = Path("stato/stato.json")

SOFA_BASI = ["https://api.sofascore.com/api/v1", "https://www.sofascore.com/api/v1"]
SOFA_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}
if not SOFA_EXTRA:
    SOFA_HEADERS["User-Agent"] = (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
    )

STATISTICHE = {  # nostro nome -> (chiave Sofascore, nome inglese Sofascore)
    "tiri": ("totalShotsOnGoal", "total shots"),
    "in_porta": ("shotsOnGoal", "shots on target"),
    "angoli": ("cornerKicks", "corner kicks"),
    "occasioni": ("bigChanceCreated", "big chances"),
}


# ---------------------------------------------------------------- STATO -----
def carica_stato():
    FILE_STATO.parent.mkdir(exist_ok=True)
    stato = {}
    if FILE_STATO.exists():
        try:
            stato = json.loads(FILE_STATO.read_text())
        except ValueError:
            stato = {}
    stato.setdefault("iscritti", {})
    stato.setdefault("offset", 0)
    stato.setdefault("avvisate", {})
    stato.setdefault("scartate", {})
    stato.setdefault("errori_di_fila", 0)
    return stato


def salva_stato(stato):
    FILE_STATO.write_text(json.dumps(stato, ensure_ascii=False, indent=1))


# ------------------------------------------------------------- TELEGRAM -----
def invia(chat_id, testo):
    try:
        http.post(
            TG + "/sendMessage",
            json={
                "chat_id": chat_id,
                "text": testo,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
    except Exception as e:
        print("Invio Telegram fallito:", e)


def invia_a_tutti(stato, testo):
    for chat_id in stato["iscritti"]:
        invia(chat_id, testo)


def testo_condizioni():
    return (
        "Ti avviso quando a fine primo tempo:\n"
        "• il risultato è 0-0\n"
        f"• una squadra ha almeno {TIRI_TOTALI} tiri, di cui {TIRI_IN_PORTA} in porta\n"
        f"• almeno {ANGOLI_TOTALI} calci d'angolo in totale\n"
        f"• almeno {OCCASIONI_PER_SQUADRA} grande occasione per squadra\n\n"
        "Campionati: " + ", ".join(CAMPIONATI.values()) + "\n\n"
        "Scrivi /stop per non ricevere più avvisi."
    )


def aggiorna_iscritti(stato):
    """Legge chi ha scritto al bot: /start iscrive, /stop disiscrive."""
    risposta = http.get(
        TG + "/getUpdates", params={"offset": stato["offset"], "timeout": 0}, timeout=20
    ).json()
    if not risposta.get("ok"):
        raise RuntimeError("Telegram non risponde: controlla il codice del bot")
    for agg in risposta.get("result", []):
        stato["offset"] = agg["update_id"] + 1
        msg = agg.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            continue
        chat_id = str(chat["id"])
        nome = chat.get("first_name") or "amico"
        testo = (msg.get("text") or "").strip().lower()
        if testo.startswith("/stop"):
            if stato["iscritti"].pop(chat_id, None) is not None:
                invia(chat_id, "🔕 Avvisi disattivati. Premi /start per riattivarli.")
        elif chat_id not in stato["iscritti"]:
            if len(stato["iscritti"]) >= MAX_ISCRITTI:
                invia(chat_id, "Questo bot è privato.")
                continue
            stato["iscritti"][chat_id] = nome
            invia(chat_id, f"✅ Ciao {html.escape(nome)}! Sei iscritto agli avvisi.\n\n" + testo_condizioni())


# ------------------------------------------------------------ SOFASCORE -----
def sofa(percorso):
    ultimo_errore = None
    for base in SOFA_BASI:
        try:
            r = http.get(base + percorso, headers=SOFA_HEADERS, timeout=20, **SOFA_EXTRA)
            if r.status_code == 200:
                return r.json()
            ultimo_errore = f"risposta {r.status_code}"
        except Exception as e:
            ultimo_errore = str(e)
    raise RuntimeError(ultimo_errore)


def numero(voce, lato):
    valore = voce.get(lato + "Value")
    if valore is None:
        try:
            valore = float(str(voce.get(lato, "0")).split()[0].replace("%", ""))
        except (ValueError, IndexError):
            valore = 0
    return int(valore)


def statistiche_primo_tempo(id_partita):
    dati = sofa(f"/event/{id_partita}/statistics")
    periodo = next((p for p in dati.get("statistics", []) if p.get("period") == "1ST"), None)
    if not periodo:
        return None
    valori = {}
    for gruppo in periodo.get("groups", []):
        for voce in gruppo.get("statisticsItems", []):
            chiave = voce.get("key", "")
            nome = (voce.get("name") or "").strip().lower()
            for nostro, (k, n) in STATISTICHE.items():
                if nostro not in valori and (chiave == k or nome == n):
                    valori[nostro] = (numero(voce, "home"), numero(voce, "away"))
    return valori


def rispetta_condizioni(s):
    if not s or any(k not in s for k in STATISTICHE):
        return False
    (tc, to), (pc, po), (ac, ao), (oc, oo) = s["tiri"], s["in_porta"], s["angoli"], s["occasioni"]
    una_squadra_spinge = (tc >= TIRI_TOTALI and pc >= TIRI_IN_PORTA) or (
        to >= TIRI_TOTALI and po >= TIRI_IN_PORTA
    )
    return (
        una_squadra_spinge
        and ac + ao >= ANGOLI_TOTALI
        and oc >= OCCASIONI_PER_SQUADRA
        and oo >= OCCASIONI_PER_SQUADRA
    )


def gol(evento):
    return (
        (evento.get("homeScore") or {}).get("current", 0) or 0,
        (evento.get("awayScore") or {}).get("current", 0) or 0,
    )


def nome_partita(evento):
    return f"{evento['homeTeam']['name']} - {evento['awayTeam']['name']}"


def link(evento):
    if evento.get("slug") and evento.get("customId"):
        return f"https://www.sofascore.com/{evento['slug']}/{evento['customId']}#id:{evento['id']}"
    return "https://www.sofascore.com"


def controlla_partite(stato):
    eventi = sofa("/sport/football/events/live").get("events", [])
    seguite = [
        e for e in eventi
        if ((e.get("tournament") or {}).get("uniqueTournament") or {}).get("id") in CAMPIONATI
    ]
    ora = time.time()
    for e in seguite:
        eid = str(e["id"])
        if eid in stato["avvisate"] or eid in stato["scartate"]:
            continue
        codice = (e.get("status") or {}).get("code")
        if codice not in (31, 7):  # 31 = intervallo, 7 = secondo tempo
            continue
        if gol(e) != (0, 0):
            stato["scartate"][eid] = ora
            continue
        momento = "Intervallo"
        if codice == 7:
            inizio = (e.get("time") or {}).get("currentPeriodStartTimestamp")
            minuto = 45 + int((ora - inizio) / 60) if inizio else 46
            if minuto > MINUTO_MASSIMO:
                stato["scartate"][eid] = ora
                continue
            momento = f"2º tempo, circa {minuto}'"
        try:
            s = statistiche_primo_tempo(eid)
        except RuntimeError:
            continue  # riprova al prossimo giro
        if s is None:
            continue  # statistiche non ancora pronte
        if not rispetta_condizioni(s):
            stato["scartate"][eid] = ora
            continue
        campionato = CAMPIONATI[e["tournament"]["uniqueTournament"]["id"]]
        testo = (
            f"⚽ <b>{html.escape(nome_partita(e))}</b>\n"
            f"🏆 {campionato} · 🕐 {momento} · 0-0\n\n"
            "<b>Primo tempo</b>\n"
            f"🎯 Tiri: {s['tiri'][0]} - {s['tiri'][1]} "
            f"(in porta {s['in_porta'][0]} - {s['in_porta'][1]})\n"
            f"🚩 Angoli: {s['angoli'][0]} - {s['angoli'][1]}\n"
            f"💥 Grandi occasioni: {s['occasioni'][0]} - {s['occasioni'][1]}\n\n"
            f'<a href="{link(e)}">Apri su Sofascore</a>'
        )
        invia_a_tutti(stato, testo)
        stato["avvisate"][eid] = {"nome": nome_partita(e), "campionato": campionato, "quando": ora}
    return len(eventi), len(seguite)


def controlla_risultati(stato):
    """Per le partite avvisate, a fine gara manda com'è andata."""
    ora = time.time()
    for eid, info in list(stato["avvisate"].items()):
        if ora - info["quando"] > 5 * 3600:
            del stato["avvisate"][eid]
            continue
        try:
            e = sofa(f"/event/{eid}")["event"]
        except (RuntimeError, KeyError):
            continue
        if (e.get("status") or {}).get("type") != "finished":
            continue
        casa, ospiti = gol(e)
        esito = "✅ Gol nel secondo tempo" if casa + ospiti > 0 else "❌ Nessun gol, finita 0-0"
        invia_a_tutti(
            stato,
            f"📋 <b>Finale: {html.escape(info['nome'])} {casa}-{ospiti}</b>\n{esito}",
        )
        del stato["avvisate"][eid]


def pulisci(stato):
    limite = time.time() - 24 * 3600
    stato["scartate"] = {k: v for k, v in stato["scartate"].items() if v > limite}


# ---------------------------------------------------------------- AVVIO -----
def main():
    stato = carica_stato()
    try:
        aggiorna_iscritti(stato)
        try:
            totali, seguite = controlla_partite(stato)
            controlla_risultati(stato)
            esito_sofa = f"✅ ok — partite in corso ora: {totali}, nei campionati seguiti: {seguite}"
            if stato["errori_di_fila"] >= 6:
                invia_a_tutti(stato, "✅ Collegamento ai dati tornato a funzionare.")
            stato["errori_di_fila"] = 0
        except RuntimeError as err:
            esito_sofa = f"❌ bloccato ({err})"
            stato["errori_di_fila"] += 1
            if stato["errori_di_fila"] == 6:  # circa mezz'ora di fila
                invia_a_tutti(stato, "⚠️ Da mezz'ora non riesco a leggere i dati delle partite. Gli avvisi sono in pausa.")
        if MANUALE:
            nomi = ", ".join(stato["iscritti"].values()) or "nessuno"
            invia_a_tutti(
                stato,
                "🔧 <b>Prova di collegamento</b>\n"
                "Telegram: ✅ ok\n"
                f"Sofascore: {esito_sofa}\n"
                f"Iscritti: {html.escape(nomi)}",
            )
        print("Sofascore:", esito_sofa, "| iscritti:", len(stato["iscritti"]))
        pulisci(stato)
    finally:
        salva_stato(stato)


if __name__ == "__main__":
    main()
