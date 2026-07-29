Bot restock TCG — v9

Bot Telegram pour les restocks, nouveautés et ouvertures de précommandePokémon / One Piece.

Boutiques

Boutique

Surveillance

Cadence

UltraJeux

Fiches + rayons HTML

5 min

Play-in

Fiches + rayons HTML filtrés par jeu

5 min

Micromania

Fiches + rayons HTML

20 min

Blazingtail

Rayons HTML

5 min

KingDultes

Collections Shopify, nouveautés et restocks

5 min

KingDultes bénéficie aussi d'un bouton Telegram Ajouter au panier, fondésur la première variante réellement disponible.

Fiabilité de la v9

Trois états : in_stock, out_of_stock, unknown. Une erreur réseau ou uncaptcha ne devient jamais une rupture.

Deux lectures de rupture sont exigées après un produit disponible. Cela évitequ'une lecture HTML incorrecte crée ensuite un faux restock.

Le JSON-LD Product/Offer est analysé avant le marqueur schema.org global.

Un rayon vide ou brutalement réduit ne remplace pas immédiatement la mémoire.

Les URL sont normalisées et les pages suivantes déclarées avec rel=nextsont parcourues.

Après trois échecs consécutifs, Telegram signale la surveillance dégradée.Un message confirme ensuite son rétablissement.

Les alertes non livrées restent dans une file persistante. Avec plusieursdestinataires, seuls les envois manquants sont retentés.

etat_stock.json est écrit atomiquement et la v8 est migrée sans rafaled'alertes.

Utilisation locale

python -m pip install requests
python restock_bot.py --dry-run --une-fois
python restock_bot.py

Le dry-run ne modifie ni l'état ni Telegram. Pour convertir uniquement unancien fichier :

python restock_bot.py --migrate-only

Les identifiants peuvent être fournis avec :

TG_TOKEN : token du bot ;

TG_CHAT : un ou plusieurs chat IDs séparés par une virgule ;

ou secrets.local, avec le token sur la première ligne et les destinatairessur la seconde.

secrets.local et .env doivent rester ignorés par Git.

GitHub Actions

Placer restock.yml dans .github/workflows/restock.yml, puis créer lessecrets TG_TOKEN et TG_CHAT.

Une exécution surveille pendant environ 5 h 20 et déclenche elle-même sa relève.Le cron à 13 et 43 minutes sert uniquement de filet de sécurité. Laconcurrence garantit une exécution active et garde seulement la relève la plusrécente, sans empiler d'anciens runs.

Le workflow récupère explicitement la dernière version de la branche pardéfaut. Il ne redémarre donc pas avec l'état correspondant à un anciendéclenchement resté en attente.

L'état est committé immédiatement après un changement critique ou une alerte,puis au maximum une fois par heure comme checkpoint. Un dernier commit est faità la fin de chaque run.

Moniteur externe facultatif

Créer un secret HEALTHCHECK_URL contenant l'URL de ping d'un service dedead-man switch. Le bot la contacte après chaque tour. Si GitHub Actions cessecomplètement de fonctionner, le service extérieur peut alors prévenir del'absence de pouls.

Ajouter une surveillance

Fiche produit : ajouter son URL dans PRODUITS.

Rayon HTML : ajouter son URL dans RAYONS si sa boutique est déjà configurée.

Shopify : ajouter le slug de collection dans collections.

Une nouvelle URL déjà disponible est mémorisée silencieusement. Elle ne seraannoncée comme restock qu'après une rupture confirmée, conformément aucomportement historique du bot.

Diagnostic

Les logs distinguent :

[+] : restock ou nouveauté ;

[-] : rupture confirmée ;

[?] : état illisible, mémoire conservée ;

[!] : requête, parseur ou rayon dégradé.

Un rond jaune dans Actions indique normalement l'exécution longue active.Le commit d'état peut dater d'une heure sans que le bot soit arrêté.
