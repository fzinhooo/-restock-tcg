import sys
import types
import unittest

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    # Les tests unitaires purs restent exécutables dans un environnement minimal.
    sys.modules["requests"] = types.ModuleType("requests")

import restock_bot as bot


class DetectionStockTests(unittest.TestCase):
    def test_version_v10(self):
        self.assertEqual(bot.VERSION, "v10")
        self.assertEqual(bot.SCHEMA_ETAT, 10)

    def test_schema_json_ld_in_stock(self):
        html = """
        <script type="application/ld+json">
        {"@type":"Product","offers":{"availability":
        "https://schema.org/InStock"}}
        </script>
        """
        self.assertEqual(bot.stock_schema(html), bot.StockStatus.IN_STOCK)

    def test_schema_contradictoire_reste_inconnu(self):
        html = (
            '<link href="https://schema.org/InStock">'
            '<meta content="https://schema.org/OutOfStock">'
        )
        self.assertEqual(bot.stock_schema(html), bot.StockStatus.UNKNOWN)

    def test_ultrajeux(self):
        self.assertEqual(
            bot.stock_ultrajeux('<b style="font-size:18px">Indisponible</b>'),
            bot.StockStatus.OUT_OF_STOCK,
        )


class SortiesCibleesTests(unittest.TestCase):
    def test_normalisation_accents_et_ponctuation(self):
        self.assertEqual(
            bot.normaliser_texte("Règne—Delta / Célébrations !"),
            "regne delta celebrations",
        )

    def test_op17_nom_officiel(self):
        trouvee = bot.classifier_sortie(
            "OP-17 Les Guerriers les plus puissants au monde",
            "https://exemple.test/op-17",
            "one-piece",
        )
        self.assertEqual(trouvee[0], "one_piece_op17")

    def test_dp12_est_rattache_a_op17(self):
        trouvee = bot.classifier_sortie(
            "Double Pack Set Vol.12",
            "https://exemple.test/dp-12",
            "one-piece",
        )
        self.assertEqual(trouvee[0], "one_piece_op17")

    def test_regne_delta(self):
        trouvee = bot.classifier_sortie(
            "Display Pokémon ME06 Règne Delta",
            "https://exemple.test/regne-delta",
            "pokemon",
        )
        self.assertEqual(trouvee[0], "pokemon_me06_regne_delta")

    def test_anniversaire_generique_exige_pokemon(self):
        self.assertIsNone(
            bot.classifier_sortie(
                "Coffret anniversaire",
                "https://exemple.test/anniversaire",
                "one-piece",
            )
        )
        self.assertEqual(
            bot.classifier_sortie(
                "Coffret célébrations",
                "https://exemple.test/celebrations",
                "pokemon",
            )[0],
            "pokemon_30e_anniversaire",
        )

    def test_un_tournoi_celebration_n_est_pas_un_produit_cible(self):
        self.assertIsNone(
            bot.classifier_sortie(
                "Tournoi JCC Pokémon célébration de mi-année",
                "https://exemple.test/tournoi-celebration",
                "pokemon",
            )
        )


class EtatEtRayonsTests(unittest.TestCase):
    def test_migration_v9_vers_v10(self):
        ancien = {
            "schema_version": 9,
            "products": {"https://example.test/a": {"status": "out_of_stock"}},
            "categories": {},
            "health": {},
            "outbox": [],
            "passages": {},
            "heartbeat": {"version": "v9", "last_cycle": None},
        }
        etat, migre = bot.migrer_etat(ancien)
        self.assertTrue(migre)
        self.assertEqual(etat["schema_version"], 10)
        self.assertIn("dynamic_products", etat)
        self.assertEqual(
            etat["products"]["https://example.test/a"]["status"],
            "out_of_stock",
        )

    def test_premier_rayon_silencieux_puis_auto_suivi(self):
        etat = bot.etat_vide()
        rayon = "https://www.play-in.com/fr/gamme/24/one-piece/catalogue"
        ancien = "https://www.play-in.com/fr/produit/1/ancien-one-piece"
        nouveau = (
            "https://www.play-in.com/fr/produit/2/"
            "display-op-17-les-guerriers-les-plus-puissants-one-piece-fr"
        )
        alertes, _, _ = bot.appliquer_categorie(
            etat,
            bot.CategoryReading(
                rayon, "Play-in", {ancien}, game="one-piece"
            ),
        )
        self.assertEqual(alertes, [])

        alertes, _, _ = bot.appliquer_categorie(
            etat,
            bot.CategoryReading(
                rayon,
                "Play-in",
                {ancien, nouveau},
                {nouveau: "Display OP-17"},
                game="one-piece",
            ),
        )
        self.assertEqual(alertes[0].kind, "release_new")
        self.assertEqual(
            etat["dynamic_products"][nouveau]["target"],
            "one_piece_op17",
        )

    def test_cible_deja_presente_est_amorcee_sans_alerte(self):
        etat = bot.etat_vide()
        url = (
            "https://www.blazingtail.fr/"
            "9999-display-me06-regne-delta-pokemon.html"
        )
        alertes, _, _ = bot.appliquer_categorie(
            etat,
            bot.CategoryReading(
                "https://www.blazingtail.fr/2060-nouveautes",
                "Blazingtail",
                {url},
                game="pokemon",
            ),
        )
        self.assertEqual(alertes, [])
        self.assertEqual(
            etat["dynamic_products"][url]["target"],
            "pokemon_me06_regne_delta",
        )

    def test_sitemap_ne_garde_que_les_fiches(self):
        conf = bot.BOUTIQUES["play-in.com"]
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.play-in.com/fr/produit/42/display-op-17</loc></url>
          <url><loc>https://www.play-in.com/fr/gamme/24/one-piece</loc></url>
        </urlset>
        """
        self.assertEqual(
            bot.extraire_urls_sitemap(xml, conf),
            {"https://www.play-in.com/fr/produit/42/display-op-17"},
        )

    def test_restock_cible_devient_alerte_prioritaire(self):
        etat = bot.etat_vide()
        url = "https://www.play-in.com/fr/produit/42/display-op-17-one-piece"
        etat["dynamic_products"][url] = {
            "name": "Display OP-17",
            "shop": "Play-in",
            "target": "one_piece_op17",
        }
        etat["products"][url] = {
            "status": "out_of_stock",
            "out_confirmations": 2,
        }
        alertes, _ = bot.appliquer_stock(
            etat,
            bot.ProductReading(
                url,
                "Display OP-17",
                "Play-in",
                bot.StockStatus.IN_STOCK,
            ),
        )
        self.assertEqual(alertes[0].kind, "target_available")
        self.assertIn("PRÉCOMMANDE / STOCK OUVERT", alertes[0].text)

    def test_429_active_un_backoff(self):
        etat = bot.etat_vide()
        resultat = bot.ShopResult("Play-in")
        resultat.observations.append(
            bot.Observation(
                "shop:Play-in",
                "Play-in",
                "Play-in",
                False,
                "HTTP 429; retry_after=180",
            )
        )
        self.assertTrue(bot.actualiser_backoff(etat, resultat))
        suivi = etat["scheduler"]["shop_backoff"]["Play-in"]
        self.assertGreaterEqual(suivi["until_epoch"], bot.time.time() + 175)


if __name__ == "__main__":
    unittest.main()
