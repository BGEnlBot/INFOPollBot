"""
Bot Telegram per sondaggi - versione WEBHOOK per Vercel, con interfaccia
di creazione basata su TELEGRAM WEB APP (Mini App), non più su messaggi
di testo a step rigidi.

COME FUNZIONA L'INTERFACCIA:
  - /newpoll manda in chat privata un bottone "📊 Crea sondaggio"
  - Il bottone apre una pagina HTML DENTRO Telegram (Web App), con lo
    stesso stile del tema dell'utente (chiaro/scuro), dove si scrive
    liberamente domanda + opzioni + impostazioni, tutto su una sola
    schermata modificabile in qualsiasi ordine (come il sondaggio
    nativo di Telegram)
  - Nella domanda si possono inserire link cliccabili con il pulsante
    "🔗 Inserisci link" (testo + URL, anche Google Maps)
  - Alla pressione di "Crea", la Web App manda i dati al bot
    (Telegram.WebApp.sendData) che pubblica il sondaggio nel canale

ARCHITETTURA:
  - Nessun polling: Telegram chiama /api/webhook ad ogni evento
  - Nessun file locale: tutti i dati vivono su Upstash Redis
  - La pagina della Web App è servita da /api/pollform (stesso progetto)
  - Il promemoria settimanale (sondaggi ricorrenti) è innescato da un
    Vercel Cron Job che chiama /api/cron una volta al giorno

VARIABILI D'AMBIENTE (Vercel -> Project Settings -> Environment Variables):
    TELEGRAM_TOKEN              token del bot, da @BotFather
    CHANNEL_ID                  ID del CANALE (es. "@nomecanale" o -100...)
    UPSTASH_REDIS_REST_URL      da dashboard Upstash
    UPSTASH_REDIS_REST_TOKEN    da dashboard Upstash
    CRON_SECRET                 generato automaticamente da Vercel

DOPO IL DEPLOY, imposta il webhook (una volta sola):
    https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<tuo-progetto>.vercel.app/api/webhook

NOTA IMPORTANTE: la Web App di Telegram richiede HTTPS con certificato
valido (Vercel lo fornisce sempre di default, nessuna azione richiesta)
e funziona solo se aperta da una chat PRIVATA con il bot (non nei canali).
"""

import hashlib
import hmac
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request
from upstash_redis import Redis

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
CRON_SECRET = os.environ.get("CRON_SECRET", "")
API = f"https://api.telegram.org/bot{TOKEN}"

WEEKDAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

# L'orario del promemoria settimanale è fissato nel cron di vercel.json
# ("0 8 * * *" = 08:00 UTC). Se cambi quell'orario, aggiorna anche questo.
CRON_HOUR_UTC = 8


def cron_time_rome_str() -> str:
    """Converte l'orario del promemoria da UTC a Roma, gestendo automaticamente
    il passaggio ora solare/legale (nessun calcolo manuale del fuso)."""
    utc_dt = datetime.now(timezone.utc).replace(
        hour=CRON_HOUR_UTC, minute=0, second=0, microsecond=0)
    rome_dt = utc_dt.astimezone(ZoneInfo("Europe/Rome"))
    return rome_dt.strftime("%H:%M")
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")

redis = Redis.from_env()
app = Flask(__name__)


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """
    Rete di sicurezza: se qualcosa va storto in modo imprevisto, risponde
    con un errore JSON leggibile invece di una pagina HTML di errore
    (che altrimenti il JavaScript della Web App non riesce a interpretare,
    mostrando solo un generico "errore di connessione").
    """
    import traceback
    print(f"[ERRORE NON GESTITO] {traceback.format_exc()}", flush=True)
    return {"ok": False, "message": f"Errore interno del bot: {e}"}, 500


# ================= HELPER TESTO / HTML =================

def esc(s) -> str:
    """Escape per testo semplice dentro un messaggio Telegram in parse_mode HTML."""
    return html.escape(str(s), quote=False)


def esc_attr(s) -> str:
    """Escape per un valore dentro un attributo HTML (es. href)."""
    return html.escape(str(s), quote=True)


def format_rich_text(raw: str) -> str:
    """
    Converte i link scritti come [testo](url) (inseriti dalla Web App)
    in veri hyperlink HTML Telegram, mantenendo il resto del testo
    correttamente sfuggito. Usato solo per la domanda del sondaggio.
    """
    out, last = [], 0
    for m in LINK_PATTERN.finditer(raw):
        out.append(esc(raw[last:m.start()]))
        label, url = m.group(1), m.group(2)
        out.append(f'<a href="{esc_attr(url)}">{esc(label)}</a>')
        last = m.end()
    out.append(esc(raw[last:]))
    return "".join(out)


# ================= HELPER TELEGRAM =================

def tg(method: str, **params):
    params = {k: v for k, v in params.items() if v is not None}
    r = requests.post(f"{API}/{method}", json=params, timeout=8)
    result = r.json()
    if not result.get("ok"):
        print(f"[TELEGRAM API ERROR] {method} -> {result}", flush=True)
    return result


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    return tg("sendMessage", chat_id=chat_id, text=text,
              reply_markup=reply_markup, parse_mode=parse_mode)


def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    return tg("editMessageText", chat_id=chat_id, message_id=message_id,
               text=text, reply_markup=reply_markup, parse_mode=parse_mode)


def edit_markup(chat_id, message_id, reply_markup):
    return tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
               reply_markup=reply_markup)


def answer_callback(callback_id, text=None, alert=False):
    tg("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)


def get_bot_username() -> str:
    cached = redis.get("bot_username")
    if cached:
        return cached
    res = tg("getMe")
    username = res.get("result", {}).get("username", "")
    if username:
        redis.set("bot_username", username)
    return username


def is_channel_admin(user_id: int) -> bool:
    res = tg("getChatAdministrators", chat_id=CHANNEL_ID)
    if not res.get("ok"):
        return False
    return any(a["user"]["id"] == user_id for a in res["result"])


def validate_init_data(init_data: str, max_age_seconds: int = 86400):
    """
    Verifica che i dati arrivati dalla Web App siano autenticamente
    firmati da Telegram (algoritmo ufficiale HMAC-SHA256), per evitare
    che chiunque possa chiamare /api/submitpoll direttamente fingendosi
    un altro utente. Ritorna il dict "user" se valido, altrimenti None.
    """
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if auth_date and (time.time() - int(auth_date)) > max_age_seconds:
        return None

    try:
        return json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError):
        return None


# ================= HELPER REDIS =================

def get_json(key, default=None):
    val = redis.get(key)
    return json.loads(val) if val else default


def set_json(key, value):
    redis.set(key, json.dumps(value))


def next_id(counter_key: str) -> str:
    return str(redis.incr(counter_key))


def parse_item_id(id_str: str):
    """Interpreta un ID: 'R5' -> ('template', '5'), '5' -> ('poll', '5')."""
    if id_str.startswith("R"):
        return "template", id_str[1:]
    return "poll", id_str


