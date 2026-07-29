"""Bot Telegram v10 de restocks, nouveautés et sorties TCG Pokémon / One Piece.

Cette version comprend :
  - une veille prioritaire OP-17 / DP-12, ME06 Règne Delta et 30 ans Pokémon ;
  - l'ajout automatique des nouvelles fiches ciblées à la surveillance ;
  - un ordonnanceur rapide par boutique, avec backoff et circuit breaker ;
  - un mode shadow silencieux pour valider un VPS sans doubler les alertes ;
  - un heartbeat Telegram quotidien et un socle de découverte par sitemap.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

VERSION = "v10"
SCHEMA_ETAT = 10


class StockStatus(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


@dataclass
class FetchResult:
    body: str | None
    status_code: int | None = None
    error: str | None = None


@dataclass
class ProductReading:
    url: str
    name: str
    shop: str
    status: StockStatus
    variant_id: str | None = None


@dataclass
class CategoryReading:
    url: str
    shop: str
    links: set[str]
    names: dict[str, str] = field(default_factory=dict)
    game: str | None = None


@dataclass
class Observation:
    key: str
    label: str
    shop: str
    ok: bool
    error: str | None = None


@dataclass
class Alert:
    kind: str
    key: str
    text: str
    url: str | None = None
    cart_url: str | None = None


@dataclass
class ShopResult:
    shop: str
    ran: bool = True
    full_run: bool = True
    dynamic_urls: list[str] = field(default_factory=list)
    products: list[ProductReading] = field(default_factory=list)
    categories: list[CategoryReading] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)


def _lire_secrets() -> tuple[str, str]:
    """Lit Telegram depuis l'environnement ou, en local, secrets.local."""
    token = os.environ.get("TG_TOKEN", "").strip()
    chats = os.environ.get("TG_CHAT", "").strip()
    if token and chats:
        return token, chats
    chemin = Path("secrets.local")
    if chemin.is_file():
        with chemin.open(encoding="utf-8") as fichier:
            lignes = [ligne.strip() for ligne in fichier if ligne.strip()]
        if len(lignes) >= 2:
            return lignes[0], lignes[1]
    return token, chats


TELEGRAM_TOKEN, _CHATS_BRUT = _lire_secrets()
TELEGRAM_CHATS = [
    chat.strip() for chat in re.split(r"[,;\s]+", _CHATS_BRUT) if chat.strip()
]


# ---------------------------------------------------------------------------
# Détecteurs de stock
# ---------------------------------------------------------------------------
MOTIF_SCHEMA = re.compile(
    r"availability[\s\\\"':=hrefHREF]{0,60}https?://schema\.org/"
    r"(InStock|OutOfStock|PreOrder|BackOrder|SoldOut|Discontinued)",
    re.IGNORECASE,
)
MOTIF_JSON_LD = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
UJ_PRINCIPAL = re.compile(
    r'font-size:\s*18px[^"\']*"[^>]*>\s*((?:In)?Disponible)\s*<',
    re.IGNORECASE,
)
UJ_VARIANTE = re.compile(r">\s*((?:In)?Disponible)\s*</b>", re.IGNORECASE)
STATUTS_RUPTURE = {
    "outofstock",
    "preorder",
    "backorder",
    "soldout",
    "discontinued",
}


def _parcourir_json(valeur: Any):
    if isinstance(valeur, dict):
        yield valeur
        for enfant in valeur.values():
            yield from _parcourir_json(enfant)
    elif isinstance(valeur, list):
        for enfant in valeur:
            yield from _parcourir_json(enfant)


def _est_type_produit(valeur: Any) -> bool:
    types = valeur if isinstance(valeur, list) else [valeur]
    return any(str(type_).lower().endswith("product") for type_ in types)


def _statut_depuis_disponibilites(disponibilites: set[str]) -> StockStatus:
    normalisees = {valeur.rsplit("/", 1)[-1].lower() for valeur in disponibilites}
    if "instock" in normalisees:
        return StockStatus.IN_STOCK
    if normalisees and normalisees.issubset(STATUTS_RUPTURE):
        return StockStatus.OUT_OF_STOCK
    return StockStatus.UNKNOWN


def stock_schema(html: str) -> StockStatus:
    """Privilégie le JSON-LD du produit et refuse les marqueurs contradictoires."""
    statuts_produits: set[StockStatus] = set()
    for brut in MOTIF_JSON_LD.findall(html):
        try:
            document = json.loads(unescape(brut).strip())
        except (TypeError, ValueError):
            continue
        for objet in _parcourir_json(document):
            if not _est_type_produit(objet.get("@type")):
                continue
            offres = objet.get("offers")
            disponibilites = {
                str(noeud["availability"])
                for noeud in _parcourir_json(offres)
                if isinstance(noeud, dict) and noeud.get("availability")
            }
            statut = _statut_depuis_disponibilites(disponibilites)
            if statut is not StockStatus.UNKNOWN:
                statuts_produits.add(statut)

    if len(statuts_produits) == 1:
        return next(iter(statuts_produits))
    if len(statuts_produits) > 1:
        return StockStatus.UNKNOWN

    disponibilites = {trouve.group(1).lower() for trouve in MOTIF_SCHEMA.finditer(html)}
    if disponibilites == {"instock"}:
        return StockStatus.IN_STOCK
    if disponibilites and disponibilites.issubset(STATUTS_RUPTURE):
        return StockStatus.OUT_OF_STOCK
    return StockStatus.UNKNOWN


def stock_ultrajeux(html: str) -> StockStatus:
    for motif in (UJ_PRINCIPAL, UJ_VARIANTE):
        trouve = motif.search(html)
        if trouve:
            return (
                StockStatus.IN_STOCK
                if trouve.group(1).lower() == "disponible"
                else StockStatus.OUT_OF_STOCK
            )
    return StockStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Boutiques connues
# ---------------------------------------------------------------------------
BOUTIQUES: dict[str, dict[str, Any]] = {
    "ultrajeux.com": {
        "nom": "UltraJeux",
        "stock": stock_ultrajeux,
        "motif_lien": r'href=["\'](produit-\d+-[^"\'?#]+\.html)',
        "motif_url_produit": r"/produit-\d+-[^/?#]+\.html$",
        "prefixe": "https://www.ultrajeux.com/",
        "accueil": "https://www.ultrajeux.com/",
        "cadence": 120,
        "cadence_actions": 300,
        "sitemaps": [],
    },
    "micromania.fr": {
        "nom": "Micromania",
        "stock": stock_schema,
        "motif_lien": (
            r'href=["\'](?:https://www\.micromania\.fr/)?'
            r'(p/[^"\'?#]+\.html)'
        ),
        "motif_url_produit": r"/p/[^/?#]+\.html$",
        "prefixe": "https://www.micromania.fr/",
        "accueil": "https://www.micromania.fr/",
        "cadence": 1200,
        "cadence_actions": 1200,
        "sitemaps": [],
    },
    "play-in.com": {
        "nom": "Play-in",
        "stock": stock_schema,
        "motif_lien": (
            r"(?:https://www\.play-in\.com/fr/)?"
            r"(produit/\d+/[a-z0-9-]{5,120})"
        ),
        "motif_url_produit": r"/fr/produit/\d+/[a-z0-9-]+$",
        "prefixe": "https://www.play-in.com/fr/",
        "accueil": "https://www.play-in.com/fr/",
        "cadence": 180,
        "cadence_actions": 300,
        "filtrer_par_jeu": True,
        "sitemaps": [],
    },
    "blazingtail.fr": {
        "nom": "Blazingtail",
        "stock": stock_schema,
        "motif_lien": (
            r'href=["\'](?:https://www\.blazingtail\.fr/)?'
            r'(\d+-[^"\'#?]+?\.html)'
        ),
        "motif_url_produit": r"/\d+-[^/?#]+\.html$",
        "prefixe": "https://www.blazingtail.fr/",
        "accueil": "https://www.blazingtail.fr/",
        "cadence": 180,
        "cadence_actions": 300,
        "jeu_par_defaut": "pokemon",
        "sitemaps": [],
    },
    "kingdultes.com": {
        "nom": "KingDultes",
        "type": "shopify",
        "base": "https://www.kingdultes.com",
        "accueil": "https://www.kingdultes.com/",
        "cadence": 75,
        "cadence_actions": 300,
        "motif_url_produit": r"/products/[^/?#]+$",
        "sitemaps": [],
        "collections": [
            "tcg-pokemon",
            "tcg-one-piece",
            "les-nouveautes-preco-tcg",
        ],
    },
}

