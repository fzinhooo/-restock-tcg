name: Bot restock TCG

on:
  workflow_dispatch:
  schedule:
    # GitHub ne garantit pas l'heure : les tours planifies sont retardes, et
    # parfois abandonnes. On ne lui demande donc plus de relancer le bot sans
    # arret. Une execution surveille toute seule pendant 5 h 20 ; ce minuteur
    # ne sert qu'a en redemarrer une quand la precedente se termine.
    # Minutes volontairement decalees : le debut d'heure est le pire moment.
    - cron: "13,43 * * * *"

# Une seule execution a la fois. Si une nouvelle est declenchee pendant qu'une
# autre tourne, elle attend son tour au lieu de la remplacer : la surveillance
# ne s'interrompt jamais.
concurrency:
  group: restock
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  surveiller:
    runs-on: ubuntu-latest
    timeout-minutes: 345          # 5 h 45, la limite de GitHub etant 6 h

    steps:
      - name: Recuperer le depot
        uses: actions/checkout@v4

      - name: Installer Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Installer les dependances
        run: pip install requests

      - name: Lancer un tour de surveillance
        env:
          TG_TOKEN: ${{ secrets.TG_TOKEN }}
          TG_CHAT: ${{ secrets.TG_CHAT }}
        run: python restock_bot.py

      - name: Sauvegarder l'etat (filet de securite)
        if: always()
        run: |
          git config user.name "bot"
          git config user.email "bot@users.noreply.github.com"
          git add etat_stock.json
          git diff --cached --quiet && exit 0
          git commit -m "etat $(date -u '+%Y-%m-%d %H:%M UTC')"
          git pull --rebase --quiet || true
          git push --quiet