def log_event(poll_id, user, event_type, old_choice, new_choice):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_id": user["id"],
        "name": user.get("first_name", "") + ((" " + user["last_name"]) if user.get("last_name") else ""),
        "event_type": event_type,
        "old_choice": old_choice,
        "new_choice": new_choice,
    }
    redis.rpush(f"log:{poll_id}", json.dumps(entry))


def remember_admin_chat(user_id: int, chat_id: int):
    if is_channel_admin(user_id):
        redis.sadd("admin_chats", f"{user_id}:{chat_id}")


# ================= COSTRUZIONE SONDAGGIO =================

def build_text(poll: dict) -> str:
    """Genera il testo del messaggio in HTML (Telegram parse_mode=HTML)."""
    icon = "🧠" if poll["quiz"] else "📊"
    lines = [f"{icon} {format_rich_text(poll['question'])}", ""]
    for i, opt in enumerate(poll["options"]):
        voters = [v["name"] for v in poll["votes"].values() if i in v["choices"]]
        mark = "✅ " if poll["quiz"] and i == poll["correct_index"] and poll["closed"] else ""
        lines.append(f"▫️ {mark}{esc(opt)} — {len(voters)} voti")
        if voters and not poll["anonymous"]:
            lines.append("   " + esc(", ".join(voters)))
    lines.append("")
    tags = ["anonimo" if poll["anonymous"] else "voti pubblici",
            "risposte multiple" if poll["multiple"] else "risposta singola"]
    if poll["quiz"]:
        tags.append("quiz")
    if not poll["allow_revote"]:
        tags.append("voto non modificabile")
    lines.append("(" + " • ".join(tags) + ")")
    if poll["closed"]:
        lines.append("")
        lines.append("🔒 Sondaggio chiuso.")
        if poll["quiz"] and poll.get("explanation"):
            lines.append(f"ℹ️ {esc(poll['explanation'])}")
    return "\n".join(lines)


def build_keyboard(poll: dict):
    if poll["closed"]:
        return None
    rows = [
        [{"text": opt, "callback_data": f"vote|{poll['id']}|{i}"}]
        for i, opt in enumerate(poll["options"])
    ]
    if poll.get("allow_suggestions") and len(poll["options"]) < 10:
        username = get_bot_username()
        if username:
            rows.append([{"text": "➕ Proponi un'opzione", "url": f"https://t.me/{username}?start=addopt_{poll['id']}"}])
    if poll.get("allow_external_share"):
        username = get_bot_username()
        if username:
            # Telegram NON permette bottoni "switch_inline_query" nei post
            # di un canale: si passa quindi da un bottone url che apre la
            # chat privata col bot, dove il vero bottone di condivisione
            # (permesso solo lì) viene mostrato subito dopo.
            rows.append([{"text": "↗️ Condividi e vota altrove", "url": f"https://t.me/{username}?start=share_{poll['id']}"}])
    return {"inline_keyboard": rows}


def sync_poll(poll: dict):
    """
    Aggiorna TUTTE le copie conosciute del sondaggio: quella nel canale
    e le eventuali copie condivise altrove tramite modalità inline.
    Se la copia nel canale risulta cancellata, chiude il sondaggio
    ovunque (unico modo per accorgersi di una cancellazione, dato che
    Telegram non notifica i bot quando un messaggio viene eliminato).
    """
    poll_id = poll["id"]
    res = tg("editMessageText", chat_id=CHANNEL_ID, message_id=poll["message_id"],
             text=build_text(poll), reply_markup=build_keyboard(poll), parse_mode="HTML")

    if not res.get("ok") and "not found" in res.get("description", "").lower():
        poll["closed"] = True
        set_json(f"poll:{poll_id}", poll)

    still_valid = []
    for imid in poll.get("inline_message_ids", []):
        r = tg("editMessageText", inline_message_id=imid,
               text=build_text(poll), reply_markup=build_keyboard(poll), parse_mode="HTML")
        if r.get("ok") or "not found" not in r.get("description", "").lower():
            still_valid.append(imid)
    if still_valid != poll.get("inline_message_ids", []):
        poll["inline_message_ids"] = still_valid
        set_json(f"poll:{poll_id}", poll)


def publish_poll(fields: dict):
    """Ritorna (poll, None) se la pubblicazione riesce, oppure (None, errore) se Telegram la rifiuta."""
    poll_id = next_id("poll_counter")
    poll = dict(fields)
    poll.update({"id": poll_id, "closed": False, "votes": {}, "inline_message_ids": []})
    res = send_message(CHANNEL_ID, build_text(poll), build_keyboard(poll), parse_mode="HTML")
    if not res.get("ok"):
        return None, res.get("description", "Errore sconosciuto restituito da Telegram.")
    poll["message_id"] = res["result"]["message_id"]
    set_json(f"poll:{poll_id}", poll)
    redis.sadd("polls_index", poll_id)
    return poll, None


def save_template(fields: dict, weekday: int, creator_chat_id: int) -> str:
    tpl_id = next_id("template_counter")
    tpl = dict(fields)
    tpl.update({"id": tpl_id, "weekday": weekday, "creator_chat_id": creator_chat_id})
    set_json(f"template:{tpl_id}", tpl)
    redis.sadd("templates_index", tpl_id)
    redis.sadd(f"templates_by_day:{weekday}", tpl_id)
    return tpl_id


def close_poll_only(poll_id: str, poll: dict):
    """Chiude il sondaggio ma lo mantiene nell'elenco (riapribile)."""
    poll["closed"] = True
    set_json(f"poll:{poll_id}", poll)
    sync_poll(poll)


def reopen_poll(poll_id: str, poll: dict):
    poll["closed"] = False
    set_json(f"poll:{poll_id}", poll)
    sync_poll(poll)


def delete_poll_completely(poll_id: str, poll: dict):
    """Chiude il sondaggio, aggiorna un'ultima volta tutte le sue copie,
    poi lo rimuove del tutto (log dei voti inclusi, persi volutamente)."""
    poll["closed"] = True
    final_text = build_text(poll) + "\n\n🗑️ Sondaggio eliminato dall'amministratore."
    tg("editMessageText", chat_id=CHANNEL_ID, message_id=poll["message_id"],
       text=final_text, parse_mode="HTML")
    for imid in poll.get("inline_message_ids", []):
        tg("editMessageText", inline_message_id=imid, text=final_text, parse_mode="HTML")
    redis.delete(f"poll:{poll_id}")
    redis.srem("polls_index", poll_id)
    redis.delete(f"log:{poll_id}")


def delete_template_completely(tpl_id: str, tpl: dict):
    redis.delete(f"template:{tpl_id}")
    redis.srem("templates_index", tpl_id)
    redis.srem(f"templates_by_day:{tpl['weekday']}", tpl_id)


POLL_FIELD_KEYS = ("question", "options", "multiple", "anonymous", "quiz",
                    "allow_revote", "correct_index", "explanation",
                    "allow_suggestions", "allow_external_share")


def extract_fields(payload: dict) -> dict:
    return {k: payload.get(k) for k in POLL_FIELD_KEYS}


