# Assistant médicaments — nouvelle base

Prototype indépendant avec DeepSeek, données Azure SQL, transcription locale MoulSot
et lecture des photos de boîtes avec Azure Vision OCR.
La conversation peut présenter plusieurs prix puis mémoriser une précision telle que
« j'ai la boîte de 12 ». Aucun menu de sélection obligatoire. Une page web de test
est fournie ; ce n'est pas Streamlit.

## Démarrage sur Windows

Dans ce nouveau dossier, avec Python 3.11 ou 3.12 :

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[azure,test]"
Copy-Item .env.example .env
```

Renseigner `.env` avec les paramètres du fournisseur choisi :

- API DeepSeek directe : `LLM_PROVIDER=deepseek`, `LLM_BASE_URL=https://api.deepseek.com`,
  `LLM_MODEL=deepseek-flash`, clé DeepSeek dans `LLM_API_KEY`.
- Azure Foundry : `LLM_PROVIDER=azure`, URL **d'inférence v1** fournie par Azure
  (finissant par `/openai/v1/`), nom exact du déploiement dans `LLM_MODEL`, clé Azure
  correspondante dans `LLM_API_KEY`. L'URL du projet Foundry n'est pas cet endpoint.
  La présence du modèle dans ton abonnement n'est pas vérifiée par ce prototype.
- Conserver le serveur, la base et l'utilisateur SQL déjà utilisés, puis renseigner
  `AZURE_SQL_PASSWORD`. Le catalogue est lu sans modification. Le module pharmacies facultatif possède ses propres tables, créées uniquement par une commande explicite.
- `MOULSOT_BASE_URL=http://127.0.0.1:8001` si MoulSot tourne sur le même PC.
  Le service MoulSot reste dans son environnement GPU séparé, dans WSL pour ton installation.
- Photos facultatives : renseigner `AZURE_VISION_ENDPOINT` et `AZURE_VISION_KEY`
  avec les valeurs de la ressource **Azure Vision / Computer Vision** compatible
  Image Analysis 4.0. Utiliser son endpoint HTTPS racine, par exemple
  `https://nom-ressource.cognitiveservices.azure.com/`. La clé DeepSeek et son
  endpoint `/openai/v1/` ne conviennent pas à ce service. Aucune ressource Azure
  n'est créée par le code. Sans ces paramètres, le texte et le vocal restent utilisables.

