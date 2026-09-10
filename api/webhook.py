"""
Bot Telegram per sondaggi - versione WEBHOOK per Vercel, con:
  - Log cronologico di voti e cambi voto (/log ID)
  - Sondaggi RICORRENTI: crei un modello una volta sola, scegli il
    giorno della settimana, e ogni settimana in quel giorno ricevi un
    promemoria con un bottone "Crea sondaggio ora" che lo pubblica nel
    canale identico a come l'hai impostato la prima volta.

ARCHITETTURA:
  - Nessun polling: Telegram chiama /api/webhook ad ogni evento
  - Nessun file locale: tutti i dati vivono su Upstash Redis
  - Il promemoria settimanale è innescato da un Vercel Cron Job che
    chiama /api/cron una volta al giorno (il piano gratuito di Vercel
    permette al massimo un cron al giorno, va benissimo per questo caso)

VARIABILI D'AMBIENTE (Vercel -> Project Settings -> Environment Variables):
    TELEGRAM_TOKEN              token del bot, da @BotFather
    CHANNEL_ID                  es. "@nomecanale" oppure ID numerico
    UPSTASH_REDIS_REST_URL      da dashboard Upstash
    UPSTASH_REDIS_REST_TOKEN    da dashboard Upstash
    CRON_SECRET                 generato automaticamente da Vercel quando
                                 aggiungi un cron in vercel.json

DOPO IL DEPLOY, imposta il webhook (una volta sola), visitando nel browser:
    https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<tuo-progetto>.vercel.app/api/webhook
"""

import json
import os
from datetime import datetime, timezone

import requests
from flask import Flask, request
from upstash_redis import Redis

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
CRON_SECRET = os.environ.get("CRON_SECRET", "")
API = f"https://api.telegram.org/bot{TOKEN}"

WEEKDAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

redis = Redis.from_env()
app = Flask(__name__)


# ================= HELPER TELEGRAM =================

def tg(method: str, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=8)
    return r.json()


def send_message(chat_id, text, reply_markup=None):
    return tg("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)


def edit_message(chat_id, message_id, text, reply_markup=None):
    return tg("editMessageText", chat_id=chat_id, message_id=message_id,
               text=text, reply_markup=reply_markup)


def edit_markup(chat_id, message_id, reply_markup):
    return tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
               reply_markup=reply_markup)


