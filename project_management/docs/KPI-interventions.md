# KPI Interventions et raccourcis « À traiter par l'équipe »

Lot local (aucun commit, push ni déploiement). Dépôts sdelacrose/* uniquement ; travaux concurrents préservés.

## 1. Existant examiné
- Moteur commun : statuts ouvert / en cours / clôturé (`action_progress.status_state`), progression pondérée (`compute_progress`, mère sans sous-action 0 / 10 / 100), BPU hérité de la mère (`effective_bpu`), clôture automatique de la mère, séparation `scope=interventions`.
- Modèle : `InterventionRequest` (une demande = une intervention ; `received_date`, `expected_end_date` = **échéance client**, les deux obligatoires) ; `ActionItem` (`closed_at`, `start_date`, `forecast_date` = dates de planification, `assignees`, `is_bpu`).
- Visibilité : `ActionItemVisibilityService` (droits serveur, utilisateur restreint « assignées / créées »).
- Dashboard projet : conteneur de widgets (`WidgetsContainer`), disposition par utilisateur fusionnée avec les widgets par défaut (`mergeDashboardWidgets`). Graphiques : `recharts` déjà présent.
- **Écart avec la demande : il n'existe pas de « dashboard Client Portal » exploitable ni de règle de visibilité client.** `/customer-portal` est une page d'accueil minimale (travail concurrent non suivi par git) sans notion d'interventions autorisées pour un client. **Client Portal : restant à faire** (aucune décision de report n'est attribuée à Sébastien) ; voir §7.

## 2. Unité de comptage
Une intervention = une demande (`InterventionRequest`). Les sous-actions (générées ou ajoutées à la main) ne sont jamais comptées. Les anciennes demandes à actions plates sont regroupées par demande ; BPU compté une seule fois par intervention (mère, ou une action plate BPU).

Toutes les mesures partent des **actions visibles** pour l'utilisateur (visibilité serveur, puis périmètre interventions, puis filtre projet `project=`/entreprise de la requête). Utilisateur restreint : une intervention n'est comptée que si sa mère (ou action plate) lui est **directement** accessible ; une mère atteinte seulement par une de ses sous-actions sert à la navigation, pas à l'autorisation, et n'entre dans aucun agrégat.

## 3. Règles de calcul (une par mesure)
| Mesure | Règle | Période |
|---|---|---|
| Nombre d'interventions | demandes visibles | état actuel, toutes dates |
| BPU / % BPU | mère `is_bpu` (ou action plate BPU) ; % = BPU / total, « — » si total 0 | état actuel |
| Terminées | statut de la mère clôturé (action plate : toutes clôturées) | état actuel |
| En cours | statut de la mère « in progress » (action plate : au moins une démarrée ou clôturée, pas toutes clôturées) | état actuel |
| Non commencées | mère « open » (action plate : toutes ouvertes) | état actuel |
| Autre statut | statut ni ouvert / en cours / clôturé (jamais compté « en cours ») | état actuel |
| En retard | non terminée **et** `expected_end_date` < aujourd'hui (Hold inclus s'il est en retard) | aujourd'hui |
| Hold / suspendues | statut normalisé ∈ {hold, on hold, on_hold, suspended} | état actuel |
| Livrées à temps | terminée, `closed_at` ≤ `expected_end_date` ; **dénominateur** = terminées avec échéance ET date de clôture ; « — » si 0 | toutes les clôtures |
| Avancement global | moyenne des progressions mesurables (mère : `compute_progress` ; mère sans sous-action 0 / 10 / 100 ; action plate : moyenne 0 / 10 / 100) | valeur actuelle |
| Histogramme | reçues = `received_date` ; clôturées = `closed_at` de l'intervention terminée ; semaines ISO (lundi), 8 par défaut, 26 max | N dernières semaines |

**Hold peut recouper un retard** : le widget n'affiche pas retard / Hold / à temps comme des parts d'un même ensemble ; le recoupement est donné (`late_and_hold`).
**Aucun statut Hold n'existe dans la configuration par défaut** (ouvert / en cours / clôturé) : le compteur vaut 0 tant qu'un projet n'en configure pas un. Rien n'est supposé.

## 4. Historique réellement disponible
- `received_date` (date de la demande) : réelle → histogramme « reçues » fiable.
- `closed_at` : réelle mais non garantie (clôture sans date possible) ; une clôture sans date n'est **jamais** datée par le widget : elle est signalée (`closed_without_date`, `closed_without_closure_date`) et exclue du taux et de l'histogramme.
- **Aucun historique de progression n'est stocké** : l'« avancement global » est la valeur actuelle, pas une courbe ; aucune série historique n'est inventée.
- L'échéance client est obligatoire en base (`expected_end_date` non nul) : « échéance absente » ne peut pas se produire ; le compteur `closed_without_deadline` reste calculé et affiché s'il devenait non nul.

## 5. Raccourcis « À traiter par l'équipe » (page Interventions, bandeau interne)
Mêmes unités que les KPI ; le filtre est appliqué côté serveur (`kpi=<flag>`) à la liste, au Board, au Calendrier, au Gantt et à Time (même requête partagée), le périmètre interventions est conservé, le filtre est dans l'URL (`?kpi=`) et s'efface d'une commande.
- **Non affectées** (`unassigned`) : intervention non terminée dont la **mère n'a aucun responsable ET aucune sous-action non clôturée n'en a** (action plate : aucune action ouverte n'a de responsable). **Règle retenue : une intervention prise en charge par ses sous-actions n'est pas « non affectée »**. Ambiguïté : alternative stricte (mère sans responsable, quel que soit l'état des sous-actions) non retenue ; les sous-actions sans responsable ne sont pas comptées séparément (pas de compteur distinct créé).
- **À planifier** (`to_plan`) : intervention non terminée sans **date de planification** (`start_date` / `forecast_date` « Planned end ») sur la mère ni sur une sous-action non clôturée. L'**échéance client** (`expected_end_date`) n'est jamais une date de planification. Ambiguïté : `target_date` (échéance propre de l'action, par défaut recopiée) est ignorée car renseignée d'office par l'import.
- **À vérifier avant clôture** (`to_verify`) : seul état réel disponible = mère non clôturée dont **toutes** les sous-actions sont clôturées (la clôture automatique n'a pas abouti, cas historique). Aucun circuit de validation n'est créé : le raccourci n'apparaît que si le compteur est > 0 (ou s'il est le filtre actif).

Filtres de clic des KPI : `bpu`, `done`, `in_progress`, `not_started`, `other_status`, `late`, `hold`, `on_time` (les deux derniers de clôture révèlent les interventions clôturées).

## 6. Présentation
Teintes centralisées (`src/constants/interventionKpi.ts`) : non commencée gris, en cours bleu, terminée / à temps vert, Hold orange, en retard rouge, BPU badge bleu libellé « BPU » ; toujours un libellé à côté de la couleur ; fonds discrets. Chargement (squelettes), absence de données, erreur avec « Réessayer ». Traductions fr / en / de (`InterventionKpis`). **Limite** : les puces de statut existantes (listes, cartes, calendrier, Gantt) gardent la configuration de couleur de statut du projet (ouvert bleu clair, en cours jaune, clôturé vert) ; elles n'ont pas été recolorées globalement pour ne pas écraser des couleurs configurables par projet.

## 7. Client Portal — restant à faire
Pas d'interface portail dans ce lot ; ce point reste à traiter. Le calcul accepte n'importe quel ensemble d'actions autorisées (`build_units(visible, limited_ids=…)`) : les agrégats ne sont jamais calculés au-delà. Pour brancher le portail il manque une **règle de visibilité client** (quelles interventions pour quel client : entreprise cliente, projet, assignation) et un dashboard portail ; l'endpoint actuel suppose les droits Action Items du projet.

## 8. API
`GET /api/project_management/action_items/intervention-kpis/?project=<id>[&weeks=8]` (droits Action Items du projet, périmètre interventions forcé) → `portfolio`, `progress`, `deadlines`, `todo`, `trends`, `as_of`. Filtre de liste : `…/action_items/?scope=interventions&kpi=<flag>` (400 si le flag est inconnu ; ignoré hors périmètre interventions).

## 9. Résultats (après les derniers correctifs)
**Tests automatisés**
- Backend `test_intervention_kpis` (25 tests : comptage sans double compte, demandes plates groupées, BPU, statuts, progression, retards, Hold recoupant un retard, dénominateur nul, clôture sans date, histogramme, filtres, raccourcis, utilisateur restreint, autre projet / autre entreprise) + séparation + scope + colonnes de liste : **67/67 OK**.
- Backend suite Action Items : **142/142 OK**.
- Front (liste, scope, KPI, widget, bandeau, plans, MOD-03) : **82/82 OK** ; `tsc` sans erreur ; `eslint` 0 erreur (un avertissement ancien dans `ActionItems/index.tsx`).

**Recette navigateur** (projet 19, 1600 px) : widget sur le dashboard projet (15 interventions, 2 BPU = 13,3 %, 3 terminées / 1 en cours / 11 non commencées, 0 en retard, 0 Hold, 100 % livrées à temps 3 sur 3 = base) ; tendances (avancement global 25 %, histogramme 8 semaines) ; clic « Non commencées » → `?kpi=not_started`, 11 interventions affichées = compteur ; bandeau « Non affectées 9 » / « À planifier 10 » = calcul indépendant en base ; liste 9 lignes, Board et Gantt conservent le filtre dans l'URL ; commande « Effacer le filtre » → URL propre ; lien profond `?kpi=on_time` → clôturées révélées, 3 lignes = compteur ; utilisateur limité : bandeau à 0 (sa mère n'est atteinte que par une sous-action) ; widget Actions classique inchangé (action « test » seule). Aucune donnée de démonstration modifiée.

**Limites** : pas d'interface Client Portal (règle de visibilité client absente) ; Hold = 0 sans statut Hold configuré ; avancement global = valeur actuelle (aucun historique) ; puces de statut existantes non recolorées globalement ; widget non vérifié visuellement en dark mode ni sur mobile (grille auto-adaptative) ; le dashboard n'est pas visible pour un utilisateur sans droit « projet » (le bandeau l'est avec le droit Action Items).

## 10. Procédure de recette courte (Sébastien)
1. Dashboard projet : le widget « KPI Interventions » montre Portefeuille / Avancement / Respect des délais ; la note rappelle que Hold peut recouper un retard.
2. Cliquer chaque indicateur (BPU, terminées, en cours, non commencées, en retard, Hold, à temps) : la page Interventions s'ouvre avec `?kpi=…`, le filtre actif est affiché, le nombre de lignes = le compteur.
3. « Voir les tendances » : avancement global et histogramme hebdomadaire.
4. Page Interventions : bandeau « À traiter par l'équipe » ; cliquer « Non affectées » puis changer de vue (Board, Calendrier, Gantt) : le filtre reste ; « Effacer le filtre » le retire.
5. Utilisateur restreint : seuls ses propres éléments sont comptés.
6. Actions classique : aucune intervention, aucun filtre `kpi`.

## 11. Vérifications complémentaires (cas non nuls, mobile, mode sombre, couleurs, utilisateur restreint)
**Données de test isolées** : 5 interventions préfixées `ZZKPI` (projet 19) créées pour l'essai puis **supprimées** (15 demandes restantes = état initial ; actions 112 et 115 « open »). Cas : en retard seule ; Hold seule ; **en retard ET Hold** ; « à vérifier » (toutes les sous-actions clôturées, mère encore ouverte) ; terminée après l'échéance.

**Cas non nuls (navigateur, avec ces données)** : widget = 20 interventions (15 + 5), 4 terminées / 1 en cours / 13 non commencées / 2 « autre statut (Hold) » ; **en retard 2, Hold 2, dont 1 aussi en retard** ; livrées à temps **75 % (3 sur 4)** ; raccourci « À vérifier avant clôture · 1 » visible dans le bandeau (il est masqué à 0). Filtres : `?kpi=late` → 2 lignes (ZZKPI-LATE « Open », ZZKPI-LATEHOLD « On Hold ») ; `?kpi=hold` → 2 lignes (ZZKPI-HOLD, ZZKPI-LATEHOLD) ; `?kpi=to_verify` → 1 ligne (ZZKPI-VERIFY) ; « Non affectées 12 » et « À planifier 14 » égaux à un calcul indépendant en base. Dans chaque cas le compteur = le nombre d'interventions (mères) affichées.

**Utilisateur voyant seulement une sous-action** : le serveur renvoie `navigation_only` (interventions visibles mais non comptées). Le bandeau affiche : « 1 intervention est visible uniquement à travers vos propres sous-actions : elle reste listée pour vous permettre d'y accéder, mais n'entre pas dans les compteurs, qui ne portent que sur les interventions que vous pouvez voir en entier. » (fr / en / de). Vérifié avec le compte `limited` : ligne visible, compteurs à 0, note affichée. Un utilisateur qui voit sa mère entière : `navigation_only = 0`. Un utilisateur au périmètre large : toujours 0.

**Mobile** : à 375 px **toute l'application** (menu latéral inclus) laisse environ 55 px au contenu du dashboard (tous les widgets, y compris « Overdue and upcoming Action Items », sont écrasés) : limite existante de la mise en page de LINKS, pas du widget ; la recette visuelle du widget sur téléphone n'est donc pas faisable telle quelle dans l'application. Vérifié à la place : (1) tablette 768 px, colonne de widget de 177 px ; (2) le widget isolé à 330 px de large. Correction faite : les largeurs minimales des grilles sont plafonnées (`minmax(min(100%, …), 1fr)`) ; après correction, **0 élément débordant**, tuiles de 118 px (colonne 177 px) et 132 px (330 px), deux tuiles par ligne.

**Mode sombre** : LINKS n'a pas de thème sombre (la page reste blanche avec `prefers-color-scheme: dark` émulé) ; le widget garde ses teintes claires fixes, cohérent avec le reste de l'application. Contrastes texte / fond des teintes : non commencée 6,98 ; en cours 5,57 ; terminée 5,40 ; Hold 5,20 ; en retard 6,05 ; BPU 7,43 (tous ≥ 4,5 : AA). Pas de mode sombre à livrer sans thème applicatif.

**Cohérence des couleurs widget / statuts configurés du projet** (rien n'a été modifié ni écrasé) :
| Élément | Puce de statut du projet (configuration par défaut, projet 19) | Teinte du widget |
|---|---|---|
| Ouvert / non commencée | #99DCE8 (bleu clair) | gris (#98A2B3) |
| En cours | #FFF3AD (jaune) | bleu (#2E90FA) |
| Clôturé / terminée | #BDEFE0 (vert) | vert (#17B26A) |
| On Hold | puce orange (fond #FEF6E7, texte #865503) | orange (#F79009) |
| En retard | pas de statut : alerte distincte | rouge |
Constat : **terminée et Hold sont cohérents ; « ouvert » et « en cours » ne le sont pas** (la configuration du projet donne bleu clair et jaune, le widget gris et bleu), donc les barres de progression des listes (couleur du statut configuré) et le widget n'ont pas la même couleur pour « en cours ». Les configurations de statut étant propres à chaque projet, aucune couleur n'a été imposée. **Décision à prendre (Sébastien)** : (A) le widget reprend les couleurs de statut configurées du projet pour ouvert / en cours / clôturé (cohérence totale avec les listes, les alertes retard / Hold / BPU gardent leurs teintes fixes) ; (B) garder la sémantique fixe gris / bleu / vert demandée et faire évoluer la configuration par défaut des statuts (impact sur tous les écrans, à valider).

## 12. Procédure de recette — interventions ou lignes ?
Ne pas confondre :
- **le compteur** = un nombre d'**interventions** (une demande = une mère, ou un groupe d'anciennes actions plates) ;
- **les lignes de la liste** : une ligne par intervention quand les mères sont repliées (nombre de lignes = compteur) ; une fois les mères **dépliées**, les sous-actions s'ajoutent aux lignes ;
- **les cartes du Board / éléments du Calendrier et du Gantt** : une carte ou un élément par **action** (mère **et** sous-actions) : le nombre est supérieur au compteur (ex. 9 interventions non affectées = 9 lignes repliées, 27 cartes au Board).
Pour comparer au compteur : onglet **List**, mères repliées, compter les lignes de premier niveau.

**Procédure courte**
1. Dashboard projet : lire le widget ; cliquer un indicateur ; sur la page Interventions, onglet List, mères repliées : nombre de lignes = nombre de l'indicateur.
2. Passer au Board, Calendrier, Gantt : le filtre reste (URL `?kpi=`), les éléments comprennent les sous-actions (plus nombreux que le compteur).
3. « Effacer le filtre » : retour à la liste complète.
4. Cas retard / Hold / à vérifier : nécessitent des données (voir les cas ZZKPI ci-dessus, supprimés après essai).
5. Utilisateur restreint : la note du bandeau explique les lignes visibles hors compteurs.
