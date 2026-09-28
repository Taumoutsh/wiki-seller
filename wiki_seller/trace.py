"""Journal des appels à l'API du site (--trace-api / API_TRACE=1), pour connaître les
requêtes que fait la page (ex. ce qu'envoie « Miser ») : méthode, chemin, corps envoyé,
code de retour et début de la réponse, une ligne JSON par appel dans state/api.log.

Jamais d'en-têtes ni de cookies ; les champs qui ressemblent à des secrets sont masqués."""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAX_BODY = 4000
SECRET_KEY = re.compile(r"token|password|passwd|secret|session|cookie|authorization|apikey|api_key", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "***" if SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _body(text: str | None):
    if not text:
        return None
    try:
        data = redact(json.loads(text))
        text = json.dumps(data, ensure_ascii=False)
    except ValueError:
        pass
    return text if len(text) <= MAX_BODY else text[:MAX_BODY] + f"… ({len(text)} caractères)"


def install_api_trace(context, base_url: str, path: Path) -> None:
    host = urlsplit(base_url).netloc
    path.parent.mkdir(parents=True, exist_ok=True)

    def on_finished(request) -> None:
        url = urlsplit(request.url)
        if url.netloc != host or not url.path.startswith("/api/"):
            return
        try:
            response = request.response()
            status = response.status if response else None
            try:
                answer = response.text() if response else None
            except Exception:
                answer = None
            entry = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "method": request.method,
                "path": url.path + (f"?{url.query}" if url.query else ""),
                "sent": _body(request.post_data),
                "status": status,
                "received": _body(answer),
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # le journal ne doit jamais gêner la passe
            log.debug("Trace API impossible : %s", exc)

    context.on("requestfinished", on_finished)
    log.info("Trace des appels API dans %s.", path)
