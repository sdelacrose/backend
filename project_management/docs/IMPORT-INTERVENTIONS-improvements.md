# Import et fiche intervention — améliorations (BPU, sous-actions, date d'import)

Périmètre : espace Interventions uniquement. Aucune migration, aucune modification du modèle entreprise,
aucune donnée existante réécrite.

## 1. BPU dans la fiche intervention
* Case « BPU » sur la **mère** d'une intervention (action `intervention_action_key = "mother"`), enregistrée
  immédiatement par `PATCH action_items/<id>` `{"is_bpu": …}` avec les droits de modification habituels de l'action.
* Contrôle serveur (`ActionItemSerializer.validate`) : une valeur ne change que sur la mère d'une demande ;
  sous-action, action classique ou ancienne action « plate » → 400. Une valeur inchangée renvoyée en écho par la
  fenêtre de détail reste acceptée. L'héritage (mère BPU ⇒ sous-actions BPU) et le badge sur la mère seule sont
  inchangés ; liste, panneau et KPI se rafraîchissent par l'invalidation du tag `ProjectActionItems`.
* Une mère clôturée reste corrigeable (le BPU est une classification, pas du travail).

## 2. BPU à l'étape de sélection de l'import
* Colonne « BPU » (case à cocher) dans le tableau de prévisualisation, lignes **nouvelles** uniquement.
* Stockée côté serveur dans le plan de la ligne : `PATCH …/import/<batch>/rows/<n>` `{"is_bpu": bool}`, même si le plan
  est encore vide (`{"is_bpu": true}` ; la ligne reste « à définir »). L'éditeur de sous-actions n'envoie plus
  `is_bpu` : s'il est absent, le serveur conserve la valeur (modifier les sous-actions ou changer de modèle ne la perd
  jamais). Un `is_bpu` explicite dans `plan` reste accepté (compatibilité API).
* Réimport : `confirm_import` ne crée que les lignes nouvelles ; une demande déjà importée (identique ou source
  modifiée) n'est ni recréée ni modifiée, donc une correction manuelle du BPU n'est jamais écrasée.

## 3. Modèle et personnalisation des sous-actions
* Colonne « Sous-actions » : sélecteur des modèles de l'entreprise du projet + crayon qui ouvre l'éditeur existant
  (lignes, poids, responsables, ajout manuel). Les adaptations ne concernent que la ligne d'import ; le modèle n'est
  jamais écrit (`template_plan` copie ses lignes).
