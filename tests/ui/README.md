# Tests du rendu Markdown

Ces tests ouvrent Chromium sur la vraie page HTML. Le serveur de fichiers et les
réponses de chat sont locaux ; aucun appel Azure, SQL ou MoulSot n'est effectué.
Les prix sont fictifs. Node.js et Playwright servent seulement aux tests.

Depuis la racine du projet :

```powershell
npm install --prefix tests/ui --no-save playwright
npx --prefix tests/ui playwright install chromium
node --test tests/ui/test_markdown.mjs
```

Vérifications : gras/listes/tableaux, conservation des montants, défilement mobile,
direction arabe, utilisateur/OCR/sources en texte brut, filtrage des contenus HTML
et liens dangereux, absence de chargement d'images distantes et repli en texte brut
si un script manque. `python -m pytest -q` vérifie aussi les routes de fichiers.

Le respect des nouvelles consignes de style doit ensuite être évalué avec le vrai
modèle, en français et en darija, sur une conversation de plusieurs tours.

## Parcours pharmacies

Le test `test_pharmacies.mjs` démarre un serveur FastAPI temporaire, un catalogue
fictif et un modèle simulé. Il ne contacte aucun service externe. Deux contextes
navigateur séparent patient et pharmacien. Le second cas couvre le brouillon
préparé par le chat sur mobile.

```powershell
$env:TEST_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
node --test tests/ui/test_pharmacies.mjs
```

Installer Playwright et son Chromium selon les instructions précédentes.
`TEST_PYTHON` doit désigner le Python de l'environnement du projet.
Les variables `PLAYWRIGHT_MODULE`, `CHROMIUM_PACKAGE_PATH` et
`UI_SCREENSHOT_DIR` des tests existants sont également prises en charge.

## Fiches provenant de l'API Google

`test_pharmacy_apis.mjs` démarre la même application en mode live sur une base
temporaire, avec uniquement des données **synthétiques** et un transport HTTP
Google simulé. Il vérifie l'attribution, le refus d'envoi aux non-partenaires,
la confirmation du brouillon, le mobile et un retour explicite en cas de quota.
Il n'utilise aucune clé ni aucune pharmacie réelle.

```powershell
node --test tests/ui/test_pharmacy_apis.mjs
```

Les tests pharmacies couvrent aussi le relais humain dans le même chat patient,
la reprise de l’assistant, la récupération après rechargement, la confidentialité
des messages et le dashboard pharmacien sur desktop/mobile. Aucun partenaire
réel ni fournisseur externe n’est contacté. Le parcours de demande sans relais
reste vérifié et le choix de consentement survit à l’actualisation des fiches.