def answer_callback(callback_id, text=None, alert=False):
    tg("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)


def is_channel_admin(user_id: int) -> bool:
    res = tg("getChatAdministrators", chat_id=CHANNEL_ID)
    if not res.get("ok"):
        return False
    return any(a["user"]["id"] == user_id for a in res["result"])


# ================= HELPER REDIS =================

def get_json(key, default=None):
    val = redis.get(key)
    return json.loads(val) if val else default


def set_json(key, value):
    redis.set(key, json.dumps(value))


def next_id(counter_key: str) -> str:
    return str(redis.incr(counter_key))


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
    """Tiene traccia di quali chat private usare per inviare i promemoria settimanali."""
    if is_channel_admin(user_id):
        redis.sadd("admin_chats", f"{user_id}:{chat_id}")


# ================= COSTRUZIONE SONDAGGIO =================

def build_text(poll: dict) -> str:
    icon = "🧠" if poll["quiz"] else "📊"
    lines = [f"{icon} {poll['question']}", ""]
    for i, opt in enumerate(poll["options"]):
        voters = [v["name"] for v in poll["votes"].values() if i in v["choices"]]
        mark = "✅ " if poll["quiz"] and i == poll["correct_index"] and poll["closed"] else ""
        lines.append(f"▫️ {mark}{opt} — {len(voters)} voti")
        if voters and not poll["anonymous"]:
            lines.append("   " + ", ".join(voters))
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
            lines.append(f"ℹ️ {poll['explanation']}")
    return "\n".join(lines)


def build_keyboard(poll: dict):
    if poll["closed"]:
        return None
    return {"inline_keyboard": [
        [{"text": opt, "callback_data": f"vote|{poll['id']}|{i}"}]
        for i, opt in enumerate(poll["options"])
    ]}


def settings_keyboard(draft: dict):
    def flag(label, key):
        return f"✅ {label}" if draft.get(key) else f"⬜ {label}"

    publish_label = "📅 Scegli il giorno del promemoria" if draft.get("mode") == "recurrent" else "🚀 Pubblica nel canale"
    return {"inline_keyboard": [
        [{"text": flag("Risposte multiple", "multiple"), "callback_data": "cfg|multiple"}],
        [{"text": flag("Sondaggio anonimo", "anonymous"), "callback_data": "cfg|anonymous"}],
        [{"text": flag("Modalità quiz", "quiz"), "callback_data": "cfg|quiz"}],
        [{"text": flag("Consenti cambio voto", "allow_revote"), "callback_data": "cfg|allow_revote"}],
        [{"text": publish_label, "callback_data": "cfg|publish"}],
        [{"text": "❌ Annulla", "callback_data": "cfg|cancel"}],
    ]}


def weekday_keyboard():
    rows = [[{"text": name, "callback_data": f"day|{i}"}] for i, name in enumerate(WEEKDAY_NAMES)]
    rows.append([{"text": "❌ Annulla", "callback_data": "cfg|cancel"}])
    return {"inline_keyboard": rows}


def poll_fields_from(draft: dict) -> dict:
    return {
        "question": draft["question"],
        "options": draft["options"],
        "multiple": draft.get("multiple", False),
        "anonymous": draft.get("anonymous", False),
        "quiz": draft.get("quiz", False),
        "allow_revote": draft.get("allow_revote", True),
        "correct_index": draft.get("correct_index"),
        "explanation": draft.get("explanation", ""),
    }


def publish_poll(fields: dict) -> dict:
    poll_id = next_id("poll_counter")
    poll = dict(fields)
    poll.update({"id": poll_id, "closed": False, "votes": {}})
    res = send_message(CHANNEL_ID, build_text(poll), build_keyboard(poll))
    poll["message_id"] = res["result"]["message_id"]
    set_json(f"poll:{poll_id}", poll)
    redis.sadd("polls_index", poll_id)
    return poll


def save_template(draft: dict, weekday: int, creator_chat_id: int) -> str:
    tpl_id = next_id("template_counter")
    tpl = poll_fields_from(draft)
    tpl.update({"id": tpl_id, "weekday": weekday, "creator_chat_id": creator_chat_id})
    set_json(f"template:{tpl_id}", tpl)
    redis.sadd("templates_index", tpl_id)
    redis.sadd(f"templates_by_day:{weekday}", tpl_id)
    return tpl_id


# ================= COMANDI (chat privata) =================

def cmd_start(chat_id, user_id):
    remember_admin_chat(user_id, chat_id)
    send_message(chat_id, "Ciao! Se sei amministratore del canale puoi usare:\n"
                           "/newpoll - crea un sondaggio da pubblicare subito\n"
                           "/newrecurrent - crea un sondaggio ricorrente con promemoria settimanale\n"
                           "/polls - elenco sondaggi pubblicati\n"
                           "/recurrents - elenco modelli ricorrenti\n"
                           "/close ID - chiude un sondaggio\n"
                           "/delrecurrent ID - elimina un modello ricorrente\n"
                           "/log ID - riepilogo voti di un sondaggio")


def cmd_newpoll(chat_id, user_id, mode="immediate"):
    if not is_channel_admin(user_id):
        send_message(chat_id, "Comando riservato agli amministratori del canale.")
        return
    remember_admin_chat(user_id, chat_id)
    set_json(f"draft:{user_id}", {"step": "question", "options": [], "mode": mode})
    intro = "Creiamo un nuovo sondaggio ricorrente." if mode == "recurrent" else "Creiamo un nuovo sondaggio."
    send_message(chat_id, f"{intro}\n\nScrivi la domanda:")


def cmd_polls(chat_id, user_id):
    if not is_channel_admin(user_id):
        send_message(chat_id, "Comando riservato agli amministratori del canale.")
        return
    ids = redis.smembers("polls_index") or []
    if not ids:
        send_message(chat_id, "Nessun sondaggio creato ancora.")
        return
    lines = []
    for pid in sorted(ids, key=int):
        p = get_json(f"poll:{pid}")
        if p:
            stato = "chiuso" if p["closed"] else "attivo"
            lines.append(f"#{pid} [{stato}] {p['question']}")
    send_message(chat_id, "\n".join(lines))


def cmd_recurrents(chat_id, user_id):
    if not is_channel_admin(user_id):
        send_message(chat_id, "Comando riservato agli amministratori del canale.")
        return
    ids = redis.smembers("templates_index") or []
    if not ids:
        send_message(chat_id, "Nessun sondaggio ricorrente impostato.")
        return
    lines = []
    for tid in sorted(ids, key=int):
        t = get_json(f"template:{tid}")
        if t:
            lines.append(f"#{tid} [{WEEKDAY_NAMES[t['weekday']]}] {t['question']}")
    send_message(chat_id, "\n".join(lines))


def cmd_delrecurrent(chat_id, user_id, args):
    if not is_channel_admin(user_id):
        send_message(chat_id, "Comando riservato agli amministratori del canale.")
        return
    if not args:
        send_message(chat_id, "Uso: /delrecurrent ID  (vedi /recurrents per gli ID)")
        return
    tid = args[0]
    t = get_json(f"template:{tid}")
    if not t:
        send_message(chat_id, "Modello ricorrente non trovato.")
        return
    redis.srem("templates_index", tid)
    redis.srem(f"templates_by_day:{t['weekday']}", tid)
    redis.delete(f"template:{tid}")
    send_message(chat_id, f"Modello ricorrente #{tid} eliminato.")


def cmd_close(chat_id, user_id, args):
    if not is_channel_admin(user_id):
        send_message(chat_id, "Comando riservato agli amministratori del canale.")
        return
    if not args:
        send_message(chat_id, "Uso: /close ID  (vedi /polls per gli ID)")
        return
    poll_id = args[0]
    poll = get_json(f"poll:{poll_id}")
    if not poll:
        send_message(chat_id, "Sondaggio non trovato.")
        return
    poll["closed"] = True
    set_json(f"poll:{poll_id}", poll)
    edit_message(CHANNEL_ID, poll["message_id"], build_text(poll), build_keyboard(poll))
    send_message(chat_id, f"Sondaggio #{poll_id} chiuso.")


def cmd_log(chat_id, user_id, args):
    if not is_channel_admin(user_id):
        send_message(chat_id, "Comando riservato agli amministratori del canale.")
        return
    if not args:
        send_message(chat_id, "Uso: /log ID  (vedi /polls per gli ID)")
        return
    poll_id = args[0]
    raw = redis.lrange(f"log:{poll_id}", 0, -1) or []
    if not raw:
        send_message(chat_id, "Nessun voto registrato per questo sondaggio.")
        return
    events = [json.loads(r) for r in raw]
    per_user_events, per_user_name = {}, {}
    for e in events:
        uid = e["user_id"]
        per_user_name[uid] = e["name"]
        per_user_events.setdefault(uid, []).append(e)
    lines = [f"📋 Riepilogo voti — sondaggio #{poll_id}", ""]
    for uid in sorted(per_user_events, key=lambda u: per_user_name[u].lower()):
        lines.append(f"👤 {per_user_name[uid]}")
        for i, e in enumerate(per_user_events[uid]):
            label = "Primo voto" if i == 0 else "Cambio voto"
            lines.append(f"{label}: {e['new_choice']}")
        lines.append("")
    send_message(chat_id, "\n".join(lines).strip())


def cmd_cancel(chat_id, user_id):
    redis.delete(f"draft:{user_id}")
    send_message(chat_id, "Creazione annullata.")


# ================= FLUSSO CREAZIONE (messaggi di testo) =================

def handle_draft_message(chat_id, user_id, text):
    draft = get_json(f"draft:{user_id}")
    if not draft:
        return False

    step = draft["step"]

    if step == "question":
        draft["question"] = text
        draft["step"] = "options"
        set_json(f"draft:{user_id}", draft)
        send_message(chat_id, "Ora invia le opzioni di risposta, UNA PER RIGA "
                               "(minimo 2, massimo 10), tutte in un unico messaggio.")
        return True

    if step == "options":
        options = [line.strip() for line in text.splitlines() if line.strip()]
        if not (2 <= len(options) <= 10):
            send_message(chat_id, "Servono tra 2 e 10 opzioni, una per riga. Riprova.")
            return True
        draft["options"] = options
        draft["step"] = "settings"
        set_json(f"draft:{user_id}", draft)
        opts_text = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        send_message(chat_id, f"Domanda: {draft['question']}\n\nOpzioni:\n{opts_text}\n\n"
                               "Imposta le opzioni del sondaggio:", settings_keyboard(draft))
        return True

    if step == "correct":
        try:
            idx = int(text.strip()) - 1
            assert 0 <= idx < len(draft["options"])
        except (ValueError, AssertionError):
            send_message(chat_id, "Numero non valido, riprova.")
            return True
        draft["correct_index"] = idx
        draft["step"] = "explanation"
        set_json(f"draft:{user_id}", draft)
        send_message(chat_id, "Vuoi aggiungere una spiegazione mostrata a chiusura sondaggio? "
                               "Scrivila, oppure invia /skip per saltare.")
        return True

    if step == "explanation":
        if text.strip() != "/skip":
            draft["explanation"] = text
        finalize_draft(chat_id, user_id, draft)
        return True

    return False


def finalize_draft(chat_id, user_id, draft):
    """Dopo settings/quiz: pubblica subito (immediate) oppure chiede il giorno (recurrent)."""
    if draft.get("mode") == "recurrent":
        draft["step"] = "weekday"
        set_json(f"draft:{user_id}", draft)
        send_message(chat_id, "In quale giorno della settimana vuoi ricevere il promemoria "
                               "per pubblicare questo sondaggio?", weekday_keyboard())
    else:
        publish_poll(poll_fields_from(draft))
        redis.delete(f"draft:{user_id}")
        send_message(chat_id, "✅ Sondaggio pubblicato nel canale.")


def handle_settings_callback(callback_id, user_id, chat_id, message_id, action):
    if not is_channel_admin(user_id):
        answer_callback(callback_id, "Riservato agli amministratori del canale.", alert=True)
        return

    draft = get_json(f"draft:{user_id}")
    if not draft:
        answer_callback(callback_id, "Nessuna creazione in corso.", alert=True)
        return

    if action == "cancel":
        redis.delete(f"draft:{user_id}")
        edit_message(chat_id, message_id, "Creazione annullata.")
        answer_callback(callback_id)
        return

    if action == "publish":
        if draft.get("quiz") and "correct_index" not in draft:
            draft["step"] = "correct"
            set_json(f"draft:{user_id}", draft)
            opts_text = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(draft["options"]))
            edit_message(chat_id, message_id, f"Quale opzione è quella corretta?\n\n{opts_text}\n\n"
                                               "Rispondi con il numero in un messaggio.")
            answer_callback(callback_id)
            return

        if draft.get("mode") == "recurrent":
            draft["step"] = "weekday"
            set_json(f"draft:{user_id}", draft)
            edit_message(chat_id, message_id, "In quale giorno della settimana vuoi ricevere il "
                                               "promemoria per pubblicare questo sondaggio?", weekday_keyboard())
            answer_callback(callback_id)
            return

        publish_poll(poll_fields_from(draft))
        redis.delete(f"draft:{user_id}")
        edit_message(chat_id, message_id, "✅ Sondaggio pubblicato nel canale.")
        answer_callback(callback_id)
        return

    draft[action] = not draft.get(action, False)
    set_json(f"draft:{user_id}", draft)
    edit_markup(chat_id, message_id, settings_keyboard(draft))
    answer_callback(callback_id)


