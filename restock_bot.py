#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot d'alertes restock et nouveautés - TCG Pokémon / One Piece.
Boutiques gérées : UltraJeux, Micromania, Play-in.

Deux surveillances tournent en parallèle :

  1. PRODUITS   -> alerte quand une fiche repasse EN STOCK.
  2. RAYONS     -> alerte quand un NOUVEAU produit apparaît dans un rayon.
                   C'est ça qui attrape les ouvertures de précommande.

Pour ajouter un produit : colle simplement l'URL de sa fiche dans PRODUITS.
Le bot reconnaît la boutique tout seul et déduit le nom depuis l'adresse.

Installation :
    python -m pip install requests
Lancement :
    python restock_bot.py
Test sans rien envoyer sur Telegram :
    python restock_bot.py --dry-run
"""

import json
import os
import random
import re
import sys
import time
from urllib.parse import urlparse

import requests
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# 1. TELEGRAM
# ---------------------------------------------------------------------------
# Si tu publies ce fichier un jour, vide les valeurs par défaut ci-dessous et
# passe par les variables d'environnement TG_TOKEN et TG_CHAT.
def _lire_secrets() -> tuple[str, str]:
    """Cherche les identifiants dans les variables d'environnement, sinon dans
    un fichier secrets.local (2 lignes : le token, puis le chat id).
    Ce fichier est ignore par git, il ne partira jamais sur GitHub."""
    token = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT", "")
    if token and chat:
        return token, chat
    if os.path.exists("secrets.local"):
        lignes = [l.strip() for l in open("secrets.local", encoding="utf-8") if l.strip()]
        if len(lignes) >= 2:
            return lignes[0], lignes[1]
    return token, chat


TELEGRAM_TOKEN, TELEGRAM_CHAT_ID = _lire_secrets()

# ---------------------------------------------------------------------------
# 2. DÉTECTEURS DE STOCK
# ---------------------------------------------------------------------------
# Micromania et Play-in publient la disponibilité au format standard
# schema.org. UltraJeux a son propre balisage, avec deux mises en page.

MOTIF_SCHEMA = re.compile(
    r'availability[\s\\"\':=hrefHREF]{0,40}https?://schema\.org/'
    r'(InStock|OutOfStock|PreOrder|BackOrder|SoldOut|Discontinued)', re.I)

UJ_PRINCIPAL = re.compile(r'font-size:\s*18px[^"\']*"[^>]*>\s*((?:In)?Disponible)\s*<', re.I)
UJ_VARIANTE = re.compile(r'>\s*((?:In)?Disponible)\s*</b>', re.I)


def stock_schema(html: str) -> bool | None:
    trouve = MOTIF_SCHEMA.search(html)
    if not trouve:
        return None
    return trouve.group(1).lower() == "instock"


def stock_ultrajeux(html: str) -> bool | None:
    for motif in (UJ_PRINCIPAL, UJ_VARIANTE):
        trouve = motif.search(html)
        if trouve:
            return trouve.group(1).lower() == "disponible"
    return None


# ---------------------------------------------------------------------------
# 3. BOUTIQUES CONNUES
# ---------------------------------------------------------------------------
BOUTIQUES = {
    "ultrajeux.com": {
        "nom": "UltraJeux",
        "stock": stock_ultrajeux,
        "motif_lien": r'href="(produit-\d+-[^"]+\.html)"',
        "prefixe": "https://www.ultrajeux.com/",
        "accueil": "https://www.ultrajeux.com/",
        "cadence": 0,        # a chaque tour, ce site est tolerant
    },
    "micromania.fr": {
        "nom": "Micromania",
        "stock": stock_schema,
        "motif_lien": r'href="https://www\.micromania\.fr/(p/[^"]+\.html)"',
        "prefixe": "https://www.micromania.fr/",
        "accueil": "https://www.micromania.fr/",
        "cadence": 1200,     # 20 min : protege par Imperva, tres sensible
    },
    "play-in.com": {
        "nom": "Play-in",
        "stock": stock_schema,
        "motif_lien": r'(produit/\d+/[a-z0-9-]{5,80})',
        "prefixe": "https://www.play-in.com/fr/",
        "accueil": "https://www.play-in.com/fr/",
        "cadence": 300,      # 5 min, par prudence
    },
}


def boutique_de(url: str) -> dict | None:
    hote = urlparse(url).netloc.lower()
    for domaine, conf in BOUTIQUES.items():
        if hote.endswith(domaine):
            return conf
    return None


def nom_depuis_url(url: str) -> str:
    """Déduit un nom lisible depuis l'adresse, pour ne rien avoir à saisir."""
    bout = urlparse(url).path.rstrip("/").split("/")[-1]
    bout = re.sub(r"\.html?$", "", bout)
    bout = re.sub(r"^produit-\d+-", "", bout)
    bout = re.sub(r"-\d{6,14}$", "", bout)
    bout = re.sub(r"-[a-z]{2}$", "", bout)
    return bout.replace("-", " ").strip().capitalize() or url