def open_edit_webapp(chat_id, id_str: str):
    """Manda il bottone per aprire la Web App in modalità modifica,
    sia per un sondaggio pubblicato sia per un modello ricorrente."""
    kind, raw = parse_item_id(id_str)
    if kind == "template":
        tpl = get_json(f"template:{raw}")
        if not tpl:
            send_message(chat_id, "Sondaggio ricorrente non trovato.")
            return
        label = f"R{raw}"
        form_url = request.host_url.rstrip("/") + f"/api/pollform?edit={label}"
        keyboard = {"inline_keyboard": [[{"text": "✏️ Modifica sondaggio ricorrente", "web_app": {"url": form_url}}]]}
        send_message(chat_id, f"Tocca il bottone per modificare il sondaggio ricorrente #{label}:", keyboard)
        return

    poll = get_json(f"poll:{raw}")
    if not poll:
        send_message(chat_id, "Sondaggio non trovato.")
        return
    if poll["closed"]:
        send_message(chat_id, "Questo sondaggio è chiuso e non può più essere modificato.")
        return
    form_url = request.host_url.rstrip("/") + f"/api/pollform?edit={raw}"
    keyboard = {"inline_keyboard": [[{"text": "✏️ Modifica sondaggio", "web_app": {"url": form_url}}]]}
    send_message(chat_id, f"Tocca il bottone per modificare il sondaggio #{raw}:", keyboard)


# ================= COMANDI (chat privata) =================

def cmd_start(chat_id, user_id, args):
    remember_admin_chat(user_id, chat_id)

    if args and args[0].startswith("addopt_"):
        poll_id = args[0][len("addopt_"):]
        poll = get_json(f"poll:{poll_id}")
        if not poll or poll["closed"]:
            send_message(chat_id, "Questo sondaggio non è più disponibile.")
            return
        if not poll.get("allow_suggestions"):
            send_message(chat_id, "Questo sondaggio non accetta opzioni proposte dagli utenti.")
            return
        if len(poll["options"]) >= 10:
            send_message(chat_id, "Questo sondaggio ha già raggiunto il numero massimo di opzioni.")
            return
        redis.set(f"awaiting_option:{user_id}", poll_id)
        send_message(chat_id, f"Scrivi il testo della nuova opzione da proporre per:\n\n"
                               f"«{poll['question']}»")
        return

    if args and args[0].startswith("share_"):
        poll_id = args[0][len("share_"):]
        poll = get_json(f"poll:{poll_id}")
        if not poll or poll["closed"] or not poll.get("allow_external_share"):
            send_message(chat_id, "Questo sondaggio non è più disponibile per la condivisione.")
            return
        keyboard = {"inline_keyboard": [[
            {"text": "↗️ Scegli dove condividerlo", "switch_inline_query": poll_id}
        ]]}
        send_message(chat_id, f"Tocca il bottone per scegliere in quale chat condividere:\n\n"
                               f"«{poll['question']}»", keyboard)
        return

    send_message(chat_id, "Ciao! Se sei amministratore del canale puoi usare:\n"
                           "/newpoll - crea un sondaggio\n"
                           "/polls - gestisci sondaggi e ricorrenti (modifica, chiudi, riapri, elimina, log)",
                 reply_markup={"remove_keyboard": True})


def handle_option_suggestion(chat_id, user_id, text):
    poll_id = redis.get(f"awaiting_option:{user_id}")
    if not poll_id:
        return False

    redis.delete(f"awaiting_option:{user_id}")
    option = text.strip()
    if not option:
        send_message(chat_id, "Opzione vuota, non è stata aggiunta.")
        return True

    poll = get_json(f"poll:{poll_id}")
    if not poll or poll["closed"] or not poll.get("allow_suggestions"):
        send_message(chat_id, "Questo sondaggio non è più disponibile per nuove proposte.")
        return True
    if len(poll["options"]) >= 10:
        send_message(chat_id, "Il sondaggio ha già raggiunto il numero massimo di opzioni.")
        return True
    if any(o.strip().lower() == option.lower() for o in poll["options"]):
        send_message(chat_id, "Questa opzione è già presente nel sondaggio.")
        return True

    poll["options"].append(option)
    set_json(f"poll:{poll_id}", poll)
    sync_poll(poll)
    send_message(chat_id, f"✅ Opzione aggiunta al sondaggio: «{option}»")
    return True


def cmd_newpoll(chat_id, user_id):
    if not is_channel_admin(user_id):
        send_message(chat_id, "Comando riservato agli amministratori del canale.")
        return
    remember_admin_chat(user_id, chat_id)
    form_url = request.host_url.rstrip("/") + "/api/pollform"
    keyboard = {"inline_keyboard": [[{"text": "📊 Crea sondaggio", "web_app": {"url": form_url}}]]}
    send_message(chat_id, "Tocca il bottone per aprire la creazione del sondaggio:", keyboard)


def build_log_text(poll_id: str) -> str:
    raw = redis.lrange(f"log:{poll_id}", 0, -1) or []
    if not raw:
        return "Nessun voto registrato per questo sondaggio."
    events = [json.loads(r) for r in raw]
    per_user_events, per_user_name = {}, {}
    for e in events:
        uid = e["user_id"]
        per_user_name[uid] = e["name"]
        per_user_events.setdefault(uid, []).append(e)
    lines = ["📋 Riepilogo voti", ""]
    for uid in sorted(per_user_events, key=lambda u: per_user_name[u].lower()):
        lines.append(f"👤 {per_user_name[uid]}")
        for i, e in enumerate(per_user_events[uid]):
            label = "Primo voto" if i == 0 else "Cambio voto"
            lines.append(f"{label}: {e['new_choice']}")
        lines.append("")
    return "\n".join(lines).strip()


def cmd_polls(chat_id, user_id):
    if not is_channel_admin(user_id):
        send_message(chat_id, "Comando riservato agli amministratori del canale.")
        return

    poll_ids = sorted(redis.smembers("polls_index") or [], key=int)
    tpl_ids = sorted(redis.smembers("templates_index") or [], key=int)

    if not poll_ids and not tpl_ids:
        send_message(chat_id, "Nessun sondaggio creato ancora.")
        return

    for pid in poll_ids:
        p = get_json(f"poll:{pid}")
        if not p:
            continue
        if p["closed"]:
            text = f"🔒 {p['question']}\n(chiuso)"
            keyboard = {"inline_keyboard": [
                [{"text": "🗑️ Elimina", "callback_data": f"mgmt|delete|{pid}"}],
                [{"text": "🔓 Riapri", "callback_data": f"mgmt|reopen|{pid}"}],
                [{"text": "📋 Log", "callback_data": f"mgmt|log|{pid}"}],
            ]}
        else:
            text = f"📊 {p['question']}"
            keyboard = {"inline_keyboard": [
                [{"text": "✏️ Modifica", "callback_data": f"mgmt|edit|{pid}"}],
                [{"text": "🔒 Chiudi", "callback_data": f"mgmt|close|{pid}"}],
                [{"text": "📋 Log", "callback_data": f"mgmt|log|{pid}"}],
            ]}
        send_message(chat_id, text, keyboard)

    for tid in tpl_ids:
        t = get_json(f"template:{tid}")
        if not t:
            continue
        rid = f"R{tid}"
        text = (f"📅 Sondaggio Scadenziario (ogni {WEEKDAY_NAMES[t['weekday']]} "
                f"alle {cron_time_rome_str()})\n{t['question']}")
        keyboard = {"inline_keyboard": [
            [{"text": "✏️ Modifica", "callback_data": f"mgmt|edit|{rid}"}],
            [{"text": "🗑️ Elimina", "callback_data": f"mgmt|delete|{rid}"}],
        ]}
        send_message(chat_id, text, keyboard)


