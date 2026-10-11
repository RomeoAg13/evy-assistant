"""Evy — endpoint du skill Alexa + écran télé, à faire tourner sur la Raspberry Pi.

Routes :
  POST /alexa         -> appelé par Alexa (seule route exposée via Cloudflare Tunnel)
  GET  /              -> page affichée sur la télé (Chromium en mode kiosque)
  GET  /events        -> flux temps réel vers la télé (Server-Sent Events)
  GET  /visual/<id>   -> un visuel généré
  GET  /api/history   -> historique des visuels
  POST /api/show      -> test local sans Alexa : {"question": "..."}
"""
import hmac
import html
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from flask import Flask, Response, abort, jsonify, request, send_from_directory  # noqa: E402

import brain  # noqa: E402
import fiches  # noqa: E402
import tv  # noqa: E402
from alexa_verify import VerificationError, verify_request  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("evy")

BASE = Path(__file__).parent
DATA = Path(os.getenv("DATA_DIR", BASE / "data"))
VISUALS = DATA / "visuals"
VISUALS.mkdir(parents=True, exist_ok=True)
INDEX = DATA / "index.json"

SKILL_ID = os.getenv("ALEXA_SKILL_ID") or None
VERIFY = os.getenv("VERIFY_ALEXA", "true").lower() == "true"

FICHES_PASSWORD = os.getenv("FICHES_PASSWORD", "")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 Mo par envoi

store = fiches.Store(DATA / "fiches")


# --------------------------------------------------------------------------- #
# Diffusion temps réel vers la télé
# --------------------------------------------------------------------------- #
class Bus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.last: dict | None = None

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        if self.last:
            q.put(self.last)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, msg: dict) -> None:
        self.last = msg
        with self._lock:
            for q in list(self._subs):
                q.put(msg)


bus = Bus()


# --------------------------------------------------------------------------- #
# Mémoire de conversation (côté serveur, survit à la fermeture de la session Alexa)
# --------------------------------------------------------------------------- #
CONV_TIMEOUT = float(os.getenv("CONVERSATION_TIMEOUT_MIN", "20")) * 60


class Conversation:
    def __init__(self):
        self._lock = threading.Lock()
        self._turns: list[dict] = []
        self._last = 0.0
        self._screen: dict | None = None

    def history(self) -> list[dict]:
        with self._lock:
            if time.time() - self._last > CONV_TIMEOUT:
                self._turns = []  # trop ancien : nouvelle conversation
            return list(self._turns)

    def add(self, question: str, answer: str, visual: str | None) -> None:
        with self._lock:
            self._turns = (self._turns + [{"q": question, "a": answer, "visual": visual}])[-8:]
            self._last = time.time()

    def reset(self) -> None:
        with self._lock:
            self._turns = []

    @property
    def screen(self) -> dict | None:
        with self._lock:
            return self._screen

    @screen.setter
    def screen(self, value: dict | None) -> None:
        with self._lock:
            self._screen = value


conv = Conversation()
_index_lock = threading.Lock()