# ---------------------------------------------------------------------------
# 4. FICHES À SURVEILLER
# ---------------------------------------------------------------------------
# Liste établie par un scan réel des trois boutiques : ce sont des références
# effectivement en rupture. Pour en ajouter, colle l'URL. Pour en retirer,
# mets un # devant la ligne.

PRODUITS = [
    # --- UltraJeux : One Piece ---
    "https://www.ultrajeux.com/produit-31828-illustration-box-ib-05--0810158837850.html",
    "https://www.ultrajeux.com/produit-31829-illustration-box-ib-06--0810158837881.html",
    "https://www.ultrajeux.com/produit-32340-japanese-3rd-anniversary-set-810158838536.html",
    "https://www.ultrajeux.com/produit-31086-prb-02-premium-one-piece-card-the-best-4582769865527.html",
    "https://www.ultrajeux.com/produit-32086-eb03-one-piece-heroines-edition-4582769937330.html",
    "https://www.ultrajeux.com/produit-31936-ts02-tin-pack-monkey-d-luffy-4582769865640.html",
    "https://www.ultrajeux.com/produit-31938-ts02-tin-pack-portgas-dace-4582769865640.html",
    "https://www.ultrajeux.com/produit-32341-op15-dp-10-adventure-on-kami-s-island-0810158838482.html",
    "https://www.ultrajeux.com/produit-32610-double-pack-op16-l-heure-de-la-bataille-decisive-en-francais-4582770058734.html",
    "https://www.ultrajeux.com/produit-32612-st30-luffy-ace-4582769982606.html",
    "https://www.ultrajeux.com/produit-32579-op14-les-sept-de-la-mer-d-azur-blister-4582769923166.html",

    # --- Micromania : Pokémon ---
    "https://www.micromania.fr/p/display-36-boosters-pokemon-ev04-paradox-rift-version-anglaise-146164.html",
    "https://www.micromania.fr/p/booster-pokemon-premium-checklane-ev02-evolution-a-paldea-version-anglaise-143166.html",
    # --- Micromania : One Piece ---
    "https://www.micromania.fr/p/booster-display-one-piece-op16-161507.html",
    "https://www.micromania.fr/p/booster-bipack-one-piece-op14-156814.html",
    "https://www.micromania.fr/p/booster-one-piece-prb-02-premium-booster-153601.html",
    "https://www.micromania.fr/p/booster-one-piece-op12-151818.html",

    # --- Play-in : Pokémon ---
    "https://www.play-in.com/fr/produit/650372/coffret-dresseur-d-elite-etb-mega-evolution-nuit-noire-pokemon-fr",
    "https://www.play-in.com/fr/produit/650369/display-de-36-boosters-mega-evolution-nuit-noire-pokemon-fr",
    "https://www.play-in.com/fr/produit/650373/bundle-6-boosters-mega-evolution-nuit-noire-pokemon-fr",
    # --- Play-in : One Piece ---
    "https://www.play-in.com/fr/produit/646300/display-de-24-boosters-op-16-l-heure-de-la-bataille-decisive-one-piece-fr",
    "https://www.play-in.com/fr/produit/652672/double-pack-set-11-l-heure-de-la-bataille-decisive-op-16-one-piece-en",
    "https://www.play-in.com/fr/produit/638158/double-pack-set-10-aventure-sur-l-ile-de-dieu-op-15-one-piece-fr",
    "https://www.play-in.com/fr/produit/652495/deck-de-demarrage-ex-luffy-et-ace-st-30-one-piece-en",
]

# ---------------------------------------------------------------------------
# 5. RAYONS À SURVEILLER (nouveautés et précommandes)
# ---------------------------------------------------------------------------
RAYONS = [
    "https://www.ultrajeux.com/jeu-4-pokemon.html",
    "https://www.ultrajeux.com/jeu-1031-one-piece-card-game.html",
    "https://www.micromania.fr/c/cartespokemon",
    "https://www.micromania.fr/c/cartes-one-piece",
    "https://www.play-in.com/fr/gamme/3/pokemon/catalogue",
    "https://www.play-in.com/fr/gamme/24/one-piece/catalogue",
]