* Changer de modèle : avertissement uniquement si le plan contient des adaptations (lignes à la main, ou lignes qui
  diffèrent de leur modèle d'origine : titre, points, responsable).
* Après import : crayon à côté du titre « Sous-actions » de la fiche (mère d'intervention) → éditeur.
  `PUT intervention-actions/<mère>/sub-actions` `{"sub_actions":[{"id"|null, title, in_progress_points,
  closed_points, assignee_id?}]}` (`services/intervention_subactions.py`), **une transaction** :
  * ligne avec `id` : la ligne est conservée (identifiant, statut, responsables, dates, liens, commentaires) ; seuls le
    titre et les points changent ; ligne sans `id` : nouvelle sous-action ouverte ; absente de la liste : supprimée ;
  * sous-action clôturée : figée (titre, points, suppression refusés → 409) ;
  * mère clôturée : refus 409 `mother_closed` (même règle que « pas de sous-action sous une action clôturée ») ;
  * droits : modification de la mère ; la suppression exige en plus le droit de suppression (403 sinon) ;
  * la suppression d'une sous-action ouverte qui laisse uniquement des sous-actions clôturées ferme la mère
    (`close_parent_if_complete`, règle existante) ;
  * `source_template` des réglages de points n'est pas modifié ; le modèle entreprise n'est jamais lu en écriture.

## 4. Pondérations
* Total des poids « clôturée » = **exactement 100** pour toute intervention ayant des sous-actions, vérifié côté serveur
  (`validate_points`, import, ajout manuel et édition) et côté interface.
* Ajout avec poids choisi : les sous-actions **ouvertes** se répartissent le reste proportionnellement à leurs poids
  précédents (`src/utils/subActionWeights.ts`) ; arrondi par plus grand reste (total exactement 100, égalité → première
  ligne) ; sans poids existants : partage égal du reste ; sous-actions clôturées figées ; si toutes les autres sont
  clôturées, le nouveau poids doit être exactement le reste. Le poids « en cours » est réduit proportionnellement et
  ne dépasse jamais « clôturée ». L'aperçu est le tableau complet (lignes recalculées surlignées), chaque poids reste
  modifiable à la main ; Annuler ne modifie rien (rien n'est envoyé avant Enregistrer).
* La progression de la mère n'est pas stockée : elle est recalculée (`compute_progress`) avec les nouveaux poids et les
  statuts conservés ; une intervention dont toutes les sous-actions sont clôturées mais pas encore la mère, puis une
  nouvelle sous-action ouverte, repasse sous 100 % « à clôturer » tant que la nouvelle n'est pas clôturée.
* **Intervention déjà terminée** : l'ajout est refusé (règle existante) ; décision à confirmer par Sébastien si une
  réouverture automatique est souhaitée.

## 5. Date d'import
* Aucune nouvelle colonne : `InterventionRequest.import_batch.confirmed_at`, renseigné par le serveur dans
  `confirm_import` (statut `confirmed`). Une prévisualisation, une annulation ou un import échoué (transaction annulée)
  n'en crée pas ; un réimport ne change pas `import_batch` ; les sous-actions ajoutées plus tard ne sont pas un import.
* Anciennes données / demandes sans lot confirmé : **date inconnue**, affichée « - », jamais inventée.
* Liste : colonne « Date d'import » (mère ou action plate d'une demande ; vide sur les sous-actions) ; une ancienne
  demande « plate » affiche la même date de sa demande sur chacune de ses actions.

## 6. Filtre « Date d'import »
* Fuseau de référence : `TIME_ZONE` de l'application (**UTC**, `settings.common`). Bornes du filtre, « aujourd'hui » et
  date affichée utilisent le même jour calendaire dans ce fuseau (test avec `Europe/Paris`).
* Valeurs (`import_period`, + `import_from` / `import_to` pour `custom`) : `all`, `today`, `week` (lundi → aujourd'hui),
  `last7` (aujourd'hui et les six jours précédents), `month` (1er du mois → aujourd'hui), `custom` (bornes inclusives,
  une seule borne = intervalle ouvert ; invalide / inversé → 400). Toute période datée exclut les dates inconnues ; « Toutes
  les dates » les affiche.
* Appliqué côté serveur (`apply_import_period`) aux vues liste, board, temps, calendrier, Gantt **de l'espace
  Interventions** (`scope=interventions`) : mères (ou actions plates) importées dans la période **avec leurs
  sous-actions**, jamais les actions classiques (paramètre ignoré hors espace Interventions). Se combine avec `kpi=`.
  Conservé dans l'URL (`?import_period=…`) à côté de `?kpi=` quand on change de vue. Les compteurs KPI du bandeau et
  du dashboard ne sont pas filtrés par période.

## 7. Recette (résumé)
1. Importer un CSV : cocher BPU pour une demande **avant** l'étape sous-actions ; revenir en arrière : le choix reste.
2. Choisir un modèle dans le sélecteur, cliquer le crayon, ajouter une sous-action avec un poids, vérifier le recalcul
   proportionnel, ajuster à la main jusqu'à 100, importer.
3. Vérifier que le modèle (Template → Modèles d'intervention) est inchangé.
4. Ouvrir la fiche de la mère : corriger BPU ; vérifier badge, liste et KPI.
5. Crayon des sous-actions : ajouter une sous-action ; vérifier la progression (statuts conservés).
6. Colonne « Date d'import », réimport du même CSV (« déjà importée », pas de doublon, date inchangée).
7. Filtre : chaque période, puis changer de vue (liste, board, calendrier, Gantt) : le filtre reste dans l'URL.
* Nombre d'interventions ≠ nombre de lignes (mères repliées vs sous-actions) : voir `KPI-interventions.md` §12.
