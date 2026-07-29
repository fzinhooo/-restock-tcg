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
import subprocess
import sys
import time
from urllib.parse import urlparse

import requests
from concurrent.futures import ThreadPoolExecutor

VERSION = "v8-blazingtail"

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


TELEGRAM_TOKEN, _CHATS_BRUT = _lire_secrets()

# Plusieurs destinataires possibles : separe les identifiants par des virgules
# dans le secret TG_CHAT. Exemple : 7407116210,123456789
TELEGRAM_CHATS = [c.strip() for c in re.split(r"[,;\s]+", _CHATS_BRUT) if c.strip()]

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
        # Les pages catalogue de Play-in melangent des mises en avant d'autres
        # jeux (jeux de plateau, Lorcana...). On ne garde que les liens dont
        # l'adresse mentionne le jeu du rayon.
        "filtrer_par_jeu": True,
    },
    "blazingtail.fr": {
        "nom": "Blazingtail",
        "stock": stock_schema,
        "motif_lien": r'href="https://www\.blazingtail\.fr/(\d+-[^"#]+?\.html)"',
        "prefixe": "https://www.blazingtail.fr/",
        "accueil": "https://www.blazingtail.fr/",
        "cadence": 300,
    },
    "kingdultes.com": {
        "nom": "KingDultes",
        "type": "shopify",
        "base": "https://www.kingdultes.com",
        "accueil": "https://www.kingdultes.com/",
        "cadence": 300,
        # Pas de liste de produits a tenir : on surveille le rayon entier,
        # ruptures et nouveautes comprises.
        "collections": [
            "tcg-pokemon",
            "tcg-one-piece",
            "les-nouveautes-preco-tcg",
        ],
    },
}


JEUX = ("one-piece", "pokemon")


def jeu_du_rayon(url: str) -> str | None:
    """Devine de quel jeu parle une page rayon, d'apres son adresse."""
    minuscule = url.lower()
    for jeu in JEUX:
        if jeu in minuscule:
            return jeu
    return None


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
    # Blazingtail : petit catalogue de scelle, surtout du Pokemon.
    "https://www.blazingtail.fr/1318-booster-pokemon",
    "https://www.blazingtail.fr/1319-etb-pokemon",
    "https://www.blazingtail.fr/1320-tripack-pokemon",
    "https://www.blazingtail.fr/1321-display-pokemon",
    "https://www.blazingtail.fr/1324-coffret-pokemon",
    "https://www.blazingtail.fr/2060-nouveautes",

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
EN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
UNE_FOIS = "--une-fois" in sys.argv

# GitHub ne respecte pas le minuteur : les tours planifies sont retardes, et
# parfois abandonnes quand leurs serveurs sont charges. On ne compte donc plus
# sur lui pour relancer le bot toutes les 10 min. Une execution vit plusieurs
# heures et enchaine les tours elle-meme ; le minuteur ne sert plus qu'a
# rallumer la mèche quand la precedente s'eteint.
DUREE_MAX = 5 * 3600 + 20 * 60      # 5 h 20, sous la limite de 6 h de GitHub
DEBUT = time.time()

# Sauvegarde de l'etat sur GitHub : au moins tous les quarts d'heure, et tout
# de suite apres une alerte, pour ne rien perdre si le tour est interrompu.
INTERVALLE_SAUVEGARDE = 900
_derniere_sauvegarde = 0.0


def _git(*arguments) -> int:
    return subprocess.run(["git", *arguments],
                          capture_output=True, text=True).returncode


def pousser_etat(force: bool = False) -> None:
    """Enregistre etat_stock.json sur GitHub. Sans effet hors GitHub."""
    global _derniere_sauvegarde
    if not EN_ACTIONS:
        return
    if not force and time.time() - _derniere_sauvegarde < INTERVALLE_SAUVEGARDE:
        return

    _git("config", "user.name", "bot")
    _git("config", "user.email", "bot@users.noreply.github.com")
    _git("add", "etat_stock.json")
    if _git("diff", "--cached", "--quiet") == 0:
        _derniere_sauvegarde = time.time()
        return                      # rien n'a change, inutile de commiter

    horodatage = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    _git("commit", "-m", f"etat {horodatage}")
    _git("pull", "--rebase", "--quiet")
    if _git("push", "--quiet") != 0:
        print("[!] Sauvegarde sur GitHub echouee, on reessaiera au prochain tour.")
    _derniere_sauvegarde = time.time()