# ---------------------------------------------------------------------------
# 6. RÉGLAGES
# ---------------------------------------------------------------------------
INTERVALLE = 300         # secondes entre deux tours complets (5 min)
JITTER = 45              # variation aléatoire, pour avoir l'air moins robotique
PAUSE_ENTRE_PAGES = 2    # secondes entre deux pages, pour rester poli
FICHIER_ETAT = "etat_stock.json"

ENTETES = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Chromium";v="126", "Not)A;Brand";v="24", "Google Chrome";v="126"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

# Une session par boutique : elle garde les cookies, comme un vrai navigateur.
# Au premier appel on passe par la page d'accueil pour se faire connaitre.
SESSIONS: dict[str, requests.Session] = {}
ECHECS: dict[str, int] = {}
SEUIL_ABANDON = 3      # au-dela, on met la boutique en pause pour ce tour
DERNIER_PASSAGE: dict[str, float] = {}


def trop_tot(conf: dict) -> bool:
    """Respecte la cadence propre a chaque boutique, pour ne pas se faire bannir.
    Les horodatages vivent dans le fichier d'etat, donc la cadence tient aussi
    quand le script est relance de zero a chaque fois (cas de GitHub Actions)."""
    cadence = conf.get("cadence", 0)
    if not cadence:
        return False
    dernier = DERNIER_PASSAGE.get(conf["nom"], 0)
    return (time.time() - dernier) < cadence


def session_de(conf: dict) -> requests.Session:
    cle = conf["nom"]
    if cle not in SESSIONS:
        s = requests.Session()
        s.headers.update(ENTETES)
        try:
            s.get(conf["accueil"], timeout=25)
            time.sleep(1)
        except requests.RequestException:
            pass
        SESSIONS[cle] = s
    return SESSIONS[cle]


DRY_RUN = "--dry-run" in sys.argv
# Un seul tour puis on sort : c'est le mode utilise par GitHub Actions,
# qui relance le script a chaque passage au lieu de le laisser tourner.
UNE_FOIS = "--une-fois" in sys.argv or os.environ.get("GITHUB_ACTIONS") == "true"


# ---------------------------------------------------------------------------
def envoyer_telegram(message: str) -> None:
    if DRY_RUN:
        print(f"\n--- [DRY RUN] message qui aurait été envoyé ---\n{message}\n")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[!] Envoi Telegram échoué : {e}")


def charger_etat() -> dict:
    if os.path.exists(FICHIER_ETAT):
        try:
            with open(FICHIER_ETAT, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("[!] Fichier d'état illisible, on repart de zéro.")
    return {"stock": {}, "vus": {}, "passages": {}}


def sauver_etat(etat: dict) -> None:
    with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=2)


def recuperer(url: str, etiquette: str, conf: dict) -> str | None:
    boutique = conf["nom"]
    if ECHECS.get(boutique, 0) >= SEUIL_ABANDON:
        return None

    session = session_de(conf)
    entetes = {"Referer": conf["accueil"], "Sec-Fetch-Site": "same-origin"}
    try:
        r = session.get(url, headers=entetes, timeout=25)
    except requests.RequestException as e:
        print(f"[!] {etiquette} : erreur reseau ({e})")
        ECHECS[boutique] = ECHECS.get(boutique, 0) + 1
        return None

    if r.status_code in (403, 429):
        ECHECS[boutique] = ECHECS.get(boutique, 0) + 1
        if ECHECS[boutique] == SEUIL_ABANDON:
            print(f"[!] {boutique} : bloque cette boutique ({r.status_code}). "
                  f"Mise en pause jusqu'au prochain tour.")
        elif ECHECS[boutique] < SEUIL_ABANDON:
            print(f"[!] {etiquette} : acces refuse ({r.status_code}).")
        return None

    if r.status_code != 200:
        print(f"[!] {etiquette} : code HTTP {r.status_code}")
        return None

    ECHECS[boutique] = 0
    return r.text


