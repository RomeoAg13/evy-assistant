"""Fiches de cours : stockage, extraction du texte, banque de questions et suivi des réponses."""
from __future__ import annotations

import difflib
import json
import logging
import random
import re
import shutil
import threading
import time
import unicodedata
import uuid
from pathlib import Path

log = logging.getLogger("evy.fiches")

ALLOWED = {".pdf", ".docx", ".pptx", ".md", ".markdown", ".txt"}
MAX_TEXT = 150_000  # caractères envoyés à Claude pour fabriquer les questions


class FicheError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Extraction du texte
# --------------------------------------------------------------------------- #
def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = [f"--- Page {i} ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages, 1)]
        text = "\n\n".join(parts)
    elif ext == ".docx":
        import docx

        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        text = "\n".join(parts)
    elif ext == ".pptx":
        from pptx import Presentation

        prs = Presentation(str(path))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            lines = [f"## Diapo {i}"]
            for shape in slide.shapes:
                if shape.has_text_frame:
                    lines += [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        lines.append(" | ".join(c.text.strip() for c in row.cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                lines.append("Notes : " + slide.notes_slide.notes_text_frame.text.strip())
            parts.append("\n".join(lines))
        text = "\n\n".join(parts)
    elif ext in (".md", ".markdown", ".txt"):
        text = path.read_text("utf-8", errors="replace")
    else:
        raise FicheError(f"Format non pris en charge : {ext}")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


STOPWORDS = {"les", "des", "une", "sur", "pour", "avec", "dans", "fiche", "cours", "chapitre", "partie", "mon", "mes", "the"}


def _similar(word: str, vocab: set[str]) -> float:
    """1 si le mot (ou un mot très proche) est dans le vocabulaire, 0 sinon."""
    if word in vocab:
        return 1.0
    for v in vocab:
        if len(v) > 3 and len(word) > 3 and (v.startswith(word[:5]) or word.startswith(v[:5])):
            if difflib.SequenceMatcher(None, word, v).ratio() >= 0.75:
                return 0.9
    return 0.0


def _norm(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return {w for w in re.findall(r"[a-z0-9]+", s) if len(w) > 2}


# --------------------------------------------------------------------------- #
# Stockage
# --------------------------------------------------------------------------- #
class Store:
    """Une fiche = un dossier data/fiches/<id>/ avec l'original, le texte, les questions, les stats."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _dir(self, fid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", fid or ""):
            raise KeyError(fid)
        d = self.root / fid
        if not d.is_dir():
            raise KeyError(fid)
        return d

    @staticmethod
    def _read(path: Path, default):
        try:
            return json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    @staticmethod
    def _write(path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)

    # --- Fiches ------------------------------------------------------------ #
    def add(self, filename: str, data: bytes, subject: str = "", title: str = "") -> dict:
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED:
            raise FicheError("Format non pris en charge (PDF, Word, PowerPoint, Markdown ou texte).")
        fid = uuid.uuid4().hex[:12]
        d = self.root / fid
        d.mkdir()
        original = d / f"original{ext}"
        original.write_bytes(data)
        try:
            text = extract_text(original)
        except FicheError:
            shutil.rmtree(d)
            raise
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(d)
            raise FicheError(f"Impossible de lire ce fichier ({e.__class__.__name__}).") from e
        if len(re.sub(r"--- Page \d+ ---|## Diapo \d+|\s", "", text)) < 80:
            shutil.rmtree(d)
            raise FicheError("Presque aucun texte trouvé. Si c'est un PDF scanné, exporte-le en texte d'abord.")
        (d / "text.md").write_text(text[:MAX_TEXT], "utf-8")
        meta = {
            "id": fid,
            "filename": filename,
            "subject": subject.strip(),
            "title": title.strip() or Path(filename).stem.replace("_", " "),
            "auto_title": not title.strip(),
            "added": int(time.time()),
            "chars": len(text),
            "truncated": len(text) > MAX_TEXT,
            "status": "generating",
            "error": None,
            "n_questions": 0,
        }
        self._write(d / "meta.json", meta)
        return meta

    def get(self, fid: str) -> dict:
        return self._read(self._dir(fid) / "meta.json", {})

    def text(self, fid: str) -> str:
        return (self._dir(fid) / "text.md").read_text("utf-8")

    def update(self, fid: str, **fields) -> dict:
        with self._lock:
            path = self._dir(fid) / "meta.json"
            meta = self._read(path, {})
            meta.update(fields)
            self._write(path, meta)
            return meta

    def delete(self, fid: str) -> None:
        shutil.rmtree(self._dir(fid))

    def list(self) -> list[dict]:
        out = []
        for d in self.root.iterdir():
            meta = self._read(d / "meta.json", None) if d.is_dir() else None
            if not meta:
                continue
            stats = self._read(d / "stats.json", {})
            asked = sum(s["asked"] for s in stats.values())
            good = sum(s["good"] + 0.5 * s["partial"] for s in stats.values())
            meta["answers"] = asked
            meta["success"] = round(100 * good / asked) if asked else None
            out.append(meta)
        return sorted(out, key=lambda m: m["added"], reverse=True)

    # --- Questions --------------------------------------------------------- #
    def set_questions(self, fid: str, bank: dict) -> None:
        questions = [q for q in bank.get("questions", []) if q.get("q") and q.get("reponse")]
        for i, q in enumerate(questions, 1):
            q["id"] = f"q{i}"
        self._write(self._dir(fid) / "questions.json", {**bank, "questions": questions})
        fields = {"status": "ready", "n_questions": len(questions), "error": None,
                  "summary": bank.get("resume", "")}
        if self.get(fid).get("auto_title") and bank.get("titre"):
            fields["title"] = bank["titre"].strip()  # titre déduit du contenu plutôt que du nom de fichier
        self.update(fid, **fields)

    def questions(self, fid: str) -> list[dict]:
        return self._read(self._dir(fid) / "questions.json", {}).get("questions", [])

    # --- Suivi des réponses ------------------------------------------------- #
    def stats(self, fid: str) -> dict:
        return self._read(self._dir(fid) / "stats.json", {})

    def record(self, fid: str, qid: str, verdict: str) -> None:
        with self._lock:
            path = self._dir(fid) / "stats.json"
            stats = self._read(path, {})
            s = stats.setdefault(qid, {"asked": 0, "good": 0, "partial": 0, "bad": 0, "last": 0})
            s["asked"] += 1
            s[{"juste": "good", "partiel": "partial"}.get(verdict, "bad")] += 1
            s["last"] = int(time.time())
            self._write(path, stats)

    # --- Choix ---------------------------------------------------------------- #
    def find(self, topic: str) -> dict | None:
        """Trouve la fiche qui correspond le mieux au sujet prononcé (tolère « transformeurs »/« transformers »)."""
        words = _norm(topic) - STOPWORDS
        if not words:
            return None
        best, best_score = None, 0.0
        for meta in self.list():
            if meta["status"] != "ready":
                continue
            title_words = _norm(f"{meta['title']} {meta['subject']} {Path(meta['filename']).stem}")
            other_words = _norm(meta.get("summary", ""))
            try:
                other_words |= _norm(self.text(meta["id"])[:3000])
            except KeyError:
                continue
            score = sum(3 * _similar(w, title_words) + _similar(w, other_words) for w in words)
            if score > best_score:
                best, best_score = meta, score
        return best if best_score >= 0.75 else None

    def latest_ready(self) -> dict | None:
        return next((m for m in self.list() if m["status"] == "ready"), None)


def pick_question(questions: list[dict], stats: dict, exclude: set[str]) -> dict | None:
    """Répétition espacée simple : priorité aux questions jamais posées, puis ratées, puis anciennes."""
    pool = [q for q in questions if q["id"] not in exclude] or [q for q in questions]
    if not pool:
        return None
    now = time.time()
    weights = []
    for q in pool:
        s = stats.get(q["id"])
        if not s:
            w = 4.0
        else:
            miss = (s["bad"] + 0.5 * s["partial"]) / max(s["asked"], 1)
            age_days = (now - s["last"]) / 86400
            w = 0.5 + 3 * miss + min(age_days, 7) / 7
        weights.append(w)
    return random.choices(pool, weights=weights, k=1)[0]