JEUX = ("one-piece", "pokemon")
SORTIES_CIBLES: dict[str, dict[str, Any]] = {
    "one_piece_op17": {
        "jeu": "one-piece",
        "nom": "One Piece OP-17 — Les Guerriers les plus puissants au monde",
        "mots_cles": [
            "op17",
            "op-17",
            "op 17",
            "les guerriers les plus puissants au monde",
            "guerriers les plus puissants",
            "the world's strongest warriors",
            "the worlds strongest warriors",
            "world's strongest warriors",
            "worlds strongest warriors",
            "strongest warriors",
            "dp12",
            "dp-12",
            "dp 12",
            "double pack set vol.12",
            "double pack set vol 12",
            "double pack vol.12",
            "double pack vol 12",
        ],
    },
    "pokemon_me06_regne_delta": {
        "jeu": "pokemon",
        "nom": "Pokémon ME06 — Règne Delta",
        "mots_cles": [
            "me06",
            "me6",
            "règne delta",
            "regne delta",
            "delta reign",
            "méga-évolution 06",
            "mega evolution 06",
        ],
    },
    "pokemon_30e_anniversaire": {
        "jeu": "pokemon",
        "nom": "Pokémon — 30e anniversaire",
        "mots_cles": [
            "30e anniversaire",
            "30ème anniversaire",
            "30 ans",
            "30th anniversary",
            "30th celebration",
            "30th celebrations",
            "célébration",
            "celebration",
            "célébrations",
            "celebrations",
            "anniversaire",
        ],
        # Ces termes seuls sont trop génériques : ils exigent un rayon Pokémon.
        "mots_generiques": [
            "célébration",
            "celebration",
            "célébrations",
            "celebrations",
            "anniversaire",
        ],
    },
}
MOTS_SCELLE = {
    "booster",
    "display",
    "etb",
    "coffret",
    "bundle",
    "tripack",
    "tri pack",
    "blister",
    "box",
    "collection",
    "tin",
    "pokebox",
    "deck",
    "pack",
}
MOTS_EXCLUS_CIBLES = {
    "tournoi",
    "tournament",
    "inscription",
    "ticket",
    "billet",
    "evenement",
    "event",
}
PARAMETRES_TRACKING = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


def normaliser_texte(texte: str) -> str:
    """Rend comparables accents, ponctuation, apostrophes et tirets."""
    decompose = unicodedata.normalize("NFKD", unescape(str(texte)).lower())
    sans_accents = "".join(
        caractere for caractere in decompose if not unicodedata.combining(caractere)
    )
    return re.sub(r"[^a-z0-9]+", " ", sans_accents).strip()


def jeu_du_produit(nom: str, url: str, indice: str | None = None) -> str | None:
    if indice in JEUX:
        return indice
    texte = normaliser_texte(f"{nom} {url}")
    if "one piece" in texte:
        return "one-piece"
    if "pokemon" in texte:
        return "pokemon"
    return None


def classifier_sortie(
    nom: str, url: str, jeu: str | None = None
) -> tuple[str, dict[str, Any]] | None:
    """Retourne la sortie ciblée, sans laisser un terme générique changer de jeu."""
    texte = normaliser_texte(f"{nom} {url}")
    mots_texte = set(texte.split())
    if mots_texte & MOTS_EXCLUS_CIBLES:
        return None
    jeu_detecte = jeu_du_produit(nom, url, jeu)
    for identifiant, cible in SORTIES_CIBLES.items():
        if jeu_detecte and jeu_detecte != cible["jeu"]:
            continue
        generiques = {
            normaliser_texte(mot) for mot in cible.get("mots_generiques", [])
        }
        for mot in cible["mots_cles"]:
            mot_normalise = normaliser_texte(mot)
            if mot_normalise not in texte:
                continue
            if mot_normalise in generiques and jeu_detecte != cible["jeu"]:
                continue
            if mot_normalise in generiques and not any(
                normaliser_texte(format_) in texte for format_ in MOTS_SCELLE
            ):
                continue
            return identifiant, cible
    return None


def jeu_du_rayon(url: str) -> str | None:
    minuscule = url.lower()
    return next((jeu for jeu in JEUX if jeu in minuscule), None)


def boutique_de(url: str) -> dict[str, Any] | None:
    hote = urlsplit(url).netloc.lower().split(":", 1)[0]
    return next(
        (conf for domaine, conf in BOUTIQUES.items() if hote.endswith(domaine)),
        None,
    )


def normaliser_url(url: str, base: str | None = None) -> str:
    absolue = urljoin(base or url, url)
    morceaux = urlsplit(absolue)
    parametres = [
        (cle, valeur)
        for cle, valeur in parse_qsl(morceaux.query, keep_blank_values=True)
        if not cle.lower().startswith("utm_") and cle.lower() not in PARAMETRES_TRACKING
    ]
    chemin = re.sub(r"/{2,}", "/", morceaux.path)
    if chemin != "/":
        chemin = chemin.rstrip("/")
    return urlunsplit(
        ("https", morceaux.netloc.lower(), chemin, urlencode(parametres), "")
    )


def nom_depuis_url(url: str) -> str:
    bout = urlsplit(url).path.rstrip("/").split("/")[-1]
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
# Réglages
# ---------------------------------------------------------------------------
EN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
INTERVALLE = 300 if EN_ACTIONS else 30
JITTER = 45 if EN_ACTIONS else 5
PAUSE_MIN = 1.2
PAUSE_MAX = 2.4
FICHIER_ETAT_PRINCIPAL = Path(
    os.environ.get("RESTOCK_STATE_FILE", "etat_stock.json")
)

DUREE_MAX = 5 * 3600 + 20 * 60
INTERVALLE_CHECKPOINT = 3600
CONFIRMATIONS_RUPTURE = 2
SEUIL_ALERTE_SANTE = 3
SEUIL_RATIO_RAYON = 0.30
CONFIRMATIONS_ACCEPTATION_RAYON = 6
MAX_PAGES_RAYON = 5
MAX_ECHECS_CONSECUTIFS_BOUTIQUE = 3
HTTP_RETRIES = 2

DRY_RUN = "--dry-run" in sys.argv
SHADOW = "--shadow" in sys.argv
UNE_FOIS = "--une-fois" in sys.argv
MIGRATE_ONLY = "--migrate-only" in sys.argv
VERSION_ONLY = "--version" in sys.argv
if SHADOW:
    FICHIER_ETAT = Path(
        os.environ.get(
            "RESTOCK_SHADOW_STATE_FILE",
            str(
                FICHIER_ETAT_PRINCIPAL.with_name(
                    f"{FICHIER_ETAT_PRINCIPAL.stem}.shadow"
                    f"{FICHIER_ETAT_PRINCIPAL.suffix}"
                )
            ),
        )
    )