def _load_index() -> list[dict]:
    try:
        return json.loads(INDEX.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _add_to_index(entry: dict) -> None:
    with _index_lock:
        items = _load_index()
        items.insert(0, entry)
        INDEX.write_text(json.dumps(items[:200], ensure_ascii=False, indent=1), "utf-8")


_seq_lock = threading.Lock()
_latest_seq = 0


def _new_seq() -> int:
    """Chaque nouvelle question reçoit un numéro : seul le plus récent a le droit d'être affiché."""
    global _latest_seq
    with _seq_lock:
        _latest_seq += 1
        return _latest_seq


def _is_latest(seq: int) -> bool:
    with _seq_lock:
        return seq == _latest_seq


def show_answer(question: str, speech: str) -> None:
    """Réponse sans schéma : on remplace l'ancien visuel par le texte de la réponse."""
    _new_seq()  # un schéma plus ancien encore en cours ne pourra plus s'afficher
    conv.screen = {"title": question, "text": speech}
    bus.publish({"type": "answer", "title": question, "text": speech})


def start_visual(question: str, brief: str) -> None:
    """Allume la télé et génère le visuel en arrière-plan."""
    seq = _new_seq()
    conv.screen = {"title": question, "brief": brief, "pending": True}
    bus.publish({"type": "loading", "title": question})

    def job():
        vid = uuid.uuid4().hex[:12]
        try:
            tv.wake()
            page = brain.make_visual(question, brief)
            (VISUALS / f"{vid}.html").write_text(page, "utf-8")
            entry = {"id": vid, "title": question, "ts": int(time.time())}
            _add_to_index(entry)
            if _is_latest(seq):
                conv.screen = {"title": question, "brief": brief, "notes": brain.screen_notes(page)}
                bus.publish({"type": "visual", **entry})
                log.info("Visuel %s prêt : %s", vid, question)
            else:
                log.info("Visuel %s prêt mais dépassé par une question plus récente "
                         "(gardé dans l'historique) : %s", vid, question)
        except Exception as e:  # noqa: BLE001
            log.exception("Échec du visuel")
            if _is_latest(seq):
                conv.screen = None
                bus.publish({"type": "error", "title": question, "message": str(e)[:200]})

    threading.Thread(target=job, daemon=True).start()


# --------------------------------------------------------------------------- #
# Réponses Alexa
# --------------------------------------------------------------------------- #
def reply(text: str, end: bool, attrs: dict | None = None, reprompt: str | None = None) -> dict:
    ssml = html.escape(text, quote=False)
    out = {
        "version": "1.0",
        "sessionAttributes": attrs or {},
        "response": {
            "outputSpeech": {"type": "SSML", "ssml": f"<speak>{ssml}</speak>"},
            "shouldEndSession": end,
        },
    }
    if not end:
        out["response"]["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt or "Autre chose ?"}
        }
    return out


FOLLOW_UP = "Une autre question ?"


def handle_question(envelope: dict, force_visual: bool) -> dict:
    question = _slot_text(envelope)
    if not question:
        return reply("Je n'ai pas compris ta question. Tu peux répéter ?", end=False)

    try:
        res = brain.answer(question, conv.history(), force_visual=force_visual, screen=conv.screen)
        speech = res["speech"]
        if res["visual"]:
            start_visual(question, res["visual"])
            speech += " Je t'affiche ça sur la télé."
        else:
            show_answer(question, speech)
    except brain.TooSlow:
        # Claude met trop de temps : on bascule tout sur l'écran
        brief = "Réponds à la question de façon visuelle et complète."
        start_visual(question, brief)
        speech = "Ça demande un peu de réflexion, je t'affiche la réponse sur la télé."
        res = {"speech": speech, "visual": brief}

    conv.add(question, res["speech"], res.get("visual"))
    # Le micro reste ouvert : tu peux enchaîner sans redire « Alexa, demande à Evy ».
    return reply(speech, end=False, reprompt=FOLLOW_UP)


# --------------------------------------------------------------------------- #
# Fiches : génération de la banque de questions en arrière-plan
# --------------------------------------------------------------------------- #
def build_bank(fid: str) -> None:
    def job():
        try:
            meta = store.update(fid, status="generating", error=None)
            bank = brain.make_question_bank(meta["title"], meta["subject"], store.text(fid))
            store.set_questions(fid, bank)
            log.info("Fiche %s prête : %d questions", fid, len(store.questions(fid)))
        except KeyError:
            pass  # fiche supprimée entre-temps
        except Exception as e:  # noqa: BLE001
            log.exception("Échec de la génération des questions")
            try:
                store.update(fid, status="error", error=str(e)[:200])
            except KeyError:
                pass

    threading.Thread(target=job, daemon=True).start()


# --------------------------------------------------------------------------- #
# Mode révision
# --------------------------------------------------------------------------- #
QUIZ_TIMEOUT = 30 * 60
_quiz_lock = threading.Lock()
_quiz: dict | None = None


def quiz_active() -> bool:
    with _quiz_lock:
        return bool(_quiz) and time.time() - _quiz["last"] < QUIZ_TIMEOUT


def _next_question(feedback: dict | None = None) -> str:
    """Choisit la question suivante, l'affiche sur la télé et renvoie le texte à dire."""
    with _quiz_lock:
        if len(_quiz["asked"]) >= len(_quiz["questions"]):
            _quiz["asked"] = set()  # tout a été posé : on recommence un tour
        q = fiches.pick_question(_quiz["questions"], store.stats(_quiz["fid"]), _quiz["asked"])
        _quiz["asked"].add(q["id"])
        _quiz["current"] = q
        _quiz["num"] += 1
        _quiz["last"] = time.time()
        event = {"type": "quiz", "title": _quiz["title"], "num": _quiz["num"],
                 "question": q.get("q_ecran") or q["q"], "feedback": feedback}
    _new_seq()
    bus.publish(event)
    return q["q"]


def start_quiz(topic: str | None) -> dict:
    global _quiz
    all_fiches = store.list()
    if not all_fiches:
        return reply("Tu n'as encore déposé aucune fiche. Ajoute-en depuis la page fiches, "
                     "puis redemande-moi.", end=False, reprompt=FOLLOW_UP)
    meta = store.find(topic) if topic else store.latest_ready()
    if not meta:
        ready = [m["title"] for m in all_fiches if m["status"] == "ready"]
        if not ready:
            return reply("Les questions de tes fiches sont encore en préparation. Réessaie dans une minute.",
                         end=False, reprompt=FOLLOW_UP)
        titles = ", ".join(ready)[:300]
        return reply(f"Je n'ai pas trouvé de fiche sur {topic}. Tes fiches sont : {titles}. "
                     "Dis par exemple : interroge-moi sur, suivi du titre.", end=False, reprompt=FOLLOW_UP)
    questions = store.questions(meta["id"])
    with _quiz_lock:
        _quiz = {"fid": meta["id"], "title": meta["title"], "questions": questions, "asked": set(),
                 "current": None, "num": 0, "last": time.time(),
                 "score": {"juste": 0, "partiel": 0, "faux": 0}}
    threading.Thread(target=tv.wake, daemon=True).start()
    first = _next_question()
    return reply(f"C'est parti, je t'interroge sur {meta['title']}. "
                 f"Réponds en commençant par c'est, ou je pense que. Première question : {first}",
                 end=False, reprompt=f"Je répète : {first}")


def _record(verdict: str) -> None:
    with _quiz_lock:
        q = _quiz["current"]
        _quiz["score"][verdict] += 1
        fid = _quiz["fid"]
    store.record(fid, q["id"], verdict)


def quiz_answer(said: str) -> dict:
    with _quiz_lock:
        q = _quiz["current"]
    try:
        res = brain.grade_answer(q, said)
    except brain.TooSlow:
        res = {"verdict": None, "speech": f"Je n'ai pas eu le temps de corriger. La réponse attendue : {q['reponse']}"}
    conv.add(f"Question de révision : {q['q']} Ma réponse : {said}", res["speech"], None)
    if res["verdict"] == "question":
        with _quiz_lock:
            _quiz["last"] = time.time()
        return reply(res["speech"], end=False, reprompt=f"Je répète : {q['q']}")
    if res["verdict"]:
        _record(res["verdict"])
    feedback = {"verdict": res["verdict"] or "faux", "text": res["speech"], "question": q["q"], "reponse": q["reponse"]}
    nxt = _next_question(feedback)
    return reply(f"{res['speech']} Question suivante : {nxt}", end=False, reprompt=f"Je répète : {nxt}")


def quiz_dont_know() -> dict:
    with _quiz_lock:
        q = _quiz["current"]
    _record("faux")
    feedback = {"verdict": "faux", "text": "Pas de souci, voici la réponse.", "question": q["q"], "reponse": q["reponse"]}
    nxt = _next_question(feedback)
    return reply(f"Pas de souci. La réponse : {q['reponse']} Question suivante : {nxt}",
                 end=False, reprompt=f"Je répète : {nxt}")


def quiz_summary() -> str:
    global _quiz
    with _quiz_lock:
        quiz, _quiz = _quiz, None
    sc = quiz["score"]
    total = sum(sc.values())
    bus.publish({"type": "quiz_end", "title": quiz["title"], "score": sc})
    if not total:
        return "Fin de la révision."
    pct = (sc["juste"] + 0.5 * sc["partiel"]) / total
    mood = ("Très solide, bravo." if pct >= 0.8 else "C'est un bon début, on retravaillera les points ratés."
            if pct >= 0.5 else "Il y a encore du travail sur cette fiche, je te reposerai les questions ratées.")
    return (f"Fin de la révision sur {quiz['title']}. Sur {total} questions : {sc['juste']} justes, "
            f"{sc['partiel']} partielles et {sc['faux']} ratées. {mood}")


def handle_quiz_intent(name: str, text: str) -> dict | None:
    """Gère les intents pendant une révision. Renvoie None si l'intent n'est pas pour le quiz."""
    with _quiz_lock:
        q = _quiz["current"]
    if name in ("QuestionIntent", "AnswerIntent"):
        if not text:
            return reply("Je n'ai pas entendu ta réponse. Commence par c'est, ou je pense que.",
                         end=False, reprompt=f"Je répète : {q['q']}")
        return quiz_answer(text)
    if name == "DontKnowIntent":
        return quiz_dont_know()
    if name == "SkipIntent":
        nxt = _next_question()
        return reply(f"On passe. Question suivante : {nxt}", end=False, reprompt=f"Je répète : {nxt}")
    if name == "HintIntent":
        return reply(f"Indice : {q.get('indice') or 'pense aux idées principales de la fiche.'} Je répète : {q['q']}",
                     end=False, reprompt=f"Je répète : {q['q']}")
    if name == "RepeatIntent":
        return reply(q["q"], end=False, reprompt=f"Je répète : {q['q']}")
    if name == "ScoreIntent":
        with _quiz_lock:
            sc = dict(_quiz["score"])
        return reply(f"Pour l'instant : {sc['juste']} justes, {sc['partiel']} partielles, {sc['faux']} ratées. "
                     f"On continue : {q['q']}", end=False, reprompt=f"Je répète : {q['q']}")
    if name == "StopQuizIntent":
        return reply(quiz_summary() + " Je reste à l'écoute si tu as des questions.", end=False, reprompt=FOLLOW_UP)
    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
        return reply(quiz_summary(), end=True)
    if name == "AMAZON.FallbackIntent":
        return reply("Je n'ai pas compris ta réponse. Commence par c'est, ou je pense que, "
                     "ou dis je sais pas, un indice, ou question suivante.", end=False, reprompt=f"Je répète : {q['q']}")
    return None


def _slot_text(envelope: dict) -> str:
    slots = envelope["request"]["intent"].get("slots") or {}
    return next((v.get("value", "").strip() for v in slots.values() if v.get("value")), "")


def route(envelope: dict) -> dict:
    req = envelope.get("request", {})
    rtype = req.get("type")
    attrs = (envelope.get("session") or {}).get("attributes") or {}

    if rtype == "LaunchRequest":
        return reply("Oui ? Je t'écoute.", end=False, reprompt="Pose-moi ta question.")
    if rtype == "SessionEndedRequest":
        return {"version": "1.0", "response": {}}
    if rtype != "IntentRequest":
        return reply("Je ne sais pas gérer ça.", end=True)

    name = req["intent"]["name"]
    if name in ("QuizIntent", "QuizAnyIntent"):
        return start_quiz(_slot_text(envelope) or None)
    if quiz_active():
        handled = handle_quiz_intent(name, _slot_text(envelope))
        if handled:
            return handled
    if name in ("DontKnowIntent", "SkipIntent", "HintIntent", "RepeatIntent", "ScoreIntent", "StopQuizIntent"):
        return reply("Il n'y a pas de révision en cours. Dis par exemple : interroge-moi sur, suivi du nom de ta fiche.",
                     end=False, reprompt=FOLLOW_UP)
    if name == "AnswerIntent":
        name = "QuestionIntent"
    if name == "QuestionIntent":
        return handle_question(envelope, force_visual=False)
    if name == "VisualIntent":
        return handle_question(envelope, force_visual=True)
    if name == "NewTopicIntent":
        conv.reset()
        return reply("D'accord, on repart de zéro. Je t'écoute.", end=False, reprompt=FOLLOW_UP)
    if name == "TvOffIntent":
        threading.Thread(target=tv.standby, daemon=True).start()
        return reply("J'éteins la télé.", end=True)
    if name == "AMAZON.HelpIntent":
        return reply(
            "Pose ta question en commençant par comment, pourquoi, c'est quoi ou explique-moi. "
            "Dis montre-moi pour avoir un schéma, ou interroge-moi sur, suivi d'une de tes fiches, pour réviser.",
            end=False, attrs=attrs,
        )
    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
        return reply("À plus.", end=True)
    if name == "AMAZON.FallbackIntent":
        return reply(
            "Je n'ai pas saisi. Commence par explique-moi, dis-moi ou montre-moi.",
            end=False, attrs=attrs,
        )
    return reply("Je ne connais pas encore cette commande.", end=False, attrs=attrs)


# --------------------------------------------------------------------------- #
# Routes HTTP
# --------------------------------------------------------------------------- #
_failed: dict[str, list[float]] = {}


def _from_tunnel() -> bool:
    return "Cf-Connecting-Ip" in request.headers or "X-Forwarded-For" in request.headers


def _client_ip() -> str:
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()


@app.before_request
def guard():
    """/alexa est public (signé par Amazon), /fiches est protégé par mot de passe, le reste est local."""
    if request.path == "/alexa":
        return None
    if request.path == "/fiches" or request.path.startswith("/fiches/"):
        if not FICHES_PASSWORD:
            if _from_tunnel():
                abort(403, "Ajoute FICHES_PASSWORD dans le fichier .env pour ouvrir cette page depuis Internet.")
            return None
        ip = _client_ip()
        recent = [t for t in _failed.get(ip, []) if time.time() - t < 600]
        if len(recent) >= 10:
            abort(429, "Trop d'essais, réessaie dans 10 minutes.")
        auth = request.authorization
        if auth and hmac.compare_digest((auth.password or "").encode(), FICHES_PASSWORD.encode()):
            return None
        if auth:
            _failed[ip] = recent + [time.time()]
        return Response("Mot de passe requis.", 401, {"WWW-Authenticate": 'Basic realm="Evy - fiches", charset="UTF-8"'})
    if _from_tunnel():
        abort(404)
    return None


@app.post("/alexa")
def alexa():
    raw = request.get_data()
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        abort(400)
    if VERIFY:
        try:
            verify_request(request.headers, raw, envelope, SKILL_ID)
        except VerificationError as e:
            log.warning("Requête rejetée : %s", e)
            abort(400)
    try:
        return jsonify(route(envelope))
    except Exception:  # noqa: BLE001
        log.exception("Erreur dans le skill")
        return jsonify(reply("Oups, j'ai eu un souci. Réessaie.", end=True))


@app.get("/")
def index():
    return send_from_directory(BASE / "display", "index.html")


@app.get("/events")
def events():
    def stream():
        q = bus.subscribe()
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/visual/<vid>")
def visual(vid: str):
    if not re.fullmatch(r"[0-9a-f]{12}", vid):
        abort(404)
    return send_from_directory(VISUALS, f"{vid}.html")


@app.get("/api/history")
def history():
    return jsonify(_load_index())


@app.post("/api/show")
def show():
    """Test sans Alexa : curl -X POST localhost:5000/api/show -H 'Content-Type: application/json' -d '{"question":"..."}'"""
    question = ((request.get_json(silent=True) or {}).get("question") or "").strip()
    if not question:
        abort(400)
    start_visual(question, question)
    return jsonify({"ok": True})


@app.post("/api/display/<vid>")
def redisplay(vid: str):
    """Réaffiche un ancien visuel."""
    entry = next((e for e in _load_index() if e["id"] == vid), None)
    if not entry:
        abort(404)
    bus.publish({"type": "visual", **entry})
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Page des fiches (accessible via ngrok, protégée par FICHES_PASSWORD)
# --------------------------------------------------------------------------- #
@app.get("/fiches")
def fiches_page():
    return send_from_directory(BASE / "display", "fiches.html")


@app.get("/fiches/api/list")
def fiches_list():
    return jsonify(store.list())


@app.post("/fiches/api/upload")
def fiches_upload():
    files = request.files.getlist("files")
    subject = request.form.get("subject", "")
    title = request.form.get("title", "") if len(files) == 1 else ""
    results = []
    for f in files:
        try:
            meta = store.add(f.filename or "fiche", f.read(), subject, title)
            build_bank(meta["id"])
            results.append({"ok": True, "title": meta["title"]})
        except fiches.FicheError as e:
            results.append({"ok": False, "filename": f.filename, "error": str(e)})
    return jsonify(results)


@app.get("/fiches/api/<fid>/questions")
def fiches_questions(fid: str):
    try:
        return jsonify(store.questions(fid))
    except KeyError:
        abort(404)


@app.post("/fiches/api/<fid>/delete")
def fiches_delete(fid: str):
    try:
        store.delete(fid)
    except KeyError:
        abort(404)
    return jsonify({"ok": True})


@app.post("/fiches/api/<fid>/regenerate")
def fiches_regenerate(fid: str):
    try:
        store.get(fid)
    except KeyError:
        abort(404)
    build_bank(fid)
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(_e):
    return jsonify([{"ok": False, "filename": "", "error": "Fichier trop lourd (50 Mo maximum)."}]), 413


def resume_pending() -> None:
    """Relance les générations interrompues par un redémarrage."""
    for meta in store.list():
        if meta["status"] == "generating":
            build_bank(meta["id"])


if __name__ == "__main__":
    tv.setup()
    resume_pending()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), threaded=True)