# ================= RICEZIONE DATI DALLA WEB APP =================

def handle_poll_edit(chat_id, user_id, payload):
    poll_id = payload["edit_poll_id"]
    poll = get_json(f"poll:{poll_id}")
    if not poll:
        send_message(chat_id, "Sondaggio non trovato.")
        return False, "Sondaggio non trovato."
    if poll["closed"]:
        send_message(chat_id, "Questo sondaggio è ormai chiuso e non può più essere modificato.")
        return False, "Questo sondaggio è ormai chiuso."

    question = (payload.get("question") or "").strip()
    new_options = [o.strip() for o in payload.get("options", []) if o.strip()]
    if not question:
        send_message(chat_id, "La domanda non può essere vuota. Modifica annullata.")
        return False, "La domanda non può essere vuota."
    if not (2 <= len(new_options) <= 10):
        send_message(chat_id, "Servono tra 2 e 10 opzioni. Modifica annullata.")
        return False, "Servono tra 2 e 10 opzioni."

    # Rimappa i voti esistenti: le opzioni il cui testo non è cambiato
    # mantengono i voti; le opzioni rimosse o rinominate perdono i loro
    # voti (in modo pulito, senza corrompere i voti sulle altre opzioni).
    old_index_by_text = {}
    for i, t in enumerate(poll["options"]):
        old_index_by_text.setdefault(t, i)
    old_to_new = {}
    remaining = dict(old_index_by_text)
    for new_i, t in enumerate(new_options):
        if t in remaining:
            old_to_new[remaining.pop(t)] = new_i

    new_votes = {}
    for uid, entry in poll["votes"].items():
        remapped = [old_to_new[c] for c in entry["choices"] if c in old_to_new]
        if remapped:
            new_votes[uid] = {"name": entry["name"], "choices": remapped}

    multiple = bool(payload.get("multiple"))
    if not multiple:
        # Se le risposte multiple vengono disattivate, ogni voto deve
        # mantenere una sola scelta (la prima), altrimenti resterebbe
        # temporaneamente incoerente fino al prossimo voto dell'utente.
        for entry in new_votes.values():
            entry["choices"] = entry["choices"][:1]

    poll["question"] = question
    poll["options"] = new_options
    poll["votes"] = new_votes
    poll["multiple"] = multiple
    poll["anonymous"] = bool(payload.get("anonymous"))
    poll["quiz"] = bool(payload.get("quiz"))
    poll["allow_revote"] = bool(payload.get("allow_revote", True))
    poll["correct_index"] = payload.get("correct_index")
    poll["explanation"] = (payload.get("explanation") or "").strip()
    poll["allow_suggestions"] = bool(payload.get("allow_suggestions"))
    poll["allow_external_share"] = bool(payload.get("allow_external_share"))

    set_json(f"poll:{poll_id}", poll)
    sync_poll(poll)
    send_message(chat_id, "✅ Sondaggio aggiornato nel canale.")
    return True, "Sondaggio aggiornato."


def handle_template_edit(chat_id, user_id, payload):
    tpl_id = payload["edit_poll_id"][1:]  # toglie il prefisso "R"
    tpl = get_json(f"template:{tpl_id}")
    if not tpl:
        send_message(chat_id, "Sondaggio ricorrente non trovato.")
        return False, "Sondaggio ricorrente non trovato."

    question = (payload.get("question") or "").strip()
    new_options = [o.strip() for o in payload.get("options", []) if o.strip()]
    if not question:
        send_message(chat_id, "La domanda non può essere vuota. Modifica annullata.")
        return False, "La domanda non può essere vuota."
    if not (2 <= len(new_options) <= 10):
        send_message(chat_id, "Servono tra 2 e 10 opzioni. Modifica annullata.")
        return False, "Servono tra 2 e 10 opzioni."

    weekday = payload.get("weekday")
    if weekday is None or not (0 <= int(weekday) <= 6):
        send_message(chat_id, "Giorno della settimana non valido. Modifica annullata.")
        return False, "Giorno della settimana non valido."
    weekday = int(weekday)

    old_weekday = tpl["weekday"]
    tpl["question"] = question
    tpl["options"] = new_options
    tpl["multiple"] = bool(payload.get("multiple"))
    tpl["anonymous"] = bool(payload.get("anonymous"))
    tpl["quiz"] = bool(payload.get("quiz"))
    tpl["allow_revote"] = bool(payload.get("allow_revote", True))
    tpl["correct_index"] = payload.get("correct_index")
    tpl["explanation"] = (payload.get("explanation") or "").strip()
    tpl["allow_suggestions"] = bool(payload.get("allow_suggestions"))
    tpl["allow_external_share"] = bool(payload.get("allow_external_share"))
    tpl["weekday"] = weekday

    set_json(f"template:{tpl_id}", tpl)
    if weekday != old_weekday:
        redis.srem(f"templates_by_day:{old_weekday}", tpl_id)
        redis.sadd(f"templates_by_day:{weekday}", tpl_id)

    send_message(chat_id, f"✅ Sondaggio ricorrente aggiornato (ogni {WEEKDAY_NAMES[weekday]}).")
    return True, "Sondaggio ricorrente aggiornato."


def handle_web_app_data(chat_id, user_id, payload):
    if not is_channel_admin(user_id):
        return False, "Comando riservato agli amministratori del canale."

    edit_id = payload.get("edit_poll_id")
    if edit_id:
        if edit_id.startswith("R"):
            return handle_template_edit(chat_id, user_id, payload)
        return handle_poll_edit(chat_id, user_id, payload)

    question = (payload.get("question") or "").strip()
    options = [o.strip() for o in payload.get("options", []) if o.strip()]

    if not question:
        return False, "La domanda non può essere vuota."
    if not (2 <= len(options) <= 10):
        return False, "Servono tra 2 e 10 opzioni."

    fields = {
        "question": question,
        "options": options,
        "multiple": bool(payload.get("multiple")),
        "anonymous": bool(payload.get("anonymous")),
        "quiz": bool(payload.get("quiz")),
        "allow_revote": bool(payload.get("allow_revote", True)),
        "correct_index": payload.get("correct_index"),
        "explanation": (payload.get("explanation") or "").strip(),
        "allow_suggestions": bool(payload.get("allow_suggestions")),
        "allow_external_share": bool(payload.get("allow_external_share")),
    }

    if payload.get("recurrent"):
        weekday = payload.get("weekday")
        if weekday is None or not (0 <= int(weekday) <= 6):
            return False, "Giorno della settimana non valido."
        tpl_id = save_template(fields, int(weekday), chat_id)
        send_message(chat_id, f"✅ Sondaggio ricorrente salvato (#{tpl_id}).\n"
                               f"Ogni {WEEKDAY_NAMES[int(weekday)]} riceverai un promemoria "
                               f"con un bottone per pubblicarlo nel canale.")
        return True, "Sondaggio ricorrente salvato."
    else:
        poll, err = publish_poll(fields)
        if not poll:
            send_message(chat_id, f"❌ Telegram ha rifiutato la pubblicazione: {err}")
            return False, f"Telegram ha rifiutato la pubblicazione: {err}"
        send_message(chat_id, "✅ Sondaggio pubblicato nel canale.")
        return True, "Sondaggio pubblicato."


