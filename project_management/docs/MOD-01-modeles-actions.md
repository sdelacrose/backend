# PROSEVIEW — MOD-01 : modèles d'actions et avancement par statut (révision 2 : périmètre ENTREPRISE)

Statut : corrigé en local (backend + écran). **Non déployé, non commité.** MOD-02 non commencé. En attente de validation de Sébastien.
Aucune donnée client ni secret ici ; les comptes cités sont des comptes locaux jetables (`*.invalid`).

## 1. Ce qui change dans cette révision

| Point demandé | Réalisation |
|---|---|
| Config au niveau entreprise | Les modèles appartiennent à une **entreprise** (`ActionTemplate.company`), indépendamment d'un projet, et servent dans tous les projets de l'entreprise. L'écran est dans **Configuration entreprise** (`/company-config/action-templates`, bouton « Modèles d'actions »). Le bouton et la page de la configuration projet sont retirés. |
| Unicité | Titre normalisé unique **par entreprise** (contrainte BDD `unique_action_template_title_per_company` + validation 400). Le même titre reste possible dans une autre entreprise. |
| Application | `apply_template` lit l'entreprise **depuis le projet de l'action mère**, côté serveur : modèle d'une autre entreprise refusé, projet non accessible à l'appelant refusé, modèle ambigu refusé. `find_template(project, titre)` cherche dans l'entreprise du projet (jamais d'une valeur fournie par le client). |
| Niveau projet conservé | Demandes, actions mères et sous-actions restent des données projet. Les points déjà copiés sur les actions (`ActionItemProgressSetting`) ne changent pas. |
| Droits | Droits **entreprise existants** `company_configurations` (voir §3). |

## 2. Reprise des modèles existants (migration `0237`)

- `0236` n'est **pas réécrite** : `0237_action_templates_company_scope` dépend de `0236` (déjà appliquée localement) ; appliquée localement sans erreur.
- Opérations : suppression de l'ancienne contrainte par projet, ajout de `company` et `needs_review`, `project` → `origin_project` (provenance seulement, `SET_NULL`), rattachement de chaque modèle à l'entreprise de son projet, nouvelle contrainte par entreprise.
- **Aucun modèle supprimé, aucune fusion.** Les collisions de titre normalisé entre modèles de plusieurs projets d'une même entreprise sont **détectées** : tous les membres du groupe passent `needs_review = true` et la migration l'affiche (`title collision … between templates [ids] (from projects […]) … nothing merged`). Un modèle sans entreprise (projet sans entreprise) est conservé et signalé de la même façon.
- Un modèle `needs_review` est listé avec un avertissement, **ne peut pas être appliqué** ni trouvé par `find_template`. Résolution manuelle : renommer l'un d'eux (ou supprimer un doublon) ; le drapeau des autres est levé automatiquement dès que le titre n'est plus ambigu.
- Un test exécute la vraie migration `0236 → 0237` sur des lignes (modèle unique, doublon inter-projets, projet sans entreprise). Il a révélé un défaut de la première version de la migration (PostgreSQL refuse `CREATE INDEX` avec des contrôles de clés étrangères différés en attente après les mises à jour) : corrigé dans la migration par `SET CONSTRAINTS ALL IMMEDIATE/DEFERRED` après les mises à jour. La migration locale avait été appliquée sur 0 ligne avant cette correction ; seule l'étape de données change.
- Retour arrière : `0237` est inversible pour le schéma ; le retour à `0236` n'est valable que si aucun modèle sans projet d'origine n'existe (les modèles créés après `0237` n'ont pas de projet).

## 3. Droits (audit et choix)

