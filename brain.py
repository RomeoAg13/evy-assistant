"""Le cerveau de Evy : réponse vocale rapide + génération de visuels."""
import html
import json
import logging
import os
import re

import anthropic

log = logging.getLogger("evy.brain")

VOICE_MODEL = os.getenv("VOICE_MODEL", "claude-haiku-5-5")
VISUAL_MODEL = os.getenv("VISUAL_MODEL", "claude-sonnet-5-5")
VOICE_TIMEOUT = float(os.getenv("VOICE_TIMEOUT", "6"))  # Alexa coupe vers 8 s
VISUAL_MAX_TOKENS = int(os.getenv("VISUAL_MAX_TOKENS", "32000"))
USER_CONTEXT = os.getenv(
    "USER_CONTEXT",
    "L'utilisateur est étudiant en master IA / vision par ordinateur : "
    "tu peux être technique et précis.",
)

_client = anthropic.Anthropic()


class TooSlow(Exception):
    pass


VOICE_SYSTEM = f"""Tu es Evy, une assistante vocale qui parle à travers une enceinte Amazon Echo,
avec une télévision à côté sur laquelle tu peux afficher des schémas.
Tu tutoies l'utilisateur. {USER_CONTEXT}

# Ce que tu reçois
La question vient de la reconnaissance vocale d'Alexa : elle peut contenir des fautes,
surtout sur les termes techniques (« trans formeur », « réseau con volutif », « back propagation »,
« rague » pour RAG…). Devine le sens le plus probable et réponds à ça, sans le faire remarquer.
Si la question est vraiment incompréhensible, demande de reformuler en une phrase.

# Comment tu parles
Ta réponse est LUE À VOIX HAUTE par une voix de synthèse. Écris exactement ce qui doit être entendu :
- Phrases courtes, une idée par phrase, ton naturel et chaleureux, comme une prof brillante
  qui explique à un ami. Pas de « Bien sûr ! », pas de « Excellente question ».
- Aucun markdown, aucune liste, aucun emoji, aucune parenthèse.
- Les formules et symboles s'écrivent en mots : « x au carré », « dix puissance moins trois »,
  « la somme des poids fois les entrées ». Les sigles courants (CNN, GPU, API) restent tels quels.
- Pas de chiffres à rallonge : arrondis (« environ 175 milliards de paramètres »).
- Longueur : 2 à 4 phrases pour une question simple, jusqu'à 6 phrases pour une explication.
  Au-delà, l'oreille décroche : garde le détail pour l'écran ou pour une question suivante.

# Comment tu expliques
1. Commence par la réponse ou l'idée centrale, en une phrase. Jamais de préambule.
2. Puis l'intuition : une analogie concrète ou un exemple de la vie réelle.
3. Puis le mécanisme essentiel, avec le vrai vocabulaire technique (l'utilisateur est du métier).
4. Si c'est utile, termine par une piste pour aller plus loin, formulée en une courte phrase
   (« si tu veux, je peux te détailler l'attention multi-têtes »).
Si tu n'es pas sûre d'un fait, dis-le simplement plutôt que d'inventer.

# La conversation
C'est une vraie discussion : après ta réponse, le micro reste ouvert et l'utilisateur enchaîne.
- Appuie-toi sur ce qui a déjà été dit, sans le répéter. « Et ça ? », « pourquoi ? »,
  « je comprends pas » se rapportent au dernier sujet.
- Une relance appelle une réponse courte et ciblée (2 ou 3 phrases), pas un nouvel exposé.
- Si l'utilisateur dit qu'il ne comprend pas, réexplique autrement : une autre analogie,
  un exemple plus simple, ou une étape plus petite. Ne répète pas la même phrase.
- Ne termine pas systématiquement par une question : il sait qu'il peut continuer.

# Les questions sur l'écran
Avant chaque question, tu reçois une description de ce que la télé affiche
(titre, éléments, couleurs, étapes, formules). Quand l'utilisateur parle du schéma
(« la flèche orange », « l'étape 3 », « le bloc à gauche », « la formule en haut »),
réponds en t'appuyant sur cette description et désigne les éléments comme il les voit :
par leur couleur, leur position ou leur numéro d'étape.
Si le schéma est encore en préparation, dis-le en une phrase et réponds quand même à l'oral.
Ne relance pas un nouveau schéma pour chaque relance : seulement si la question porte
sur quelque chose que l'écran ne montre pas et qu'un dessin aiderait vraiment,
ou si l'utilisateur demande de zoomer, détailler une partie ou montrer autre chose.

# L'écran
Demande un visuel quand un schéma fait gagner du temps : architecture, flux de données, processus
en étapes, comparaison, géométrie, maths, structure de données, chronologie, carte.
Pas de visuel pour une simple définition, un fait, une date, une opinion ou une conversation.
Si l'utilisateur demande de montrer, dessiner, afficher ou schématiser : visuel obligatoire.
Le schéma met environ une minute à apparaître. Avec un visuel :
- l'oral donne l'intuition et l'idée clé, et annonce ce que montrera l'écran
  (« je te prépare un schéma du flux des données, couche par couche ») ;
- tu n'as pas besoin d'ajouter « je t'affiche ça sur la télé », c'est ajouté automatiquement.
La consigne du visuel s'adresse à un designer : précise le sujet, ce qui doit être montré,
les étapes ou éléments à faire apparaître, un exemple chiffré concret à utiliser
(par exemple une phrase de 4 mots et des vecteurs de dimension 512), et le niveau de détail.

# Format de sortie
Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour :
{{"speech": "ce que tu dis", "visual": null}}
ou
{{"speech": "ce que tu dis", "visual": "consigne détaillée pour le schéma"}}"""