def handle_weekday_callback(callback_id, user_id, chat_id, message_id, weekday):
    if not is_channel_admin(user_id):
        answer_callback(callback_id, "Riservato agli amministratori del canale.", alert=True)
        return
    draft = get_json(f"draft:{user_id}")
    if not draft:
        answer_callback(callback_id, "Nessuna creazione in corso.", alert=True)
        return
    tpl_id = save_template(draft, weekday, chat_id)
    redis.delete(f"draft:{user_id}")
    edit_message(chat_id, message_id,
                 f"✅ Sondaggio ricorrente salvato (#{tpl_id}).\n"
                 f"Ogni {WEEKDAY_NAMES[weekday]} riceverai un promemoria con un bottone "
                 f"per pubblicarlo nel canale.")
    answer_callback(callback_id)


def handle_recur_callback(callback_id, user_id, chat_id, message_id, tpl_id):
    if not is_channel_admin(user_id):
        answer_callback(callback_id, "Riservato agli amministratori del canale.", alert=True)
        return
    tpl = get_json(f"template:{tpl_id}")
    if not tpl:
        answer_callback(callback_id, "Modello non più disponibile.", alert=True)
        return
    fields = {k: tpl[k] for k in
              ("question", "options", "multiple", "anonymous", "quiz", "allow_revote",
               "correct_index", "explanation")}
    publish_poll(fields)
    edit_message(chat_id, message_id, f"✅ Sondaggio pubblicato nel canale (dal modello #{tpl_id}).")
    answer_callback(callback_id)


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

    edit_message(CHANNEL_ID, poll["message_id"], build_text(poll), build_keyboard(poll))
    answer_callback(callback_id, f"Voto registrato: {', '.join(new_names) or 'nessuna scelta'}")


