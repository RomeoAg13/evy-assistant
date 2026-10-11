"""Evy — endpoint du skill Alexa + écran télé, à faire tourner sur la Raspberry Pi.

Routes :
  POST /alexa         -> appelé par Alexa (seule route exposée via Cloudflare Tunnel)
  GET  /              -> page affichée sur la télé (Chromium en mode kiosque)
  GET  /events        -> flux temps réel vers la télé (Server-Sent Events)
  GET  /visual/<id>   -> un visuel généré
  GET  /api/history   -> historique des visuels
  POST /api/show      -> test local sans Alexa : {"question": "..."}
"""
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

app = Flask(__name__, static_folder=None)


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


def start_visual(question: str, brief: str) -> None:
    """Allume la télé et génère le visuel en arrière-plan."""

    def job():
        vid = uuid.uuid4().hex[:12]
        try:
            tv.wake()
            bus.publish({"type": "loading", "title": question})
            page = brain.make_visual(question, brief)
            (VISUALS / f"{vid}.html").write_text(page, "utf-8")
            entry = {"id": vid, "title": question, "ts": int(time.time())}
            _add_to_index(entry)
            bus.publish({"type": "visual", **entry})
            log.info("Visuel %s prêt : %s", vid, question)
        except Exception as e:  # noqa: BLE001
            log.exception("Échec du visuel")
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


def handle_question(envelope: dict, force_visual: bool) -> dict:
    session = envelope.get("session", {})
    attrs = session.get("attributes") or {}
    history = attrs.get("history", [])
    one_shot = session.get("new", True)  # « Alexa, demande à Evy … » -> on ne garde pas le micro ouvert

    slots = envelope["request"]["intent"].get("slots") or {}
    question = ((slots.get("query") or {}).get("value") or "").strip()
    if not question:
        return reply("Je n'ai pas compris ta question. Tu peux répéter ?", end=False, attrs=attrs)

    try:
        res = brain.answer(question, history, force_visual=force_visual)
        speech = res["speech"]
        if res["visual"]:
            start_visual(question, res["visual"])
            speech += " Je t'affiche ça sur la télé."
    except brain.TooSlow:
        # Claude met trop de temps : on bascule tout sur l'écran
        start_visual(question, "Réponds à la question de façon visuelle et complète.")
        speech = "Ça demande un peu de réflexion, je t'affiche la réponse sur la télé."
        res = {"speech": speech}

    history = (history + [{"q": question, "a": res["speech"]}])[-4:]
    attrs["history"] = history
    if one_shot:
        return reply(speech, end=True)
    return reply(speech, end=False, attrs=attrs)


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
    if name == "QuestionIntent":
        return handle_question(envelope, force_visual=False)
    if name == "VisualIntent":
        return handle_question(envelope, force_visual=True)
    if name == "TvOffIntent":
        threading.Thread(target=tv.standby, daemon=True).start()
        return reply("J'éteins la télé.", end=True)
    if name == "AMAZON.HelpIntent":
        return reply(
            "Commence ta phrase par explique-moi, dis-moi, ou montre-moi si tu veux un schéma à l'écran.",
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
@app.before_request
def local_only():
    """Tout sauf /alexa est réservé au réseau local (jamais via le tunnel)."""
    if request.path != "/alexa" and "Cf-Connecting-Ip" in request.headers:
        abort(404)


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


if __name__ == "__main__":
    tv.setup()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), threaded=True)