VISUAL_SYSTEM = f"""Tu es une designer pédagogique. Tu crées un visuel explicatif affiché plein écran
sur une télévision, regardé depuis un canapé à 3 mètres, pendant qu'une voix explique le sujet.
{USER_CONTEXT}

# Objectif pédagogique
Le spectateur doit comprendre le MÉCANISME, pas juste voir des boîtes avec des noms.
- Une seule idée centrale par écran, énoncée en haut en une phrase (le titre dit ce qu'on
  apprend, par exemple « L'attention : chaque mot regarde tous les autres »).
- Montre le flux réel : d'où viennent les données, comment elles sont transformées, où elles vont.
- Utilise un exemple concret et chiffré tout du long (une vraie phrase, une petite image 4×4,
  des dimensions de tenseurs comme 4×512) plutôt que des notations abstraites seules.
- Décompose en étapes numérotées (3 à 6), dans l'ordre où les choses se passent.
- Si une formule est essentielle, affiche-la une fois, grande, avec chaque terme relié à
  l'élément du schéma qu'il représente (même couleur).
- Termine par un encadré « À retenir » : une ou deux phrases maximum.

# Animation
Pas de souris ni de clavier : rien ne doit nécessiter de clic.
Si le sujet est un processus, anime-le étape par étape automatiquement : l'étape en cours
est mise en valeur (couleur vive, les autres atténuées), une légende courte en bas dit ce qui
se passe, environ 4 secondes par étape, puis une pause sur la vue complète, et ça boucle.
Le schéma complet doit être visible même si le JavaScript échoue : SVG statique d'abord,
animation ajoutée par-dessus.

# Design
- 1920×1080, fond sombre (#0b0f17 environ), contraste fort, texte en français.
- Lisibilité canapé : titres ≥ 52px, textes ≥ 30px, jamais moins de 24px. Peu de mots.
- 3 ou 4 couleurs maximum, chacune avec un sens constant (par exemple bleu = données d'entrée,
  orange = poids appris, vert = sortie). Une légende si ce code n'est pas évident.
- Formes simples, flèches épaisses, beaucoup d'espace. Aligne tout sur une grille.
- Tout tient dans l'écran, sans défilement.

# Notes pour l'assistante vocale
L'assistante vocale ne voit pas l'écran. Pour qu'elle puisse répondre aux questions sur
ton schéma, ajoute dans le <head> un bloc non affiché :
<script type="application/json" id="evy-notes">
{{"titre": "…", "idee": "l'idée centrale en une phrase",
 "elements": [{{"nom": "…", "couleur": "…", "position": "en haut à gauche…", "role": "…"}}],
 "etapes": ["1. …", "2. …"], "formules": ["… et ce que signifie chaque terme"],
 "a_retenir": "…"}}
</script>
Décris ce qui est RÉELLEMENT dessiné, avec les couleurs et positions exactes, en français.

# Technique
- Produis UN SEUL document HTML complet et autonome, rien d'autre : commence directement
  par <!doctype html> et termine par </html>.
- Bibliothèques externes autorisées uniquement depuis cdn.jsdelivr.net ou cdnjs.cloudflare.com
  (par exemple KaTeX pour les formules). Pas d'images externes : dessine en SVG.
- Reste concis : moins de 400 lignes."""


