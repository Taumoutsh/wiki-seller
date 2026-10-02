"""Client Playwright du banc d'essai : ouvre des paquets et gère la vérification
humaine PROPREMENT, c'est-à-dire en attendant qu'elle soit faite — par le widget
lui-même avec la clé de test « pass », ou par une personne avec la clé « interactive »
(lancer alors avec --headed, ou via un écran distant, et cocher la case soi-même).

Le script ne résout rien : il détecte, attend, puis reprend. C'est cette partie-là qui
se porte telle quelle dans le projet principal (voir sandbox/README.md).

    python sandbox/client.py --packs 5
    python sandbox/client.py --packs 5 --headed --wait 300    # défi interactif : à vous de cocher
"""

import argparse
import os
import time

from playwright.sync_api import sync_playwright

URL = os.getenv("SANDBOX_URL", "http://127.0.0.1:8765/")


def wait_for_verification(page, timeout_s: float) -> bool:
    """La fenêtre de vérification est affichée : attendre qu'elle disparaisse (jeton
    accepté par le serveur) ou abandonner après timeout_s."""
    modal = page.locator("#modal")
    print(f"[client] Vérification demandée par le site ; attente (au plus {timeout_s:.0f} s)…")
    start = time.monotonic()
    try:
        modal.wait_for(state="hidden", timeout=timeout_s * 1000)
    except Exception:
        print("[client] Vérification non faite à temps : on s'arrête là (on ne force rien).")
        return False
    print(f"[client] Vérification faite en {time.monotonic() - start:.1f} s.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packs", type=int, default=5)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait", type=float, default=30, help="secondes d'attente de la vérification")
    args = parser.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed,
                                     executable_path=os.getenv("CHROMIUM_EXECUTABLE") or None)
        page = browser.new_page()
        page.on("console", lambda m: None)
        page.goto(URL)
        opened = 0
        while opened < args.packs:
            before = page.locator("#count").inner_text()
            page.locator("#open").click()
            # Soit le paquet s'ouvre (compteur qui baisse), soit la vérification s'affiche.
            for _ in range(50):
                page.wait_for_timeout(100)
                if page.locator("#modal").is_visible() or page.locator("#count").inner_text() != before:
                    break
            if page.locator("#modal").is_visible():
                if not wait_for_verification(page, args.wait):
                    break
                # La page réessaie elle-même l'ouverture après la vérification.
                page.wait_for_function(f"() => document.getElementById('count').textContent !== '{before}'",
                                       timeout=10000)
            opened += 1
            print(f"[client] Paquet {opened} ouvert ; reste {page.locator('#count').inner_text()}.")
            page.wait_for_timeout(500)
        print("[client] Journal de la page :\n" + page.locator("#log").inner_text())
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