# ---------------------------------------------------------------------------
# Une boutique = un fil d'execution. A l'interieur d'une boutique on reste
# lent et poli ; entre boutiques, tout avance en meme temps. Le tour dure donc
# le temps de la plus lente, au lieu de la somme des trois.
# ---------------------------------------------------------------------------
def traiter_boutique(domaine: str, conf: dict, etat: dict) -> tuple[list, dict, dict]:
    """Retourne (messages a envoyer, maj des stocks, maj des rayons vus)."""
    messages: list[str] = []
    maj_stock: dict = {}
    maj_vus: dict = {}

    if trop_tot(conf):
        return messages, maj_stock, maj_vus

    # --- fiches produit ---
    for url in PRODUITS:
        if boutique_de(url) is not conf:
            continue
        nom = nom_depuis_url(url)
        etiquette = f"{conf['nom']} - {nom}"
        page = recuperer(url, etiquette, conf)
        time.sleep(PAUSE_ENTRE_PAGES)
        if page is None:
            continue

        dispo = conf["stock"](page)
        if dispo is None:
            print(f"[?] {etiquette} : stock illisible, fiche ignoree.")
            continue

        ancien = etat["stock"].get(url)
        if dispo and ancien is not True:
            messages.append(f"\U0001F514 RESTOCK - {conf['nom']}\n\n{nom}\n{url}")
            print(f"[+] ALERTE RESTOCK : {etiquette}")
        elif not dispo and ancien is True:
            print(f"[-] Repasse en rupture : {etiquette}")
        else:
            print(f"    {etiquette} : {'dispo' if dispo else 'rupture'}")
        maj_stock[url] = dispo

    # --- rayons ---
    for url in RAYONS:
        if boutique_de(url) is not conf:
            continue
        etiquette = f"{conf['nom']} - rayon"
        page = recuperer(url, etiquette, conf)
        time.sleep(PAUSE_ENTRE_PAGES)
        if page is None:
            continue

        liens = {conf["prefixe"] + l.lstrip("/")
                 for l in re.findall(conf["motif_lien"], page)}
        if not liens:
            print(f"[!] {etiquette} : aucun lien produit trouve.")
            continue

        connus = etat["vus"].get(url)
        if connus is None:
            maj_vus[url] = sorted(liens)
            print(f"    {etiquette} : {len(liens)} produits memorises (1er passage).")
            continue

        nouveaux = sorted(liens - set(connus))
        for lien in nouveaux:
            messages.append(
                f"\U0001F195 NOUVEAUTE - {conf['nom']}\n\n{nom_depuis_url(lien)}\n{lien}")
            print(f"[+] ALERTE NOUVEAUTE : {lien}")
        if not nouveaux:
            print(f"    {etiquette} : rien de neuf ({len(liens)} produits).")
        maj_vus[url] = sorted(set(connus) | liens)

    return messages, maj_stock, maj_vus


def faire_un_tour(etat: dict) -> None:
    with ThreadPoolExecutor(max_workers=len(BOUTIQUES)) as executeur:
        resultats = list(executeur.map(
            lambda item: traiter_boutique(item[0], item[1], etat),
            BOUTIQUES.items()))

    # On applique les changements et on envoie les alertes depuis le fil
    # principal : pas de collision possible sur le fichier d'etat.
    for messages, maj_stock, maj_vus in resultats:
        etat["stock"].update(maj_stock)
        etat["vus"].update(maj_vus)
        for message in messages:
            envoyer_telegram(message)

    for conf in BOUTIQUES.values():
        if not trop_tot(conf):
            DERNIER_PASSAGE[conf["nom"]] = time.time()


# ---------------------------------------------------------------------------
def main() -> None:
    etat = charger_etat()
    etat.setdefault("stock", {})
    etat.setdefault("vus", {})
    etat.setdefault("passages", {})
    DERNIER_PASSAGE.update(etat["passages"])

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Identifiants Telegram absents. Renseigne TG_TOKEN et TG_CHAT,")
        print("    ou cree un fichier secrets.local avec le token puis le chat id.")
        if not DRY_RUN:
            sys.exit(1)

    mode = " (DRY RUN, rien ne part sur Telegram)" if DRY_RUN else ""
    print(f"Surveillance démarrée{mode}.")
    print(f"{len(PRODUITS)} fiches et {len(RAYONS)} rayons suivis.\n")

    while True:
        print(f"--- Tour de {time.strftime('%H:%M:%S')} ---")
        ECHECS.clear()
        faire_un_tour(etat)
        etat["passages"] = dict(DERNIER_PASSAGE)
        sauver_etat(etat)
        print()
        if UNE_FOIS:
            return
        time.sleep(INTERVALLE + random.randint(0, JITTER))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nArrêt.")
