# Action Items : colonnes de la liste, statut de la mère, import direct

Lot local (aucun commit, push ni déploiement). MOD-01/02/03, CRM-1B et les autres travaux concurrents sont préservés : seuls les blocs décrits ci-dessous ont été ajoutés aux fichiers partagés.

## 1. Avant modification : état constaté
- Règles du dépôt : lots validés un par un, pas de migration automatique de données anciennes, pas de renumérotation, aucun calcul modifié, local uniquement.
- Travaux concurrents présents et non touchés : CRM-1B (`crm_links` dans `serializers/action_item_list.py`, jeu de modifications Correspondances/GED/Network dans le dépôt front et back).
- Champs et sérialiseurs vérifiés : la liste `ActionItemListSerializer` n'exposait ni la référence externe, ni la référence LINKS, ni la date de réception, ni l'échéance client. Ces données existent dans `InterventionRequest` (champs `external_reference`, `reference`, `received_date`, `expected_end_date`), liée à la **mère** par `ActionItem.intervention_request`.
- **Aucun champ manquant, aucun changement de modèle, aucune migration** (`makemigrations --check` : aucune dérive).

## 2. Mapping exact des colonnes
| Colonne (EN / FR) | Source | Mère | Sous-action | Action indépendante |
|---|---|---|---|---|
| External ref. / Réf. externe | `InterventionRequest.external_reference` | valeur | « - » | « - » |
| LINKS ref. / Réf. LINKS | mère issue d'une demande : `InterventionRequest.reference` (ex. IR-LLDP-00001) ; sous-action : `sub_reference` (`<id mère>-<rang>`, inchangée) ; sinon l'id de l'action | oui | `sub_reference` ou id (anciennes sous-actions non numérotées) | id |
| Titre | `ActionItem.name` (badge BPU sur la mère uniquement) | oui | oui (sans badge) | oui |
| Status / Priority / Assignee | inchangés | | | |
| **Received on / Reçue le** | `InterventionRequest.received_date` (date métier, jamais réécrite) | valeur | « - » | « - » |
| **Expected end / Fin attendue** | `InterventionRequest.expected_end_date` = **échéance du client** | valeur | « - » | « - » |
| **Start date / Date de début** | `ActionItem.start_date` (propre à l'action, jamais inventée) | valeur ou « - » | valeur ou « - » | valeur ou « - » |
| **Planned end / Fin planifiée** | `ActionItem.forecast_date` (« Date prévisionnelle » existante), **distincte** de l'échéance client | valeur ou « - » | valeur ou « - » | valeur ou « - » |
| Closure date | `ActionItem.closed_at` (seulement si clôturée) | | | |
| Progress / Avancement | calcul MOD-01 existant (`compute_progress`, mère seule 0-10-100) ; sous-action : ses points ; « - » sans calcul | barre | barre | « - » |

Points à connaître :
- `target_date` (« Date de fin ») n'est plus affichée dans la liste (colonnes imposées). Pour une action importée, elle recopie l'échéance client (règle d'import existante, inchangée) : ce n'est donc **pas** la fin planifiée. Elle reste visible et modifiable dans le panneau.
- Les colonnes issues de la demande sont vides pour les sous-actions : la demande appartient à la mère.
- Colonnes supprimées de la liste par défaut (disponibles dans le panneau) : ID, Target Date, heures et budget alloués, heures et coûts réalisés, labels, documents, opportunités, nombre de sous-actions.
- Hiérarchie, références des sous-actions, droits, filtres, ouverture du panneau, édition en ligne, menu de commandes (colonne « More ») : conservés. Les nouvelles colonnes ne sont pas triables (pas de tri serveur défini pour elles).

## 3. Changements
Backend (`C:/LINKS-MIGRATION/backend`) :
- `serializers/action_item_list.py` : champs en lecture seule `external_reference`, `links_reference`, `received_date`, `expected_end_date` (mère seulement), `progress_percent`, `progress_state`.
- `services/action_item_queries.py` : `select_related("intervention_request")` sur les mères, `progress_setting` sur les sous-actions, un préchargement unique de **toutes** les sous-actions pour la progression (la progression d'une mère ne dépend pas des sous-actions visibles : clôturées ou invisibles pour un droit restreint comprises). Nombre de requêtes constant (testé).
- `services/action_progress.py` + `action_progress_signals.py` : statut de la mère (voir 4).
- Aucune migration, aucun calcul de progression modifié.

Front (`energy-consultant-solution-work`) :
- `constants/GridTable/ColDef.tsx` : colonnes de la liste dans l'ordre imposé.
- `hooks/Grid-Table/useTanStackColumnActionItems.tsx` + `components/ActionItems/List/listColumnsLogic.ts` (règles pures testées) : cellules des nouvelles colonnes, barre de progression réutilisant `ProgressStatusBar` (couleur du badge de statut réel).
- `messages/{fr,en,de}.json` : libellés (Titre, Réf. externe, Réf. LINKS, Reçue le, Fin attendue, Fin planifiée, Avancement, À clôturer).
- `components/ActionItems/Header.tsx` et `views/ProjectInterventionRequests/index.tsx` : import direct (voir 5).

## 4. Statut de la mère
Règle (mapping des statuts existants : `open` = non commencée, `in progress` = en cours, `closed` = clôturée) :
- toutes les sous-actions non commencées : la mère reste ouverte ;
- au moins une sous-action démarrée ou clôturée, pas toutes clôturées : une mère **ouverte** passe à « in progress » ;
- toutes clôturées : clôture selon la règle MOD-01 existante (mère à points), inchangée.

Garde-fous : seul le passage ouverte -> en cours est automatique (jamais de rétrogradation, jamais de réouverture d'une mère clôturée, une mère dont le statut est inconnu n'est pas touchée) ; un statut inconnu d'une sous-action n'est jamais lu comme « en cours » ; une action sans sous-action garde son fonctionnement ; calcul pondéré et verrouillages inchangés. Ajouts manuels : une sous-action ajoutée déjà démarrée fait démarrer la mère, une sous-action ouverte ne change rien (l'API n'accepte que les statuts configurés du projet).

**Mères existantes (pas de reprise massive)** : aucune donnée n'est réécrite. Une mère déjà « open » avec une sous-action en cours (cas constaté en recette : IR-LLDP-00001, 40 % et « Open ») reste telle quelle jusqu'à la prochaine sauvegarde d'une de ses sous-actions, qui l'évalue alors. Si Sébastien souhaite les corriger, il faudra une décision et une commande dédiée avec simulation préalable (non faite).

## 5. Bouton Import
Le bouton « Import » de la liste ouvre directement le parcours CSV existant (`/project-intervention-requests?import=1`, paramètre consommé aussitôt) : pas d'étape dans la liste des demandes. Aperçu, choix du modèle, édition du plan, BPU, confirmation et dédoublonnage sont le même dialogue qu'avant. Annuler ou fermer renvoie aux Action Items. Le serveur décide encore (`can_import`) : le bouton n'est **affiché que si le serveur le permet** (un utilisateur sans ce droit n'a plus de bouton menant à une page vide).

## 5 bis. Périmètre BPU et modèles : interventions uniquement
Précision de Sébastien : les modèles de MOD-01/MOD-02 sont exclusivement des **modèles d'intervention** (niveau entreprise, réutilisables entre projets) et le BPU est exclusivement lié aux **interventions** (saisi à l'import, héritage conservé, badge sur la mère seulement). Cette précision remplace le périmètre plus large du complément précédent.

État constaté : aucun champ BPU ni sélecteur de modèle n'existait dans l'espace Actions classique (le BPU se saisit dans le plan d'import, les modèles ne sont appliqués qu'à l'import ; la section « points » du formulaire de sous-action n'apparaît que pour une mère d'intervention). Deux failles résiduelles ont été fermées, sans rien supprimer :
- **Affichage** : `effective_bpu` / `bpu_source` ne sont exposés (liste, tableau, détail, panneau) que pour une action d'intervention (mère issue d'une demande ou sa sous-action) ; une action classique n'affiche jamais de badge ni de BPU, même si une valeur était stockée. La donnée stockée reste intacte (`is_bpu` jamais modifié ni supprimé).
- **API** : `is_bpu` ne peut plus être activé sur une action classique (400) ; une valeur déjà stockée, renvoyée telle quelle par la fenêtre d'édition, n'est pas refusée, et une mère d'intervention reste modifiable.
- **Libellés** : « Modèles d'actions / Action templates / Aktionsvorlagen » deviennent « Modèles d'intervention / Intervention templates / Interventionsvorlagen » (page, menu de configuration entreprise, messages d'erreur), avec une phrase rappelant qu'ils ne s'appliquent pas aux actions classiques. Les clés techniques, les données et les modèles existants sont inchangés (aucune suppression).
- Le moteur commun (hiérarchie, pondérations, progression, statuts, clôture, affectations, dates, numérotation, panneau latéral) est inchangé.

Résultats après ce complément (bases fraîches, serveur local redémarré pour charger le code à jour) : suite Action Items **142 / 142 OK** ; PROSEVIEW + licences + nouveaux modules **206 / 206 OK** ; front **56 / 56 OK** ; `makemigrations --check` : aucune dérive. Recette : mère d'intervention avec BPU (liste et détail, héritage aux sous-actions) vérifiée par l'API du serveur à jour ; action classique avec BPU stocké : aucun BPU exposé ; données temporaires restaurées. La recette navigateur de ce complément s'est limitée à la liste (le projet actif se perd au rechargement de page) ; le rendu du badge repose sur `effective_bpu`, déjà validé en recette précédente.

Tests : `project_management/test_intervention_scope.py` (8 tests : badge conservé sur la mère d'intervention et hérité, aucun BPU affiché pour une action classique avec valeur stockée, donnée jamais supprimée, refus API, valeur historique tolérée, aucune mécanique de modèle pour une action classique, moteur commun intact).

## 6. Résultats
(Voir §8 pour la procédure.)

### Tests automatisés
- Backend, nouveau module `project_management/test_action_item_list_columns.py` : **20 / 20 OK** (mapping des colonnes, dates absentes, planifiée ≠ échéance client, sous-actions, indépendante, BPU, progression, nombre de requêtes constant, transitions du statut de la mère, statuts inconnus, mère inconnue ou clôturée, ajout manuel, absence de reprise massive).
- Front : 7 tests de logique et de câblage (colonnes, ordre, mapping, import direct, libellés FR/EN/DE) ; ensemble des tests front concernés (liste, barre, demandes, fenêtre d'édition) **41 / 41 OK** ; `tsc` et `eslint` sans erreur.
- Non-régression backend (bases de test fraîches, jetables) : suite Action Items (`manage.py test project_management -k action_item -k ActionItem`) **142 / 142 OK** (122 auparavant + 20 nouveaux) ; PROSEVIEW (`test_intervention_plans_b`, `test_intervention_plans`, `test_intervention_import`, `test_action_templates`) + nouveau module + licences (`test_license_control`, `ActiveLicenseQueryTests`) **198 / 198 OK** ; `makemigrations --check` : aucune dérive.
- **Tests existants modifiés (changement de règle voulu)** : 4 assertions figeaient l'ancienne règle « la mère reste `open` tant que tout n'est pas clôturé » (`test_action_templates.AutomaticClosingTests` : 3 tests ; `test_intervention_plans.ManualSubActionTests` : 1 test). Elles attendent désormais « in progress » dans l'état intermédiaire ; la clôture finale et les 100 % ne changent pas. Une 5e défaillance (`MotherBpuTests`, liste sans requête sur `progress_setting`) a été corrigée dans le code : le sérialiseur n'interroge jamais lui-même et n'affiche aucune progression sans la projection de la liste.

### Recette navigateur (démo locale, 1600 px, identités `author` et `limited`)
- Colonnes et ordre : 12 colonnes demandées + « More », valeurs conformes pour mères, sous-actions (références `120-1`, `146-1`), mères sans sous-action.
- Transition de la mère MOD2-002 (3 sous-actions) : ouverte -> une sous-action « In Progress » -> mère « In Progress » (10 %) -> sous-actions clôturées -> mère « Closed », date de clôture 09/11/2026, 100 % (visible avec « Show closed items »).
- Dates absentes : « - » pour début, fin planifiée, clôture. Échéance client différente de la fin planifiée : mère MOD2-004, reçue 13/11/2026, fin attendue 14/12/2026, début 16/11/2026, **fin planifiée 10/12/2026**.
- Action indépendante (créée temporairement) : réf. externe « - », réf. LINKS = id, pas de dates de demande, progression « - ».
- Action importée : valeurs de la demande présentes (badge BPU sur la mère uniquement).
- Import direct : le bouton ouvre directement « Import intervention requests / Choose a CSV file » ; aperçu d'un CSV de 2 lignes (une référence déjà importée : « Source changed (not updated) », donc pas de doublon ; une nouvelle : « To create ») ; Annuler : retour aux Action Items, **aucune** demande ni action créée (26 demandes avant et après).
- Droits restreints (`demo.limited`) : liste limitée à la mère atteinte par une sous-action autorisée (réf. `146-1`, progression réelle), badge BPU sur la mère seule, **pas de bouton Import**.
- Fenêtre étroite (768 px et 390 px) : les boutons d'en-tête passent à la ligne sans chevauchement, le tableau défile dans son conteneur, sans dépassement de page propre aux nouvelles colonnes.
- Données de recette temporaires **restaurées** : actions 116-119 remises à « open » sans date de clôture, action 179 supprimée, dates de la mère 123 remises à vide.

## 7. Limites et points non exécutés
- **Réimport avec confirmation** (création effective puis réimport) non rejoué dans le navigateur pour ne pas créer de données dans la démo : couvert par `test_intervention_import` (dédoublonnage) et par l'aperçu navigateur ci-dessus.
- Pas de tri serveur sur les colonnes issues de la demande ni sur la progression.
- La page des demandes reste inaccessible à un utilisateur sans droit de lecture des demandes (comportement existant) ; le bouton Import n'y mène plus.
- Mères existantes : voir §4 (pas de reprise massive).
- Métadonnées métier en attente de validation client : non créées.
- Suite complète du backend et du front non lancée (modules concernés uniquement).

## 8. Procédure de validation (Sébastien)
1. Se connecter (démo locale) et ouvrir Project Action Items : vérifier les 12 colonnes dans l'ordre demandé ; déplier une mère, vérifier les références des sous-actions.
2. Mère MOD2-004 : comparer « Expected end » (échéance client) et « Planned end » (fin planifiée).
3. Passer une sous-action d'une mère ouverte à « In Progress » : la mère passe à « In Progress » ; clôturer toutes les sous-actions : la mère se clôture.
4. Cliquer « Import » : le dialogue CSV s'ouvre directement ; Annuler ramène à la liste.
5. Identité `limited` : pas de bouton Import, liste restreinte, mère en lecture selon les droits.
6. Backend : `docker exec -e DB_NAME=al_val backend_server python manage.py test project_management.test_action_item_list_columns --noinput` (attendu 20 / 20). Front : `node --experimental-strip-types --test src/components/ActionItems/List/tests/*.test.mjs` (attendu 7 / 7).