```powershell
.\.venv\Scripts\python.exe -m uvicorn pharmacy_next.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Ouvrir **http://localhost:8000**. Documentation API : **http://localhost:8000/docs**.
Le micro fonctionne sur localhost avec l'autorisation du navigateur. L'enregistrement
s'arrête après 25 secondes. Un fichier vocal de 30 secondes maximum peut être joint.

Sous Linux/WSL : `python3 -m venv .venv`, `source .venv/bin/activate`,
`python -m pip install -e '.[azure,test]'`, puis la même commande Uvicorn.
Les dépendances système du pilote SQL dépendent de l'OS : voir la documentation
[mssql-python](https://github.com/microsoft/mssql-python).

## Ce qui est construit

- `POST /api/v1/chat` : JSON `{"message":"...", "session_id":null}`.
  Réutiliser le `session_id` reçu pour poursuivre la conversation.
- `POST /api/v1/chat/audio` : multipart `file` et `session_id` facultatif.
  La transcription brute est envoyée au même dialogue et renvoyée dans la réponse.
- `POST /api/v1/chat/image` : multipart `file` (JPG/PNG), `message` facultatif
  (question jointe, 5000 caractères maximum), `session_id` facultatif. Renvoie
  la réponse habituelle et `image_analysis` : texte OCR, lignes, confiance par
  mot, état de lecture et demande éventuelle de confirmation.
- `DELETE /api/v1/sessions/{session_id}` : nouvelle discussion.
- `GET /health` : état local et configuration, sans appel payant au modèle.

Les outils sont des fonctions Python exécutées par FastAPI après validation des
arguments. DeepSeek choisit quand les demander et formule la réponse. Le SDK
`openai` est un client HTTP compatible ; il n'impose pas un modèle GPT.

Les outils disponibles sont `search_medications`, `get_medication_details`,
`search_notices`, `read_notice`, `remember_context`, `estimate_cnops_reimbursement`,
`estimate_cnss_reimbursement`
et `find_nearby_pharmacy`.
Le lien Maps ne fait pas partie du rendu automatique et doit être demandé dans
la conversation. Le modèle reçoit cette consigne ; son respect reste à évaluer.

## Affichage des réponses

Le champ JSON `answer` reste une chaîne de caractères. La page interprète son
Markdown avec Marked, puis nettoie le HTML avec DOMPurify avant insertion dans
le DOM : gras, listes, paragraphes et tableaux. Les tableaux défilent
horizontalement sur petit écran. Les messages utilisateur, le texte OCR et les
sources restent affichés comme du texte brut.

Seuls les éléments de mise en forme autorisés sont conservés. Les images du
modèle, scripts, styles, formulaires et attributs d'événements sont exclus ;
seuls les liens absolus HTTP(S) peuvent être ouverts, avec `noopener noreferrer`.
Le rendu n'ajoute aucun lien Maps. Si les bibliothèques ne sont pas disponibles,
la réponse reste lisible en texte brut.

Les bibliothèques sont livrées dans `static/vendor`, avec versions et licences :
aucun CDN ni installation npm nécessaire au démarrage. FastAPI sert `/static`.
Les consignes de style privilégient les réponses courtes ; les tableaux servent
aux comparaisons utiles. Ces consignes doivent encore être évaluées avec le vrai
modèle en darija. Les calculs et données de remboursement ne sont pas modifiés.

Après application de cette mise à jour : redémarrer FastAPI, puis actualiser la
page avec Ctrl+F5. Conserver le `.env` existant. Les tests navigateur sont
documentés dans `tests/ui/README.md`.

## Photos de boîtes

Dans la même interface, cliquer sur **Joindre une photo**, vérifier l'aperçu,
ajouter éventuellement « ch7al taman ? » et envoyer. On peut ensuite poursuivre
par écrit ou par vocal dans la même discussion.

Le backend vérifie les octets du fichier puis appelle Azure Vision Image Analysis
4.0 via REST (`api-version=2024-02-01`, seule fonctionnalité `read`). Il ne force
pas de langue OCR. Le texte reçu est fourni à DeepSeek comme observation non fiable ;
DeepSeek relève les champs visibles et utilise les outils du catalogue pour trouver
les présentations. Il reçoit le texte et les scores OCR, pas l'image elle-même.
L'extraction des champs et leur rapprochement restent donc à évaluer avec le vrai LLM.

Limites de cette première version : JPG/PNG fixe, 10 Mo maximum en entrée,
50 à 10 000 pixels par côté et 20 mégapixels maximum. La préparation applique
l'orientation du téléphone, enlève les métadonnées EXIF et redimensionne à 3000
pixels maximum par côté. La version envoyée à Azure est un JPEG de 4 Mo maximum.
Les fichiers ne sont pas enregistrés durablement par le backend ; le texte OCR
reste dans la session en mémoire selon sa durée de vie et est envoyé au LLM.

Le seuil de confiance par mot de 0,80 est une **heuristique à calibrer**, pas une
probabilité d'identification du médicament. Si un mot a un score faible ou absent,
`requires_confirmation=true` : la mémorisation d'une présentation est bloquée pour
ce tour photo, même si le LLM tente de la sélectionner. Une réponse écrite au tour
suivant peut lever l'ambiguïté. Une photo sans texte appelle une demande de nouvelle
photo ou de saisie écrite. Les champs absents ne sont pas complétés par intuition.
Une lecture sans alerte n'est pas une preuve d'identité : dosage, forme et quantité
doivent rester compatibles avec la fiche du catalogue.

Une nouvelle photo remet la sélection de présentation à vide tout en conservant
l'historique et l'organisme. Cela évite d'attribuer à la nouvelle boîte le médicament
précédent. Un échec OCR ou LLM conserve le contexte précédent. Le verrou de session
couvre aussi l'OCR ; une session expirée est rejetée avant l'appel Azure.
Les instructions éventuellement imprimées dans l'image restent dans un message de
données utilisateur. Les tests vérifient ce cloisonnement, pas une résistance absolue
aux injections ni la justesse des réponses d'un modèle réel.

Pour appliquer la mise à jour dans un projet déjà installé : conserver ton `.env`,
y ajouter les trois variables `AZURE_VISION_*` de `.env.example`, puis exécuter
`python -m pip install -e '.[azure,test]'` dans l'environnement de l'API et redémarrer
FastAPI. `GET /health` indique `vision_configured`, sans vérifier les droits Azure.

## Recherche et données

Tables attendues : `dbo.medicaments` et `dbo.notices_asafar`. Les colonnes utilisées
sont définies dans `pharmacy_next/data.py`, à partir du schéma du projet existant.

Le catalogue est lu en SQL puis indexé en mémoire pendant cinq minutes. La recherche
combine code/nom/DCI exacts, noms de variantes et similarité de caractères TF-IDF,
puis reclasse les candidats selon la ressemblance du nom complet et l'ordre des caractères.
Le LLM peut proposer plusieurs orthographes/translittérations ; toutes sont vérifiées
dans ce catalogue. La similarité sert à **trouver des candidats**, pas à choisir
automatiquement un médicament. Ce prototype conserve donc une recherche approximative
comme signal, sans dépendre des anciens seuils RapidFuzz ou des listes d'alias manuelles.
Il n'emploie pas encore d'embeddings sémantiques ni de fine-tuning.

Les champs de dosage et les unités sources sont conservés, y compris µG. Les codes
restent des chaînes. Des doublons de code interrompent le chargement au lieu d'être
supprimés silencieusement. Une pagination indique explicitement les résultats partiels.

Les prix et remboursements restent propres à chaque présentation. Le taux CNOPS absent
du catalogue reste `null` dans les données sources. Un outil séparé applique la règle
CNOPS ambulatoire, vérifiée sur le site CNOPS le 16 octobre 2026 : estimation standard
à 70 % du PBR, ou 100 % lorsque l'utilisateur déclare une ALD/ALC déclarée à la CNOPS,
un accord ETM valide et un traitement lié à l'affection couverte. Ces déclarations ne
constituent pas une vérification des droits. L'estimation est par boîte et hors mutuelle.
Le calcul utilise Decimal et un arrondi au centime. Le PBR peut être égal au prix public.
Sans déclaration particulière, le taux standard est présenté comme une hypothèse de
calcul, sans imposer de questionnaire préalable. Les déclarations sont mémorisées par
code de médicament ; elles ne sont pas transférées automatiquement aux autres codes.
Une correction, un accord expiré ou un changement de personne/traitement doit mettre à
jour ou effacer ce contexte via l'outil. L'hospitalisation est hors périmètre de la règle.
Les conditions sont interprétées par le LLM ; les tests simulés ne vérifient pas sa
capacité réelle à les extraire correctement de toutes les formulations en darija.

Le PBR CNOPS est exploitable seulement si le statut est remboursable. Pour la CNSS,
la signification de `cnss_ppv_br_source` (PPV-BR) a été confirmée par le propriétaire
du dataset le 17 octobre 2026 : elle est lue dans le SELECT SQL et devient la
`base_remboursement` CNSS si le médicament est présent et remboursable.
`estimate_cnss_reimbursement` calcule en Python **base CNSS × 0,70**, avec Decimal
et arrondi au centime, par boîte. Exemple fictif : PPV de 100 DH et base CNSS de
80 DH donnent une estimation standard de 56 DH, et non 70 DH. Il n'utilise jamais
le PBR CNOPS ni le prix public pour remplacer une base CNSS absente. Un statut
faux/inconnu ou une base manquante ne produit pas de montant. Un taux de catalogue
différent du standard, une hospitalisation ou un contexte explicitement inconnu
bloquent cette estimation standard. Si le taux du catalogue manque, la règle
standard sourcée à 70 % permet néanmoins une estimation présentée comme hypothèse.
Les deux organismes figurent dans chaque fiche structurée, même si la réponse
conversationnelle ne traite que la question posée.

Les estimations CNOPS et CNSS sont conservées séparément par code. Relire une fiche
inchangée ne les efface pas, ce qui permet une comparaison des deux organismes.
Une modification de la fiche source invalide les deux estimations. La règle CNOPS
ne s'applique jamais à la CNSS. Aucune table ni donnée source n'est modifiée.

Remarque CNSS dans les consignes conversationnelles : une ALD/ALC reconnue peut
donner droit à une exonération partielle ou totale du ticket modérateur. La CNSS
annonce des taux de 77 % à 100 % pour ces situations ; une ALD ou une ETM ne
déclenche donc pas automatiquement 100 %. Le taux de l'accord doit concerner le
traitement discuté. L'assistant peut demander ce taux lorsque c'est utile, sans
insister si l'utilisateur ne le connaît pas. Le taux standard reste alors une
hypothèse et les déclarations ne sont pas des droits vérifiés. L'outil CNSS calcule
uniquement le scénario standard : aucun montant sur la base d'un accord ETM
individuel ni passage automatique à 100 %. Le taux standard et les exceptions
générales sont corroborés par l'ACAPS, consultée le 17 octobre 2026. La signification
du champ PPV-BR provient du schéma confirmé par l'utilisateur, pas d'une vérification
automatique des droits auprès de la CNSS.

Asafar reste indépendant. La comparaison nom/DCI/dosage/forme est volontairement
conservatrice, sans conversion de dosage et sans considérer la quantité de la boîte.
Une contradiction ou un champ non vérifiable bloque l'attribution des rubriques à
une présentation précise. Les synonymes non reconnus peuvent donc nécessiter une
vérification ultérieure. Une section absente signifie « aucune info », pas absence
de danger. Aucun contrôle clinique automatique de la cohérence interne des notices
n'est revendiqué.

Un garde-fou contrôle les nombres explicitement suivis d'une monnaie ou de `%`.
Il ne traite pas un nombre de comprimés ou un dosage comme un prix. Une valeur
non étayée déclenche une nouvelle génération interne puis, si nécessaire, un affichage
des valeurs sources. **Ce contrôle ne prouve pas que chaque phrase associe le bon
montant au bon médicament** et ne couvre pas toutes les formulations linguistiques.
Le respect des sources doit être évalué sur de vraies conversations.

## Vérifications

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Les tests utilisent des réponses LLM, OCR et transcriptions simulées et des médicaments
fictifs. Ils vérifient les contrats, la mémoire, les erreurs et les données ; ils
ne prouvent pas la qualité linguistique de DeepSeek. Aucun appel au vrai modèle ni
à Azure SQL ou Azure Vision n'est nécessaire pour les exécuter. Les tests photo
vérifient les fichiers invalides, les erreurs Azure, le contrat REST, l'incertitude
OCR, les sessions et la continuité photo puis vocal.

À tester ensuite avec tes clés :

1. « Ch7al taman byotik plus ? » → prix de plusieurs présentations réellement trouvées.
2. « J'ai celui de 12 comprimés » → contexte mis à jour si la présentation est unique.
3. « Wach kaytremboursa f CNSS ? » → même présentation, données CNSS.
4. « Achnahia comprimé ? » → explication, sans recherche d'un médicament « comprimé ».
5. « Et les effets secondaires ? » → lecture Asafar compatible ou manque indiqué.
6. Un vocal dans cette même discussion → même session et même médicament.
7. Arrêter MoulSot → message audio explicite et chat texte toujours utilisable.
8. Demander une pharmacie → recherche de l’annuaire si le module est activé ; lien Maps sur demande. Aucune apparition spontanée auparavant.
9. Demander un remboursement CNOPS → estimation standard, puis déclarer une ETM
   valide pour l'ALD déclarée et ce traitement → estimation à 100 %. Corriger ensuite
   l'accord comme expiré → retour au scénario standard. Aucun 100 % automatique pour
   un autre médicament ou simplement parce que la personne indique une maladie chronique.
10. Photo nette de boîte + « ch7al taman ? » → recherche et prix issus du catalogue.
11. Quantité cachée → demander uniquement la quantité si elle distingue les variantes.
12. Photo floue/sombre/reflets → annoncer l'incertitude, nouvelle photo ou saisie écrite.
13. Photo puis vocal « wach kaytremboursa ? » → conserver la présentation identifiée.
14. Photo d'une autre boîte → aucune reprise automatique de la précédente sélection.
15. Demander le remboursement CNSS → outil de calcul standard sur PPV-BR ; base
    absente → manque signalé ; une ALD/ETM ne déclenche pas un 100 % automatique.

Évaluer ensuite sur 30 à 50 photos réelles variées et annotées : identification
exacte du nom, dosage et présentation, erreurs d'identification, demandes de reprise,
latence et consommation Azure. Les images propres d'Asafar peuvent compléter ce lot.

Pour tester un export local explicitement : `DATA_SOURCE=csv`, chemins absolus dans
`MEDICATIONS_CSV` et `NOTICES_CSV`. Aucun basculement silencieux vers des données fictives.

## Limites du prototype

Un seul worker. Sessions en mémoire pendant une heure ; redémarrer le processus efface
les discussions. Une session expirée renvoie 410 et n'est jamais recréée silencieusement.
Les anciennes conversations sont bornées en taille ; le médicament choisi et l'organisme
sont maintenus séparément. Les fichiers audio ne sont pas conservés par ce backend.

Le service démarre sur localhost. Le module pharmacies possède des accès privés
limités au patient ou à une pharmacie, mais le chat général n’a pas de compte public.
Avant une exposition Internet, compléter l’authentification et les quotas du serveur
frontal. Ne placer aucune clé de service Azure/LLM dans le navigateur. Si FastAPI tourne plus tard dans Azure, localhost ne pointe
plus vers ton PC : l'accès à MoulSot devra passer par une liaison authentifiée.

Cette version couvre texte, audio et OCR de photos de boîtes. Les ordonnances,
dossiers médicaux, calculs CNSS personnalisés et calculs AMO sur consultations/analyses
restent les étapes suivantes. Aucune identification visuelle par couleur/logo,
lecture de code-barres ou interprétation d'écriture médicale n'est implémentée.

## Références techniques

- [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [Endpoints Foundry](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/endpoints)
- [MoulSot](https://huggingface.co/atlasia/moulsot.v0.3)
- [Azure Vision Image Analysis 4.0 : appel REST](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/how-to/call-analyze-image-40)
- [Azure Vision OCR : réponse Read](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/concept-ocr)
- [CNOPS : médicaments remboursables et conditions ETM](https://www.cnops.org.ma/fr/medicaments)
- [CNSS : panier de soins et taux ALD/ALC](https://www.cnss.ma/fr/professionnel-de-sante/demarches-et-procedures/panier-soins)
- [ACAPS : taux et indemnisation en AMO](https://www.acaps.ma/fr/grand-public/droits/indemnisation-en-amo)

## Pharmacies proches, gardes et espace pharmacien

Le module facultatif ajoute un annuaire, des gardes datées et des demandes de
disponibilité confirmées par le patient. Les demandes sont persistantes, avec
un espace protégé pour chaque pharmacie. Commencer avec les pharmacies fictives
du mode démonstration. Installation, architecture, schéma, API et limites :
[docs/PHARMACIES.md](docs/PHARMACIES.md).

La recherche réelle via **Google Places API (New)** est maintenant disponible
avec `PHARMACY_DIRECTORY_PROVIDER=google_places` et `PHARMACY_ENVIRONMENT=live`.
Elle nécessite une clé Google côté backend. Les demandes restent limitées aux
partenaires inscrits et liés par un Place ID vérifié. Les gardes utilisent les
plannings datés de notre annuaire : **aucun flux externe de garde n'est encore validé**.
Activation, diagnostic Le Matin et limites : [docs/PHARMACIES_API.md](docs/PHARMACIES_API.md).

## Relais humain et dashboard pharmacien

Le patient peut maintenant demander un relais textuel dans le même chat auprès
la pharmacie partenaire choisie. L’IA reste en pause jusqu’à la fermeture ou
l’expiration du relais. Le pharmacien dispose d’un dashboard privé, d’une boîte
de réception et d’un chat avec prise de relais explicite. Les messages humains
ne sont pas partagés avec le LLM.

Après la mise à jour, exécuter `python -m pharmacy_next.pharmacies.cli init` puis
redémarrer le serveur. Le guide [docs/RELAIS_PHARMACIEN.md](docs/RELAIS_PHARMACIEN.md)
précise les parcours, les nouveaux KPI, les durées et les limites du prototype.

## Correction du parcours disponibilité

L’assistant peut préparer la demande auprès du partenaire admissible le plus
proche avec `prepare_nearest_availability_request`. Le patient n’a plus de
formulaire technique à remplir. La confirmation reste dans le chat.
Navigation, Actualiser et horaires de démonstration ont été corrigés ; voir
[docs/CORRECTIONS_DISPONIBILITE.md](docs/CORRECTIONS_DISPONIBILITE.md) pour
la migration `init`, `refresh-demo` et le contrôle de version du serveur.
