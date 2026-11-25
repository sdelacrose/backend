# PROSEVIEW — MOD-02 (compléments) : BPU sur la mère, numérotation, choix du modèle, demande sans sous-action

Statut : réalisé en local (backend + écran). **Non commité, non déployé.** MOD-02 **non clôturé**, MOD-03 non commencé. En attente de validation explicite de Sébastien.
Complète `MOD-02-import-modeles.md`. Aucune donnée client ni secret ; données de recette synthétiques.

## 1. BPU porté par l'action mère, hérité par les sous-actions

- `ActionItem.is_bpu` (défaut faux) : le BPU se règle **une fois par demande**, sur l'action mère. Les sous-actions **héritent** (`effective_bpu`). Une sous-action ne peut plus porter son propre BPU : `is_bpu` vrai sur une sous-action, une ligne de plan avec `is_bpu` vrai (`bpu_inherited`) ou un `progress_setting.is_bpu` vrai à l'ajout manuel sont refusés (400). Le plan d'import porte `is_bpu` (booléen strict, faux par défaut) pour la mère ; l'ancien BPU par ligne n'existe plus dans l'écran.
- **Règle de priorité** (`action_progress.effective_bpu`) : mère BPU ⇒ toutes ses sous-actions sont BPU (source `mother`). Mère non BPU ⇒ une valeur **déjà enregistrée par MOD-02 sur une sous-action** (`ActionItemProgressSetting.is_bpu`) **continue de s'appliquer** (source `legacy_line`).
- **Reprise des données déjà enregistrées** : aucune donnée n'est modifiée ni réécrite par la migration ; les anciennes valeurs par ligne restent consultables (détail de la demande, détail d'action). Reprise éventuelle, à décider par Sébastien : pour une demande dont toutes les lignes sont BPU, passer sa mère à BPU (action unitaire, par API) puis effacer les drapeaux de ligne ; une demande à lignes mixtes est à traiter au cas par cas. Rien n'est converti automatiquement.
- **Badge visible** : liste « Action items » (mère et sous-actions héritières), fenêtre de détail d'une action (ligne « BPU », « hérité de l'action mère »), détail de la demande. Limite : la liste lit le BPU de la mère et de l'action elle-même ; un ancien drapeau par ligne n'y est pas affiché (pas de requête par ligne), il l'est dans le détail.
- Limite : le BPU d'une mère **existante** se modifie par l'API (`PATCH action_items/<id>` `{is_bpu}`, refusé si la mère est clôturée) ; l'écran de modification d'action ne propose pas encore l'interrupteur (fenêtre modifiée par un autre travail CRM, non écrasée). Il se règle dans l'aperçu d'import à la création.

## 2. Numérotation `ID-mère-n` des sous-actions

- `ActionItem.sub_sequence` (rang) + table de compteur `ActionItemSubCounter` (même principe que les autres compteurs de référence LINKS : compteur verrouillé, sous verrou de la **ligne de la mère**), contrainte unique `(parent_action_item, sub_sequence)` en dernier recours. Référence affichée : `"<id mère>-<rang>"` (`sub_reference`). Les clés techniques `intervention_action_key` (`mother`, `s1`…) sont **inchangées**.
- Attribuée à **toute nouvelle sous-action** (import, ajout manuel, ajouts concurrents) dans `ActionItem.save`, jamais réutilisée, y compris après suppression ; réimport : aucun nouveau numéro. **Aucune renumérotation** : les sous-actions existantes n'ont pas de numéro (affichage de l'identifiant technique) ; sur une mère qui a déjà des sous-actions non numérotées, la première nouvelle prend le rang 1 (ex. `120-1`).
- Affichage : colonne ID de la liste, détail de la demande (colonne « Réf. »), détail d'action.

## 3. Choix manuel du modèle dans l'aperçu

- Sélecteur « Utiliser un modèle entreprise (quel que soit le titre) » dans l'éditeur de plan. Serveur : `GET …/import/<lot>/templates` (modèles de l'entreprise du projet autorisé, hors « à revoir », avec le droit d'import) et `PATCH …/rows/<ligne>` `{apply_template: id}` (modèle d'une autre entreprise, ambigu ou inconnu : 400 `template_invalid`).
- Le titre de la demande et de la mère est conservé ; les sous-actions sont préremplies puis éditables ; le modèle n'est **jamais** modifié ; le BPU reste faux par défaut.
- **Avertissement avant remplacement** d'un plan déjà défini ou modifié (« Remplacer le plan actuel ? », Annuler laisse le plan intact). Côté serveur, seule une application explicite de modèle remplace le plan.