# ================= CALLBACK: VOTO E RICORRENTI =================

def handle_recur_callback(callback_id, user_id, chat_id, message_id, tpl_id):
    if not is_channel_admin(user_id):
        answer_callback(callback_id, "Riservato agli amministratori del canale.", alert=True)
        return
    tpl = get_json(f"template:{tpl_id}")
    if not tpl:
        answer_callback(callback_id, "Modello non più disponibile.", alert=True)
        return
    poll, err = publish_poll(extract_fields(tpl))
    if not poll:
        edit_message(chat_id, message_id, f"❌ Telegram ha rifiutato la pubblicazione: {err}")
        answer_callback(callback_id, "Errore nella pubblicazione.", alert=True)
        return
    edit_message(chat_id, message_id, f"✅ Sondaggio pubblicato nel canale (dal modello #R{tpl_id}).")
    answer_callback(callback_id)


def handle_mgmt_callback(callback_id, user_id, chat_id, message_id, action, item_id):
    if not is_channel_admin(user_id):
        answer_callback(callback_id, "Riservato agli amministratori del canale.", alert=True)
        return

    kind, raw = parse_item_id(item_id)

    if kind == "template":
        tpl = get_json(f"template:{raw}")
        if not tpl:
            answer_callback(callback_id, "Modello non trovato.", alert=True)
            return
        if action == "edit":
            open_edit_webapp(chat_id, item_id)
            answer_callback(callback_id)
        elif action == "delete":
            delete_template_completely(raw, tpl)
            edit_message(chat_id, message_id, "🗑️ Sondaggio ricorrente eliminato.")
            answer_callback(callback_id, "Eliminato.")
        else:
            answer_callback(callback_id, "Azione non disponibile per un sondaggio ricorrente.", alert=True)
        return

    poll = get_json(f"poll:{raw}")
    if not poll:
        answer_callback(callback_id, "Sondaggio non trovato.", alert=True)
        return

    if action == "edit":
        open_edit_webapp(chat_id, item_id)
        answer_callback(callback_id)

    elif action == "log":
        send_message(chat_id, build_log_text(raw))
        answer_callback(callback_id)

    elif action == "close":
        close_poll_only(raw, poll)
        edit_message(chat_id, message_id, f"🔒 {poll['question']}\n(chiuso)", {"inline_keyboard": [
            [{"text": "🗑️ Elimina", "callback_data": f"mgmt|delete|{raw}"}],
            [{"text": "🔓 Riapri", "callback_data": f"mgmt|reopen|{raw}"}],
            [{"text": "📋 Log", "callback_data": f"mgmt|log|{raw}"}],
        ]})
        answer_callback(callback_id, "Sondaggio chiuso.")

    elif action == "reopen":
        reopen_poll(raw, poll)
        edit_message(chat_id, message_id, f"📊 {poll['question']}", {"inline_keyboard": [
            [{"text": "✏️ Modifica", "callback_data": f"mgmt|edit|{raw}"}],
            [{"text": "🔒 Chiudi", "callback_data": f"mgmt|close|{raw}"}],
            [{"text": "📋 Log", "callback_data": f"mgmt|log|{raw}"}],
        ]})
        answer_callback(callback_id, "Sondaggio riaperto.")

    elif action == "delete":
        delete_poll_completely(raw, poll)
        edit_message(chat_id, message_id, "🗑️ Sondaggio eliminato.")
        answer_callback(callback_id, "Eliminato.")


def handle_vote_callback(callback_id, user, poll_id, idx):
    poll = get_json(f"poll:{poll_id}")
    if not poll or poll["closed"]:
        answer_callback(callback_id, "Sondaggio non più attivo.")
        return

    uid = str(user["id"])
    name = user.get("first_name", "") + ((" " + user["last_name"]) if user.get("last_name") else "")
    entry = poll["votes"].get(uid, {"name": name, "choices": []})
    had_voted_before = bool(entry["choices"])

    if not poll["allow_revote"] and had_voted_before:
        answer_callback(callback_id, "Il voto è definitivo per questo sondaggio.")
        return

    old_names = [poll["options"][i] for i in entry["choices"]]

    if poll["multiple"]:
        if idx in entry["choices"]:
            entry["choices"].remove(idx)
        else:
            entry["choices"].append(idx)
    else:
        entry["choices"] = [idx]

    entry["name"] = name
    poll["votes"][uid] = entry
    set_json(f"poll:{poll_id}", poll)

    new_names = [poll["options"][i] for i in entry["choices"]]
    log_event(poll_id, user, "cambio_voto" if had_voted_before else "voto",
              ", ".join(old_names), ", ".join(new_names) or "(nessuna)")

    sync_poll(poll)
    answer_callback(callback_id, f"Voto registrato: {', '.join(new_names) or 'nessuna scelta'}")


# ================= ENTRY POINT WEBHOOK =================

def plain_preview(raw: str, limit: int = 60) -> str:
    """Testo semplice (senza marcatori di link) per l'anteprima nella modalità inline."""
    text = LINK_PATTERN.sub(lambda m: m.group(1), raw)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def handle_inline_query(iq: dict):
    query_id = iq["id"]
    query_text = (iq.get("query") or "").strip()
    poll = get_json(f"poll:{query_text}") if query_text else None

    if not poll or poll.get("closed") or not poll.get("allow_external_share"):
        tg("answerInlineQuery", inline_query_id=query_id, results=[])
        return

    result = {
        "type": "article",
        "id": poll["id"],
        "title": plain_preview(poll["question"]) or "Sondaggio",
        "description": "Tocca per condividere questo sondaggio qui: resta sincronizzato con il canale",
        "input_message_content": {
            "message_text": build_text(poll),
            "parse_mode": "HTML",
        },
        "reply_markup": build_keyboard(poll),
    }
    tg("answerInlineQuery", inline_query_id=query_id, results=[result], cache_time=0)


def handle_chosen_inline_result(cir: dict):
    poll_id = cir.get("result_id")
    inline_message_id = cir.get("inline_message_id")
    if not poll_id or not inline_message_id:
        return
    poll = get_json(f"poll:{poll_id}")
    if not poll:
        return
    ids = poll.get("inline_message_ids", [])
    if inline_message_id not in ids:
        ids.append(inline_message_id)
        poll["inline_message_ids"] = ids
        set_json(f"poll:{poll_id}", poll)