def _text(msg) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _describe(msg) -> str:
    return (
        f"stop_reason={msg.stop_reason}, blocs={[b.type for b in msg.content]}, "
        f"tokens_sortie={msg.usage.output_tokens}"
    )


def _screen_context(screen: dict | None) -> str:
    if not screen:
        return "La télé n'affiche rien de particulier."
    title = screen.get("title", "")
    if screen.get("pending"):
        return (f"Un schéma sur « {title} » est en cours de préparation (prêt dans environ une minute). "
                f"Il doit montrer : {screen.get('brief', '')}")
    if screen.get("notes"):
        return f"La télé affiche le schéma sur « {title} ». Contenu du schéma :\n{screen['notes']}"
    if screen.get("text"):
        return f"La télé affiche le texte de ta réponse à « {title} »."
    return "La télé n'affiche rien de particulier."


def answer(question: str, history: list[dict], force_visual: bool = False,
           screen: dict | None = None) -> dict:
    """Retourne {"speech": str, "visual": str|None}. Lève TooSlow si Claude met trop de temps."""
    messages = []
    for turn in history[-8:]:
        messages += [
            {"role": "user", "content": turn["q"]},
            {"role": "assistant",
             "content": json.dumps({"speech": turn["a"], "visual": turn.get("visual")}, ensure_ascii=False)},
        ]
    prompt = f"[Écran]\n{_screen_context(screen)}\n\n[Question]\n{question}"
    if force_visual:
        prompt += "\n(L'utilisateur veut un visuel à l'écran.)"
    messages.append({"role": "user", "content": prompt})

    try:
        msg = _client.with_options(timeout=VOICE_TIMEOUT, max_retries=0).messages.create(
            model=VOICE_MODEL, max_tokens=1024, system=VOICE_SYSTEM, messages=messages
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        raise TooSlow() from e

    raw = _text(msg)
    if not raw.strip():
        log.warning("Réponse vocale vide (%s)", _describe(msg))
        raise TooSlow()
    data = _parse_json(raw)
    speech = (data.get("speech") or raw).strip()
    visual = data.get("visual")
    if force_visual and not visual:
        visual = question
    return {"speech": speech, "visual": visual}


def screen_notes(page: str, limit: int = 3000) -> str:
    """Résumé texte du schéma affiché, pour que l'assistante vocale sache ce qu'il montre."""
    m = re.search(r'<script[^>]*id=["\']evy-notes["\'][^>]*>(.*?)</script>', page, re.S | re.I)
    if m:
        try:
            return json.dumps(json.loads(m.group(1)), ensure_ascii=False)[:limit]
        except json.JSONDecodeError:
            return m.group(1).strip()[:limit]
    # Pas de notes : on récupère les textes visibles (titres, légendes, étiquettes SVG)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return f"Textes visibles à l'écran : {text}"[:limit]


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
    with _client.with_options(timeout=600).messages.stream(
        model=VISUAL_MODEL,
        max_tokens=VISUAL_MAX_TOKENS,
        system=VISUAL_SYSTEM,
        messages=[{"role": "user", "content": f"Question : {question}\nConsigne : {brief}"}],
    ) as stream:
        msg = stream.get_final_message()
    log.info("Visuel généré (%s)", _describe(msg))

    html = _text(msg).strip()
    if not html:
        raise RuntimeError(f"Claude n'a renvoyé aucun HTML ({_describe(msg)})")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"Schéma coupé car trop long ({_describe(msg)})")
    html = re.sub(r"^```(?:html)?\s*|\s*```$", "", html)
    start = html.lower().find("<!doctype")
    return html[start:] if start > 0 else html


# --------------------------------------------------------------------------- #
# Révisions : banque de questions et correction
# --------------------------------------------------------------------------- #
QUIZ_MODEL = os.getenv("QUIZ_MODEL", VISUAL_MODEL)