## 4. Demande confirmée sans sous-action

- Interrupteur « Confirmer cette demande sans sous-action » : plan `no_sub_actions: true` avec liste vide, **seul cas** où une liste vide est acceptée ; aucun total de 100 exigé ; pas de création de modèle (`template_needs_sub_action`) ; l'import ne crée que la mère.
- Calcul (mapping explicite de MOD-01, `status_state`) : mère seule `open` 0 %, `in progress` 10 %, `closed` 100 % (mode `mother_only` de `weighted-progress`) ; statut inconnu : 0 %, listé dans `unmapped_statuses`, jamais 10 %. La liste des demandes suit le statut de la mère lorsqu'elle n'a pas de sous-action.
- **Premier ajout manuel** : le formulaire d'ajout affiche la section de points (mode `mother_only`) ; le serveur exige des points explicites totalisant **100** pour cette première sous-action ; dès lors la mère passe au calcul **par contributions uniquement** (plus de règle 0/10/100), avec clôture automatique de MOD-01. Une action ordinaire sans sous-action n'est pas concernée (mode inchangé).

## 5. Limites, titre, recette

- Titre > 255 caractères : ligne **en erreur explicite** (« Titre trop long (255 caractères maximum)… »), jamais tronqué, aucune erreur 500, aucune demande créée pour cette ligne ; le titre sert aussi de nom de mère (max 255).
- **Panneau d'action « qui ne s'ouvre pas »** : diagnostic = le panneau (fenêtre de modification) s'ouvre en cliquant le **nom** de l'action (c'est un composant modal MUI, pas un `role=dialog`) ; le passage par le menu ⋮ → Modifier est lent en recette (rendu retardé, navigateur et API fortement chargés par les tests) et ma recherche d'élément précédente utilisait un mauvais sélecteur. Aucun défaut applicatif trouvé ; erreurs console rencontrées : traductions manquantes du composant `InfoDrawer` (préexistant, sans lien).
- Lignée de migrations : `0239_mod02b_bpu_mother_and_sub_reference` au-dessus de `0238`, aucune migration appliquée ailleurs n'est modifiée, aucune renumérotation. **Conflit `0231` GED/PROSEVIEW toujours à trancher** (voir MOD-02) ; la livraison de MOD-01/02/02b devra embarquer `0236` → `0239` ensemble, après la migration PROSEVIEW retenue. L'ancienne archive PROSEVIEW ne contient rien de MOD-01/02.

## 6. Fichiers

Backend — nouveaux : `migrations/0239_mod02b_bpu_mother_and_sub_reference.py`, `test_intervention_plans_b.py`, ce document ; modifiés : `models/project.py` (champs `is_bpu`, `sub_sequence`, contrainte, numérotation dans `save`, propriété `sub_reference`), `models/action_template.py` (`ActionItemSubCounter`, `allocate_sub_sequence`), `models/__init__.py`, `services/action_progress.py` (BPU effectif, mode mère seule, ajout pondéré), `services/intervention_plan.py`, `services/intervention_import.py`, `views/intervention.py`, `serializers/action_item.py` (hunks MOD-02b), `serializers/action_item_list.py` (champs liste), `urls.py`, `test_intervention_plans.py`.
Frontend — modifiés : `ProjectInterventionRequests/{PlanEditor.tsx,planLogic.ts,index.tsx,tests/planLogic.test.mjs}`, `create_sub_action_item_form/{WeightedSubActionSection.tsx,weightedLogic.ts,tests/weightedLogic.test.mjs}`, `view_edit_action_item_form/index.tsx` (2 lignes de lecture seule + import `Chip`, **sans toucher** aux hunks CRM-1B), `hooks/Grid-Table/useTanStackColumnActionItems.tsx` (ID/badge), `useColumnProjectActionItem.tsx`, `redux/api/endpoints/shared/{interventionRequests.ts,actionTemplates.ts}`, `messages/{en,fr,de}.json`.

## 7. Tests et recette

- Backend ciblé : voir le bilan de la session (suites MOD-01, MOD-02, MOD-02b, import PROSEVIEW, Action Items) ; nouveaux tests : numérotation (import, manuel, legacy, unicité BDD, jamais réutilisée), BPU mère/héritage/priorité/données non réécrites, choix manuel de modèle (autre titre, édition, remplacement, refus étranger/ambigu/inconnu, droits, isolation), demande sans sous-action (liste vide explicite, 0/10/100, statut inconnu, bascule au premier ajout, mère clôturée verrouillée), limite 255 sans 500. Défauts trouvés par les tests : aucun côté logique ; un test de teardown corrigé (suppression brute).
- Front : `planLogic` + `weightedLogic` + `ActionTemplates` : **30/30** ; `tsc --noEmit` propre sur les fichiers touchés ; eslint sans erreur ; traductions FR/EN/DE à parité (131 clés `InterventionRequests`, 20 `WeightedSubAction`).
- **Recette navigateur réelle** (compte configurateur, projet 19, données synthétiques) :
  - ajout manuel via le formulaire (panneau ouvert par le nom de l'action) : responsable, dates, statut, rappel, points, déséquilibre (120) signalé puis ajustement explicite d'une sous-action ouverte (60 → 40) → création `120-1`, total 100 ;
  - import `prosevi_mod02b_synthetique.csv` : modèle choisi à la main pour un titre différent, interrupteur BPU de la mère (aucun interrupteur par ligne), avertissement de remplacement (Annuler garde le plan), demande « sans sous-action » enregistrée sans total, titre de 256 caractères refusé explicitement ; confirmation : 3 demandes, 9 actions ; mère BPU `146` et sous-actions `146-1…3` héritant du BPU, modèles entreprise inchangés ;
  - liste Action items : badges BPU sur la mère 146 et ses 3 sous-actions (aucun sur 150), références `146-1`, `150-1`, `120-1`, anciennes sous-actions inchangées ; détail d'action : « Référence 146-1 », « BPU (hérité de l'action mère) » ;
  - première sous-action d'une mère sans sous-action : points 10/100 exigés (0 et 60 refusés), `154-1` créée, calcul passé aux contributions ;
  - verrouillage (via l'API réelle de la session) : clôture de la dernière sous-action → mère clôturée automatiquement ; sous-action clôturée non modifiable (400) ; ajout sous une mère clôturée refusé (400) ; progression 100 %.
  - Non refait dans le navigateur : réimport sans doublon (couvert par tests et par la recette MOD-02 précédente), remplacement effectif après « Remplacer le plan ».

## 8. Procédure de validation (Sébastien)

Prérequis : `docker exec backend_server python manage.py migrate project_management 0239` ; frontend lancé par le login helper ; connexion `http://localhost:3001/login?as=configurator&returnTo=/projects`, projet « LINKS Local Demo Project ».

1. **Import** `C:\LINKS-MIGRATION\backend\docs\demo\prosevi_mod02b_synthetique.csv` : 3 demandes à créer + 1 ligne en erreur (titre trop long). Pour `MOD2B-001` : « Utiliser un modèle entreprise » → choisir un modèle ; activer **BPU** ; enregistrer. Pour `MOD2B-002` : modifier un titre de sous-action, puis choisir un autre modèle → avertissement, tester Annuler puis Remplacer. Pour `MOD2B-003` : « Confirmer cette demande sans sous-action ». Confirmer.
2. **Liste Action items** (« Expand all ») : badge BPU sur la mère de `MOD2B-001` et ses sous-actions, références `ID-n` ; ouvrir une sous-action : Référence et BPU hérité.
3. **Sans sous-action** : ouvrir la mère de `MOD2B-003` → « + Add Sub Action Item » : la section de points apparaît, 0 et 60 refusés, 100 accepté ; la mère suit alors les contributions.
4. **Ajout manuel sous une mère à sous-actions** : points explicites + ajustement d'une sous-action ouverte pour garder 100 ; numéro suivant.
5. **Verrouillage** : clôturer toutes les sous-actions → mère clôturée ; plus d'ajout, plus de modification.
6. Titre > 255 : refus explicite, rien créé.
7. FR/EN/DE : relire éditeur de plan, section d'ajout, détail.

## 9. Hors périmètre

MOD-03 (bouton, budget, barre de progression) ; interrupteur BPU dans l'écran de modification d'une action existante ; affichage des anciens BPU par ligne dans la liste ; reprise des anciennes demandes ; déploiement.