def ensure_commands_registered():
    """Registra una volta sola il menu comandi di Telegram (icona accanto
    al campo di scrittura), così l'utente ha un elenco delle funzioni
    disponibili senza doverle ricordare a memoria."""
    if redis.get("commands_registered"):
        return
    tg("setMyCommands", commands=[
        {"command": "newpoll", "description": "📊 Crea un sondaggio"},
        {"command": "polls", "description": "🗂 Gestisci i sondaggi"},
    ])
    redis.set("commands_registered", "1")


@app.route("/api/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    print(f"[UPDATE RICEVUTO] {json.dumps(update)}", flush=True)
    ensure_commands_registered()

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]

        text = msg.get("text", "")
        if text.startswith("/start"):
            cmd_start(chat_id, user_id, text.split()[1:])
        elif text.startswith("/newpoll") or text.startswith("/newrecurrent"):
            cmd_newpoll(chat_id, user_id)
        elif text.startswith("/polls"):
            cmd_polls(chat_id, user_id)
        elif not text.startswith("/"):
            handle_option_suggestion(chat_id, user_id, text)

    elif "inline_query" in update:
        handle_inline_query(update["inline_query"])

    elif "chosen_inline_result" in update:
        handle_chosen_inline_result(update["chosen_inline_result"])

    elif "callback_query" in update:
        cq = update["callback_query"]
        callback_id = cq["id"]
        user = cq["from"]
        data = cq.get("data", "")
        # cq["message"] è assente quando il bottone appartiene a una copia
        # condivisa in modalità inline (in quel caso c'è "inline_message_id"
        # invece); handle_vote_callback funziona in entrambi i casi perché
        # aggiorna il sondaggio tramite sync_poll(), non tramite chat/message.
        msg_ref = cq.get("message")

        if data.startswith("recur|") and msg_ref:
            handle_recur_callback(callback_id, user["id"], msg_ref["chat"]["id"],
                                   msg_ref["message_id"], data.split("|")[1])
        elif data.startswith("mgmt|") and msg_ref:
            _, action, item_id = data.split("|")
            handle_mgmt_callback(callback_id, user["id"], msg_ref["chat"]["id"],
                                  msg_ref["message_id"], action, item_id)
        elif data.startswith("vote|"):
            _, poll_id, idx = data.split("|")
            handle_vote_callback(callback_id, user, poll_id, int(idx))

    return {"ok": True}


@app.route("/api/webhook", methods=["GET"])
def health():
    return {"status": "il bot è online"}


# ================= WEB APP (interfaccia di creazione) =================