# ================= ENTRY POINT WEBHOOK =================

@app.route("/api/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")

        if text.startswith("/start"):
            cmd_start(chat_id, user_id)
        elif text.startswith("/newrecurrent"):
            cmd_newpoll(chat_id, user_id, mode="recurrent")
        elif text.startswith("/newpoll"):
            cmd_newpoll(chat_id, user_id, mode="immediate")
        elif text.startswith("/polls"):
            cmd_polls(chat_id, user_id)
        elif text.startswith("/recurrents"):
            cmd_recurrents(chat_id, user_id)
        elif text.startswith("/delrecurrent"):
            cmd_delrecurrent(chat_id, user_id, text.split()[1:])
        elif text.startswith("/close"):
            cmd_close(chat_id, user_id, text.split()[1:])
        elif text.startswith("/log"):
            cmd_log(chat_id, user_id, text.split()[1:])
        elif text.startswith("/cancel"):
            cmd_cancel(chat_id, user_id)
        else:
            handle_draft_message(chat_id, user_id, text)

    elif "callback_query" in update:
        cq = update["callback_query"]
        callback_id = cq["id"]
        user = cq["from"]
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]
        data = cq["data"]

        if data.startswith("cfg|"):
            handle_settings_callback(callback_id, user["id"], chat_id, message_id, data.split("|", 1)[1])
        elif data.startswith("day|"):
            handle_weekday_callback(callback_id, user["id"], chat_id, message_id, int(data.split("|")[1]))
        elif data.startswith("recur|"):
            handle_recur_callback(callback_id, user["id"], chat_id, message_id, data.split("|")[1])
        elif data.startswith("vote|"):
            _, poll_id, idx = data.split("|")
            handle_vote_callback(callback_id, user, poll_id, int(idx))

    return {"ok": True}


