# PROSEVIEW — MOD-03 : ajustements UX

Statut : réalisé en local (backend + écrans), panneau corrigé (§11) et finalisé en itération 3 (§12). **Non commité, non déployé.** MOD-03 **non clôturé** : en attente de validation explicite de Sébastien, lot suivant non commencé.
MOD-02 (dont 02b/02c) a été explicitement validé par Sébastien avant ce lot. Aucune migration ajoutée, aucune reprise de données anciennes. Données de recette synthétiques.

## 1. Fenêtre d'action → panneau latéral droit (présentation en une colonne : voir §11)

- La fenêtre de consultation/modification d'une action (`EDIT_ACTION_ITEM`) s'ouvre désormais dans un **panneau latéral droit** (MUI `Drawer`, même principe que le panneau Contacts), implémenté dans `GlobalModalFetcher` : les autres fenêtres restent centrées.
- **Composants et formulaires réutilisés tels quels** : statuts, sous-actions, BPU (mère), avancement, droits, validations, enregistrement inchangés. Largeur `min(1040px, 94 %)`, plein écran sous la largeur étroite.
- **Fermeture** : croix, Échap, clic hors du panneau → même garde qu'avant : si des modifications existent, « Enregistrer les modifications ? » **Annuler / Non / Oui** (Annuler garde le panneau ouvert ; Non ferme sans enregistrer ; Oui enregistre puis ferme). Sans modification : fermeture directe.
- **La liste n'est ni rechargée ni repositionnée** (filtres et défilement conservés ; la ligne modifiée est mise à jour en place).
- **Fenêtre étroite** : le formulaire à deux colonnes était écrasé (colonne de saisie ≈ 40 px à 700 px). Une modification minimale (3 lignes) dans le formulaire empile les deux colonnes sous le point de rupture `lg` (1200 px) ; au-dessus, rendu inchangé. Hors blocs CRM-1B.
- Travaux concurrents : `view_edit_action_item_form/index.tsx` (CRM-1B) : seules mes lignes de mise en page ont été ajoutées ; `Network/CrmRecordDrawer.tsx` (CRM-1B) non touché.

## 2. Bouton « Import »

- Dans « Action items » d'un **projet** : bouton **Import** (contour) **juste à droite** de « + Nouvelle action », espacement de 16 px, même ligne ; libellé réutilisé des traductions existantes (EN **Import**, FR **Importer**, DE **Importieren**).
- Destination inchangée : liste des demandes (`/project-intervention-requests`) et import CSV. Retiré de l'emplacement précédent.
- Les deux boutons passent à la ligne (`flex-wrap`) en fenêtre étroite sans sortir de l'écran.

## 3. Liste des demandes

- Colonne **Budget retirée de l'affichage** (la donnée reste en base et dans l'API).
- Nouvelle colonne **Date de clôture** (EN *Closure date*, DE *Abschlussdatum*) = **date de clôture effective de l'action mère liée** (`ActionItem.closed_at`).
- « — » tant que la mère n'est pas clôturée ; jamais déduite de l'échéance, de la dernière modification ni de la dernière sous-action ; **anciennes demandes sans mère : aucune date inventée** (« — »), même si toutes leurs actions sont clôturées ; mère clôturée sans date enregistrée : « — ».
- **Vérification demandée — la date existe déjà en base** : `closed_at` / `closed_by` sont déjà renseignés à chaque clôture (enregistrement de la fenêtre, mise à jour groupée, clôture automatique MOD-01). **Aucune migration nécessaire, aucun ajout de champ.** Seule l'API de la liste expose `closure_date` (lecture seule, sans requête supplémentaire par ligne).

## 4. Identifiants des sous-actions

- L'ID d'une sous-action prend **exactement la taille de police de son titre** (hérite de la cellule : 14 px = 14 px mesuré), poids normal, couleur secondaire, retrait de 24 px. Les actions mères gardent leur style. **Identifiants et numérotation inchangés.**

## 5. Défauts trouvés par la recette navigateur et corrigés

1. **Enregistrement refusé (400) d'une action sans points** : la fenêtre renvoie `progress_setting: null` (lecture seule) et le serveur refusait le `null`. Corrigé (`allow_null`, valeur ignorée à la mise à jour comme les autres échos) ; test dédié. Ce cas touche toute mère importée.
2. **Formulaire écrasé en fenêtre étroite** : cf. §1.

## 6. Constats hors périmètre (non modifiés)

