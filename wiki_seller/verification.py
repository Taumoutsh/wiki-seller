"""Vérification humaine (Turnstile) faite par une PERSONNE : le script prévient,
affiche la fenêtre du site et attend qu'elle disparaisse. Il ne résout rien."""

import logging
import time
import urllib.request

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

# Fenêtre affichée par WikiMasters (sur le sandbox : "#modal").
VERIFICATION_MODAL = "text=Vérification rapide"


def wait_for_verification(page: Page, timeout_s: float, modal_selector: str = VERIFICATION_MODAL) -> bool:
    """Attend que la fenêtre de vérification disparaisse (faite par un humain).
    Renvoie False si elle est toujours là après timeout_s."""
    modal = page.locator(modal_selector).first
    if not modal.is_visible():
        return True
    log.info("Vérification humaine demandée ; attente (au plus %.0f min)…", timeout_s / 60)
    start = time.monotonic()
    try:
        modal.wait_for(state="hidden", timeout=timeout_s * 1000)
    except PlaywrightTimeout:
        log.warning("Vérification non faite à temps.")
        return False
    log.info("Vérification faite en %.0f s.", time.monotonic() - start)
    return True


def notify(url: str | None, message: str) -> None:
    """Notification push (ntfy : https://ntfy.sh/<sujet>) ; silencieux si pas d'URL."""
    if not url:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=message.encode()), timeout=10)
    except Exception as exc:
        log.warning("Notification non envoyée : %s", exc)