POLLFORM_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Nuovo sondaggio</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: #efeff4; --card: #ffffff; --text: #000000; --hint: #8e8e93;
    --link: #007aff; --button: #007aff; --button-text: #ffffff;
    --separator: #e3e3e8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding-bottom: 90px;
  }
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 16px; position: sticky; top: 0; background: var(--bg); z-index: 10;
  }
  .topbar h1 { font-size: 17px; margin: 0; }
  .create-btn {
    background: var(--button); color: var(--button-text); border: none;
    border-radius: 14px; padding: 9px 18px; font-size: 15px; font-weight: 600;
  }
  .create-btn:disabled { opacity: 0.4; }
  .card {
    background: var(--card); border-radius: 14px; margin: 10px 16px; overflow: hidden;
  }
  .section-label {
    color: var(--link); font-size: 13px; font-weight: 600; text-transform: uppercase;
    padding: 12px 16px 6px;
  }
  .row {
    padding: 12px 16px; border-bottom: 1px solid var(--separator);
    display: flex; align-items: center; gap: 10px;
  }
  .row:last-child { border-bottom: none; }
  input[type=text], textarea {
    border: none; outline: none; font-size: 16px; width: 100%; background: transparent;
    color: var(--text); font-family: inherit; resize: none;
  }
  textarea { min-height: 44px; }
  ::placeholder { color: var(--hint); }
  .opt-row { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-bottom: 1px solid var(--separator); }
  .opt-remove { color: #ff3b30; font-size: 20px; line-height: 1; cursor: pointer; padding: 4px; }
  .opt-correct { width: 20px; height: 20px; flex-shrink: 0; }
  .add-opt { padding: 12px 16px; color: var(--link); font-size: 16px; display: flex; align-items: center; gap: 10px; cursor: pointer; }
  .hint { color: var(--hint); font-size: 13px; padding: 4px 16px 0; }
  .setting-row { padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--separator); gap: 12px; }
  .setting-row:last-child { border-bottom: none; }
  .setting-text b { display: block; font-size: 16px; }
  .setting-text span { display: block; font-size: 13px; color: var(--hint); margin-top: 2px; }
  .switch { position: relative; width: 46px; height: 28px; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; inset: 0; background: #d1d1d6; border-radius: 28px; transition: .2s; cursor: pointer; }
  .slider::before { content: ""; position: absolute; width: 24px; height: 24px; left: 2px; top: 2px; background: white; border-radius: 50%; transition: .2s; box-shadow: 0 1px 3px rgba(0,0,0,.3); }
  input:checked + .slider { background: #34c759; }
  input:checked + .slider::before { transform: translateX(18px); }
  .link-btn { color: var(--link); font-size: 14px; padding: 8px 16px; cursor: pointer; }
  .link-panel { padding: 10px 16px; display: none; gap: 8px; flex-direction: column; border-top: 1px solid var(--separator); }
  .link-panel.open { display: flex; }
  .link-panel input { border: 1px solid var(--separator); border-radius: 8px; padding: 8px 10px; }
  .link-panel button { background: var(--button); color: var(--button-text); border: none; border-radius: 8px; padding: 8px; font-size: 14px; }
  select {
    border: none; background: transparent; font-size: 16px; color: var(--text);
    font-family: inherit; width: 100%; outline: none;
  }
</style>
</head>
<body>

<div class="topbar">
  <h1>Nuovo sondaggio</h1>
  <button class="create-btn" id="createBtn" disabled>Crea</button>
</div>

<div class="card">
  <div class="row">
    <textarea id="question" placeholder="Fai una domanda" rows="2"></textarea>
  </div>
  <div class="link-btn" id="linkToggle">🔗 Inserisci link (sito, Google Maps...)</div>
  <div class="link-panel" id="linkPanel">
    <input type="text" id="linkLabel" placeholder="Testo del link (es. Apri su Maps)">
    <input type="text" id="linkUrl" placeholder="https://...">
    <button id="linkInsert">Inserisci nella domanda</button>
  </div>
</div>

<div class="section-label">Opzioni</div>
<div class="card" id="optionsCard"></div>
<div class="hint" id="optHint"></div>

<div class="section-label">Impostazioni</div>
<div class="card">
  <div class="setting-row">
    <div class="setting-text"><b>Mostra chi ha votato</b><span>Il nome dei votanti è visibile a tutti</span></div>
    <label class="switch"><input type="checkbox" id="showVoters" checked><span class="slider"></span></label>
  </div>
  <div class="setting-row">
    <div class="setting-text"><b>Consenti risposte multiple</b><span>I votanti possono scegliere più opzioni</span></div>
    <label class="switch"><input type="checkbox" id="multiple"><span class="slider"></span></label>
  </div>
  <div class="setting-row">
    <div class="setting-text"><b>Consenti di cambiare voto</b><span>I votanti possono cambiare la scelta</span></div>
    <label class="switch"><input type="checkbox" id="allowRevote" checked><span class="slider"></span></label>
  </div>
  <div class="setting-row">
    <div class="setting-text"><b>Modalità quiz</b><span>Segna una risposta corretta tra le opzioni</span></div>
    <label class="switch"><input type="checkbox" id="quiz"><span class="slider"></span></label>
  </div>
  <div class="setting-row">
    <div class="setting-text"><b>Consenti di inserire opzioni</b><span>Gli utenti possono proporre nuove opzioni scrivendo al bot</span></div>
    <label class="switch"><input type="checkbox" id="allowSuggestions"><span class="slider"></span></label>
  </div>
  <div class="setting-row">
    <div class="setting-text"><b>Condivisione esterna</b><span>Può essere condiviso e votato anche fuori dal canale, restando sincronizzato</span></div>
    <label class="switch"><input type="checkbox" id="allowExternalShare"><span class="slider"></span></label>
  </div>
  <div class="row" id="explanationRow" style="display:none">
    <textarea id="explanation" placeholder="Spiegazione mostrata a chiusura (opzionale)" rows="2"></textarea>
  </div>
</div>

<div class="card" id="recurrentCard">
  <div class="setting-row" id="recurrentToggleRow">
    <div class="setting-text"><b>Sondaggio ricorrente</b><span>Ricevi un promemoria settimanale per ripubblicarlo</span></div>
    <label class="switch"><input type="checkbox" id="recurrent"><span class="slider"></span></label>
  </div>
  <div class="row" id="weekdayRow" style="display:none">
    <select id="weekday">
      <option value="0">Ogni Lunedì</option>
      <option value="1">Ogni Martedì</option>
      <option value="2">Ogni Mercoledì</option>
      <option value="3">Ogni Giovedì</option>
      <option value="4">Ogni Venerdì</option>
      <option value="5">Ogni Sabato</option>
      <option value="6">Ogni Domenica</option>
    </select>
  </div>
</div>

<script>
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// Applica i colori del tema Telegram, se disponibili
const tp = tg.themeParams || {};
const root = document.documentElement.style;
if (tp.bg_color) root.setProperty('--bg', tp.bg_color);
if (tp.text_color) root.setProperty('--text', tp.text_color);
if (tp.hint_color) root.setProperty('--hint', tp.hint_color);
if (tp.link_color) root.setProperty('--link', tp.link_color);
if (tp.button_color) root.setProperty('--button', tp.button_color);
if (tp.button_text_color) root.setProperty('--button-text', tp.button_text_color);
if (tp.secondary_bg_color) root.setProperty('--card', tp.secondary_bg_color);

const optionsCard = document.getElementById('optionsCard');
const optHint = document.getElementById('optHint');
const MAX_OPT = 10;
let optCount = 0;

function addOption(value) {
  if (optCount >= MAX_OPT) return;
  optCount++;
  const row = document.createElement('div');
  row.className = 'opt-row';
  row.innerHTML = `
    <input type="radio" name="correctOpt" class="opt-correct" style="display:none">
    <input type="text" class="opt-input" placeholder="Opzione" value="${value ? value.replace(/"/g, '&quot;') : ''}">
    <span class="opt-remove">✕</span>`;
  row.querySelector('.opt-remove').onclick = () => { row.remove(); optCount--; refresh(); };
  row.querySelector('.opt-input').addEventListener('input', refresh);
  optionsCard.appendChild(row);
  refresh();
}

document.getElementById('optionsCard').insertAdjacentHTML('afterend', '');
const addRow = document.createElement('div');
addRow.className = 'add-opt';
addRow.innerHTML = '➕ Aggiungi un\\'opzione...';
addRow.onclick = () => addOption('');

function mountAddRow() {
  optionsCard.parentNode.insertBefore(addRow, optionsCard.nextSibling);
}
mountAddRow();

addOption(''); addOption('');

// ---- Modalità modifica: se la Web App è aperta con ?edit=ID, precompila ----
const urlParams = new URLSearchParams(location.search);
const editId = urlParams.get('edit');
const isTemplateEdit = !!(editId && editId.startsWith('R'));

if (editId) {
  document.getElementById('createBtn').textContent = 'Salva';

  // rimuove le due righe opzione vuote create di default, verranno ripopolate
  optionsCard.innerHTML = '';
  optCount = 0;

  fetch(`/api/pollformdata?id=${encodeURIComponent(editId)}`)
    .then(r => r.json())
    .then(data => {
      if (!data.ok) {
        alert('Sondaggio non trovato o non più modificabile.');
        tg.close();
        return;
      }
      const p = data.poll;

      if (data.is_template) {
        document.querySelector('.topbar h1').textContent = 'Modifica sondaggio ricorrente';
        document.getElementById('recurrentToggleRow').style.display = 'none';
        document.getElementById('weekdayRow').style.display = 'flex';
        document.getElementById('weekday').value = String(p.weekday);
      } else {
        document.querySelector('.topbar h1').textContent = 'Modifica sondaggio';
        document.getElementById('recurrentCard').style.display = 'none';
      }

      document.getElementById('question').value = p.question || '';
      (p.options || []).forEach(o => addOption(o));
      document.getElementById('showVoters').checked = !p.anonymous;
      document.getElementById('multiple').checked = !!p.multiple;
      document.getElementById('allowRevote').checked = !!p.allow_revote;
      document.getElementById('allowSuggestions').checked = !!p.allow_suggestions;
      document.getElementById('allowExternalShare').checked = !!p.allow_external_share;
      document.getElementById('explanation').value = p.explanation || '';
      quizToggle.checked = !!p.quiz;
      quizToggle.dispatchEvent(new Event('change'));
      if (p.quiz && p.correct_index !== null && p.correct_index !== undefined) {
        const radios = document.querySelectorAll('.opt-correct');
        if (radios[p.correct_index]) radios[p.correct_index].checked = true;
      }
      validate();
    })
    .catch(() => {
      alert('Errore nel caricamento del sondaggio da modificare.');
      tg.close();
    });
}

function refresh() {
  const remaining = MAX_OPT - optCount;
  optHint.textContent = remaining > 0 ? `Puoi aggiungere altre ${remaining} opzioni.` : 'Hai raggiunto il massimo di opzioni.';
  addRow.style.display = optCount >= MAX_OPT ? 'none' : 'flex';
  validate();
}

// Toggle modalità quiz: mostra selettore risposta corretta + spiegazione
const quizToggle = document.getElementById('quiz');
quizToggle.addEventListener('change', () => {
  document.getElementById('explanationRow').style.display = quizToggle.checked ? 'flex' : 'none';
  document.querySelectorAll('.opt-correct').forEach(el => el.style.display = quizToggle.checked ? 'inline-block' : 'none');
});

// Toggle sondaggio ricorrente: mostra selettore giorno
const recurrentToggle = document.getElementById('recurrent');
recurrentToggle.addEventListener('change', () => {
  document.getElementById('weekdayRow').style.display = recurrentToggle.checked ? 'flex' : 'none';
});

// Pannello inserimento link
const linkToggle = document.getElementById('linkToggle');
const linkPanel = document.getElementById('linkPanel');
linkToggle.onclick = () => linkPanel.classList.toggle('open');
document.getElementById('linkInsert').onclick = () => {
  const label = document.getElementById('linkLabel').value.trim();
  const url = document.getElementById('linkUrl').value.trim();
  if (!label || !url) return;
  const q = document.getElementById('question');
  const marker = `[${label}](${url})`;
  const pos = (typeof q.selectionStart === 'number') ? q.selectionStart : q.value.length;
  q.value = q.value.slice(0, pos) + marker + q.value.slice(pos);
  const newPos = pos + marker.length;
  q.focus();
  q.setSelectionRange(newPos, newPos);
  document.getElementById('linkLabel').value = '';
  document.getElementById('linkUrl').value = '';
  linkPanel.classList.remove('open');
  validate();
};

document.getElementById('question').addEventListener('input', validate);

function validate() {
  const question = document.getElementById('question').value.trim();
  const opts = [...document.querySelectorAll('.opt-input')].map(i => i.value.trim()).filter(Boolean);
  const ok = question.length > 0 && opts.length >= 2;
  document.getElementById('createBtn').disabled = !ok;
}

document.getElementById('createBtn').addEventListener('click', () => {
  const btn = document.getElementById('createBtn');
  btn.disabled = true;

  const question = document.getElementById('question').value.trim();
  const optNodes = [...document.querySelectorAll('.opt-row')];
  const options = optNodes.map(r => r.querySelector('.opt-input').value.trim()).filter(Boolean);

  let correct_index = null;
  if (quizToggle.checked) {
    const idx = optNodes.findIndex(r => r.querySelector('.opt-correct').checked);
    correct_index = idx >= 0 ? idx : null;
  }

  const payload = {
    edit_poll_id: editId || null,
    question: question,
    options: options,
    multiple: document.getElementById('multiple').checked,
    anonymous: !document.getElementById('showVoters').checked,
    allow_revote: document.getElementById('allowRevote').checked,
    quiz: quizToggle.checked,
    correct_index: correct_index,
    explanation: document.getElementById('explanation').value.trim(),
    allow_suggestions: document.getElementById('allowSuggestions').checked,
    allow_external_share: document.getElementById('allowExternalShare').checked,
    recurrent: (!editId) ? recurrentToggle.checked : false,
    weekday: isTemplateEdit
      ? parseInt(document.getElementById('weekday').value, 10)
      : ((!editId && recurrentToggle.checked) ? parseInt(document.getElementById('weekday').value, 10) : null),
  };

  fetch('/api/submitpoll', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initData: tg.initData, payload: payload }),
  })
    .then(r => r.json())
    .then(res => {
      if (res.ok) {
        tg.close();
      } else {
        alert(res.message || 'Si è verificato un errore, riprova.');
        btn.disabled = false;
      }
    })
    .catch(() => {
      alert('Errore di connessione, riprova.');
      btn.disabled = false;
    });
});