else:
    FICHIER_ETAT = FICHIER_ETAT_PRINCIPAL
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()
BOT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Europe/Paris").strip()
HEARTBEAT_HOUR = os.environ.get("HEARTBEAT_HOUR", "09:00").strip()
CADENCE_CIBLE = (300, 360) if EN_ACTIONS else (45, 75)

ENTETES = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/json;q=0.9,"
        "application/xml;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}
MARQUEURS_BLOCAGE = (
    "incapsula incident id",
    "powered by imperva",
    "cf-chl-captcha",
    "px-captcha",
    "access denied |",
    "unusual traffic",
)
MOTIF_BALISE_SUIVANTE = re.compile(r"<(?:a|link)\b[^>]*>", re.IGNORECASE)
MOTIF_ATTRIBUT = re.compile(r'([a-zA-Z:-]+)\s*=\s*["\']([^"\']*)["\']')

SESSIONS: dict[str, requests.Session] = {}
DERNIER_PASSAGE: dict[str, float] = {}
_derniere_sauvegarde = 0.0


def horodatage() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def pause_poliment() -> None:
    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))


def trop_tot(conf: dict[str, Any]) -> bool:
    cadence = int(
        conf.get("cadence_actions", conf.get("cadence", 300))
        if EN_ACTIONS
        else conf.get("cadence", 300)
    )
    dernier = DERNIER_PASSAGE.get(conf["nom"], 0.0)
    return bool(cadence and time.time() - dernier < cadence)


def etat_vide() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_ETAT,
        "products": {},
        "categories": {},
        "health": {},
        "outbox": [],
        "passages": {},
        "dynamic_products": {},
        "scheduler": {"shop_backoff": {}},
        "daily_heartbeat": {"last_date": None},
        "heartbeat": {"version": VERSION, "last_cycle": None},
    }


