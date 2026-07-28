# Bot restock TCG

Bot d'alertes Telegram pour les restocks et les ouvertures de précommande
sur les cartes Pokémon et One Piece.

## Ce qu'il surveille

- **Fiches produit** : prévient quand une référence en rupture repasse en stock.
- **Rayons entiers** : prévient quand un produit inédit apparaît au catalogue,
  ce qui correspond en général à l'ouverture d'une précommande.

Boutiques gérées : UltraJeux, Micromania, Play-in.
Chaque boutique a sa propre cadence de vérification, pour éviter de se faire
bloquer par les protections anti-robot.

## Utilisation en local

```bash
pip install requests
python restock_bot.py --dry-run   # test, n'envoie rien
python restock_bot.py             # surveillance en continu
```

Les identifiants Telegram se lisent dans les variables d'environnement
`TG_TOKEN` et `TG_CHAT`, ou dans un fichier `secrets.local` de deux lignes
(le token, puis l'identifiant de conversation). Ce fichier est ignoré par git.

## Utilisation via GitHub Actions

Le workflow `.github/workflows/restock.yml` lance un tour toutes les 5 minutes
et réenregistre la mémoire du bot dans le dépôt. Il faut déclarer `TG_TOKEN`
et `TG_CHAT` dans les secrets du dépôt.

## Ajouter un produit

Colle simplement l'URL de sa fiche dans la liste `PRODUITS`. Le script
reconnaît la boutique et en déduit le nom tout seul.