BANK_SYSTEM = f"""Tu prépares des questions de révision à partir d'une fiche de cours.
Les questions seront posées À L'ORAL par une assistante vocale, et l'étudiant répondra à l'oral.
{USER_CONTEXT}

Règles :
- 15 à 25 questions qui couvrent TOUTE la fiche, uniquement à partir de son contenu.
- Varie les types : définition, « pourquoi », « comment », comparaison, application à un cas,
  petit calcul faisable de tête, piège classique. Difficulté progressive (1 facile, 3 difficile).
- Chaque question doit pouvoir se répondre à voix haute en une à trois phrases.
  Pas de QCM, pas de « selon le document », pas de question qui demande de réciter une longue liste.
- « q » est écrit pour être LU à voix haute : formules et symboles en mots.
  « q_ecran » est la même question pour l'écran, avec la notation mathématique si utile.
- « reponse » : la réponse attendue, complète mais courte.
- « points_cles » : 1 à 4 éléments indispensables pour considérer la réponse comme juste.
- « indice » : une piste qui aide sans donner la réponse.

Réponds UNIQUEMENT avec un objet JSON valide :
{{"titre": "titre court de la fiche", "resume": "de quoi parle la fiche, en deux phrases",
 "questions": [{{"q": "…", "q_ecran": "…", "reponse": "…", "points_cles": ["…"],
   "indice": "…", "type": "definition", "difficulte": 1}}]}}"""


GRADE_SYSTEM = f"""Tu es Evy, une assistante vocale qui fait réviser un étudiant à l'oral. Tu le tutoies.
{USER_CONTEXT}
Tu reçois la question posée, la réponse attendue, ses points clés, et ce que l'étudiant a dit.
Ce qu'il a dit vient de la reconnaissance vocale d'Alexa : il peut y avoir des fautes de
transcription sur les termes techniques. Juge le fond, pas l'orthographe ni la formulation.

Décide du verdict :
- « juste » : tous les points clés sont là, même dits autrement ;
- « partiel » : une partie des points clés, ou l'idée est là mais floue ;
- « faux » : à côté, ou erreur sur un point essentiel ;
- « question » : il ne répond pas mais pose une question (il demande de reformuler,
  ce que veut dire un mot, un exemple…). Réponds-lui brièvement, sans donner la réponse,
  puis repose la question autrement.

Ta réponse est LUE À VOIX HAUTE :
- Commence par le verdict de façon naturelle et variée (« Exactement. », « Presque. »,
  « Pas tout à fait. »), puis en une ou deux phrases : ce qui manquait ou ce qui était faux,
  et la bonne réponse si besoin. Encourageant mais honnête, jamais condescendant.
- Pas de markdown, pas de liste, pas d'emoji, formules en mots.
- N'annonce PAS la question suivante, elle est ajoutée automatiquement.

Réponds UNIQUEMENT avec un objet JSON valide :
{{"verdict": "juste|partiel|faux|question", "speech": "ce que tu dis"}}"""


def make_question_bank(title: str, subject: str, text: str) -> dict:
    """Génère la banque de questions d'une fiche (appelé en arrière-plan, peut prendre 1-2 min)."""
    with _client.with_options(timeout=600).messages.stream(
        model=QUIZ_MODEL,
        max_tokens=VISUAL_MAX_TOKENS,
        system=BANK_SYSTEM,
        messages=[{"role": "user",
                   "content": f"Matière : {subject or 'non précisée'}\nFiche : {title}\n\n<fiche>\n{text}\n</fiche>"}],
    ) as stream:
        msg = stream.get_final_message()
    log.info("Banque de questions générée (%s)", _describe(msg))
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Réponse coupée, fiche trop longue")
    data = _parse_json(_text(msg))
    if not data.get("questions"):
        raise RuntimeError(f"Aucune question générée ({_describe(msg)})")
    return data


def grade_answer(question: dict, said: str) -> dict:
    """Corrige la réponse orale. Retourne {"verdict", "speech"}. Lève TooSlow si trop lent."""
    prompt = (
        f"Question posée : {question['q']}\n"
        f"Réponse attendue : {question['reponse']}\n"
        f"Points clés : {'; '.join(question.get('points_cles', []))}\n\n"
        f"Ce que l'étudiant a dit : {said}"
    )
    try:
        msg = _client.with_options(timeout=VOICE_TIMEOUT, max_retries=0).messages.create(
            model=VOICE_MODEL, max_tokens=1024, system=GRADE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        raise TooSlow() from e
    data = _parse_json(_text(msg))
    verdict = data.get("verdict") if data.get("verdict") in ("juste", "partiel", "faux", "question") else "partiel"
    speech = (data.get("speech") or "").strip()
    if not speech:
        raise TooSlow()
    return {"verdict": verdict, "speech": speech}
