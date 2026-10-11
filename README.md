# Evy — ton assistant Claude dans Alexa, avec la télé comme écran

```
Toi ──► Echo Dot ──► Amazon ──► Cloudflare Tunnel ──► Raspberry Pi 5 (24h/24)
                                                        ├─ Claude (voix rapide + schémas)
                                                        ├─ HDMI-CEC ─► allume la télé
                                                        └─ Chromium plein écran ─► affiche le schéma
```

**À l'usage**
- « Alexa, demande à Evy de me montrer comment marche un transformer » → réponse orale immédiate + schéma sur la télé 20 à 60 s plus tard.
- « Alexa, ouvre Evy » → mode conversation : tu enchaînes les questions sans répéter « Alexa ».
- « Alexa, demande à Evy d'éteindre la télé ».

Tes phrases doivent commencer par **explique-moi / dis-moi / c'est quoi / comment / pourquoi** (réponse orale, schéma si utile) ou **montre-moi / dessine-moi / affiche / fais-moi un schéma de** (schéma forcé). C'est la contrainte des skills Alexa pour capter du texte libre.

---

## Ce qu'il te faut

| Élément | Remarque |
|---|---|
| Raspberry Pi 5 (8 Go) + alim officielle 27 W | Raspberry Pi OS **64 bits avec bureau** |
| Câble micro-HDMI → HDMI | Pi branchée sur la télé |
| Echo (Dot ou autre) | Sur le même compte Amazon que le compte développeur |
| Clé API Anthropic | console.anthropic.com |
| Un nom de domaine sur Cloudflare (~10 €/an) | Pour une URL de tunnel fixe. Alternative gratuite : ngrok et son domaine statique offert |

---

## Installation

### 1. Sur la Pi
```bash
git clone <ton-repo> ~/evy   # ou copie le dossier
cd ~/evy
./deploy/install.sh
nano .env                       # clé API Anthropic (le Skill ID viendra à l'étape 4)
sudo systemctl restart evy
```
Désactive la mise en veille de l'écran : `sudo raspi-config` → *Display Options* → *Screen Blanking* → *No*.
Redémarre : Chromium s'ouvre en plein écran sur l'horloge Evy.

**Test sans Alexa** (depuis la Pi ou ton PC) :
```bash
curl -X POST http://<ip-de-la-pi>:5000/api/show \
  -H 'Content-Type: application/json' \
  -d '{"question":"Architecture d un réseau de neurones convolutif"}'
```
Le schéma doit apparaître sur la télé.

### 2. La télé (HDMI-CEC)
Active le CEC dans les réglages de la télé (Anynet+ chez Samsung, SimpLink chez LG, Bravia Sync chez Sony, EasyLink chez Philips…).
```bash
cec-ctl -d /dev/cec0 --playback          # la Pi s'annonce
cec-ctl -d /dev/cec0 --to 0 --standby    # la télé doit s'éteindre
cec-ctl -d /dev/cec0 --to 0 --image-view-on  # … et se rallumer
```
Rien ne se passe ? Essaie `/dev/cec1` (l'autre port HDMI de la Pi) et mets-le dans `.env`.
La télé s'allume mais reste sur une autre source : lis l'adresse avec `cec-ctl -d /dev/cec0` (ligne *Physical Address*) et mets-la dans `CEC_PHYS_ADDR`.

### 3. Le tunnel (Cloudflare)
```bash
# Installer cloudflared : https://pkg.cloudflare.com (paquet arm64)
cloudflared tunnel login
cloudflared tunnel create evy
cloudflared tunnel route dns evy evy.<ton-domaine>
cp deploy/cloudflared-config.yml ~/.cloudflared/config.yml   # puis remplace <ID-DU-TUNNEL> et <ton-domaine>
sudo cloudflared service install
```
La config n'expose **que** `/alexa`. L'écran et l'historique restent accessibles uniquement chez toi.

### 4. Le skill Alexa
1. developer.amazon.com/alexa → *Create Skill* → nom « Evy », langue **Français (FR)**, modèle **Custom**, hébergement **Provision your own**, template *Start from scratch*.
2. *Build* → *Interaction Model* → *JSON Editor* → colle `skill/interaction_model.fr-FR.json` → *Save* → *Build skill*.
3. *Endpoint* → *HTTPS* → `https://evy.<ton-domaine>/alexa`, certificat : *My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority*.
4. Copie le **Skill ID** dans `.env` (`ALEXA_SKILL_ID`) → `sudo systemctl restart evy`.
5. Onglet *Test* → passe en *Development* → tape « demande à evy de me montrer un transformer ».

Le skill est automatiquement actif sur tes Echo, sans publication.

> Si la console refuse « evy » comme nom d'invocation (un seul mot), utilise « mon evy » dans le JSON.

---

## Réglages utiles (`.env`)
- `VOICE_MODEL` : modèle de la réponse orale. Il doit répondre en moins de 6 s, donc reste sur Haiku.
- `VISUAL_MODEL` : modèle des schémas. Passe sur Opus si tu veux des visuels plus riches (plus lent et plus cher).
- `USER_CONTEXT` : ce que Evy sait de toi, pour adapter le niveau des explications.

## Comment ça marche
- **Réponse orale** : Haiku répond en JSON `{speech, visual}`. Si un visuel est utile, la Pi lance sa génération en arrière-plan et Alexa dit « je t'affiche ça sur la télé ». Ça contourne la limite de ~8 s d'Alexa.
- **Trop lent ?** Si Claude ne répond pas à temps, tout part sur l'écran.
- **Écran** : la page `display/index.html` reçoit les événements en direct (Server-Sent Events), met chaque schéma à l'échelle 1920×1080 et revient à l'horloge au bout de 30 min.
- **Historique** : tous les schémas sont gardés dans `data/visuals/`. `GET /api/history` les liste, `POST /api/display/<id>` en réaffiche un.
- **Sécurité** : chaque requête Alexa est vérifiée (signature Amazon, horodatage, ID du skill). Les autres routes refusent tout ce qui vient du tunnel.

## Dépannage
- Logs : `journalctl -u evy -f`
- Alexa dit « problème avec la réponse du skill » : vérifie le tunnel (`curl -X POST https://evy.<ton-domaine>/alexa` doit renvoyer **400**, pas une erreur Cloudflare) et le Skill ID.
- Écran noir ou « connexion perdue » : `systemctl status evy`.

## Idées pour la suite
- Ton modèle local (Ollama sur la 7900 XT) pour la voix quand le PC est allumé, avec bascule automatique sur Claude.
- « Alexa, demande à Evy d'allumer le PC » (Wake-on-LAN).
- Brancher tes projets : « montre-moi mon portefeuille » → données CIPHER à l'écran.