def migrer_etat(brut: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Convertit silencieusement les états v8/v9 sans créer de faux restocks."""
    if brut.get("schema_version") == SCHEMA_ETAT:
        etat = brut
        etat.setdefault("products", {})
        etat.setdefault("categories", {})
        etat.setdefault("health", {})
        etat.setdefault("outbox", [])
        etat.setdefault("passages", {})
        etat.setdefault("dynamic_products", {})
        etat.setdefault("scheduler", {"shop_backoff": {}})
        etat["scheduler"].setdefault("shop_backoff", {})
        etat.setdefault("daily_heartbeat", {"last_date": None})
        etat.setdefault("heartbeat", {"version": VERSION, "last_cycle": None})
        etat["heartbeat"]["version"] = VERSION
        amorcer_dynamiques_existants(etat)
        return etat, False

    ancienne_version = brut.get("schema_version", 8)
    if ancienne_version == 9:
        etat = copy.deepcopy(brut)
        etat["schema_version"] = SCHEMA_ETAT
        etat.setdefault("products", {})
        etat.setdefault("categories", {})
        etat.setdefault("health", {})
        etat.setdefault("outbox", [])
        etat.setdefault("passages", {})
        etat.setdefault("dynamic_products", {})
        etat.setdefault("scheduler", {"shop_backoff": {}})
        etat["scheduler"].setdefault("shop_backoff", {})
        etat.setdefault("daily_heartbeat", {"last_date": None})
        etat.setdefault("heartbeat", {})
        etat["heartbeat"]["version"] = VERSION
        etat["migration"] = {"from": 9, "at": horodatage()}
        amorcer_dynamiques_existants(etat)
        return etat, True

    etat = etat_vide()
    etat["passages"] = dict(brut.get("passages", {}))
    etat["migration"] = {"from": ancienne_version, "at": horodatage()}

    for url, disponible in brut.get("stock", {}).items():
        lien = normaliser_url(url)
        statut = (
            StockStatus.IN_STOCK
            if disponible is True
            else StockStatus.OUT_OF_STOCK
            if disponible is False
            else StockStatus.UNKNOWN
        )
        etat["products"][lien] = {
            "status": statut.value,
            "out_confirmations": (
                CONFIRMATIONS_RUPTURE if statut is StockStatus.OUT_OF_STOCK else 0
            ),
            "name": nom_depuis_url(lien),
            "shop": (boutique_de(lien) or {}).get("nom"),
            "variant_id": None,
            "last_checked": None,
            "last_changed": None,
        }

    for url, liens in brut.get("vus", {}).items():
        connus = sorted({normaliser_url(lien) for lien in liens})
        etat["categories"][url] = {
            "ever_seen": connus,
            "current": connus,
            "last_count": len(connus),
            "last_checked": None,
            "anomaly_streak": 0,
            "candidate_count": None,
            "legacy_baseline": True,
        }

    return etat, True


def _lire_etat_git() -> dict[str, Any] | None:
    resultat = subprocess.run(
        ["git", "show", f"HEAD:{FICHIER_ETAT_PRINCIPAL.name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if resultat.returncode:
        return None
    try:
        return json.loads(resultat.stdout)
    except ValueError:
        return None


def charger_etat() -> tuple[dict[str, Any], bool]:
    source = FICHIER_ETAT
    if SHADOW and not source.exists() and FICHIER_ETAT_PRINCIPAL.exists():
        source = FICHIER_ETAT_PRINCIPAL
        print(f"[shadow] État initial copié depuis {source}.")
    if not source.exists():
        return etat_vide(), True
    try:
        with source.open(encoding="utf-8") as fichier:
            brut = json.load(fichier)
    except (OSError, ValueError) as erreur:
        print(f"[!] État local illisible ({erreur}). Tentative de récupération Git.")
        brut = _lire_etat_git()
        if brut is None:
            raise RuntimeError("Impossible de récupérer un état valide.") from erreur
    return migrer_etat(brut)


def sauver_etat(etat: dict[str, Any]) -> None:
    """Écriture atomique : un arrêt brutal ne peut pas laisser un JSON partiel."""
    if DRY_RUN:
        return
    temporaire = FICHIER_ETAT.with_suffix(FICHIER_ETAT.suffix + ".tmp")
    FICHIER_ETAT.parent.mkdir(parents=True, exist_ok=True)
    with temporaire.open("w", encoding="utf-8") as fichier:
        json.dump(etat, fichier, ensure_ascii=False, indent=2)
        fichier.write("\n")
        fichier.flush()
        os.fsync(fichier.fileno())
    os.replace(temporaire, FICHIER_ETAT)


def _git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], capture_output=True, text=True, check=False
    )


def pousser_etat(force: bool = False, raison: str = "checkpoint") -> bool:
    """Committe l'état sur Actions ; jamais plus d'un checkpoint par heure."""
    global _derniere_sauvegarde
    if not EN_ACTIONS or DRY_RUN or SHADOW:
        return True
    if not force and time.time() - _derniere_sauvegarde < INTERVALLE_CHECKPOINT:
        return True

    _git("config", "user.name", "bot")
    _git("config", "user.email", "bot@users.noreply.github.com")
    ajout = _git("add", "--", str(FICHIER_ETAT))
    if ajout.returncode:
        print(f"[!] git add a échoué : {ajout.stderr.strip()}")
        return False
    if _git("diff", "--cached", "--quiet").returncode == 0:
        _derniere_sauvegarde = time.time()
        return True

    date = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    raison_sure = re.sub(r"[^a-zA-Z0-9 _-]", "", raison)[:40]
    commit = _git("commit", "-m", f"etat v10 {raison_sure} - {date}")
    if commit.returncode:
        print(f"[!] Commit d'état impossible : {commit.stderr.strip()}")
        return False

    for tentative in range(2):
        rebase = _git("pull", "--rebase", "--quiet", "origin")
        if rebase.returncode:
            _git("rebase", "--abort")
            print("[!] Rebase d'état impossible ; aucune donnée distante écrasée.")
            return False
        envoi = _git("push", "--quiet", "origin", "HEAD")
        if envoi.returncode == 0:
            _derniere_sauvegarde = time.time()
            return True
        if tentative == 0:
            time.sleep(2)
    print(f"[!] Push d'état impossible : {envoi.stderr.strip()}")
    return False


def session_de(conf: dict[str, Any]) -> requests.Session:
    nom = conf["nom"]
    if nom not in SESSIONS:
        session = requests.Session()
        session.headers.update(ENTETES)
        try:
            session.get(conf["accueil"], timeout=(8, 20))
            time.sleep(random.uniform(0.3, 0.8))
        except requests.RequestException:
            pass
        SESSIONS[nom] = session
    return SESSIONS[nom]


def recuperer(url: str, etiquette: str, conf: dict[str, Any]) -> FetchResult:
    session = session_de(conf)
    entetes = {"Referer": conf["accueil"]}
    derniere_erreur = "erreur inconnue"
    dernier_code: int | None = None

    for tentative in range(HTTP_RETRIES + 1):
        try:
            reponse = session.get(url, headers=entetes, timeout=(8, 25))
            dernier_code = reponse.status_code
        except requests.RequestException as erreur:
            derniere_erreur = f"réseau : {erreur}"
        else:
            if reponse.status_code == 200:
                corps = reponse.text
                minuscule = corps.lower()
                blocage = next(
                    (motif for motif in MARQUEURS_BLOCAGE if motif in minuscule),
                    None,
                )
                if blocage:
                    return FetchResult(None, 200, f"page anti-bot détectée ({blocage})")
                if len(corps.strip()) < 80:
                    return FetchResult(None, 200, "réponse anormalement courte")
                return FetchResult(corps, 200, None)

            derniere_erreur = f"HTTP {reponse.status_code}"
            if reponse.status_code in (401, 403, 404):
                break
            if reponse.status_code == 429:
                try:
                    retry_after = max(
                        1, int(reponse.headers.get("Retry-After", "2") or "2")
                    )
                except ValueError:
                    retry_after = 2
                derniere_erreur = f"HTTP 429; retry_after={retry_after}"
                attente = min(10, retry_after)
                time.sleep(attente)

        if tentative < HTTP_RETRIES:
            time.sleep((2**tentative) + random.random())

    print(f"[!] {etiquette} : {derniere_erreur}")
    return FetchResult(None, dernier_code, derniere_erreur)


def extraire_lien_suivant(html: str, page_url: str) -> str | None:
    for balise in MOTIF_BALISE_SUIVANTE.findall(html):
        attributs = {
            cle.lower(): valeur for cle, valeur in MOTIF_ATTRIBUT.findall(balise)
        }
        if "next" not in attributs.get("rel", "").lower().split():
            continue
        suivant = attributs.get("href")
        if suivant:
            return normaliser_url(suivant, page_url)
    return None


def extraire_liens_produits(html: str, rayon: str, conf: dict[str, Any]) -> set[str]:
    trouves = re.findall(conf["motif_lien"], html, flags=re.IGNORECASE)
    if conf.get("filtrer_par_jeu"):
        jeu = jeu_du_rayon(rayon)
        if jeu:
            trouves = [lien for lien in trouves if jeu in lien.lower()]
    return {
        normaliser_url(lien.lstrip("/"), conf["prefixe"]) for lien in trouves if lien
    }


def extraire_urls_sitemap(xml: str, conf: dict[str, Any]) -> set[str]:
    """Extrait uniquement les URL qui ressemblent à des fiches de la boutique."""
    try:
        racine = ET.fromstring(xml)
    except ET.ParseError:
        return set()
    motif = conf.get("motif_url_produit")
    if not motif:
        return set()
    urls = {
        normaliser_url(element.text.strip())
        for element in racine.iter()
        if element.tag.rsplit("}", 1)[-1] == "loc"
        and element.text
        and re.search(motif, urlsplit(element.text.strip()).path, re.IGNORECASE)
    }
    return {
        url
        for url in urls
        if boutique_de(url) is conf
    }


def _observation(
    resultat: ShopResult,
    key: str,
    label: str,
    ok: bool,
    error: str | None = None,
) -> None:
    resultat.observations.append(
        Observation(key=key, label=label, shop=resultat.shop, ok=ok, error=error)
    )


def traiter_shopify(conf: dict[str, Any]) -> ShopResult:
    resultat = ShopResult(conf["nom"])
    echecs_consecutifs = 0

    for collection in conf["collections"]:
        cle_rayon = f"{conf['base']}/collections/{collection}/products.json?limit=250"
        etiquette = f"{conf['nom']} – {collection}"
        produits: list[dict[str, Any]] = []
        echec = None

        for numero_page in range(1, MAX_PAGES_RAYON + 1):
            if echecs_consecutifs >= MAX_ECHECS_CONSECUTIFS_BOUTIQUE:
                echec = "boutique mise en pause après 3 échecs consécutifs"
                break
            url = cle_rayon + (f"&page={numero_page}" if numero_page > 1 else "")
            lecture = recuperer(url, etiquette, conf)
            pause_poliment()
            if lecture.body is None:
                echecs_consecutifs += 1
                echec = lecture.error
                break
            echecs_consecutifs = 0
            try:
                lot = json.loads(lecture.body).get("products", [])
            except (TypeError, ValueError):
                echec = "JSON Shopify illisible"
                break
            if numero_page == 1 and not lot:
                echec = "rayon Shopify vide"
                break
            produits.extend(lot)
            if len(lot) < 250:
                break

        if echec:
            _observation(
                resultat,
                f"category:{cle_rayon}",
                etiquette,
                False,
                echec,
            )
            continue

        liens: set[str] = set()
        noms: dict[str, str] = {}
        for produit in produits:
            handle = produit.get("handle")
            if not handle:
                continue
            lien = normaliser_url(f"{conf['base']}/products/{handle}")
            nom = str(produit.get("title") or nom_depuis_url(lien))
            variantes = produit.get("variants") or []
            disponibles = [
                variante for variante in variantes if variante.get("available")
            ]
            variante_id = (
                str(disponibles[0].get("id"))
                if disponibles and disponibles[0].get("id") is not None
                else None
            )
            statut = StockStatus.IN_STOCK if disponibles else StockStatus.OUT_OF_STOCK
            liens.add(lien)
            noms[lien] = nom
            resultat.products.append(
                ProductReading(lien, nom, conf["nom"], statut, variant_id=variante_id)
            )

        if not liens:
            _observation(
                resultat,
                f"category:{cle_rayon}",
                etiquette,
                False,
                "aucun produit Shopify exploitable",
            )
            continue
        jeu = (
            "pokemon"
            if "pokemon" in collection
            else "one-piece"
            if "one-piece" in collection
            else None
        )
        resultat.categories.append(
            CategoryReading(cle_rayon, conf["nom"], liens, noms, game=jeu)
        )
        _observation(resultat, f"category:{cle_rayon}", etiquette, True)
        print(f"    {etiquette} : {len(liens)} produits valides.")

    return resultat


def traiter_boutique(
    domaine: str,
    conf: dict[str, Any],
    full_run: bool = True,
    dynamic_urls: list[str] | None = None,
) -> ShopResult:
    if conf.get("type") == "shopify":
        return traiter_shopify(conf)

    dynamic_urls = dynamic_urls or []
    resultat = ShopResult(
        conf["nom"], full_run=full_run, dynamic_urls=list(dynamic_urls)
    )
    echecs_consecutifs = 0

    def lire_cible(url: str, label: str, key: str) -> str | None:
        nonlocal echecs_consecutifs
        if echecs_consecutifs >= MAX_ECHECS_CONSECUTIFS_BOUTIQUE:
            erreur = "boutique mise en pause après 3 échecs consécutifs"
            _observation(resultat, key, label, False, erreur)
            return None
        lecture = recuperer(url, label, conf)
        pause_poliment()
        if lecture.body is None:
            echecs_consecutifs += 1
            _observation(resultat, key, label, False, lecture.error)
            return None
        echecs_consecutifs = 0
        return lecture.body

    cibles = list(PRODUITS) if full_run else []
    cibles.extend(dynamic_urls)
    deja_lues: set[str] = set()
    for url_brute in cibles:
        if boutique_de(url_brute) is not conf:
            continue
        url = normaliser_url(url_brute)
        if url in deja_lues:
            continue
        deja_lues.add(url)
        nom = nom_depuis_url(url)
        label = f"{conf['nom']} – fiche {nom[:70]}"
        key = f"product:{url}"
        html = lire_cible(url, label, key)
        if html is None:
            continue
        statut = conf["stock"](html)
        if statut is StockStatus.UNKNOWN:
            _observation(resultat, key, label, False, "stock illisible")
            print(f"[?] {label} : état inconnu, mémoire conservée.")
            continue
        _observation(resultat, key, label, True)
        resultat.products.append(ProductReading(url, nom, conf["nom"], statut))
        print(f"    {label} : {statut.value}.")

    if not full_run:
        return resultat

    for rayon in RAYONS:
        if boutique_de(rayon) is not conf:
            continue
        label = f"{conf['nom']} – rayon {jeu_du_rayon(rayon) or nom_depuis_url(rayon)}"
        key = f"category:{rayon}"
        liens: set[str] = set()
        page_url: str | None = rayon
        pages_vues: set[str] = set()
        echec = None

        for _ in range(MAX_PAGES_RAYON):
            if not page_url or page_url in pages_vues:
                break
            pages_vues.add(page_url)
            html = lire_cible(page_url, label, key)
            if html is None:
                echec = "page rayon indisponible"
                break
            liens.update(extraire_liens_produits(html, rayon, conf))
            suivant = extraire_lien_suivant(html, page_url)
            if suivant and urlsplit(suivant).netloc != urlsplit(rayon).netloc:
                suivant = None
            page_url = suivant

        if echec:
            continue
        if not liens:
            _observation(resultat, key, label, False, "aucun lien produit trouvé")
            print(f"[!] {label} : aucun lien produit trouvé.")
            continue
        _observation(resultat, key, label, True)
        jeu = jeu_du_rayon(rayon) or conf.get("jeu_par_defaut")
        resultat.categories.append(
            CategoryReading(rayon, conf["nom"], liens, game=jeu)
        )
        print(f"    {label} : {len(liens)} liens valides.")

    for sitemap in conf.get("sitemaps", []):
        label = f"{conf['nom']} – sitemap {nom_depuis_url(sitemap)}"
        key = f"category:sitemap:{sitemap}"
        xml = lire_cible(sitemap, label, key)
        if xml is None:
            continue
        liens = extraire_urls_sitemap(xml, conf)
        if not liens:
            _observation(resultat, key, label, False, "aucune fiche dans le sitemap")
            continue
        _observation(resultat, key, label, True)
        resultat.categories.append(
            CategoryReading(
                f"sitemap:{sitemap}",
                conf["nom"],
                liens,
                game=conf.get("jeu_par_defaut"),
            )
        )
        print(f"    {label} : {len(liens)} fiches valides.")

    return resultat


def appliquer_stock(
    etat: dict[str, Any], lecture: ProductReading
) -> tuple[list[Alert], bool]:
    produits = etat["products"]
    maintenant = horodatage()
    fiche = produits.get(lecture.url)
    if fiche is None:
        produits[lecture.url] = {
            "status": lecture.status.value,
            "out_confirmations": (
                CONFIRMATIONS_RUPTURE
                if lecture.status is StockStatus.OUT_OF_STOCK
                else 0
            ),
            "name": lecture.name,
            "shop": lecture.shop,
            "variant_id": lecture.variant_id,
            "last_checked": maintenant,
            "last_changed": maintenant,
        }
        return [], True

    ancien = StockStatus(fiche.get("status", StockStatus.UNKNOWN.value))
    confirmations = int(fiche.get("out_confirmations", 0))
    critique = False
    alertes: list[Alert] = []
    fiche.update(
        {
            "name": lecture.name,
            "shop": lecture.shop,
            "variant_id": lecture.variant_id,
            "last_checked": maintenant,
        }
    )

    if lecture.status is StockStatus.IN_STOCK:
        if ancien is StockStatus.OUT_OF_STOCK:
            dynamique = etat.get("dynamic_products", {}).get(lecture.url, {})
            cible_id = dynamique.get("target")
            cible = SORTIES_CIBLES.get(cible_id)
            panier = (
                f"https://www.kingdultes.com/cart/{lecture.variant_id}:1"
                if lecture.shop == "KingDultes" and lecture.variant_id
                else None
            )
            titre = (
                f"🚨 PRÉCOMMANDE / STOCK OUVERT — {cible['nom']}"
                if cible
                else f"🔔 RESTOCK — {lecture.shop}"
            )
            alertes.append(
                Alert(
                    "target_available" if cible else "restock",
                    lecture.url,
                    f"{titre}\n\n{lecture.shop}\n{lecture.name}\n{lecture.url}",
                    url=lecture.url,
                    cart_url=panier,
                )
            )
            print(f"[+] RESTOCK : {lecture.shop} – {lecture.name}")
        if ancien is not StockStatus.IN_STOCK or confirmations:
            critique = True
            fiche["last_changed"] = maintenant
        fiche["status"] = StockStatus.IN_STOCK.value
        fiche["out_confirmations"] = 0

    elif lecture.status is StockStatus.OUT_OF_STOCK:
        if ancien is StockStatus.IN_STOCK:
            confirmations += 1
            fiche["out_confirmations"] = confirmations
            critique = True
            if confirmations >= CONFIRMATIONS_RUPTURE:
                fiche["status"] = StockStatus.OUT_OF_STOCK.value
                fiche["last_changed"] = maintenant
                print(f"[-] Rupture confirmée : {lecture.shop} – {lecture.name}")
        elif ancien is not StockStatus.OUT_OF_STOCK:
            fiche["status"] = StockStatus.OUT_OF_STOCK.value
            fiche["out_confirmations"] = CONFIRMATIONS_RUPTURE
            fiche["last_changed"] = maintenant
            critique = True
        else:
            fiche["out_confirmations"] = CONFIRMATIONS_RUPTURE

    return alertes, critique


def inscrire_dynamique_cible(
    etat: dict[str, Any],
    lien: str,
    nom: str,
    boutique: str,
    jeu: str | None,
) -> tuple[str, dict[str, Any]] | None:
    cible_trouvee = classifier_sortie(nom, lien, jeu)
    if not cible_trouvee:
        return None
    cible_id, cible = cible_trouvee
    if boutique != "KingDultes":
        etat["dynamic_products"].setdefault(
            lien,
            {
                "name": nom,
                "shop": boutique,
                "game": cible["jeu"],
                "target": cible_id,
                "added_at": horodatage(),
                "last_scheduled": None,
                "next_check_epoch": 0,
                "active": True,
            },
        )
    return cible_id, cible


def amorcer_dynamiques_existants(etat: dict[str, Any]) -> None:
    """Récupère aussi une cible déjà vue par la v9, sans générer d'alerte."""
    for rayon, entree in etat.get("categories", {}).items():
        rayon_reel = rayon.removeprefix("sitemap:")
        jeu = jeu_du_rayon(rayon_reel)
        for lien in entree.get("current", entree.get("ever_seen", [])):
            conf = boutique_de(lien)
            if not conf:
                continue
            inscrire_dynamique_cible(
                etat,
                lien,
                nom_depuis_url(lien),
                conf["nom"],
                jeu or conf.get("jeu_par_defaut"),
            )


def alertes_nouveaux_produits(
    etat: dict[str, Any],
    lecture: CategoryReading,
    nouveaux: list[str],
) -> list[Alert]:
    alertes: list[Alert] = []
    for lien in nouveaux:
        nom = lecture.names.get(lien) or nom_depuis_url(lien)
        cible_trouvee = inscrire_dynamique_cible(
            etat, lien, nom, lecture.shop, lecture.game
        )
        if cible_trouvee:
            _, cible = cible_trouvee
            alertes.append(
                Alert(
                    "release_new",
                    lien,
                    f"🚨 SORTIE CIBLÉE DÉTECTÉE — {cible['nom']}\n\n"
                    f"{lecture.shop}\n{nom}\n{lien}\n\n"
                    "La fiche est désormais surveillée en priorité.",
                    url=lien,
                )
            )
            print(f"[+] SORTIE CIBLÉE : {lecture.shop} – {nom}")
            continue
        alertes.append(
            Alert(
                "new",
                lien,
                f"🆕 NOUVEAUTÉ — {lecture.shop}\n\n{nom}\n{lien}",
                url=lien,
            )
        )
        print(f"[+] NOUVEAUTÉ : {lecture.shop} – {nom}")
    return alertes


def appliquer_categorie(
    etat: dict[str, Any], lecture: CategoryReading
) -> tuple[list[Alert], bool, Observation | None]:
    categories = etat["categories"]
    maintenant = horodatage()
    entree = categories.get(lecture.url)
    liens = set(lecture.links)

    if entree is None:
        categories[lecture.url] = {
            "ever_seen": sorted(liens),
            "current": sorted(liens),
            "last_count": len(liens),
            "last_checked": maintenant,
            "anomaly_streak": 0,
            "candidate_count": None,
        }
        for lien in liens:
            nom = lecture.names.get(lien) or nom_depuis_url(lien)
            inscrire_dynamique_cible(
                etat, lien, nom, lecture.shop, lecture.game
            )
        print(
            f"    {lecture.shop} : {len(liens)} produits mémorisés "
            "(premier passage silencieux)."
        )
        return [], True, None

    precedent = set(entree.get("current", entree.get("ever_seen", [])))
    nombre_precedent = int(entree.get("last_count", len(precedent)))
    nombre = len(liens)
    entree["last_checked"] = maintenant

    if entree.pop("legacy_baseline", False):
        deja_vus = set(entree.get("ever_seen", []))
        nouveaux = sorted(liens - deja_vus)
        entree["ever_seen"] = sorted(deja_vus | liens)
        entree["current"] = sorted(liens)
        entree["last_count"] = nombre
        entree["anomaly_streak"] = 0
        entree["candidate_count"] = None
        alertes = alertes_nouveaux_produits(etat, lecture, nouveaux)
        print(
            f"    {lecture.shop} : référence v8 recalée sur {nombre} produits valides."
        )
        return alertes, True, None

    if nombre_precedent >= 5 and nombre < max(
        1, int(nombre_precedent * SEUIL_RATIO_RAYON)
    ):
        candidat = entree.get("candidate_count")
        tolerance = max(2, int(nombre * 0.10))
        meme_candidat = (
            candidat is not None and abs(int(candidat) - nombre) <= tolerance
        )
        entree["anomaly_streak"] = (
            int(entree.get("anomaly_streak", 0)) + 1 if meme_candidat else 1
        )
        entree["candidate_count"] = nombre
        if entree["anomaly_streak"] < CONFIRMATIONS_ACCEPTATION_RAYON:
            print(
                f"[!] {lecture.shop} : rayon partiel suspect "
                f"({nombre} au lieu de {nombre_precedent}), mémoire conservée."
            )
            return (
                [],
                True,
                Observation(
                    f"category:{lecture.url}",
                    f"{lecture.shop} – rayon {jeu_du_rayon(lecture.url) or nom_depuis_url(lecture.url)}",
                    lecture.shop,
                    False,
                    f"volume partiel : {nombre} au lieu de {nombre_precedent}",
                ),
            )
        print(
            f"[!] {lecture.shop} : baisse stable confirmée après "
            f"{CONFIRMATIONS_ACCEPTATION_RAYON} passages ; nouveau niveau accepté."
        )

    critique = precedent != liens or bool(entree.get("anomaly_streak"))
    entree["anomaly_streak"] = 0
    entree["candidate_count"] = None
    deja_vus = set(entree.get("ever_seen", []))
    nouveaux = sorted(liens - deja_vus)
    entree["ever_seen"] = sorted(deja_vus | liens)
    entree["current"] = sorted(liens)
    entree["last_count"] = nombre

    alertes = alertes_nouveaux_produits(etat, lecture, nouveaux)
    return alertes, critique or bool(nouveaux), None


def appliquer_sante(
    etat: dict[str, Any], observations: list[Observation]
) -> tuple[list[Alert], bool]:
    consolidees: dict[str, Observation] = {}
    for observation in observations:
        precedente = consolidees.get(observation.key)
        if precedente is None or (precedente.ok and not observation.ok):
            consolidees[observation.key] = observation

    maintenant = horodatage()
    nouvelles_pannes: list[str] = []
    retablissements: list[str] = []
    critique = False

    for key, observation in consolidees.items():
        suivi = etat["health"].setdefault(
            key,
            {
                "label": observation.label,
                "shop": observation.shop,
                "consecutive_failures": 0,
                "alerted": False,
                "last_success": None,
                "last_failure": None,
                "last_error": None,
            },
        )
        suivi["label"] = observation.label
        suivi["shop"] = observation.shop

        if observation.ok:
            if suivi.get("alerted"):
                retablissements.append(observation.label)
                critique = True
            if suivi.get("consecutive_failures"):
                critique = True
            suivi["consecutive_failures"] = 0
            suivi["alerted"] = False
            suivi["last_success"] = maintenant
            suivi["last_error"] = None
            continue

        ancien_compteur = int(suivi.get("consecutive_failures", 0))
        compteur = min(SEUIL_ALERTE_SANTE, ancien_compteur + 1)
        suivi["consecutive_failures"] = compteur
        suivi["last_failure"] = maintenant
        suivi["last_error"] = observation.error
        if compteur != ancien_compteur:
            critique = True
        if compteur >= SEUIL_ALERTE_SANTE and not suivi.get("alerted"):
            suivi["alerted"] = True
            nouvelles_pannes.append(
                f"{observation.label} — {observation.error or 'contrôle invalide'}"
            )
            critique = True

    alertes: list[Alert] = []
    if nouvelles_pannes:
        lignes = "\n".join(f"• {ligne}" for ligne in nouvelles_pannes[:8])
        supplement = (
            f"\n• +{len(nouvelles_pannes) - 8} autre(s) contrôle(s)"
            if len(nouvelles_pannes) > 8
            else ""
        )
        alertes.append(
            Alert(
                "health_down",
                maintenant,
                f"⚠️ SURVEILLANCE DÉGRADÉE\n\n{lignes}{supplement}",
            )
        )
    if retablissements:
        lignes = "\n".join(f"• {ligne}" for ligne in retablissements[:8])
        supplement = (
            f"\n• +{len(retablissements) - 8} autre(s) contrôle(s)"
            if len(retablissements) > 8
            else ""
        )
        alertes.append(
            Alert(
                "health_up",
                maintenant,
                f"✅ SURVEILLANCE RÉTABLIE\n\n{lignes}{supplement}",
            )
        )
    return alertes, critique


def ajouter_alertes(etat: dict[str, Any], alertes: list[Alert]) -> bool:
    existantes = {
        (element.get("kind"), element.get("key")) for element in etat["outbox"]
    }
    modifie = False
    for alerte in alertes:
        identite = (alerte.kind, alerte.key)
        if identite in existantes:
            continue
        etat["outbox"].append(
            {
                "id": uuid4().hex,
                "kind": alerte.kind,
                "key": alerte.key,
                "text": alerte.text,
                "url": alerte.url,
                "cart_url": alerte.cart_url,
                "created_at": horodatage(),
                "delivered": [],
            }
        )
        existantes.add(identite)
        modifie = True
    return modifie


def _cle_destinataire(chat: str) -> str:
    return hmac.new(
        TELEGRAM_TOKEN.encode("utf-8"),
        chat.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:20]


def envoyer_telegram(element: dict[str, Any], chat: str) -> bool:
    donnees: dict[str, Any] = {"chat_id": chat, "text": element["text"]}
    boutons = []
    if element.get("cart_url"):
        boutons.append({"text": "🛒 Ajouter au panier", "url": element["cart_url"]})
    if element.get("url"):
        boutons.append({"text": "Voir la fiche", "url": element["url"]})
    if boutons:
        donnees["reply_markup"] = json.dumps({"inline_keyboard": [boutons]})

    for tentative in range(3):
        try:
            reponse = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data=donnees,
                timeout=(8, 20),
            )
        except requests.RequestException as erreur:
            print(f"[!] Telegram : {erreur}")
        else:
            try:
                corps = reponse.json()
            except ValueError:
                corps = {}
            if reponse.status_code == 200 and corps.get("ok", True):
                return True
            if reponse.status_code == 429:
                attente = min(
                    30,
                    int(corps.get("parameters", {}).get("retry_after", 2)),
                )
                time.sleep(attente)
                continue
            print(
                f"[!] Telegram a refusé un envoi ({reponse.status_code}) : "
                f"{corps.get('description', 'réponse inconnue')}"
            )
        if tentative < 2:
            time.sleep(2**tentative)
    return False