# ---------------------------------------------------------------------------
def envoyer_telegram(message: str) -> None:
    if DRY_RUN:
        print(f"\n--- [DRY RUN] message qui aurait été envoyé ---\n{message}\n")
        return
    for chat in TELEGRAM_CHATS:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": chat, "text": message},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[!] Envoi vers {chat} refuse ({r.status_code}). "
                      f"Ce destinataire a-t-il bien demarre une conversation "
                      f"avec le bot ?")
        except requests.RequestException as e:
            print(f"[!] Envoi Telegram vers {chat} échoué : {e}")


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
def traiter_shopify(conf: dict, etat: dict) -> tuple[list, dict, dict]:
    """Boutiques Shopify : un seul appel par rayon donne le catalogue complet,
    avec le nom et la disponibilite de chaque produit. Pas besoin de lister
    les fiches une par une."""
    messages: list = []
    maj_stock: dict = {}
    maj_vus: dict = {}

    for collection in conf["collections"]:
        url = f"{conf['base']}/collections/{collection}/products.json?limit=250"
        etiquette = f"{conf['nom']} - {collection}"
        page = recuperer(url, etiquette, conf)
        time.sleep(PAUSE_ENTRE_PAGES)
        if page is None:
            continue

        try:
            produits = json.loads(page).get("products", [])
        except ValueError:
            print(f"[!] {etiquette} : reponse illisible, rayon ignore.")
            continue
        if not produits:
            print(f"[!] {etiquette} : rayon vide, ignore.")
            continue

        liens = set()
        for produit in produits:
            lien = f"{conf['base']}/products/{produit['handle']}"
            liens.add(lien)
            nom = produit.get("title") or nom_depuis_url(lien)
            dispo = any(v.get("available") for v in produit.get("variants", []))

            # On n'alerte que sur un vrai passage rupture -> disponible, jamais
            # sur un produit qu'on decouvre : sinon le 1er tour partirait en
            # rafale d'alertes pour tout ce qui est deja en stock.
            if dispo and etat["stock"].get(lien) is False:
                messages.append(
                    (lien, f"\U0001F514 RESTOCK - {conf['nom']}\n\n{nom}\n{lien}"))
                print(f"[+] ALERTE RESTOCK : {conf['nom']} - {nom}")
            maj_stock[lien] = dispo

        connus = etat["vus"].get(url)
        if connus is None:
            maj_vus[url] = sorted(liens)
            print(f"    {etiquette} : {len(liens)} produits memorises (1er passage).")
            continue

        nouveaux = sorted(liens - set(connus))
        for lien in nouveaux:
            messages.append(
                (lien,
                 f"\U0001F195 NOUVEAUTE - {conf['nom']}\n\n{nom_depuis_url(lien)}\n{lien}"))
            print(f"[+] ALERTE NOUVEAUTE : {lien}")
        if not nouveaux:
            print(f"    {etiquette} : rien de neuf ({len(liens)} produits).")
        maj_vus[url] = sorted(set(connus) | liens)

    return messages, maj_stock, maj_vus


def traiter_boutique(domaine: str, conf: dict, etat: dict) -> tuple[list, dict, dict]:
    """Retourne (messages a envoyer, maj des stocks, maj des rayons vus)."""
    messages: list[str] = []
    maj_stock: dict = {}
    maj_vus: dict = {}

    if trop_tot(conf):
        return messages, maj_stock, maj_vus

    if conf.get("type") == "shopify":
        return traiter_shopify(conf, etat)

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
        # On n'alerte que sur un vrai retour en stock. Un produit ajoute a la
        # liste alors qu'il est deja disponible ne declenche rien : sinon
        # chaque ajout partirait en rafale d'alertes inutiles.
        if dispo and ancien is False:
            messages.append((url, f"\U0001F514 RESTOCK - {conf['nom']}\n\n{nom}\n{url}"))
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

        trouves = re.findall(conf["motif_lien"], page)
        if conf.get("filtrer_par_jeu"):
            jeu = jeu_du_rayon(url)
            if jeu:
                avant = len(set(trouves))
                trouves = [l for l in trouves if jeu in l.lower()]
                ecartes = avant - len(set(trouves))
                if ecartes:
                    print(f"    {etiquette} : {ecartes} liens hors sujet ecartes.")
        liens = {conf["prefixe"] + l.lstrip("/") for l in trouves}
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
                (lien,
                 f"\U0001F195 NOUVEAUTE - {conf['nom']}\n\n{nom_depuis_url(lien)}\n{lien}"))
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
    deja_envoye: set[str] = set()
    for messages, maj_stock, maj_vus in resultats:
        etat["stock"].update(maj_stock)
        etat["vus"].update(maj_vus)
        for cle, message in messages:
            if cle in deja_envoye:
                continue      # meme produit vu dans deux rayons : une seule alerte
            deja_envoye.add(cle)
            envoyer_telegram(message)

    if deja_envoye:
        pousser_etat(force=True)

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

    if not TELEGRAM_TOKEN or not TELEGRAM_CHATS:
        print("[!] Identifiants Telegram absents. Renseigne TG_TOKEN et TG_CHAT,")
        print("    ou cree un fichier secrets.local avec le token puis le chat id.")
        if not DRY_RUN:
            sys.exit(1)

    mode = " (DRY RUN, rien ne part sur Telegram)" if DRY_RUN else ""
    print(f"Surveillance démarrée [{VERSION}]{mode}.")
    rayons_shopify = sum(len(c.get("collections", [])) for c in BOUTIQUES.values())
    print(f"{len(PRODUITS)} fiches et {len(RAYONS) + rayons_shopify} rayons suivis.")
    print(f"{len(TELEGRAM_CHATS)} destinataire(s) Telegram.")
    if EN_ACTIONS:
        print(f"Execution longue : environ {DUREE_MAX / 3600:.0f} h de "
              f"surveillance continue, un tour toutes les {INTERVALLE // 60} min.")
    print()

    while True:
        print(f"--- Tour de {time.strftime('%H:%M:%S')} ---")
        ECHECS.clear()
        faire_un_tour(etat)
        etat["passages"] = dict(DERNIER_PASSAGE)
        sauver_etat(etat)
        pousser_etat()
        print()

        if UNE_FOIS:
            return

        ecoule = time.time() - DEBUT
        if EN_ACTIONS and ecoule + INTERVALLE > DUREE_MAX:
            heures = ecoule / 3600
            print(f"Fin de cette execution apres {heures:.1f} h de surveillance. "
                  f"Le minuteur en relancera une nouvelle.")
            pousser_etat(force=True)
            return

        time.sleep(INTERVALLE + random.randint(0, JITTER))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nArrêt.")
