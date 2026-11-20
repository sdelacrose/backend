# PROSEVIEW — MOD-02 : finalisation de la validation et rendu d'avancement

Statut : réalisé en local (backend + écrans). **Non commité, non déployé.** MOD-02 **non clôturé**, MOD-03 non commencé. En attente de validation explicite de Sébastien.
Complète `MOD-02-import-modeles.md` et `MOD-02b-complements.md`. Données de recette synthétiques et anonymisées.

## 1. Barre d'avancement PROSEVIEW (rendu validé)

Composant dédié `src/components/ProgressStatusBar` (n'altère aucun indicateur d'un autre module), utilisé dans la **liste des demandes** (colonne Avancement) et en tête du **détail de la demande**.

| État | Rendu |
|---|---|
| Non commencée | barre vide, contour rouge, « 0 % » |
| En cours | contour et remplissage orange, proportionnel au pourcentage |
| Clôturée | barre entièrement verte, « 100 % » |
| 100 % avec une sous-action encore ouverte | **reste orange**, remplie, « 100 % » + « À clôturer » (EN « To close », DE « Abzuschließen ») |

Le pourcentage est toujours imprimé à côté de la barre (jamais dans la barre), sans retour à la ligne. Calculs et règles de statut **inchangés** : l'API de la liste et du détail expose `progress_percent` et `progress_to_close` en plus de `progress_status` ; le pourcentage vient des points MOD-01 (somme des contributions des sous-actions, mère seule 0 / 10 / 100, clôturée = 100), les anciennes demandes à actions plates utilisent la part d'actions clôturées. Aucune requête par ligne (sous-actions et réglages préchargés ; test dédié).

## 2. BPU : badge uniquement sur l'action mère

- Badge « BPU » visible **uniquement sur l'action mère** : liste des demandes (détail), liste « Action items », fenêtre de détail d'action. Plus aucun badge ni mention BPU sur les sous-actions, ni dans les listes, ni dans les détails, ni dans le formulaire d'ajout de sous-action.
- Inchangé : héritage métier (`effective_bpu` côté API), toggle BPU de la mère dans l'aperçu d'import, règle de priorité de `MOD-02b`.
- Conséquence assumée : les anciennes valeurs BPU **par ligne** enregistrées par MOD-02 (données conservées, jamais réécrites) ne sont plus affichées nulle part dans l'écran (elles restent dans l'API, `bpu_source = legacy_line`). Cette consigne remplace la demande précédente d'afficher leur badge sur les sous-actions ; le test correspondant vérifie désormais qu'elles ne sont pas affichées dans la liste et que la liste ne paie aucune requête pour elles (un test de non-régression existant l'exigeait).

## 3. Documentation demandée

- **BPU** : réglable **à l'import** (interrupteur de la mère dans l'éditeur de plan). Une modification **ultérieure** du BPU d'une mère existante n'est disponible **que par l'API** (`PATCH action_items/<id>` `{is_bpu}`, refusé si la mère est clôturée) ; aucun interrupteur dans la fenêtre de modification d'action à ce stade (fenêtre modifiée par le chantier CRM-1B, non écrasée).
- **Numérotation** : `ID-mère-n` s'applique **uniquement aux nouvelles sous-actions**. Aucune renumérotation : les sous-actions existantes n'ont pas de numéro (identifiant technique affiché) ; sur une mère qui a déjà des sous-actions non numérotées, la première nouvelle prend le rang 1.
- **Conflit de migrations — toujours bloquant pour la livraison** : la branche `deliver/prosevi-backend-20261105` porte `0231_intervention_requests` alors que `release/ged-2026.11.04-*` porte `0231_document_logical_withdrawal` à `0234` sur la même base `0230` ; la lignée locale (`0235` à `0239`) est cohérente mais n'est pas la lignée de livraison. Aucune renumérotation aveugle, aucune migration appliquée ailleurs modifiée ; `0236` → `0239` devront être livrées ensemble après la migration PROSEVIEW retenue ; l'ordre GED/PROSEVIEW doit être tranché avant toute livraison.

## 4. Défauts trouvés par la recette navigateur et corrigés

1. **Enregistrement de la fenêtre d'action refusé (400)** : la fenêtre renvoie toute la représentation, dont `progress_setting` (lecture seule) ; le serveur la refusait à la mise à jour. Désormais ignorée à la mise à jour (jamais appliquée) ; tests dédiés.
2. **Date de début inventée** : pour une action sans date de début, la fenêtre affichait puis **enregistrait `01/01/1970`**. Corrigé (date absente conservée absente) dans `view_edit_action_item_form` (3 lignes, hors blocs CRM-1B). La ligne de recette concernée a été remise à vide.
3. **Détail de la demande incomplet** : les sous-actions ajoutées à la main n'y figuraient pas (elles ne sont pas liées à la demande) alors qu'elles comptent dans l'avancement ; le détail liste maintenant la mère et toutes ses sous-actions.
4. **Régression évitée** : la lecture des anciens BPU par ligne dans la liste ajoutait une requête et cassait un test existant (`test_compact_projection_serializes_prefetched_children_without_queries`) ; retirée (cf. §2).

## 5. Recette navigateur (compte configurateur, projet 19, données synthétiques)

