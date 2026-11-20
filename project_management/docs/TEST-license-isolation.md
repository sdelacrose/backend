# Tests : erreurs `package_license_license` (feuilles de temps) - diagnostic et correction

## Cause racine
Les tables `package_license_*` appartiennent au **service admin** (`admin-server-backend`, app `package_license`, ses propres migrations). Ce backend n'a **ni modèle ni migration** pour elles (`INSTALLED_APPS` ne les contient pas) et les lit en SQL brut (`ecpmpapp/license_queries.py`, `ecpmpapp/utils.get_user_active_solution`, `ecpmpapp/permissions.LicensePermission`). Une base de test fraîche ne les contient donc pas.

Chaîne d'appel démontrée (trace des appels SQL) :
- `TimesheetWeekSaveSerializerTests.setUp` : `Milestone.save` -> `notify_on_milestone_save` -> `create_notification_with_recipients` -> `should_notify_user_comprehensive` -> `get_user_active_solution` -> requête licence ;
- `MonthlyTimesheetBillingIntegrationTests.setUp` : `ContactCompany` -> `notify_on_contact_company_save` -> même chaîne.

`should_notify_user_comprehensive` capte l'exception (affiche « Error in ... ») mais la requête échouée **avorte la transaction** du test : tout `setUp` casse ensuite (« transaction is aborted »). Ce n'étaient pas 3 tests mais **les 55 tests des deux classes** (33 + 22) ; le filtre `-k action_item` n'en montrait que 3.

## Choix de correction
Les notifications (donc la licence) sont **hors périmètre** de ces tests. Convention existante du dépôt (déjà utilisée dans `tests.py`, `chat`, `ai_tools`, `project_content`, etc.) : isoler explicitement `ecpmpapp.utils.should_notify_user_comprehensive`. Appliqué **uniquement** aux `setUp` des deux classes (`patch` + `addCleanup`). Écartés (interdits par le brief) : table factice, masquage de l'erreur, désactivation du contrôle de licence, `skip`, modification de la base de développement. Importer l'app `package_license` du service admin dans ce backend n'est pas conforme à l'architecture (deux services).

Le comportement réel du contrôle de licence est vérifié **séparément**, sans table : `ecpmpapp/test_license_control.py` (7 tests) + `userapis.tests.ActiveLicenseQueryTests` (2 tests existants) : le SQL de licence est exécuté, son résultat décide (solution active, accès accordé/refusé, compte inactif/supprimé refusé en 410 avant toute requête, erreur SQL non avalée par la recherche elle-même).

## Défaut masqué découvert (même `setUp`, classe Monthly)
Une fois la licence isolée, `MonthlyTimesheetBillingIntegrationTests.setUp` échouait sur `ChatRoom.DoesNotExist` : `notify_on_project_save` (déconnecté par ce `setUp`) crée la salle de chat attendue par `create_related_objects` à l'ajout d'un membre. Correctif : déconnecter aussi `create_related_objects` (+ reconnexion en cleanup), comme les autres fixtures du même module (lignes ~410 et ~10580).

## Fichiers modifiés
- `project_management/tests.py` : `setUp` de `TimesheetWeekSaveSerializerTests` (patch notifications) et de `MonthlyTimesheetBillingIntegrationTests` (patch notifications + déconnexion de `create_related_objects`).
- `ecpmpapp/test_license_control.py` (nouveau).
Aucun code applicatif modifié, aucune migration, aucune donnée.

## Résultats (bases de test fraîches et jetables, supprimées ensuite)
- Les 3 tests rejoués séparément (une base fraîche chacun) : OK, OK, OK (le 3e après le second correctif).
- Classes `TimesheetWeekSaveSerializerTests` + `MonthlyTimesheetBillingIntegrationTests` : 55 tests, **54 OK, 1 erreur indépendante** (voir ci-dessous). Avant : 55 erreurs.
- Suite élargie Action Items (`-k action_item -k ActionItem`) : **122 / 122 OK**.
- PROSEVIEW (`test_intervention_plans_b`, `test_intervention_plans`, `test_intervention_import`, `test_action_templates`) + licences (`test_license_control`, `ActiveLicenseQueryTests`) : **178 / 178 OK**.
- `makemigrations --check` : aucune dérive.

## Dernière erreur : fixture du test PDF (corrigée, lot de finalisation)
`MonthlyTimesheetBillingIntegrationTests.test_invoice_pdf_line_vat_matches_invoice_vat_rate_not_stale_amount` : `ValueError: Company level root folder for bill ID ... not found.` Le test créait un dossier racine nommé `<id>_company_root_folder_vat` alors que `get_invoices_folder_from_bill` (`project_management/utils.py:153`) cherche `<id>_company_root_folder`.

**Contrat de nommage confirmé** : le nom `<id>_company_root_folder` est utilisé par le code applicatif (`utils.py`, `models/document.py`, `permissions.py`, `views/document.py`, `views/related_objects_management.py`, `views/sharepoint.py`, commandes de gestion) et par les autres fixtures de tests, dont celle du test voisin (`tests.py` ~ligne 7765). Seule cette fixture portait le suffixe `_vat`. Défaut de données du test, sans lien avec les licences (masqué jusqu'ici, la classe ne passait pas le `setUp`).

**Correction** : `project_management/tests.py`, une seule ligne (nom du dossier racine de la fixture). Assertions TVA et code applicatif inchangés.

## Résultats du lot de finalisation (bases de test fraîches et jetables)
- Test PDF seul : **1 / 1 OK**.
- Deux classes `TimesheetWeekSaveSerializerTests` + `MonthlyTimesheetBillingIntegrationTests` (+ `test_license_control` 7 + `ActiveLicenseQueryTests` 2) : **64 tests, 64 OK** (soit **55 / 55** pour les deux classes).
- PROSEVIEW (`test_intervention_plans_b`, `test_intervention_plans`, `test_intervention_import`, `test_action_templates`) + licences : **178 / 178 OK**.
- `makemigrations --check --dry-run` : « No changes detected ».
- Suite élargie Action Items (`manage.py test project_management -k action_item -k ActionItem`) : **122 / 122 OK**.
  Note : le même filtre lancé sans le module `project_management` découvre aussi `health_check` : 160 tests, dont 1 erreur de chargement préexistante et sans lien (`health_check.test_dynamic_ids` : `ImportError: cannot import name 'DynamicIDManager'`). Non traitée (hors périmètre) ; ce n'est pas la suite de référence des 122 tests.

## Non exécuté / limites
- **Recette navigateur non exécutée** : ce lot ne modifie que des fichiers de tests (fixtures et `setUp`) ; aucun code applicatif, aucune interface, aucune donnée ne change, il n'y a donc rien d'observable dans le navigateur.
- Pas de recette locale « création de jalon / feuilles de temps » pour la même raison.
- Seuls les modules cités ont été rejoués ; la suite complète du backend n'a pas été lancée.
- Le 3e test a été rejoué sur une base de test fraîche créée avec `--keepdb` (puis réutilisée pour les lots suivants), supprimée à la fin.

## Procédure de validation (Sébastien)
```
docker exec -e DB_NAME=lic_val backend_server python manage.py test \
  project_management.tests.TimesheetWeekSaveSerializerTests \
  project_management.tests.MonthlyTimesheetBillingIntegrationTests \
  ecpmpapp.test_license_control userapis.tests.ActiveLicenseQueryTests --noinput
```
Attendu : 64 tests, tous OK (dont les 55 des deux classes). La base `test_lic_val` est créée puis supprimée automatiquement.
