# PROSEVIEW — MOD-02 : modèles entreprise branchés sur l'import CSV

Statut : réalisé en local (backend + écran). **Non commité, non déployé** (ni l'ancienne archive PROSEVIEW, ni la recette partagée). MOD-03 non commencé. En attente de validation de Sébastien.
Aucune donnée client ni secret ; données de recette synthétiques et anonymisées.

## 1. Fonctionnement

Import CSV existant : aperçu → (plan éditable par demande) → confirmation.

1. **Recherche du modèle** (à l'aperçu) : pour chaque nouvelle demande, modèle de l'**entreprise du projet autorisé** (lue côté serveur), titre comparé en ignorant casse et espaces superflus, modèles « à revoir » exclus, **aucun rapprochement approximatif**.
2. **Plan par demande** (stocké sur la ligne d'aperçu, `InterventionImportRow.plan`) : sous-actions proposées (titre, ordre, points « En cours »/« Clôturée », responsable facultatif, **BPU par ligne, faux par défaut**). Modifiable (ajout, retrait, ordre, valeurs) : propre à la demande, **le modèle entreprise n'est jamais modifié** et le BPU n'est jamais enregistré dans un modèle. BPU = prestation hors contrat cadre initial.
3. **Sans modèle** : A) traitement ponctuel (plan défini pour cette demande) ; B) idem + création d'un modèle réutilisable sous le titre importé, **à la confirmation seulement**, avec le droit entreprise `company_configurations` create/update (le même que MOD-01). Annuler l'aperçu ne crée ni demande, ni action, ni modèle (l'aperçu stocké est supprimé).
4. **Confirmation** (une transaction) : pour chaque nouvelle demande, **une action mère au titre de la demande** (phase « Interventions », clé `mother`), reliée à la demande, et ses sous-actions via `parent_action_item` (clés `s1`, `s2`…, sans phase), avec **copie des points et du BPU** (`ActionItemProgressSetting`, MOD-01). Dates : aucune date de début inventée ; échéance = fin attendue de la demande (inchangé). Responsable : celui du lot s'applique à la mère et aux sous-actions sans responsable propre ; un responsable de ligne a priorité.
5. **Aucune confirmation tant qu'un plan manque** : `400 plan_incomplete` (lignes listées), rien n'est écrit.
6. **Avancement** : la mère n'est **pas comptée comme sous-action** (liste des demandes : comptes et statut calculés sur ses sous-actions, y compris celles ajoutées à la main) ; les anciennes demandes (4 actions plates, sans mère) sont comptées comme avant. Calculs MOD-01 inchangés : progression par points, clôture automatique de la mère, statut inconnu = 0 point, KPI projet inchangés.

## 2. Contrôles (front **et** serveur)

Titre de sous-action non vide et ≤ 255 caractères ; `0 ≤ en cours ≤ clôturée ≤ 100` ; total « Clôturée » = 100 ; 1 à 50 sous-actions ; points entiers stricts (pas de booléen, flottant ou texte) ; BPU booléen strict ; responsables = membres de l'équipe du projet (revérifiés à la confirmation) ; modèle référencé = modèle de l'entreprise du projet, non « à revoir ». Le serveur revalide **tout plan stocké** à la confirmation (plan altéré hors API refusé, `PlanRejected`). Le titre de la demande est limité à **255 caractères** (il sert de nom à l'action mère, max 255) ; la limite était de 500 : ligne en erreur `title_too_long` au-delà.

Droit de créer un modèle : vérifié à l'édition du plan **et** revérifié à la confirmation (`403 template_right_required`, rien n'est écrit). Doublons : un modèle de même titre normalisé créé entre-temps par un autre utilisateur (ou par une ligne précédente du même fichier, ou par un jumeau concurrent via la contrainte BDD) n'est **pas dupliqué ni écrasé** ; la demande est créée d'après le plan de l'utilisateur et le résultat indique `templates_already_existing`.

## 3. Sous-action ajoutée après l'import

Le formulaire existant d'ajout de sous-action (projet) affiche, **uniquement si la mère utilise les points MOD-01**, une section : points « En cours »/« Clôturée » de la nouvelle sous-action, BPU (faux par défaut) et les sous-actions **ouvertes** à ajuster explicitement ; total en direct (doit rester 100). Serveur (`ActionItemSerializer`) : `progress_setting` et `sibling_progress` obligatoires pour une mère à points, total revérifié sous verrou de la mère ; **rien n'est recalculé ni redistribué** ; les sous-actions clôturées ne sont pas ajustables ; une mère clôturée reste verrouillée ; pour une action sans points, le comportement est inchangé et ces champs sont refusés. `GET action_items/<id>/weighted-progress` expose aussi points et BPU des sous-actions ; le détail d'une action expose `progress_setting` en lecture seule.

## 4. Réimport, concurrence, atomicité

Aucun doublon de demande, mère ou sous-action (contraintes `(demande, clé)` + verrous lot/projet + idempotence de la confirmation) ; double clic et relance : résultat identique, pas de seconde création ; source modifiée : signalée sans mise à jour ; responsables, dates, statuts, points et BPU existants jamais écrasés ; échec en cours de confirmation : rollback complet (demandes, actions **et modèle créé**), aperçu rejouable.

## 5. Fichiers

Backend — nouveaux : `services/intervention_plan.py`, `migrations/0238_mod02_plan_and_bpu.py`, `test_intervention_plans.py`, ce document ; modifiés : `services/intervention_import.py`, `views/intervention.py`, `services/action_progress.py` (constructeur de sous-actions partagé, ajout pondéré), `models/intervention.py` (`plan`), `models/action_template.py` (`is_bpu`), `serializers/action_item.py` (hunks MOD-02 : champs d'écriture et lecture `progress_setting`), `urls.py` (route plan de ligne), `test_intervention_import.py` (adapté : mère + 3 sous-actions).
Frontend — nouveaux : `src/views/ProjectInterventionRequests/{PlanEditor.tsx,planLogic.ts,tests/planLogic.test.mjs}`, `src/components/GlobalModalFetcher/modals/create_sub_action_item_form/{WeightedSubActionSection.tsx,weightedLogic.ts,tests/weightedLogic.test.mjs}` ; modifiés : `ProjectInterventionRequests/{index.tsx,logic.ts}`, `redux/api/endpoints/shared/{interventionRequests.ts,actionTemplates.ts}`, `create_sub_action_item_form/index.tsx`, `messages/{en,fr,de}.json` (`InterventionRequests` étendu, `WeightedSubAction`), `package.json` (scripts de test).
Non modifiés : bouton « Demande client », budget et barre de progression de la liste (MOD-03), fenêtre de détail d'une action (voir limites).

## 6. Migrations

`0238_mod02_plan_and_bpu` (champs `plan` et `is_bpu`) au-dessus de `0237`, sans toucher aux migrations déjà appliquées ; appliquée sur la base locale ; `makemigrations --check` : aucune dérive. **Lignée de livraison inchangée** : base `deliver/backend-integration-20261027` à `0230`, branche PROSEVIEW poussée avec `0231_intervention_requests`, branches `release/ged-2026.11.04-*` avec `0231_document_logical_withdrawal` à `0234` : **conflit `0231` GED/PROSEVIEW toujours à trancher** ; la livraison de MOD-01/02 devra livrer `0236` → `0238` ensemble, après la migration PROSEVIEW retenue. Aucune renumérotation aveugle ; l'ancienne archive PROSEVIEW ne contient ni MOD-01 ni MOD-02.

## 7. Tests exécutés

- Backend ciblé : **203/203** (module MOD-02 : proposition par titre, deux séries différentes, réutilisation dans deux projets, isolation entreprise/projet, droit de création de modèle réel et simulé, ponctuel et création de modèle, annulation, doublon concurrent, rollback, BPU par ligne, contributions invalides, hiérarchie et progression, ajout manuel et ajustement, mère verrouillée, réimport, légacy ; + MOD-01, import PROSEVIEW, Action Items : requêtes, liste API, dépendances, conversion, exigences). Les tests ont révélé et fait corriger un défaut réel : un total ≠ 100 à l'ajout manuel levait une erreur 500 au lieu d'un refus 400.
- Frontend : `ProjectInterventionRequests` **13/13**, `weightedLogic` **5/5** ; `tsc --noEmit` sans erreur sur les fichiers touchés ; eslint propre ; traductions FR/EN/DE ajoutées (114 clés `InterventionRequests`, 13 `WeightedSubAction`).
- **Recette navigateur** (compte configurateur local, projet 19, fichier synthétique) : modèle trouvé pour `Confinement salle A` et pour sa variante `  confinement   SALLE a ` ; confirmation bloquée tant que des demandes n'ont pas de plan ; éditeur : Enregistrer désactivé hors total 100, BPU éteint par défaut, BPU activé sur une seule ligne sans effet sur les autres ; **rien n'est écrit avant la confirmation** (vérifié en base) ; confirmation : 5 demandes, 17 actions ; mères au titre de la demande, sous-actions sans phase, points et BPU conformes, aucune date de début, un seul modèle créé (« Confinement local technique L », sans BPU ni projet d'origine) ; détail de la demande : hiérarchie, points, puce BPU ; liste des Action Items : mères avec leurs sous-actions ; réimport : les 2 références existantes non modifiables, seule la nouvelle a un plan ; annulation de l'aperçu : aucune demande, mère ni modèle créés.
- Formats testés : uniquement le CSV synthétique à point-virgule ci-dessous (+ cp1252/virgule par les tests automatisés existants). **Les 14 CSV réels ne sont pas validés.**

## 8. Anomalies et limites

- **Ajout manuel non vérifié dans le navigateur** : le panneau de modification d'action ne s'est pas ouvert de façon fiable dans le navigateur de recette (lenteur). La section est vérifiée par tests serveur (API réelle) et logique front ; à recetter par Sébastien (§10).
- **Fenêtre de détail d'une action** (`view_edit_action_item_form`) non modifiée (autre travail en cours dessus) : le BPU et les points sont consultables dans le détail de la demande et par l'API du détail d'action, pas encore dans cette fenêtre.
- Un modèle « à revoir » (titre ambigu hérité de MOD-01) n'est jamais proposé : la ligne demande un plan défini à la main.
- Pas de bouton « ignorer cette ligne » : une ligne sans plan bloque la confirmation (il faut la définir, ou retirer la ligne du fichier).
- Les noms de sous-actions créés par l'import ne portent plus la référence `IR-…` (ancien format `IR-… — libellé`) : la référence est portée par la demande.
- Recette partielle d'un autre périmètre : les demandes importées dans la base locale (références `MOD2-00x`, projet 19) y restent pour la recette de Sébastien.

## 9. Stratégie de reprise des anciennes demandes (proposée, **non exécutée**)

Les demandes importées avant MOD-02 ont 4 actions plates indépendantes (clés `preparation`, `delivery`, `assembly`, `control`), sans mère ni points ; elles restent comptées comme avant. Pas de conversion automatique. Proposition : (1) commande en simulation listant ces demandes, leurs actions, statuts et responsables ; (2) conversion **demande par demande**, validée par Sébastien : créer la mère (clé `mother`), rattacher les 4 actions comme sous-actions (retrait de leur phase, obligatoire pour une sous-action), choisir à la main les points et le BPU de chacune (les 4 actions ne correspondent à aucun modèle) ; (3) actions déjà clôturées : conserver leur statut, points figés ; (4) sauvegarde de la base avant chaque lot, transaction par demande, idempotence par clé, rapport avant/après ; (5) aucune conversion sans modèle ou plan explicitement choisi.

## 10. Procédure de vérification (Sébastien)

Prérequis : `docker exec backend_server python manage.py migrate project_management 0238` ; frontend lancé avec l'environnement du login helper ; connexion `http://localhost:3001/login?as=configurator&returnTo=/projects`, projet « LINKS Local Demo Project » sélectionné.

```bash
docker exec backend_server python manage.py test project_management.test_intervention_plans project_management.test_intervention_import project_management.test_action_templates --noinput
npm run test:import-plan
npm run test:weighted-sub-action
```

Fichier de recette (synthétique, références `MOD2-001` à `MOD2-005`) : `C:\LINKS-MIGRATION\backend\docs\demo\prosevi_mod02_synthetique.csv` (dossier ignoré par git).
Prérequis métier : un modèle entreprise « Confinement salle A » (Configuration entreprise → Modèles d'actions, par exemple 10/30, 10/20, 10/50).

1. `/project-intervention-requests` → « Import requests (CSV) » → choisir le fichier. Attendu : 5 demandes à créer, 2 avec 3 sous-actions (le modèle, y compris la variante casse/espaces), 3 « Définir ».
2. Bouton de confirmation grisé tant qu'il reste des demandes sans plan. Ouvrir « Définir » sur `MOD2-003` : saisir deux sous-actions 40 + 60, activer le BPU sur la seconde seulement, enregistrer. Vérifier : total rouge si ≠ 100, Enregistrer désactivé, autres lignes inchangées.
3. `MOD2-004` : une sous-action à 100. `MOD2-005` : cocher « Créer aussi un modèle réutilisable », trois sous-actions, BPU sur la première.
4. Avant de confirmer, contrôler en base (ou dans Configuration entreprise) qu'aucun modèle ni demande n'existe encore. Confirmer : 5 demandes, 17 actions.
5. Ouvrir une demande (clic sur la ligne) : mère, sous-actions indentées, puces BPU, points. Dans « Action items » : mères avec sous-actions (« Expand all »).
6. Réimporter le même fichier : rien n'est créé. Annuler un aperçu : rien n'est créé.
7. Ajout manuel : Action items → modifier la mère `MOD2-003` → ajouter une sous-action ; la section « Points de la nouvelle sous-action » apparaît : saisir 10 de points, BPU, sans ajuster → refus (total ≠ 100) ; ajuster explicitement une sous-action ouverte pour retrouver 100 → création. Une sous-action clôturée n'est pas ajustable.
8. Vérifier la liste des demandes : la mère n'est pas comptée (3 sous-actions → compte 3), le budget et le bouton sont inchangés (MOD-03).
9. FR/EN/DE : changer la langue et relire l'éditeur de plan et la section d'ajout.

## 11. Hors périmètre (non commencés)

MOD-03 (déplacement du bouton, retrait du budget de la liste, barre de progression), reprise des anciennes demandes, bouton « ignorer une ligne », affichage BPU dans la fenêtre de détail d'action, tout déploiement.