- **Réimport** de `prosevi_mod02b_synthetique.csv` : 0 à créer, 3 déjà importées, 1 erreur (titre de 256 caractères) ; confirmation par l'API : 0 demande / 0 action créées, seconde confirmation « non appliquée » ; **empreinte de toutes les actions, demandes et modèles identique avant et après** (statuts, responsables, dates, points, BPU, numéros, modèles).
- **« Remplacer le plan »** (`MOD2C-001`) : plan modifié, choix d'un autre modèle, avertissement ; **Remplacer** : sous-actions du modèle appliquées (Cadrage / Réalisation / Réception) ; puis édition d'un titre et d'un point ; import : plan enregistré tel quel (`169-1…3`), **modèles entreprise inchangés**.
- **Clôture depuis l'interface** (`MOD2C-002`, fenêtre d'action : statut « Closed » puis fermeture → « Enregistrer ? » → Oui) : dernière sous-action clôturée, **mère clôturée automatiquement** (date et auteur), avancement vert. Ajout ensuite refusé (400, vérifié par l'API) ; sous-action clôturée non modifiable ; l'ajout par le bouton de la fenêtre sur une mère clôturée n'a pas pu être rejoué dans l'écran (les éléments clôturés ne reviennent pas dans la liste après le filtre « Afficher les éléments clôturés »).
- **Mère sans sous-action** (`MOD2C-003` / `MOD2B-003`) : 0 % (rouge, vide) à l'import ; 0 → 10 → 100 % vérifié par les tests automatisés et le calcul du serveur ; la clôture à 100 % de `MOD2B-003` est passée par le premier ajout manuel (calcul par contributions).
- **États de la barre** (liste, DOM et captures) : `MOD2C-003` 0 % rouge, remplissage 0 ; `MOD2C-001` 30 % orange, remplissage ≈ 30 % ; `MOD2B-002` 100 % orange + « À clôturer » avec une sous-action à 0 point ouverte (puis clôture de celle-ci : vert 100 %, « clôturée ») ; `MOD2C-002` et `MOD2B-003` 100 % vert.
- **BPU** : mère `146` et `164` badgées dans « Action items » ; `147`, `148`, `149` sans badge, avec leurs références `146-1…3` ; fenêtre de la mère « BPU », fenêtre de la sous-action « Référence 146-1 » sans BPU ; détail de la demande : chip BPU sur la ligne de la mère seule.
- **Fenêtres large et étroite** : large (émulation 1400 px) et étroite (préréglage mobile 375 px) : barre, pourcentage et « À clôturer » lisibles, aucun débordement du libellé hors du conteneur ; en étroit, « À clôturer » passe à la ligne et le tableau du détail défile horizontalement.
- Limite de l'outil de recette : le volet navigateur masqué bride les minuteurs (page très lente) ; les vérifications ont été refaites volet affiché.

## 6. Résultats des tests

- Backend ciblé (MOD-01, MOD-02, MOD-02b, import PROSEVIEW, Action Items : requêtes, liste, dépendances, conversion, exigences) : **241 tests, 241 réussis** (dernière exécution complète, **après** le dernier changement de code ; l'exécution précédente avait révélé un échec réel, corrigé, voir §4.4) ; `makemigrations --check` sans dérive.
- Frontend : 36 tests (`planLogic`, `weightedLogic`, `ActionTemplates`, `ProgressStatusBar`) ; `tsc --noEmit` propre sur les fichiers touchés ; eslint sans erreur ; traductions FR/EN/DE à parité.

## 7. Procédure de validation (Sébastien)

Prérequis : base à jour (`migrate project_management 0239`), frontend et login helper actifs ; connexion `http://localhost:3001/login?as=configurator&returnTo=/projects` ; **garder le volet navigateur affiché**.

1. `/project-intervention-requests` : colonne **Avancement** — barre rouge vide « 0 % » (non commencée), orange proportionnelle (en cours), verte (clôturée) ; pourcentage toujours lisible.
2. **100 % non clôturé** : ouvrir une demande dont toutes les sous-actions à points sont clôturées mais qui garde une sous-action à 0 point ouverte (créer : « + Add Sub Action Item » avec 0/0 avant de clôturer les autres) : barre orange pleine + « À clôturer » ; clôturer la dernière : barre verte.
3. **Clôture** : liste « Action items » → nom de la sous-action → Statut « Closed » → fermer la fenêtre → Oui ; la mère se clôture automatiquement.
4. **BPU** : importer une demande avec l'interrupteur BPU : badge « BPU » sur la mère seule (liste « Action items », fenêtre de la mère, détail de la demande), aucun sur ses sous-actions.
5. **Sans sous-action** : confirmer une demande sans sous-action (0 %), puis ouvrir la mère → statut « In Progress » (10 %), « Closed » (100 %).
6. **Fenêtres** : élargir et rétrécir la fenêtre : barre et pourcentage restent lisibles.
7. FR/EN/DE : « À clôturer » / « To close » / « Abzuschließen ».

## 8. Hors périmètre

MOD-03 ; interrupteur BPU dans la fenêtre de modification d'une action ; affichage des anciens BPU par ligne ; barre d'avancement dans d'autres écrans ou modules ; reprise des anciennes demandes ; déploiement.