def livrer_outbox(etat: dict[str, Any]) -> bool:
    if DRY_RUN or SHADOW:
        etiquette = "DRY RUN" if DRY_RUN else "SHADOW"
        for element in etat["outbox"]:
            print(
                f"\n--- [{etiquette}] message qui aurait été envoyé ---\n"
                f"{element['text']}\n"
            )
        if SHADOW and etat["outbox"]:
            etat["outbox"] = []
            return True
        return False

    modifie = False
    restantes = []
    cles_actuelles = {_cle_destinataire(chat) for chat in TELEGRAM_CHATS}
    for element in etat["outbox"]:
        livres = set(element.get("delivered", []))
        for chat in TELEGRAM_CHATS:
            cle = _cle_destinataire(chat)
            if cle in livres:
                continue
            if envoyer_telegram(element, chat):
                livres.add(cle)
                modifie = True
        element["delivered"] = sorted(livres)
        if not cles_actuelles.issubset(livres):
            restantes.append(element)
        else:
            modifie = True
    etat["outbox"] = restantes
    return modifie


def boutique_en_backoff(etat: dict[str, Any], nom: str) -> bool:
    suivi = etat["scheduler"]["shop_backoff"].get(nom, {})
    return float(suivi.get("until_epoch", 0) or 0) > time.time()


