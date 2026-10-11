"""Vérification des requêtes Alexa (signature + horodatage).

Amazon exige qu'un skill hébergé hors de Lambda vérifie que chaque requête
vient bien d'Alexa. Sans ça, n'importe qui connaissant l'URL du tunnel
pourrait appeler ton Evy (et consommer ton crédit API).
"""
import base64
import logging
import threading
import time
import warnings
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import urlopen

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.verification import PolicyBuilder, Store

log = logging.getLogger("evy.verify")

MAX_SKEW_SECONDS = 150
_cert_cache: dict[str, tuple[float, list[x509.Certificate]]] = {}
_cache_lock = threading.Lock()
with warnings.catch_warnings():  # quelques vieux certificats racine déclenchent un avertissement
    warnings.simplefilter("ignore")
    _store = Store(x509.load_pem_x509_certificates(open(certifi.where(), "rb").read()))


class VerificationError(Exception):
    pass


def _check_cert_url(url: str) -> None:
    p = urlparse(url)
    path = p.path.replace("/../", "/").replace("//", "/")
    if (
        p.scheme.lower() != "https"
        or (p.hostname or "").lower() != "s3.amazonaws.com"
        or not path.startswith("/echo.api/")
        or p.port not in (None, 443)
    ):
        raise VerificationError(f"URL de certificat refusée : {url}")


def _load_chain(url: str) -> list[x509.Certificate]:
    with _cache_lock:
        hit = _cert_cache.get(url)
        if hit and hit[0] > time.time():
            return hit[1]
    with urlopen(url, timeout=3) as r:
        chain = x509.load_pem_x509_certificates(r.read())
    if not chain:
        raise VerificationError("Chaîne de certificats vide")
    leaf, intermediates = chain[0], chain[1:]
    verifier = PolicyBuilder().store(_store).build_server_verifier(
        x509.DNSName("echo-api.amazon.com")
    )
    try:
        verifier.verify(leaf, intermediates)  # dates, chaîne, SAN
    except Exception as e:  # noqa: BLE001
        raise VerificationError(f"Certificat invalide : {e}") from e
    with _cache_lock:
        _cert_cache[url] = (time.time() + 3600, chain)
    return chain


def verify_request(headers, raw_body: bytes, envelope: dict, skill_id: str | None) -> None:
    """Lève VerificationError si la requête n'est pas authentique."""
    # 1. Identifiant du skill
    if skill_id:
        app_id = (
            envelope.get("context", {}).get("System", {}).get("application", {}).get("applicationId")
            or envelope.get("session", {}).get("application", {}).get("applicationId")
        )
        if app_id not in [s.strip() for s in skill_id.split(",")]:
            raise VerificationError("applicationId inattendu")

    # 2. Horodatage (anti-rejeu)
    ts = envelope.get("request", {}).get("timestamp")
    if not ts:
        raise VerificationError("Pas d'horodatage")
    sent = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if abs((datetime.now(timezone.utc) - sent).total_seconds()) > MAX_SKEW_SECONDS:
        raise VerificationError("Requête trop ancienne")

    # 3. Signature
    cert_url = headers.get("SignatureCertChainUrl")
    signature = headers.get("Signature-256")
    if not cert_url or not signature:
        raise VerificationError("En-têtes de signature manquants")
    _check_cert_url(cert_url)
    leaf = _load_chain(cert_url)[0]
    try:
        leaf.public_key().verify(
            base64.b64decode(signature), raw_body, padding.PKCS1v15(), hashes.SHA256()
        )
    except Exception as e:  # noqa: BLE001
        raise VerificationError("Signature invalide") from e
