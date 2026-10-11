"""Le cerveau de Evy : réponse vocale rapide + génération de visuels."""
import json
import logging
import os
import re

import anthropic

log = logging.getLogger("evy.brain")

VOICE_MODEL = os.getenv("VOICE_MODEL", "claude-haiku-5-5")
VISUAL_MODEL = os.getenv("VISUAL_MODEL", "claude-sonnet-5-5")
VOICE_TIMEOUT = float(os.getenv("VOICE_TIMEOUT", "6"))  # Alexa coupe vers 8 s
USER_CONTEXT = os.getenv(
    "USER_CONTEXT",
    "L'utilisateur est étudiant en master IA / vision par ordinateur : "
    "tu peux être technique et précis.",
)

_client = anthropic.Anthropic()


class TooSlow(Exception):
    pass


VOICE_SYSTEM = f"""Tu es Evy, un assistant vocal qui parle via une enceinte Amazon Echo.
Tu tutoies l'utilisateur. {USER_CONTEXT}

Ta réponse est LUE À VOIX HAUTE :
- 2 à 4 phrases maximum, naturelles à l'oral.
- Aucun markdown, aucune liste, aucun emoji, pas de formules : écris les symboles en mots.

Tu disposes d'un écran (la télé). Demande un visuel quand un schéma aide vraiment :
architecture, processus, comparaison, maths, structure de données, géographie…
Si l'utilisateur demande explicitement de montrer, dessiner ou afficher, demande toujours un visuel.
Quand il y a un visuel, ta phrase orale résume l'idée clé et laisse le détail à l'écran.

Réponds UNIQUEMENT avec un objet JSON :
{{"speech": "ce que tu dis", "visual": null}}
ou
{{"speech": "ce que tu dis", "visual": "consigne précise pour le schéma à afficher"}}"""


VISUAL_SYSTEM = f"""Tu crées des visuels pédagogiques affichés plein écran sur une télévision.
{USER_CONTEXT}

Produis UN SEUL document HTML complet et autonome (<!doctype html> … </html>), rien d'autre.
Contraintes :
- Cible 1920×1080, fond sombre, contraste fort, texte en français.
- Lisible depuis un canapé : corps de texte ≥ 28px, titres ≥ 48px, peu de texte.
- Pas de souris ni de clavier : rien ne doit nécessiter de clic. Privilégie un schéma SVG
  soigné, et si c'est utile une animation qui se déroule seule étape par étape (puis boucle).
- Montre le vrai mécanisme (flux de données, dimensions des tenseurs, formules clés en KaTeX si utile).
- Bibliothèques externes autorisées uniquement depuis cdn.jsdelivr.net ou cdnjs.cloudflare.com.
- Tout doit tenir dans l'écran, sans défilement."""


def _text(msg) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def answer(question: str, history: list[dict], force_visual: bool = False) -> dict:
    """Retourne {"speech": str, "visual": str|None}. Lève TooSlow si Claude met trop de temps."""
    messages = []
    for turn in history[-4:]:
        messages += [
            {"role": "user", "content": turn["q"]},
            {"role": "assistant", "content": json.dumps({"speech": turn["a"], "visual": None}, ensure_ascii=False)},
        ]
    prompt = question
    if force_visual:
        prompt += "\n(L'utilisateur veut un visuel à l'écran.)"
    messages.append({"role": "user", "content": prompt})

    try:
        msg = _client.with_options(timeout=VOICE_TIMEOUT, max_retries=0).messages.create(
            model=VOICE_MODEL, max_tokens=400, system=VOICE_SYSTEM, messages=messages
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        raise TooSlow() from e

    raw = _text(msg)
    data = _parse_json(raw)
    speech = (data.get("speech") or raw).strip()
    visual = data.get("visual")
    if force_visual and not visual:
        visual = question
    return {"speech": speech, "visual": visual}


def _parse_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def make_visual(question: str, brief: str) -> str:
    """Génère la page HTML du visuel (peut prendre 20-60 s, appelé en arrière-plan)."""
    msg = _client.with_options(timeout=180).messages.create(
        model=VISUAL_MODEL,
        max_tokens=16000,
        system=VISUAL_SYSTEM,
        messages=[{"role": "user", "content": f"Question : {question}\nConsigne : {brief}"}],
    )
    html = _text(msg).strip()
    html = re.sub(r"^```(?:html)?\s*|\s*```$", "", html)
    start = html.lower().find("<!doctype")
    return html[start:] if start > 0 else html