validate();
</script>
</body>
</html>
"""


@app.route("/api/submitpoll", methods=["POST"])
def submitpoll():
    body = request.get_json(force=True, silent=True) or {}
    init_data = body.get("initData", "")
    payload = body.get("payload", {})

    user = validate_init_data(init_data)
    if not user:
        return {"ok": False, "message": "Sessione non valida, riapri la schermata da Telegram."}, 401

    user_id = user.get("id")
    chat_id = user_id  # in chat privata, il chat_id coincide con lo user_id
    ok, message = handle_web_app_data(chat_id, user_id, payload)
    return {"ok": ok, "message": message}


@app.route("/api/pollform", methods=["GET"])
def pollform():
    return POLLFORM_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/pollformdata", methods=["GET"])
def pollformdata():
    id_str = request.args.get("id", "")
    kind, raw = parse_item_id(id_str)

    if kind == "template":
        tpl = get_json(f"template:{raw}")
        if not tpl:
            return {"ok": False}, 404
        return {"ok": True, "is_template": True, "poll": {
            "question": tpl["question"],
            "options": tpl["options"],
            "multiple": tpl["multiple"],
            "anonymous": tpl["anonymous"],
            "quiz": tpl["quiz"],
            "allow_revote": tpl["allow_revote"],
            "correct_index": tpl["correct_index"],
            "explanation": tpl.get("explanation", ""),
            "allow_suggestions": tpl.get("allow_suggestions", False),
            "allow_external_share": tpl.get("allow_external_share", False),
            "weekday": tpl["weekday"],
        }}

    poll = get_json(f"poll:{raw}")
    if not poll or poll.get("closed"):
        return {"ok": False}, 404
    return {"ok": True, "is_template": False, "poll": {
        "question": poll["question"],
        "options": poll["options"],
        "multiple": poll["multiple"],
        "anonymous": poll["anonymous"],
        "quiz": poll["quiz"],
        "allow_revote": poll["allow_revote"],
        "correct_index": poll["correct_index"],
        "explanation": poll.get("explanation", ""),
        "allow_suggestions": poll.get("allow_suggestions", False),
        "allow_external_share": poll.get("allow_external_share", False),
    }}


# ================= ENDPOINT CRON (promemoria settimanale) =================

@app.route("/api/cron", methods=["GET"])
def cron():
    auth = request.headers.get("Authorization", "")
    if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
        return {"ok": False, "error": "unauthorized"}, 401

    # 1) Chiude i sondaggi il cui messaggio nel canale risulta cancellato.
    # Telegram non avvisa mai un bot quando un messaggio viene eliminato,
    # quindi l'unico modo per accorgersene è un controllo periodico come
    # questo (oltre alla verifica che avviene già ad ogni voto/modifica).
    closed_now = 0
    for pid in (redis.smembers("polls_index") or []):
        poll = get_json(f"poll:{pid}")
        if poll and not poll["closed"]:
            sync_poll(poll)
            if poll["closed"]:
                closed_now += 1

    # 2) Promemoria settimanali per i sondaggi ricorrenti.
    today = datetime.now(timezone.utc).weekday()
    tpl_ids = redis.smembers(f"templates_by_day:{today}") or []
    sent = 0
    if tpl_ids:
        admin_chats = redis.smembers("admin_chats") or []
        for tid in tpl_ids:
            tpl = get_json(f"template:{tid}")
            if not tpl:
                continue
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Crea sondaggio ora", "callback_data": f"recur|{tid}"}
            ]]}
            text = f"📅 Promemoria: oggi è il giorno per pubblicare il sondaggio ricorrente #{tid}:\n\n{tpl['question']}"
            for entry in admin_chats:
                uid_str, chat_id_str = entry.split(":")
                if is_channel_admin(int(uid_str)):
                    send_message(int(chat_id_str), text, keyboard)
                    sent += 1

    return {"ok": True, "reminders_sent": sent, "polls_closed_deleted": closed_now}