def actualiser_backoff(etat: dict[str, Any], resultat: ShopResult) -> bool:
    """Ralentit une boutique bloquée sans jamais transformer l'erreur en rupture."""
    suivi = etat["scheduler"]["shop_backoff"].setdefault(
        resultat.shop,
        {"failures": 0, "until_epoch": 0, "reason": None},
    )
    erreurs = [
        observation.error or ""
        for observation in resultat.observations
        if not observation.ok
    ]
    succes = any(observation.ok for observation in resultat.observations)
    dures = [
        erreur
        for erreur in erreurs
        if "HTTP 403" in erreur
        or "HTTP 429" in erreur
        or "anti-bot" in erreur.lower()
    ]
    panne_complete = bool(erreurs) and not succes

    if not dures and not panne_complete:
        if suivi.get("failures") or suivi.get("until_epoch"):
            suivi.update({"failures": 0, "until_epoch": 0, "reason": None})
            return True
        return False

    compteur = min(8, int(suivi.get("failures", 0)) + 1)
    erreur = (dures or erreurs)[0]
    if "HTTP 429" in erreur:
        trouve = re.search(r"retry_after=(\d+)", erreur)
        delai = int(trouve.group(1)) if trouve else 120
        delai = max(delai, min(3600, 60 * (2 ** (compteur - 1))))
    elif "HTTP 403" in erreur or "anti-bot" in erreur.lower():
        delai = min(6 * 3600, 300 * (2 ** (compteur - 1)))
    else:
        delai = min(1800, 60 * (2 ** (compteur - 1)))
    suivi.update(
        {
            "failures": compteur,
            "until_epoch": time.time() + delai,
            "reason": erreur,
            "updated_at": horodatage(),
        }
    )
    print(f"[!] {resultat.shop} en backoff {delai}s : {erreur}")
    return True


