#!/usr/bin/env bash
# Met à jour la session WikiMasters du serveur :
#   ./update_session.sh            puis coller les cookies et valider par une ligne vide
#   ./update_session.sh --no-restart   sans relancer le conteneur
#
# Les cookies viennent du favori décrit dans le README (« sb-…-auth-token.0=…; sb-…-auth-token.1=… »),
# sur une ou plusieurs lignes. Ils ne s'affichent pas pendant la saisie.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env"
STATE_FILE="state/storage_state.json"
restart=1
[[ "${1:-}" == "--no-restart" ]] && restart=0

[[ -f "$ENV_FILE" ]] || { echo "Fichier $ENV_FILE introuvable (copiez .env.example)." >&2; exit 1; }

echo "Collez les cookies (les morceaux .0 et .1), puis appuyez sur Entrée sur une ligne vide :"
cookies=""
while IFS= read -rs line; do
    line="${line//$'\r'/}"
    [[ -z "${line// /}" ]] && break
    cookies+="${cookies:+;}${line}"
done
# Une seule ligne sans espaces : « nom=valeur;nom2=valeur2 ».
cookies="$(printf '%s' "$cookies" | tr -d ' \t' | sed 's/;;*/;/g; s/^;//; s/;$//')"

count="$(printf '%s' "$cookies" | tr ';' '\n' | grep -c 'auth-token[.0-9]*=' || true)"
if [[ "$count" -lt 1 ]]; then
    echo "Aucun cookie « …auth-token…=… » reconnu : rien n'a été modifié." >&2
    exit 1
fi
if ! printf '%s' "$cookies" | grep -q 'auth-token\.0='; then
    echo "Attention : le morceau « auth-token.0 » manque, la session risque d'être incomplète." >&2
fi

# Réécrit (ou ajoute) la ligne SESSION_COOKIES, sans toucher au reste du fichier.
tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
COOKIES="$cookies" awk '
    BEGIN { done = 0 }
    /^[[:space:]]*#?[[:space:]]*SESSION_COOKIES=/ && !done { print "SESSION_COOKIES=" ENVIRON["COOKIES"]; done = 1; next }
    { print }
    END { if (!done) print "SESSION_COOKIES=" ENVIRON["COOKIES"] }
' "$ENV_FILE" > "$tmp"
chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"
echo "SESSION_COOKIES mis à jour dans $ENV_FILE ($count cookie(s), ${#cookies} caractères)."

# L'ancienne session enregistrée passerait avant les nouveaux cookies.
if [[ -f "$STATE_FILE" ]]; then
    rm -f "$STATE_FILE"
    echo "Ancienne session supprimée ($STATE_FILE)."
fi

if [[ "$restart" -eq 1 ]]; then
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        echo "Relance du conteneur…"
        docker compose up -d
        echo "Vérifiez la connexion avec : docker compose logs -f   (« Connecté avec la session de SESSION_COOKIES »)"
    else
        echo "docker compose introuvable : relancez le script vous-même pour prendre en compte la session."
    fi
fi