@app.route("/api/webhook", methods=["GET"])
def health():
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    channel = os.environ.get("CHANNEL_ID", "")
    return {
        "status": "il bot è online",
        "diagnostica_env": {
            "UPSTASH_REDIS_REST_URL_presente": bool(url),
            "UPSTASH_REDIS_REST_URL_inizia_con_https": url.startswith("https://"),
            "UPSTASH_REDIS_REST_URL_lunghezza": len(url),
            "UPSTASH_REDIS_REST_TOKEN_presente": bool(token),
            "UPSTASH_REDIS_REST_TOKEN_lunghezza": len(token),
            "TELEGRAM_TOKEN_presente": bool(tg_token),
            "CHANNEL_ID_presente": bool(channel),
            "CHANNEL_ID_valore": channel,
        },
    }


# ================= ENDPOINT CRON (promemoria settimanale) =================

@app.route("/api/cron", methods=["GET"])
def cron():
    # Verifica che la chiamata arrivi davvero da Vercel Cron, non da chiunque conosca l'URL
    auth = request.headers.get("Authorization", "")
    if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
        return {"ok": False, "error": "unauthorized"}, 401

    today = datetime.now(timezone.utc).weekday()  # 0 = Lunedì ... 6 = Domenica
    tpl_ids = redis.smembers(f"templates_by_day:{today}") or []
    if not tpl_ids:
        return {"ok": True, "reminders_sent": 0}

    admin_chats = redis.smembers("admin_chats") or []
    sent = 0
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

    return {"ok": True, "reminders_sent": sent}