def dynamiques_echus(etat: dict[str, Any], nom_boutique: str) -> list[str]:
    maintenant = time.time()
    return [
        url
        for url, fiche in etat.get("dynamic_products", {}).items()
        if fiche.get("active", True)
        and fiche.get("shop") == nom_boutique
        and float(fiche.get("next_check_epoch", 0) or 0) <= maintenant
    ]


def replanifier_dynamiques(etat: dict[str, Any], urls: list[str]) -> None:
    maintenant = time.time()
    for url in urls:
        fiche = etat.get("dynamic_products", {}).get(url)
        if not fiche:
            continue
        fiche["last_scheduled"] = horodatage()
        fiche["next_check_epoch"] = maintenant + random.randint(*CADENCE_CIBLE)


def attente_prochain_tour(etat: dict[str, Any]) -> int:
    """Réveille le moteur à l'échéance d'une cible, sans scruter plus souvent."""
    attente_base = max(5, INTERVALLE + random.randint(-JITTER, JITTER))
    maintenant = time.time()
    echeances = [
        float(fiche.get("next_check_epoch", 0) or 0)
        for fiche in etat.get("dynamic_products", {}).values()
        if fiche.get("active", True)
        and float(fiche.get("next_check_epoch", 0) or 0) > maintenant
    ]
    if not echeances:
        return attente_base
    jusqu_a_cible = max(1, int(min(echeances) - maintenant + 0.999))
    return min(attente_base, jusqu_a_cible)


def heartbeat_quotidien(etat: dict[str, Any]) -> Alert | None:
    try:
        heure, minute = (int(partie) for partie in HEARTBEAT_HOUR.split(":", 1))
        locale = datetime.now(ZoneInfo(BOT_TIMEZONE))
    except (ValueError, KeyError):
        return None
    date = locale.date().isoformat()
    if (locale.hour, locale.minute) < (heure, minute):
        return None
    if etat["daily_heartbeat"].get("last_date") == date:
        return None

    etat["daily_heartbeat"]["last_date"] = date
    controles_en_panne = {
        suivi.get("shop")
        for suivi in etat["health"].values()
        if suivi.get("alerted")
    }
    saines = len({conf["nom"] for conf in BOUTIQUES.values()} - controles_en_panne)
    dynamiques = len(etat.get("dynamic_products", {}))
    dernier_tour = etat.get("heartbeat", {}).get("last_cycle") or "pas encore"
    return Alert(
        "daily_heartbeat",
        date,
        "💚 BOT TCG ACTIF\n\n"
        f"{saines}/{len(BOUTIQUES)} boutiques sans panne déclarée\n"
        f"{len(etat['products'])} fiches connues, "
        f"{dynamiques} sortie(s) ciblée(s) auto-suivie(s)\n"
        f"Dernier cycle : {dernier_tour}",
    )


