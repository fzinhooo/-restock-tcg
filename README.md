# Bot restock TCG — v9

Bot Telegram pour les restocks, nouveautés et ouvertures de précommande
Pokémon / One Piece.

## Boutiques

| Boutique | Surveillance | Cadence |
| --- | --- | --- |
| UltraJeux | Fiches + rayons HTML | 5 min |
| Play-in | Fiches + rayons HTML filtrés par jeu | 5 min |
| Micromania | Fiches + rayons HTML | 20 min |
| Blazingtail | Rayons HTML | 5 min |
| KingDultes | Collections Shopify, nouveautés et restocks | 5 min |

KingDultes bénéficie aussi d'un bouton Telegram **Ajouter au panier**, fondé
sur la première variante réellement disponible.

## Fiabilité de la v9

- Trois états : `in_stock`, `out_of_stock`, `unknown`. Une erreur réseau ou un
  captcha ne devient jamais une rupture.
- Deux lectures de rupture sont exigées après un produit disponible. Cela évite
  qu'une lecture HTML incorrecte crée ensuite un faux restock.
- Le JSON-LD `Product/Offer` est analysé avant le marqueur schema.org global.
- Un rayon vide ou brutalement réduit ne remplace pas immédiatement la mémoire.
- Les URL sont normalisées et les pages suivantes déclarées avec `rel=next`
  sont parcourues.
- Après trois échecs consécutifs, Telegram signale la surveillance dégradée.
  Un message confirme ensuite son rétablissement.
- Les alertes non livrées restent dans une file persistante. Avec plusieurs
  destinataires, seuls les envois manquants sont retentés.
- `etat_stock.json` est écrit atomiquement et la v8 est migrée sans rafale
  d'alertes.

## Utilisation locale

```bash
python -m pip install requests
python restock_bot.py --dry-run --une-fois
python restock_bot.py
```

Le dry-run ne modifie ni l'état ni Telegram. Pour convertir uniquement un
ancien fichier :

```bash
python restock_bot.py --migrate-only
```

Les identifiants peuvent être fournis avec :

- `TG_TOKEN` : token du bot ;
- `TG_CHAT` : un ou plusieurs chat IDs séparés par une virgule ;
- ou `secrets.local`, avec le token sur la première ligne et les destinataires
  sur la seconde.

`secrets.local` et `.env` doivent rester ignorés par Git.

## GitHub Actions

Placer `restock.yml` dans `.github/workflows/restock.yml`, puis créer les
secrets `TG_TOKEN` et `TG_CHAT`.

Une exécution surveille pendant environ 5 h 20 et déclenche elle-même sa relève.
Le cron à `13` et `43` minutes sert uniquement de filet de sécurité. La
concurrence garantit une exécution active et garde seulement la relève la plus
récente, sans empiler d'anciens runs.

Le workflow récupère explicitement la dernière version de la branche par
défaut. Il ne redémarre donc pas avec l'état correspondant à un ancien
déclenchement resté en attente.

L'état est committé immédiatement après un changement critique ou une alerte,
puis au maximum une fois par heure comme checkpoint. Un dernier commit est fait
à la fin de chaque run.

### Moniteur externe facultatif

Créer un secret `HEALTHCHECK_URL` contenant l'URL de ping d'un service de
dead-man switch. Le bot la contacte après chaque tour. Si GitHub Actions cesse
complètement de fonctionner, le service extérieur peut alors prévenir de
l'absence de pouls.

## Ajouter une surveillance

- Fiche produit : ajouter son URL dans `PRODUITS`.
- Rayon HTML : ajouter son URL dans `RAYONS` si sa boutique est déjà configurée.
- Shopify : ajouter le slug de collection dans `collections`.

Une nouvelle URL déjà disponible est mémorisée silencieusement. Elle ne sera
annoncée comme restock qu'après une rupture confirmée, conformément au
comportement historique du bot.

## Diagnostic

Les logs distinguent :

- `[+]` : restock ou nouveauté ;
- `[-]` : rupture confirmée ;
- `[?]` : état illisible, mémoire conservée ;
- `[!]` : requête, parseur ou rayon dégradé.

Un rond jaune dans Actions indique normalement l'exécution longue active.
Le commit d'état peut dater d'une heure sans que le bot soit arrêté.
