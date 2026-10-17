"""Comptes du bot : un par conversation Telegram (chat_id), ajoutés à la main par
l'administrateur (TELEGRAM_CHAT_ID). La liste est dans state/users.json ; les fichiers de
chaque compte (session, listes de cartes, suivis, bilans) dans state/users/<chat_id>/.
Le cache des prix, l'état du bot et le journal restent communs, dans state/.

Au premier démarrage, les fichiers de l'ancienne version (un seul compte, dans state/)
sont déplacés dans le dossier de l'administrateur."""

import dataclasses
import logging
import re
import threading
from dataclasses import dataclass

from . import settings
from .config import Config
from .state import now_utc, read_json, write_json

log = logging.getLogger(__name__)

LOCAL = "local"  # compte unique, sans bot Telegram
CHAT_ID = re.compile(r"^-?\d{1,20}$")
# Fichiers d'un compte (les autres fichiers de state/ sont communs).
ACCOUNT_FILES = ("session.json", "storage_state.json", "session_cookies.txt", "safe_cards.json",
                 "buy_cards.json", "sell_cards.json", "wanted_state.json", "unsold.json", "run_state.json",
                 "report.json", "discard_progress.json", "debug")
_LOCK = threading.RLock()


@dataclass(frozen=True)
class Account:
    chat_id: str
    name: str
    added_at: str = ""


def _file(base: Config):
    return base.state_dir / "users.json"


def admin_id(base: Config) -> str:
    return base.telegram_chat_id or LOCAL


def accounts(base: Config) -> list[Account]:
    with _LOCK:
        data = read_json(_file(base), {})
    return [Account(chat_id, info.get("name", ""), info.get("added_at", ""))
            for chat_id, info in (data.get("users") or {}).items()]


def find(base: Config, chat_id: str) -> Account | None:
    return next((a for a in accounts(base) if a.chat_id == str(chat_id)), None)


def add(base: Config, chat_id: str, name: str) -> bool:
    """Ajoute un compte ; False s'il existait déjà (son nom est alors mis à jour)."""
    chat_id = str(chat_id)
    if not CHAT_ID.match(chat_id):
        raise ValueError(f"identifiant Telegram invalide : {chat_id!r}")
    with _LOCK:
        data = read_json(_file(base), {})
        users = data.setdefault("users", {})
        new = chat_id not in users
        entry = users.setdefault(chat_id, {"name": "", "added_at": now_utc().isoformat()})
        if name:
            entry["name"] = name
        write_json(_file(base), data)
    account_dir(base, chat_id).mkdir(parents=True, exist_ok=True)
    if new:
        config = account_config(base, chat_id)
        settings.init_new_account(config)  # en pause, ni vente automatique ni défausse
        if not (config.state_dir / "safe_cards.json").exists():  # aucune carte protégée au départ
            write_json(config.state_dir / "safe_cards.json", [])
    return new


def remove(base: Config, chat_id: str) -> bool:
    """Retire un compte de la liste ; ses fichiers sont gardés."""
    with _LOCK:
        data = read_json(_file(base), {})
        if str(chat_id) not in (data.get("users") or {}):
            return False
        del data["users"][str(chat_id)]
        write_json(_file(base), data)
        return True


def account_dir(base: Config, chat_id: str):
    return base.state_dir / "users" / str(chat_id)


def account_config(base: Config, chat_id: str) -> Config:
    """Réglages d'un compte : ses fichiers dans state/users/<chat_id>/, ses messages dans
    sa conversation. Les anciens fichiers de listes ne concernent que l'administrateur."""
    chat_id = str(chat_id)
    folder = account_dir(base, chat_id)
    account = find(base, chat_id)
    changes = {"state_dir": folder, "shared_dir": base.state_dir,
               "telegram_chat_id": "" if chat_id == LOCAL else chat_id,
               "account_name": (account.name if account else "") or chat_id}
    if chat_id != admin_id(base):
        changes.update(safe_cards_file=folder / "none", wanted_cards_file=folder / "none", session_cookies="")
    config = dataclasses.replace(base, **changes)
    return dataclasses.replace(config, **settings.apply(config))


def migrate(base: Config) -> None:
    """Premier démarrage multi-comptes : l'administrateur devient le premier compte, et les
    fichiers de l'ancienne version (dans state/) passent dans son dossier."""
    with _LOCK:
        if _file(base).exists():
            return
        admin = admin_id(base)
        folder = account_dir(base, admin)
        folder.mkdir(parents=True, exist_ok=True)
        moved = []
        for name in ACCOUNT_FILES:
            source = base.state_dir / name
            if source.exists() and not (folder / name).exists():
                source.rename(folder / name)
                moved.append(name)
        write_json(_file(base), {"users": {admin: {"name": "admin", "added_at": now_utc().isoformat()}}})
        if moved:
            log.info("Comptes : fichiers de l'ancienne version déplacés dans %s (%s).", folder, ", ".join(moved))