def faire_un_tour(etat: dict[str, Any]) -> tuple[bool, list[Alert], int]:
    a_traiter: list[tuple[str, dict[str, Any], bool, list[str]]] = []
    for domaine, conf in BOUTIQUES.items():
        if boutique_en_backoff(etat, conf["nom"]):
            continue
        full_run = not trop_tot(conf)
        dynamiques = (
            []
            if conf.get("type") == "shopify"
            else dynamiques_echus(etat, conf["nom"])
        )
        if full_run or dynamiques:
            a_traiter.append((domaine, conf, full_run, dynamiques))
    if not a_traiter:
        heartbeat = heartbeat_quotidien(etat)
        return bool(heartbeat), [heartbeat] if heartbeat else [], 0

    resultats: list[ShopResult] = []
    with ThreadPoolExecutor(max_workers=len(a_traiter)) as executeur:
        futurs = {
            executeur.submit(
                traiter_boutique, domaine, conf, full_run, dynamiques
            ): (conf, full_run, dynamiques)
            for domaine, conf, full_run, dynamiques in a_traiter
        }
        for futur in as_completed(futurs):
            conf, full_run, dynamiques = futurs[futur]
            try:
                resultats.append(futur.result())
            except Exception as erreur:  # noqa: BLE001 - isole une boutique
                print(f"[!] Erreur interne {conf['nom']} : {erreur}")
                resultat = ShopResult(
                    conf["nom"],
                    full_run=full_run,
                    dynamic_urls=dynamiques,
                )
                _observation(
                    resultat,
                    f"shop:{conf['nom']}",
                    conf["nom"],
                    False,
                    f"erreur interne : {erreur}",
                )
                resultats.append(resultat)

    critique = False
    alertes: list[Alert] = []
    observations: list[Observation] = []
    lectures: dict[str, ProductReading] = {}

    for resultat in resultats:
        if resultat.full_run:
            DERNIER_PASSAGE[resultat.shop] = time.time()
        replanifier_dynamiques(etat, resultat.dynamic_urls)
        critique = actualiser_backoff(etat, resultat) or critique
        observations.extend(resultat.observations)
        for lecture in resultat.products:
            dynamique = etat.get("dynamic_products", {}).get(lecture.url)
            if dynamique and dynamique.get("name"):
                lecture.name = dynamique["name"]
            precedente = lectures.get(lecture.url)
            if precedente and precedente.status is not lecture.status:
                observations.append(
                    Observation(
                        f"product:{lecture.url}",
                        f"{lecture.shop} – {lecture.name[:70]}",
                        lecture.shop,
                        False,
                        "statuts contradictoires entre deux rayons",
                    )
                )
                lectures.pop(lecture.url, None)
            elif lecture.url not in lectures:
                lectures[lecture.url] = lecture

    for lecture in lectures.values():
        nouvelles, change = appliquer_stock(etat, lecture)
        alertes.extend(nouvelles)
        critique = critique or change

    for resultat in resultats:
        for lecture in resultat.categories:
            nouvelles, change, anomalie = appliquer_categorie(etat, lecture)
            alertes.extend(nouvelles)
            critique = critique or change
            if anomalie:
                observations.append(anomalie)

    sante, change_sante = appliquer_sante(etat, observations)
    alertes.extend(sante)
    critique = critique or change_sante

    quotidien = heartbeat_quotidien(etat)
    if quotidien:
        alertes.append(quotidien)
        critique = True

    uniques: dict[tuple[str, str], Alert] = {}
    for alerte in alertes:
        uniques.setdefault((alerte.kind, alerte.key), alerte)
    alertes = list(uniques.values())

    etat["passages"] = dict(DERNIER_PASSAGE)
    etat["heartbeat"] = {
        "version": VERSION,
        "last_cycle": horodatage(),
        "shops_checked": len(resultats),
    }
    return critique, alertes, len(resultats)


def ping_healthcheck() -> None:
    if not HEALTHCHECK_URL or DRY_RUN or SHADOW:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=(5, 10))
    except requests.RequestException:
        print("[!] Le ping du moniteur externe a échoué.")


def main() -> None:
    if VERSION_ONLY:
        print(f"restock_bot {VERSION} — schéma d'état {SCHEMA_ETAT}")
        return

    etat, migre = charger_etat()
    if MIGRATE_ONLY:
        sauver_etat(etat)
        print(
            f"État prêt pour la v10 : {len(etat['products'])} produits, "
            f"{len(etat['categories'])} rayons."
        )
        return

    if DRY_RUN:
        etat = copy.deepcopy(etat)
    elif migre:
        sauver_etat(etat)
        pousser_etat(force=True, raison="migration")

    DERNIER_PASSAGE.update(
        {cle: float(valeur) for cle, valeur in etat.get("passages", {}).items()}
    )

    if not TELEGRAM_TOKEN or not TELEGRAM_CHATS:
        print("[!] Renseigne TG_TOKEN et TG_CHAT, ou crée secrets.local.")
        if not DRY_RUN and not SHADOW:
            raise SystemExit(1)

    if DRY_RUN:
        mode = " — DRY RUN, aucun envoi ni fichier modifié"
    elif SHADOW:
        mode = f" — SHADOW, Telegram coupé, état séparé : {FICHIER_ETAT}"
    else:
        mode = ""
    rayons_shopify = sum(
        len(conf.get("collections", [])) for conf in BOUTIQUES.values()
    )
    print(f"Surveillance démarrée [{VERSION}]{mode}.")
    print(f"{len(PRODUITS)} fiches et {len(RAYONS) + rayons_shopify} rayons suivis.")
    print(
        f"{len(TELEGRAM_CHATS)} destinataire(s) Telegram."
        if not SHADOW
        else "Telegram désactivé pour cette observation."
    )
    if EN_ACTIONS:
        print(
            f"Exécution longue de {DUREE_MAX / 3600:.1f} h maximum ; "
            f"tour nominal toutes les {INTERVALLE // 60} min."
        )
    print()

    debut = time.monotonic()
    while True:
        print(f"--- Tour de {time.strftime('%H:%M:%S')} ---")
        critique, alertes, boutiques_verifiees = faire_un_tour(etat)
        critique = ajouter_alertes(etat, alertes) or critique

        if DRY_RUN:
            livrer_outbox(etat)
        else:
            sauver_etat(etat)
            pousser_etat(
                force=critique,
                raison="changement" if critique else "checkpoint",
            )
            if livrer_outbox(etat):
                sauver_etat(etat)
                pousser_etat(force=True, raison="telegram")
            ping_healthcheck()
        print()

        if UNE_FOIS:
            break

        ecoule = time.monotonic() - debut
        attente = attente_prochain_tour(etat)
        if EN_ACTIONS and ecoule + attente > DUREE_MAX:
            print(
                f"Fin normale après {ecoule / 3600:.1f} h. "
                "Le workflow suivant prendra immédiatement la relève."
            )
            break
        if boutiques_verifiees == 0:
            attente = min(attente, 60)
        time.sleep(attente)

    if not DRY_RUN:
        sauver_etat(etat)
        pousser_etat(force=True, raison="fin-run")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nArrêt.")