- **Règle existante « la date cible ne doit pas dépasser l'échéance du parent »** : une mère importée dont l'échéance attendue dépasse celle de son jalon (ex. action 146, 18/12/2026) ne peut plus être enregistrée depuis la fenêtre (400 explicite). Comportement antérieur à MOD-03, propre aux données ; à arbitrer avec Sébastien (assouplir pour les mères importées ou aligner l'échéance du jalon).
- Le journal de la console de développement signale des clés de traduction manquantes (`InfoDrawer.*` : `rowType`, `sub_reference`, `effective_bpu`, `forecast_date`, `assignees`…) : le tiroir d'information de la liste affiche toutes les clés de la ligne avec une valeur de repli lisible ; aucun effet visible, antérieur à ce lot.
- Après un enregistrement refusé, la fenêtre rouverte peut réafficher la saisie non enregistrée (cache) ; la base n'est pas modifiée (comportement antérieur).
- Émulation « téléphone 375 px » du volet navigateur : largeur de mise en page incohérente (452 px) qui décale le panneau ; vérifié à la place à 700 px (fenêtre étroite) et 1400 px (large), mesures DOM.

## 7. Recette navigateur (compte configurateur, projet 19)

- Panneau : ouverture d'une mère (146) et d'une sous-action (113), saisie, **Échap → invite « Enregistrer ? »**, **Annuler** (reste ouvert, saisie conservée), **Non** (fermé, rien enregistré), **Oui** (action 112 : PATCH 200, ligne mise à jour en place, panneau fermé, **défilement et liste conservés**), nom restauré ensuite. Sans modification : Échap ferme sans invite.
- Action indépendante, mère et sous-action ouvertes sans régression ; aucun BPU sur la sous-action.
- Import : même ligne que « + Nouvelle action », écart 16 px, libellé « Import », clic → liste des demandes.
- Liste des demandes : colonnes External reference, LINKS reference, Title, Received on, Expected end, **Closure date**, Progress, Actions ; aucune colonne Budget ; « — » pour les demandes non clôturées, date réelle pour les clôturées (`MOD2C-002`, `MOD2B-002/003`).
- IDs : mères 16 px / sous-actions 14 px = titre 14 px, retrait 24 px.
- Larges (1400) et étroites (700) : panneau 1040 px à droite / plein écran, formulaire empilé et utilisable en étroit, aucun débordement horizontal.
- Traductions : `closureDate` et `Import` en FR/EN/DE à parité.

## 8. Résultats des tests

- Backend ciblé (MOD-01, MOD-02, MOD-02b/02c, import PROSEVIEW, Action Items : requêtes, liste, dépendances, conversion, exigences) : **247 tests, 247 réussis** (241 antérieurs + 6 nouveaux : 5 sur `closure_date` — vide avant clôture, date de la mère, clôture automatique, mère clôturée sans date, ancienne demande sans mère — et 1 sur l'enregistrement d'une action dont `progress_setting` est `null`), exécutés **après** le dernier changement de code ; `makemigrations --check` : aucune dérive.
- Frontend : 47 tests (`planLogic`, `weightedLogic`, `ActionTemplates`, `ProgressStatusBar` et 6 nouveaux contrôles de câblage MOD-03 : panneau + garde de fermeture, bouton Import, colonnes des demandes, taille de l'ID, traductions) ; `tsc --noEmit` sans erreur sur les fichiers touchés ; eslint sans erreur ; traductions FR/EN/DE à parité.
- Clavier vérifié dans le navigateur : le focus entre dans le panneau à l'ouverture, Tab circule dans ses champs, Échap ferme (avec ou sans invite).

## 9. Procédure de validation (Sébastien)

Prérequis : base à jour (`migrate project_management 0239`), frontend et login helper actifs ; connexion `http://localhost:3001/login?as=configurator&returnTo=/project-action-items` ; garder le volet navigateur affiché.

1. **Panneau** : cliquer le nom d'une action (mère, sous-action, action indépendante) → panneau à droite, liste visible derrière. Modifier un champ, **Échap** : invite Annuler/Non/Oui ; tester les trois ; **Oui** met la ligne à jour sans recharger ni faire défiler la liste.
2. **Import** : bouton « Import » juste à droite de « + Nouvelle action » ; clic → liste des demandes. Rétrécir la fenêtre : les deux boutons restent visibles.
3. **Demandes** : plus de colonne Budget ; « Date de clôture » = « — » tant que la mère n'est pas clôturée, puis sa date réelle (clôturer la dernière sous-action : la mère se clôture, la date apparaît). Anciennes demandes : « — ».
4. **Sous-actions** : « Expand all » ; l'ID de chaque sous-action a la taille du titre, retrait et couleur secondaire ; les mères inchangées.
5. **Fenêtre étroite** (~700 px) : panneau plein écran, formulaire empilé.
6. FR/EN/DE : « Importer / Import / Importieren » ; « Date de clôture / Closure date / Abschlussdatum ».
7. Clavier : Tab dans le panneau, Échap.

## 10. Hors périmètre

Lot suivant ; règle de date cible du jalon ; clés `InfoDrawer` ; reprise des anciennes demandes ; déploiement ; renumérotation des migrations (lignée locale `0235`–`0239` inchangée, conflit de livraison GED/PROSEVIEW toujours à trancher).

## 11. Correctif du panneau latéral (itération 2)

**Constat** : le panneau gardait deux colonnes sur grand écran et n'affichait **aucune sous-action**. Cause réelle : l'API de détail d'une action **omet volontairement** `sub_action_items` (`serialize_sub_action_items` est faux pour `retrieve`) ; l'ancienne grille « Subtasks » était donc toujours vide (et enfouie dans une zone à défilement de 60 vh).

**Présentation (une seule colonne, modèle fiche Document)** : référence en surtitre (`ID-mère-n` ou identifiant), titre, **badge BPU uniquement sur la mère**, jalon, puis cadre de métadonnées compact **avec retour à la ligne** (grille adaptative) : date de modification, projet (projets seulement), coût comptabilisé, heures, créé par et, si l'action est clôturée, date et auteur de clôture. Mêmes valeurs, formats, traductions et droits qu'avant ; la colonne de droite est supprimée, rien n'est dupliqué. Valeur absente : « — », jamais de valeur inventée. Ensuite viennent le formulaire (inchangé, une colonne), la section Sous-actions, puis les onglets (Commentaires, Documents liés, Structure, Prédécesseurs, Réseau CRM-1B…). Composants ajoutés dans `view_edit_action_item_form/` : `ActionPanelHeader`, `SubActionsSection`, `subActionsLogic`.

**Sous-actions de la mère** : section dédiée avec, par ligne, référence, titre, statut (même pastille que la fiche Document), **barre d'avancement** (composant PROSEVIEW existant), responsable(s) et échéance (« — » si absents). Source : l'API existante `GET action_items/<id>/sub-action-items` (accès entreprise/projet et règles de visibilité appliqués côté serveur, aucun masquage silencieux des clôturées), enrichie du seul champ `sub_reference` ; les points de la barre viennent de `weighted-progress` (contrôle d'accès existant). Sous-actions **importées et manuelles** ensemble, par ordre de création. barre : clôturée = pleine et verte ; avec points exploitables, 0 % non commencée ou part des points atteinte en cours ; sans points, statut textuel seul (voir §12). Aucun badge BPU sur les sous-actions. **Aucun calcul modifié** (affichage seulement).

**Navigation** : un clic (ou Entrée / Espace au clavier) sur une ligne ouvre la sous-action ; bouton **« Retour à l'action mère »** en tête de la sous-action. Une modification en cours est protégée avant chaque changement (même invite Annuler / Non / Oui : Oui enregistre, Non abandonne, Annuler reste) ; le panneau est **remplacé**, pas empilé. **Ajout manuel** : bouton existant « + Ajouter un sous-élément d'action » (formulaire existant) tant que l'action est modifiable ; sur une mère **clôturée**, bouton retiré et message « Cette action est clôturée : on ne peut plus lui ajouter de sous-action » (verrouillage serveur inchangé).

**Retraits assumés** : la grille de sous-tâches de la fenêtre (édition en ligne, dupliquer, supprimer par le menu « Plus ») est remplacée par la nouvelle section : lecture, ouverture, ajout. **Dupliquer et supprimer une sous-action restent disponibles depuis la liste « Action items »** ; à arbitrer par Sébastien si ces gestes doivent aussi figurer dans le panneau.

**Tests** : backend ciblé **250 / 250** (247 + 3 : toutes les sous-actions importées, manuelles et clôturées listées dans l'ordre avec référence et sans BPU ; autre entreprise refusée sans contournement des permissions ; seules les sous-actions de la mère), `makemigrations --check` sans dérive. Frontend **56 / 56** (dont 9 nouveaux : barre d'une sous-action, référence, ordre, valeurs absentes, une seule colonne, garde avant ouverture et retour, BPU, droits et verrou, traductions) ; `tsc --noEmit` et eslint sans erreur sur les fichiers touchés ; traductions FR/EN/DE à parité (`backToMother`, `noSubActions`, `openSubAction`, `lockedMother`).

**Recette navigateur** (configurateur, projet 19, données synthétiques) :
- Mère `146` (BPU) : référence, titre, badge BPU, jalon, cinq métadonnées ; formulaire sur une colonne (977 px dans un panneau de 1 040 px) ; trois sous-actions `146-1` à `146-3` sans BPU.
- Mère `120` : sous-actions **importées** (121, 122, identifiant technique), **manuelle** (`120-1`, responsable « Demo Configurator ») et **clôturée** (121 : pastille Closed, barre verte 100 %), toutes visibles. Puis mère et sous-actions clôturées : sous-actions toutes visibles, bouton d'ajout absent, message de verrouillage, champs désactivés, date et auteur de clôture ajoutés (auteur « — » car la clôture a été posée en base de test). Données de recette remises à l'état initial.
- Navigation : mère → sous-action → retour mère ; avec une modification en cours sur la mère : invite, **Annuler** (saisie conservée), **Non** (abandon, la sous-action s'ouvre), **Oui** (enregistrement, la sous-action s'ouvre, la saisie est bien enregistrée au retour) ; un seul panneau dans la pile ; Entrée sur une ligne focalisée ouvre la sous-action.
- Ajout manuel : le bouton ouvre « Add New Sub Action Item » (formulaire existant) ; Échap ferme ce formulaire sans fermer le panneau.
- Action indépendante (créée pour la recette puis supprimée) : pas de BPU, « Aucune sous-action pour le moment », bouton d'ajout, modification et enregistrement corrects, ligne mise à jour dans la liste.
- Droits : le profil local « approver » n'a pas accès au module (page « accès refusé ») ; côté serveur, une autre entreprise est refusée sur le détail et sur la liste des sous-actions (tests sans contournement).
- Fenêtres : étroite (700 px) panneau plein écran, métadonnées sur 3 colonnes qui passent à la ligne, lignes de sous-actions et formulaire à 652 px, aucun défilement horizontal ; large (1 400 px) panneau de 1 040 px.
- Limites : « Show closed items » de la liste ne remonte pas les mères clôturées (déjà constaté en MOD-02c) ; le cas « mère clôturée » a donc été vérifié en ouvrant le panneau depuis la liste déjà chargée, après clôture en base de test. Non vérifié : un utilisateur limité aux actions « assignées / créées » (aucun profil local correspondant ; couvert par les règles de visibilité serveur existantes).

## 12. Finalisation (itération 3 : trois points)

**Point 1 — plus de repli arbitraire à 50 %.** Une sous-action n'a une barre que si elle est **clôturée** (100 %, pleine, verte) ou si ses **points sont exploitables** (calcul par points conservé : 0 % non commencée, part de ses points atteinte en cours). Sans contribution finale exploitable (pas de points, ou droit de lecture des points absent), la ligne n'affiche **que le statut textuel**, sans aucun pourcentage. Le §11 est corrigé en conséquence. Aucun calcul serveur modifié.

**Point 2 — « Show closed items ».** Diagnostic : le serveur **exclut les actions clôturées par défaut** et accepte `show_closed` ; or l'écran ne l'envoyait **jamais** (le paramètre n'existait pas côté front et la requête « société » filtre ses paramètres). Le bouton ne levait donc qu'un filtre local sur des données qui ne contenaient aucune action clôturée ; même cause pour « Closed This Month » (0 ligne). Correctif minimal : la liste envoie `show_closed=true` quand l'écran demande des clôturées (bascule, statut Closed, filtres de date de clôture) ; paramètre ajouté à la requête « société » ; **sémantique du filtre inchangée** et **règles de visibilité inchangées** (le serveur reste seul juge ; `show_closed` n'élargit aucun droit : test dédié, autre entreprise = rien). Les vues Board/Calendrier/Gantt ne sont pas modifiées.

**Point 3 — utilisateur limité aux actions assignées/créées.** Compte de test **isolé et jetable** : `demo.limited@links-local.invalid`, rôle projet créé pour la recette « MOD-03 Test: Assigned Created Only » (uniquement `action_item.view_assigned_created` + lecture de la coquille projet), assigné à **une seule sous-action** (147, `146-1`) ; script `local-dev-auth/seed_mod03_limited.py` (idempotent, local) et identité `limited` ajoutée au login local. Aucun rôle existant modifié, aucun droit élargi.
- **Résultats** (navigateur et tests, vraies classes de permission, seul le rôle est simulé dans les tests) : la liste montre la mère 146 (atteignable via son enfant visible) et seulement `146-1` ; la section Sous-actions de la mère ne liste que `146-1` (« Assignees: Demo Limited », statut texte, sans barre faute de points) ; la sous-action s'ouvre directement ; « Retour à l'action mère » rouvre la mère (un seul panneau) ; accès direct à une sous-action non assignée : **403** ; un utilisateur sans aucune action assignée/créée : **403** sur la mère et sur la liste ; tentative d'enregistrement de la mère : **refusée par le serveur** (« Vous n'avez pas la permission… »).
- **Refus interentreprises** : détail de la mère, d'une sous-action et liste des sous-actions refusés (403/404, sans contournement des permissions) ; `show_closed` ne change rien ; le profil « autre entreprise » local est arrêté dès l'écran « licence requise ».
- **Constat (inchangé, antérieur)** : pour ce profil limité, l'écran présente la mère comme modifiable (calcul de droits côté interface) alors que le serveur refuse l'enregistrement ; je n'ai pas modifié ce calcul (hors périmètre, aucun droit élargi). À arbitrer.

**Dupliquer / Supprimer** : conservés dans la liste uniquement pour ce lot (rien ajouté au panneau).

**Tests** : backend ciblé **258 / 258** (250 + 8 : 5 utilisateur limité / interentreprises, 3 « show closed »), `makemigrations --check` sans dérive ; frontend **58 / 58**, `tsc --noEmit` et eslint sans erreur sur les fichiers touchés ; traductions inchangées.

**Recette navigateur** : liste **rechargée**, bascule désactivée : mère clôturée `120` absente ; bascule activée : `120` (Closed) et les autres actions clôturées apparaissent ; panneau de la mère clôturée : sous-actions clôturées visibles, ajout verrouillé, date de clôture ; bascule désactivée : `120` disparaît de nouveau. Données de recette (mère 120 et ses sous-actions) remises à l'état initial.

## 13. Correctif ciblé : édition du panneau alignée sur les permissions effectives du serveur

**Problème.** Pour un utilisateur limité (`view_assigned_created`), la mère atteinte via un enfant autorisé s'affichait modifiable (calcul de droits côté interface) alors que le serveur refusait l'enregistrement (403).

**Correctif minimal (aucune permission serveur élargie).**
- Backend : `ActionItemSerializer.can_update` (champ en lecture seule, **détail uniquement** ; `null` sur les listes), calculé par la **même vérification que le garde PATCH** (`ActionItemPermission.can_update_object`). Les gardes de lecture/écriture ne sont pas modifiés.
- Frontend (`view_edit_action_item_form/index.tsx`) : `canEdit = droit client && !clôturée && can_update !== false`. Le serveur ne peut que **restreindre** le calcul client, jamais l'élargir. Tous les champs, l'ajout de sous-action, les onglets d'édition et l'enregistrement à la fermeture dérivent déjà de `canEdit` : en lecture seule, plus aucune commande de modification, **pas de PATCH ni de boîte « enregistrer ? » à la fermeture**.
- Inchangés : la navigation vers les sous-actions autorisées (ouverture, « Retour à l'action mère »), les droits propres de chaque sous-action (calculés par le serveur sur chacune), le verrou mère clôturée, Dupliquer/Supprimer dans la liste.

**Tests ciblés.**
- Backend (+3, `LimitedUserPanelTests`) : rôle lecture seule → `can_update=false` sur la mère et la sous-action ; rôle avec `update_assigned_created` assigné à une sous-action → mère `false`, sous-action `true`, sous-action d'autrui toujours 403 ; `can_update` absent des listes et PATCH de la mère toujours 403. Module `test_intervention_plans_b` : 57/57 ; `makemigrations --check` sans dérive.
- Frontend (+1, `mod03Ux.test.mjs`) : formule `canEdit` (restriction seule) et dérivation de l'ajout, du garde de fermeture et de l'enregistrement ; navigation indépendante du droit d'édition. 18/18 sur les deux fichiers du panneau ; `tsc` sans erreur ; eslint : 0 erreur (1 avertissement `getTeamMembers` antérieur).

**Recette navigateur (`demo.limited`, liste rechargée).** Mère 146 : champs tous désactivés, pas d'ajout de sous-action, sous-action `146-1` listée ; ouverture de `146-1` : lecture seule, lien de retour, pas de badge BPU ; « Retour à l'action mère » : un seul panneau, mère toujours en lecture seule ; fermeture sans boîte de dialogue ni enregistrement. Non-régression avec l'identité auteur : mère 112 modifiable (champs actifs, ajout de sous-action présent). Viewport remis à l'état desktop.

**Constats.**
- L'onglet « Comment » est un composant partagé avec ses propres droits ; il n'est pas modifié (hors périmètre).
- Le titre du panneau reste « Edit Action Item » même en lecture seule (libellé existant) ; à arbitrer s'il faut un libellé « Voir ».

## 14. Vérification finale : 3 tests en erreur et titre du panneau

**Racine Git.** `git rev-parse --show-toplevel` : le backend est son **propre dépôt** (`C:/LINKS-MIGRATION/backend`) ; `C:/LINKS-MIGRATION` n'est pas un dépôt (cause de l'échec de mon premier essai de `git stash`, qui n'a rien modifié). Aucun stash, restauration ou annulation n'a été fait dans le dossier partagé.

**1. Exception initiale (3 tests rejoués séparément, base de test isolée `test_mod03_iso`, supprimée ensuite).** Pour chacun, l'exception d'origine est la même, avant « transaction SQL avortée » (qui n'est que secondaire) :
`Error in should_notify_user_comprehensive: relation "package_license_license" does not exist` (requête SQL brute de `ecpmpapp/license_queries.py` / `project_management/services/action_item_notifications.py` déclenchée par `Milestone.save()` dans le `setUp`, dans un bloc atomique : la requête échoue, la transaction est avortée, le `setUp` casse).
Tests : `TimesheetWeekSaveSerializerTests.test_week_save_creates_project_action_item_activity_with_project_context`, `…test_week_save_creates_project_purchase_order_activity_with_phase_and_action_item_context`, `MonthlyTimesheetBillingIntegrationTests.test_activity_documents_action_updates_company_action_item_rows_without_project`. La table `package_license_license` n'existe pas dans une base de test fraîche (elle n'est pas créée par les migrations de ce backend ; en local elle existe dans la base de développement).

**2. Contre-épreuve (deux copies de travail séparées, conteneurs jetables, bases `test_cp_with` / `test_cp_without`).** Copie « avec » = état courant (correctif `can_update` inclus) ; copie « sans » = identique, **sans ce seul correctif** (champ et méthode retirés du sérialiseur). Les 3 tests sont rejoués dans chacune.

**3. Comparaison.**
| Copie | Résultat | Exception d'origine |
|---|---|---|
| avec `can_update` | 3 erreurs / 3 | `relation "package_license_license" does not exist` ×3 |
| sans `can_update` | 3 erreurs / 3 | `relation "package_license_license" does not exist` ×3 |

**Conclusion : erreur indépendante démontrée** (identique avec et sans le correctif, cause = table absente de la base de test, antérieure et sans rapport avec les action items). Aucune régression due à `can_update`. Feuilles de temps **non corrigées** (hors périmètre, pas de diagnostic plus poussé demandé) ; à traiter dans un lot dédié (créer la table pour les tests ou neutraliser la requête de licence).
Limite : la suite élargie (122 tests) n'est **pas verte** : 119 passent, ces 3 erreurs indépendantes subsistent.

**4. Titre du panneau.** Lecture seule : « Voir l'action » (FR) / « View Action Item » (EN) / « Aktionselement anzeigen » (DE) ; édition : « Modifier l'action » / « Edit Action Item » / « Aktionselement bearbeiten » (clés existantes réutilisées) ; mêmes variantes pour les sous-actions (clés `viewSubActionItem` / `editSubActionItem`). Le titre « lecture seule » n'apparaît qu'une fois l'action chargée (pas de clignotement). Les droits propres de l'onglet Commentaires ne sont pas modifiés.
Tests : front 19/19 (+1 : titres et traductions FR/EN/DE), `tsc` sans erreur. Recette : `demo.limited` → mère « View Action Item » et sous-action « View Sub Action Item » ; auteur → « Edit Action Item ». Viewport remis en desktop.

## 15. Barres d'avancement harmonisées avec les badges de statut

**Source de couleur réutilisée.** `getColors(status_options du projet, statut)` (utils/getColors.ts) : statuts personnalisés de la configuration d'abord, puis la table par défaut (`statusStyles`), puis le repli du composant de statut (`#852222`). Le badge (`GenericChip`) et la barre utilisent désormais le **même hook** `useStatusColors` (exporté par `components/Chip`) ; aucune couleur de statut n'est modifiée.

**Barres concernées (il n'y en avait que deux emplacements).** La liste Action Items et le détail d'une action n'affichaient pas de barre avant ce lot : rien à harmoniser côté liste Action Items. Barres existantes : (1) liste et détail des **demandes** (barre de la mère liée) ; (2) section **Sous-actions** du panneau d'une mère (une barre par sous-action).

**Règle.** Contour **et** remplissage prennent la couleur du badge du statut **réel** : sous-action = son statut ; demande = statut réel de sa mère (nouveau champ en lecture seule `mother_status`, `null` pour une ancienne demande sans mère : l'état d'avancement est alors rapproché du statut par défaut le plus proche, open / in progress / closed). Pourcentage, calcul, longueur de remplissage, mention « À clôturer » et clôturée = 100 % inchangés. Une mère à 100 % encore ouverte garde la couleur de son statut réel (ex. « open ») et la mention « À clôturer ». Statut inconnu : repli du composant de statut. Sans aucun statut : couleurs de ton historiques.

**Tests ciblés.**
- Front : logique (résolution de couleur : statut réel, personnalisé, inconnu, vide ; pourcentage / remplissage / « à clôturer » inchangés ; mapping des anciennes demandes) et câblage (source unique via `useStatusColors`, même couleur sur contour et remplissage, statut transmis par les 3 appelants, couleurs par défaut intactes) : 29/29 sur les trois fichiers concernés ; `tsc` sans erreur.
- Backend : +2 tests (`mother_status` = statut réel, y compris personnalisé, liste et détail ; progression identique quel que soit le statut de la mère ; `null` sans mère). Modules `test_intervention_plans_b`, `test_intervention_plans`, `test_intervention_import` : 125/125 ; `makemigrations --check` sans dérive.

**Recette navigateur (comparaison directe, valeurs calculées).** Données de recette temporaires (restaurées ensuite : actions 112, 116, 120, 123, 146, 147 remises à « open », options de statut du projet remises à l'état initial) : statut personnalisé « Qualified » (#7A3CFF) et statut inconnu « Mystery ».

| Élément | Badge | Barre (contour = remplissage) |
|---|---|---|
| open | rgb(153, 220, 232) | rgb(153, 220, 232) |
| in progress | rgb(255, 243, 173) | rgb(255, 243, 173) |
| closed (demandes à 100 %) | non mesuré à l'écran ; table par défaut = #bdefe0 | rgb(189, 239, 224) (= #bdefe0) |
| personnalisé « Qualified » | rgb(122, 60, 255) | rgb(122, 60, 255) |
| inconnu « Mystery » | rgb(133, 34, 34) | rgb(133, 34, 34) |
| sous-action « in progress » (panneau, 33 %) | rgb(255, 243, 173) | rgb(255, 243, 173) |

Les pourcentages affichés (0 %, 10 %, 30 %, 33 %, 40 %, 100 %) sont inchangés. Viewport remis en desktop.

**Constats.**
- Certaines couleurs de statut par défaut sont très claires (« closed » #bdefe0, « completed » #E7F6EC) : contour et remplissage ressortent peu sur fond blanc. Conséquence directe de la règle demandée ; à arbitrer si un contour plus marqué est souhaité.
- Le détail d'une demande affiche le statut des actions en texte simple (pas de badge) : la comparaison directe s'y fait donc avec la liste Action Items.
- Les erreurs de console `InfoDrawer.*` (traductions manquantes) sont antérieures et hors périmètre.

## 16. Clôture

**MOD-03 validé par Sébastien (2026-11-08).** Lot clos. Rien commité, poussé ni déployé à ce stade. Points restant ouverts hors lot : 3 tests feuilles de temps en erreur (table `package_license_license` absente de la base de test), règle date cible vs échéance du jalon, conflit de migrations GED/PROSEVIEW, lisibilité des couleurs très claires des barres.