- Droit réutilisé : module **`company_configurations`**, portée **entreprise**, mêmes contrôles que `CompanyConfigurationsPermission` (`companyprofileapis/permissions.py`) : lecture = `view` ou `full_access` ; création = `create` ou `full_access` ; modification = `update` ou `full_access` ; suppression = `delete` ou `full_access`. L'entreprise est toujours celle de l'utilisateur ; un freelancer n'a pas d'entreprise donc aucun accès.
- Front : `usePermissionCheck(MODULE_TYPE.COMPANY, company_configurations)` pour `view/create/update/delete`. Sans droit d'écriture : pas de bouton « Nouveau modèle », pas de suppression, champs en lecture seule, pas de « Enregistrer » ni d'ajout/retrait de sous-action, bandeau « Lecture seule », bouton « Fermer ». Le serveur reste l'autorité (403 vérifié).
- **Cause du refus constaté sur la capture** : la première version contrôlait le droit **projet** `ecpmp_configurations` sur le projet actif (ici le projet 28, sur lequel le compte n'avait pas de rôle projet). Ce n'était pas le bon droit pour une configuration d'entreprise ; aucun contrôle n'a été supprimé ni aucun rôle accordé pour « faire marcher » l'écran.
- **Rôle/droit à attribuer** à un compte qui doit gérer les modèles : droit entreprise `company_configurations.create` + `company_configurations.update` (+ `company_configurations.delete` pour supprimer), ou `company_configurations.full_access`. Exemples de rôles locaux qui les portent : « Local Demo Company Role » (create/update/delete/view), « Local Demo Full Access » (full_access). Un lecteur n'a besoin que de `company_configurations.view` (rôle local « Test: Read Only »).
- Lookup (`action-templates/lookup?project=`) : lisible par qui peut lire les demandes du projet (accès projet + droit Action Item), sans droit de configuration, car l'import devra lire les modèles ; l'entreprise vient du projet autorisé.

## 4. Règles conservées (décisions de Sébastien)

1. **Statut inconnu = 0 point**, jamais assimilé à « en cours », listé dans `unmapped_statuses` ; aucun mapping supplémentaire (`open` → non démarrée, `in progress` → en cours, `closed` → clôturée).
2. **KPI projet inchangés** : `calculate_action_item_progress` reste binaire (clôturées / total). L'avancement par points est un calcul distinct (`GET action_items/<id>/weighted-progress`) ; les deux peuvent différer.
3. **100 % possible sans clôture** tant qu'une sous-action (à 0 point ou ajoutée à la main) reste ouverte : la mère se clôture seulement quand toutes les sous-actions sont clôturées (uniquement si ses sous-actions portent des points copiés).
4. Aucune réouverture n'existe dans LINKS : la clôture automatique ne contredit aucune règle ; une mère clôturée ne reçoit plus de sous-action.

Règles de calcul inchangées : total « Clôturée » = 100, `0 ≤ en cours ≤ clôturée ≤ 100`, progression = somme des contributions (parcours 0 → 10 → 30 → 40 → 50 → 60 → 100 %).

## 5. API (backend)

`GET/POST /api/project_management/action-templates` (entreprise de l'utilisateur ; `company` envoyé par le client ignoré) · `GET/PATCH/DELETE …/action-templates/<id>` · `GET …/action-templates/lookup?project&title` · `GET …/action_items/<id>/weighted-progress`.
Réponse d'un modèle : `id, title, sub_actions[], needs_review, created_at, updated_at`.

## 6. Fichiers

Backend — nouveaux : `models/action_template.py`, `services/action_progress.py`, `action_progress_signals.py`, `views/action_template.py`, `migrations/0236_action_templates_progress.py`, `migrations/0237_action_templates_company_scope.py`, `test_action_templates.py`, ce document ; modifiés (hunks MOD-01 seulement) : `models/__init__.py`, `urls.py`, `apps.py`.
Frontend — nouveaux : `src/views/ActionTemplates/{index.tsx,logic.ts,tests/logic.test.mjs}`, `src/redux/api/endpoints/shared/actionTemplates.ts`, `src/app/(pages)/company-config/action-templates/page.tsx` ; modifiés (hunks MOD-01 seulement) : `src/views/CompanyConfig/index.tsx` (bouton + `useRouter`), `src/redux/api/hooks.ts`, `package.json` (script `test:action-templates`), `messages/{en,fr,de}.json` (`ActionTemplates` ; clé `GlobalConfiguration.ActionTemplates`). `src/views/ProjectConfig/index.tsx` : plus aucune modification MOD-01.
Import, bouton « Demande client », liste, demandes existantes : non modifiés.

## 7. Résultats

- Backend ciblé : **165/165** (44 `test_action_templates` : droits, isolation entreprise, unicité, validation, calcul, clôture, copies figées, application depuis deux projets, refus d'un autre entreprise/projet non accessible/modèle ambigu, collisions, migration sur données ; + 121 non-régression Action Items et import PROSEVIEW). `makemigrations --check` : aucune dérive. (Les droits d'écriture sont simulés dans la plupart des tests API ; un test sans simulation vérifie que le contrôle réel refuse un utilisateur sans rôle.)
- Front : `test:action-templates` 9/9 ; `tsc --noEmit` sans erreur sur les fichiers du lot ; eslint sans erreur (2 avertissements existants de `CompanyConfig`). Traductions FR/EN/DE : 43 clés dans chaque langue, relues dans l'écran.
- **Recette navigateur, vrais comptes** (environnement local isolé) :
  - **configurator** (`demo.configurator@links-local.invalid`, rôle entreprise existant « Local Demo Company Role » : create/update/delete/view) : création du modèle 30/20/50 via l'écran sans projet sélectionné, total 100 ✓ ; modification (réordonnancement) enregistrée et relue ; doublon avec casse/espaces différents : 400 ; total 90 : 400 ; un `company` fourni par le client est ignoré ;
  - **reader** (`demo.reader@links-local.invalid`, rôle existant « Test: Read Only », `view` seul) : liste visible, bandeau « Lecture seule », pas de « Nouveau modèle » ni de suppression, éditeur en lecture seule sans « Enregistrer » ; API : liste 200, création/modification/suppression **403** ;
  - **autre entreprise** (`leave.other.company@links-local.invalid`, rôle « Test: Other Company Manager » avec create/update/delete/view) : liste vide ; lecture, modification et suppression des modèles de l'entreprise 1 : **404** ; lookup avec un projet de l'entreprise 1 : 404 ; le même titre se crée dans sa propre entreprise (201, supprimé ensuite). Vérification par API : ce compte de test est redirigé par l'application vers `/invalid/licenses` (licence), indépendamment de ce lot ;
  - **utilisation dans deux projets** de l'entreprise (projets 19 et 28) : le même modèle est trouvé depuis les deux projets, appliqué à une action mère de chacun, progression 0 → 10 → 30 dans chacun, copies indépendantes ; un modèle d'une autre entreprise est refusé (« This template belongs to another company »). Comme aucun écran/endpoint d'application n'existe dans ce lot, l'application a été exécutée par le service dans la base locale (données supprimées ensuite) ;
  - **compte habituel** (`demo.user`, rôle « Local Demo Full Access ») : bouton « Modèles d'actions » visible dans Configuration entreprise ; rôle inchangé.
- Note : une notification « permission refusée » peut apparaître pendant les tests de recette ; elle vient de l'appel existant `ecpmpconfig` du projet précédemment actif, sans lien avec les modèles.
- Données de recette supprimées ; il reste un modèle « Confinement salle A » dans l'entreprise 1 locale (30/20/50, ordre modifié) pour la recette de Sébastien.
- Comptes/outils locaux hors dépôts : `local-dev-auth/seed_mod01_configurator.py`, `seed_mod01_reader.py`, acteurs `configurator`, `reader`, `othercompany` dans le login helper.

## 8. Lignée de migrations de livraison (vérifiée avant de la modifier, rien modifié)

- Base `origin/deliver/backend-integration-20261027` : `project_management` à **0230**. Branche déjà poussée `deliver/prosevi-backend-20261105` : `0231_intervention_requests` (sans MOD-01). Branches `origin/release/ged-2026.11.04-*` : `0231_document_logical_withdrawal` à `0234`.
- Conflit existant : deux `0231` sur la base 0230. Lignée locale cohérente : `0235_intervention_requests`, `0236_action_templates_progress`, `0237_action_templates_company_scope`. Pour une livraison MOD-01, `0236` et `0237` devront être livrées **ensemble** (ou fusionnées en une seule migration de création au niveau entreprise **avant** toute application sur un environnement partagé), après la migration PROSEVIEW retenue. Aucune renumérotation anticipée ; aucune migration déjà appliquée sur un environnement partagé n'est touchée ; l'état réel de ces environnements n'est pas vérifiable depuis cette session.

## 9. Procédure de recette utilisateur (Sébastien)

Prérequis : `docker exec backend_server python manage.py migrate project_management 0237` ; frontend lancé avec l'environnement du login helper ; helper redémarré (acteurs `configurator`, `reader`, `othercompany`).

```bash
docker exec backend_server python manage.py test project_management.test_action_templates --noinput
npm run test:action-templates
```

1. **configurator** : `http://localhost:3001/login?as=configurator&returnTo=/company-config` → bouton **Modèles d'actions** (Configuration entreprise). Créer « Confinement salle B » 10/30, 10/20, 10/50 : total 100 ✓, Enregistrer actif ; mettre 40 : total 90, Enregistrer désactivé. Aucun projet n'a besoin d'être sélectionné.
2. Modifier (crayon), monter la 3ᵉ sous-action, enregistrer, rouvrir : l'ordre est conservé. Créer « confinement   SALLE B » : refus « existe déjà dans l'entreprise ».
3. **reader** : `…/login?as=reader&returnTo=/company-config/action-templates` : bandeau « Lecture seule », pas de bouton d'écriture, éditeur en lecture seule.
4. **othercompany** (API) : liste vide, `GET/PATCH/DELETE action-templates/<id d'un modèle de l'entreprise 1>` → 404.
5. Deux projets : appliquer le même modèle à une action mère du projet 19 et du projet 28 (service `apply_template`, pas d'écran dans ce lot), passer les sous-actions en « in progress »/« closed » : 0, 10, 30… dans chaque projet, indépendamment.
6. Vérifier que les KPI projet, l'import et la liste des demandes sont inchangés.
7. Traductions : changer la langue FR / EN / DE et relire l'écran.

## 10. Hors périmètre (non commencés)

Appel de `find_template` / `apply_template` depuis l'import, aperçu éditable, BPU par ligne, bouton, liste (barre de progression, budget masqué), rattachement rétroactif, nettoyage, mapping de statuts supplémentaires, traitement ponctuel sans modèle, écran de résolution guidée des titres ambigus.
