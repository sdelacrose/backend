# Séparation Actions / Interventions

Lot local (aucun commit, push ni déploiement). MOD-01/02/03, CRM-1B et les autres travaux concurrents sont préservés : seuls les blocs décrits ci-dessous ont été ajoutés aux fichiers partagés. Règles des dépôts sdelacrose/* respectées (aucun dépôt EC-PMP touché).

## 1. Existant identifié (rien refait)
- **Déjà réalisé** : le moteur commun (hiérarchie, pondérations, progression, statuts, clôture automatique, affectations, dates, numérotation, panneau latéral) ; la page `/project-intervention-requests` avec liste des demandes, import CSV complet (aperçu, modèles, plan, BPU, confirmation, dédoublonnage) ; la liste Action Items à 12 colonnes ; le statut de la mère qui suit ses sous-actions ; BPU et modèles réservés aux interventions (affichage, API, libellés).
- **Manquant** : aucune entrée « Interventions » dans le menu projet ; les actions d'intervention étaient mélangées aux actions classiques (liste, board, calendrier, Gantt, compteurs) ; l'import se lançait depuis le bouton des Actions ; le détail d'une demande était un tableau en lecture seule renvoyant vers l'espace Actions ; les anciennes demandes à actions plates n'apparaissaient que dans l'espace Actions.

## 2. Règle de séparation
Une **action d'intervention** est toute action née d'une demande : la mère, ses sous-actions (générées ou ajoutées à la main sous la mère) et les actions plates des anciennes demandes (sans mère ni sous-action). Critère unique côté serveur : `intervention_request` renseigné sur l'action ou sur sa mère (`INTERVENTION_SCOPE_Q`).

Le serveur filtre sur un paramètre **explicite** `scope` : `classic` (exclut les interventions), `interventions` (ne garde que les interventions). **Sans paramètre, rien n'est filtré** : les autres consommateurs (saisie de temps, notes de frais, lien de documents, calendrier projet) continuent de voir toutes les actions autorisées, afin de pouvoir imputer du temps sur une intervention. Les permissions serveur (visibilité, utilisateur restreint) s'appliquent avant le filtre, inchangées.

## 3. Changements
Backend (`C:/LINKS-MIGRATION/backend`) :
- `services/action_item_queries.py` : `INTERVENTION_SCOPE_Q`, `apply_action_scope`.
- `views/action_item.py` : `scope` appliqué à liste, board, time, calendrier, Gantt et options de filtre.
- Compteurs « actions ouvertes » des Actions : `views/project.py` (tableau de bord, `project_stats`) et `serializers/landing_screen.py` n'incluent plus les interventions. Les budgets, les règles de clôture de phase et le moteur ne changent pas.
- `serializers/action_item_list.py` : progression des anciennes actions plates par la correspondance existante statut → 0 / 10 / 100 (mère sans sous-action), sans modification de donnée.

Front (`energy-consultant-solution-work`) :
- `components/ActionItems/scope.tsx` : contexte de scope (défaut `classic`) et deux hooks de requête qui ajoutent le scope (liste, board, calendrier, Gantt, conteneur : mêmes requêtes et mêmes caches qu'avant, avec le paramètre en plus).
- `components/ActionItems/index.tsx` : propriétés `scope`, `canImport`, `onImport` ; en Interventions, les cinq onglets comme dans Actions (restriction « Liste seule » retirée, voir §10).
- `components/ActionItems/Header.tsx` : plus aucun import dans les Actions ; en Interventions, titre « Interventions », pas de bouton « nouvelle action », bouton Import.
- `constants/GridTable/ColDef.tsx` : deux jeux de colonnes (voir §5) ; `List/index.tsx` choisit le jeu selon le scope.
- `views/ProjectInterventionRequests/index.tsx` : la page Interventions = la **liste partagée** restreinte aux interventions (mères, sous-actions, panneau latéral, progression, saisie en ligne) avec le dialogue d'import par-dessus ; le tableau de demandes et son détail en lecture seule, redondants, ne sont plus affichés (l'API des demandes reste en place, elle fournit `can_import`).
- `components/Sidebar/sidebarData.ts` : entrée « Interventions » du menu projet (mêmes droits que le module Action Items) ; `components/Header/index.tsx` : titre et icône ; libellés FR/EN/DE.
- `components/WidgetsContainer/widgets/ProjectActionItem.tsx` : le widget « Action Items » du tableau de bord demande le scope classique.

## 4. Anciennes demandes à actions plates
Aucune migration ni reprise. Leurs actions (une par ligne de l'ancien import) sont des actions d'intervention : elles disparaissent des Actions et apparaissent **à plat** dans Interventions (pas de mère, pas de sous-action), avec la référence externe, la référence LINKS, la date de réception et l'échéance client de leur demande, et une progression suivant leur propre statut (ouverte 0 %, en cours 10 %, clôturée 100 %). Constat en démo : 6 demandes / 20 actions plates (projet 28). Limite : plusieurs actions plates d'une même demande répètent la même référence LINKS.

## 5. Colonnes
- **Actions (classique)** : Réf. LINKS (l'id), Titre, Status, Priority, Assignee, Start date, Target date, Planned end, Closure date, commandes. Les colonnes propres aux demandes (réf. externe, reçue le, fin attendue) et la progression pondérée, toujours vides pour une action classique, n'y figurent plus ; la « Date de fin » (échéance propre de l'action) y est rétablie.
- **Interventions** : External ref., LINKS réf., Titre, Status, Priority, Assignee, Received on, Expected end, Start date, Planned end, Closure date, Progress, commandes (jeu validé au lot précédent, inchangé).

## 6. Navigation mère / sous-actions
Dans Interventions : déplier la mère dans la liste ; le panneau latéral de la mère affiche ses sous-actions ; un clic ouvre la sous-action dans le même panneau (« Retour à l'action parente ») ; l'URL reste `/project-intervention-requests`. Plus aucun lien vers l'espace Actions dans la page.

## 7. Résultats
Tests automatisés (bases de test fraîches et jetables) :
- Nouveau module `project_management/test_actions_interventions_separation.py` : **12 / 12 OK** (listes classique et interventions disjointes et complémentaires ; sous-action ajoutée à la main ; actions plates d'anciennes demandes avec données de la demande et progression 0 / 10 / 100 sans migration ; board, calendrier et Gantt ; compteur « actions ouvertes » de l'écran d'accueil ; absence de filtre sans paramètre).
- Suite Action Items (`manage.py test project_management -k action_item -k ActionItem`) : **142 / 142 OK**.
- Lot PROSEVIEW + licences + modules ci-dessus (218 tests) : **217 OK**, une seule défaillance, celle de mon propre test de calendrier (plage de dates trop large pour l'API), corrigée puis rejouée dans le module (12 / 12). Aucun test existant n'a été modifié dans ce lot.
- `makemigrations --check` : aucune dérive (aucune migration, aucun changement de modèle).
- Front : **62 / 62 OK** (liste, menu, barre de progression, demandes, modèles, panneau, sous-action) ; `tsc` sans erreur, `eslint` sans erreur sur les fichiers modifiés.
- Non couvert par un test automatisé : les compteurs de `project_stats` et du tableau de bord entreprise (même exclusion `INTERVENTION_SCOPE_Q`, vérifiée par relecture ; le test de l'endpoint bute sur une garde d'accès au projet étrangère au filtre).

Recette navigateur (démo locale, 1600 px) :
- **Menu** : « Interventions » entre « Action Items » et « Calendrier » ; titre de page et libellé traduits.
- **Actions** (projet 28) : requête `scope=classic` ; la phase « Interventions » et toutes ses actions ont disparu, seule l'action classique de test apparaît ; colonnes classiques (Réf. LINKS = id, Titre, Status, …, Target date) ; pas de bouton Import ; onglets Liste / Board / Time / Calendrier / Gantt inchangés.
- **Interventions** : requête `scope=interventions` ; 12 colonnes, bouton Import seul (au moment de cette recette, onglet Liste seul ; les cinq vues sont ouvertes depuis, voir §10) ; les 20 actions plates des anciennes demandes (IR-GL43-…) et les mères s'affichent ; aucune action classique.
- **Navigation** : mère dépliée, panneau de la mère avec ses sous-actions, ouverture d'une sous-action, « Retour à l'action parente » ; URL inchangée.
- **Progression et clôture** : MOD2-001 ouverte → sous-action « In Progress » → mère « In Progress » (10 %) → sous-actions clôturées → mère « Closed » le 11/11/2026 à 100 %.
- **Import / réimport** : le dialogue s'ouvre sur la liste Interventions ; CSV de 2 lignes (une référence existante, une nouvelle) ; plan (BPU, sans sous-actions) ; confirmation : « Created: 1 », la référence existante n'est pas modifiée ; la mère `RECETTE-S2-1` (IR-GL43-00012) apparaît aussitôt avec le badge BPU sur la mère seule ; réimport du même fichier : « Already imported », 0 à créer, pas de confirmation possible.
- **Ajout manuel de sous-action** : formulaire ouvert dans Interventions (section de points) ; sous-action `183-1` créée (voir limites), progression 20 % à « In Progress », clôture automatique de la mère à la clôture de la sous-action.
- **BPU** : badge sur la mère uniquement (liste), puce BPU dans l'en-tête du panneau de la mère.
- Données de recette temporaires **supprimées / restaurées** : demandes 26, actions d'intervention 83, lots d'import 14, compteur de références du projet 28 remis à 11 ; actions 129 à 132 rouvertes ; actions classiques et d'essai supprimées.

## 8. Limites et points non exécutés
- Utilisateur restreint : vérifié **côté serveur** avec le jeton de `demo.limited` (liste Actions vide, liste Interventions limitée à la mère atteinte par sa sous-action `146-1`, accès à l'import refusé, 403 sur les demandes). La recette visuelle de son interface n'a pas pu être rejouée aujourd'hui : son projet actif doit être sélectionné au préalable par son propre profil, et sa session démo avait perdu cette sélection (le contrôle d'affichage le renvoie alors vers « accès refusé »). Le rendu restreint de cette même liste a été vérifié au lot précédent.
- L'ajout manuel d'une sous-action : le formulaire s'ouvre bien dans Interventions (avec la section de points d'une mère à points) ; sa saisie à la souris étant peu fiable (dates masquées), la soumission a été faite par le même appel d'API que le formulaire (numérotation `183-1`, progression 20 %, clôture automatique vérifiées dans l'interface).
- Le calendrier projet (module Calendrier), la saisie de temps, les notes de frais et le lien de documents ne sont pas filtrés (voulu).
- Suites complètes du backend et du front non lancées, seulement les modules concernés.
- Métadonnées métier en attente de validation client : non créées.

## 9. Procédure de validation (Sébastien)
1. Menu projet : « Action Items » et « Interventions » côte à côte. Actions : aucune action d'intervention, pas de bouton Import ; seules les actions classiques.
2. Interventions : liste hiérarchique à 12 colonnes, bouton « Import » seul (pas de « nouvelle action »), et les mêmes onglets que dans Actions : Liste, Board, Time, Calendrier, Gantt.
3. Déplier une mère, ouvrir son panneau, ouvrir une sous-action, « Retour à l'action parente » : l'URL ne change pas.
4. Passer une sous-action d'une mère ouverte à « In Progress » : la mère passe en cours ; clôturer toutes les sous-actions : la mère se clôture (liste, « Show closed items »).
5. Import : « Import » ouvre le dialogue sur la liste Interventions ; un CSV avec une référence existante et une nouvelle : aperçu « Source changed (not updated) » / « To create » ; plan (BPU, sans sous-actions) puis confirmation : la mère apparaît avec le badge BPU ; réimport du même fichier : « Already imported », rien à confirmer.
6. Anciennes demandes (projet 28) : actions plates visibles dans Interventions, absentes des Actions.
7. Tests : voir §9 du rapport de session (commandes ci-dessous).

## 10. Complément : toutes les vues dans Interventions
Exigence ajoutée en cours de lot : l’espace Interventions propose les mêmes vues que les Actions (Liste, Board, Time, Calendrier, Gantt).
- **Changement** : la restriction « onglet Liste seul » est retirée (`components/ActionItems/index.tsx`). Les vues sont les composants existants, sans duplication ; chacune lit déjà la liste d’actions par les hooks de scope, donc Interventions → `scope=interventions`, Actions → `scope=classic`.
- **Sources de données vérifiées** : Liste, Board, Time, Calendrier, Gantt et le conteneur (filtres, listes d’options) appellent tous `action_items/` avec le scope. **Un oubli corrigé** : la requête « société » (`redux/api/endpoints/company/action_item.ts`) ne transmettait pas `scope` (liste blanche de paramètres) ; elle le transmet maintenant, la vue Actions au niveau société n’affiche donc plus les interventions.
- **Limite connue** : le Gantt d’Interventions affiche aussi les phases du projet (ex. « DEFAULT PROJECT PHASE », vide), les phases et jalons n’étant pas filtrés par scope.
- **Tests ajoutés** : front (la restriction a disparu, la requête société transmet le scope) ; backend, classes `ClosedItemsScopeTests` (éléments clôturés : chaque espace garde les siens, avec et sans « Show closed items ») et `RestrictedUserScopeTests` (utilisateur limité : chaque espace n’affiche que ce qu’il a le droit de voir, aucune intervention dans Actions).
- **Recette navigateur** (projet 19, 1600 px) : Interventions : les cinq onglets s’affichent, requête unique `scope=interventions` ; Board : mères, sous-actions, flèche « Back to the parent action » ; Time, Calendrier et Gantt montrent les interventions (Gantt : demandes, Cadrage / Réalisation / Réception). Actions : requête `scope=classic`, aucune intervention dans aucun des cinq onglets (seule l’action classique « test »). Utilisateur limité : Board, Time, Calendrier et Liste ne montrent que sa mère et sa sous-action ; menu Interventions visible.
- **Non exécuté** : glisser-déposer du Board (le HTML5 drag-and-drop ne se simule pas par la souris pilotée) ; la modification passe par les mêmes composants et le même panneau, vérifiés à l’ouverture. Rendu visuel du Calendrier / Gantt pour les éléments clôturés non vérifié (couvert côté API).

## 11. Passe de vérification complémentaire (filtres, compteurs, éléments clôturés, glisser-déposer)
**Noms exacts des cinq vues** (onglets, identiques dans Actions et Interventions) : **List** (« Project List » au niveau projet), **Board**, **Time**, **Calendar**, **Gantt**.

**Anomalie trouvée et corrigée (commune aux deux espaces).** Le serveur exclut les actions clôturées sauf demande `show_closed` (lot MOD-03). Seule la Liste envoyait ce paramètre : Board, Calendar, Gantt et Time ne recevaient jamais d’élément clôturé, et « Show closed items », « Closed This Month » et la colonne « Closed » du Board restaient sans effet. Correction minimale : une fonction partagée `shouldIncludeClosed(filters)` (`filterUtils.ts`, mêmes conditions que la Liste : bascule, statut clôturé, filtre de date de clôture), utilisée par Liste, Board, Calendar et le hook Gantt. Aucune règle serveur modifiée.

**Recette navigateur** (projet 19, utilisateur `author`, données connues relevées en base ; Interventions = 11 mères ouvertes, 1 en cours, 3 clôturées + sous-actions ; Actions = 1 action classique ouverte, n° 181) :
- **Status (Interventions)** : filtre « in progress » → Board 4 cartes (114, 123, 124, 171) = base ; Calendrier : les mêmes 4 éléments ; Gantt : ces 4 lignes avec leurs mères comme contexte ; aucune action classique. Les options de statut ne proposent que celles des interventions.
- **Assignee (Interventions)** : « Demo User » → Board 1 carte (114), Calendrier « Montage » (8–9 nov.), Gantt « Montage » ; l’action classique 181, aussi affectée à Demo User, n’apparaît pas. Les options d’affectation proposent Demo Configurator, Demo Limited, Demo User.
- **Actions** : une seule option d’affectation (Demo User) ; filtre → Board [181], Calendrier « test », Gantt « test » ; aucune intervention ; requêtes `scope=classic` (et `show_closed=true&scope=classic` avec la bascule).
- **Compteurs / cartes du Board** : les en-têtes de colonnes n’affichent aucun nombre ; comparaison faite sur les cartes effectivement affichées contre la base. Interventions, avant filtre : Open 34, In Progress 4 (= base) ; filtre « in progress » : 4 ; filtre « Demo User » : 1. Avec « Show closed items » : colonne Closed 12 (3 mères + 9 sous-actions clôturées = base). Actions : 1 carte (181) avant et après filtre, avec ou sans clôturés, aucune carte d’intervention.
- **Éléments clôturés, Calendrier et Gantt (Interventions)** : bascule + « Closed This Month » → le Calendrier de novembre 2026 affiche les éléments clôturés (ex. « Préparation » le 5 nov., sous-action 113 clôturée le 8 nov.) et le Gantt les lignes « Closed » ; sans la bascule ils n’apparaissent pas ; dans Actions, rien de clôturé d’intervention n’apparaît.
- **Glisser-déposer du Board : vérifié** (la capture d’écran d’après-glissement est en retard sur l’affichage, c’est ce qui avait fait croire à un échec) : la carte 115 déposée dans « In Progress » passe la sous-action et sa mère 112 à « in progress » en base (règle de la mère inchangée).
- **Restauration** : les actions 112 et 115, déplacées par deux glisser-déposer d’essai, ont été remises « open » ; seul leur horodatage de modification a changé. Aucune autre donnée de démonstration modifiée.
- Constat annexe : le bouton « + New Intervention » du Header Interventions provient d’un travail concurrent (non modifié ici).

**Procédure de test manuel du glisser-déposer (Sébastien)** : Interventions → Board → glisser une carte d’une colonne à l’autre (ex. une sous-action de « Open » vers « In Progress ») → la carte change de colonne et la mère passe « In Progress » ; la remettre dans « Open » (ou utiliser le panneau) pour restaurer ; refaire dans Actions avec l’action classique ; vérifier avec un utilisateur restreint qu’une carte non modifiable ne se déplace pas. Réserve conservée : sa validation interactive est la tienne, l’outil ne la rejoue qu’à l’aveugle.

**Résultats des suites relancées après les derniers correctifs** : voir le bilan de la session (tests automatisés distingués de la recette navigateur).
