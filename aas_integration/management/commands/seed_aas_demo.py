from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from models.models import (
    CompanyConfig,
    CompanyProfile,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanySolutionSpaceItem,
    PartsListConfig,
    Project,
    ProjectSolutionSpaceItem,
)

from aas_integration.models import AASResourceIdentity, ProductAASProfile
from aas_integration.services import get_or_create_product_identity, get_or_create_project_identity


class Command(BaseCommand):
    help = "Create deterministic local AAS and Festo handover fixtures."

    def add_arguments(self, parser):
        parser.add_argument("--performance", action="store_true", help="Add a 1,000-row BOM.")
        parser.add_argument("--stress", action="store_true", help="Add a 10,000-row BOM.")

    def handle(self, *args, **options):
        user, _ = User.objects.get_or_create(
            email="aas-demo@sharemundo.local",
            defaults={"first_name": "AAS", "last_name": "Demo", "is_active": True, "user_type": "company_user"},
        )
        company, _ = CompanyProfile.objects.get_or_create(
            sharemundo_links_id="aas-demo-company",
            defaults={
                "company_name": "AAS Demo Manufacturing",
                "company_address": "Demo Street 1",
                "post_code": "10000",
                "company_town": "Demo City",
                "company_country": "Germany",
                "company_tell_no": "-",
                "company_mob_no": "-",
                "company_web_address": "https://example.invalid",
                "company_email_address": "aas-demo@sharemundo.local",
                "company_tax_reference": "DEMO",
                "company_iban": "DEMO",
                "admin_user": user,
            },
        )
        if user.company_id != company.id:
            user.company = company
            user.save(update_fields=["company"])
        CompanyConfig.objects.get_or_create(
            company_profile=company,
            solution="Energy Consultant Solution",
        )
        self._ensure_demo_license(user, company)
        self._ensure_demo_permissions(user, company)

        products = [
            ("Festo Servo Drive CMMT", "8084001", "Festo", "https://supplier.example/aas/festo-cmmt"),
            ("SICK Safety Scanner microScan3", "1100333", "SICK", "https://supplier.example/aas/sick-microscan3"),
            ("WAGO I/O System 750", "750-8212", "WAGO", "https://supplier.example/aas/wago-750"),
        ]
        product_rows = []
        for index, (name, number, supplier, supplier_aas_id) in enumerate(products, start=1):
            product_fields = {
                "supplier": supplier,
                "supplier_aas_id": supplier_aas_id,
                "rated_voltage": "24 V DC" if supplier != "Festo" else "400 V AC",
                "protection_class": "IP65",
                "documentation_reference": f"https://example.invalid/documents/{number}",
            }
            if supplier == "Festo":
                product_fields.update(
                    {
                        "name_de": "Festo Servoantrieb CMMT",
                        "name_fr": "Servovariateur Festo CMMT",
                        "rated_voltage_manual_override": True,
                    }
                )
            product, _ = AvailableProductPart.objects.update_or_create(
                company_profile=company,
                part_number=number,
                defaults={
                    "name": name,
                    "sort_order": index,
                    "level": 1,
                    "custom_fields": product_fields,
                },
            )
            product_rows.append(product)
            ProductAASProfile.objects.update_or_create(
                product=product,
                defaults={
                    "origin": ProductAASProfile.Origin.PURCHASED,
                    "manufacturer_name": supplier,
                    "supplier_name": supplier,
                    "supplier_aas_id": supplier_aas_id,
                    "global_asset_id": supplier_aas_id,
                    "order_code": number if supplier == "Festo" else "",
                },
            )
            get_or_create_product_identity(product)

        project, _ = Project.objects.get_or_create(
            project_name="AAS Demo Electric Motor Assembly",
            company_profile=company,
            defaults={
                "project_description": "Three-level mixed-supplier assembly for AAS verification.",
                "contract_value": Decimal("100000.00"),
                "project_start_date": date(2026, 1, 1),
                "project_end_date": date(2026, 12, 31),
                "user": user,
                "solution": "Energy Consultant Solution",
            },
        )
        PartsListConfig.objects.update_or_create(
            project=project,
            defaults={
                "company_profile": company,
                "bom_columns": [
                    {"key": "supplier", "label": "Supplier", "idta_semantic_id": "https://admin-shell.io/idta/Nameplate/3/0/ManufacturerName"},
                    {"key": "quantity", "label": "Quantity", "idta_semantic_id": "https://admin-shell.io/idta/HierarchicalStructures/1/1/Quantity"},
                    {"key": "rated_voltage", "label": "Rated voltage", "idta_semantic_id": "https://example.com/semantic/rated-voltage"},
                ],
            },
        )
        PartsListConfig.objects.update_or_create(
            company_profile=company,
            project=None,
            defaults={
                "available_product_columns": [
                    {"key": "rated_voltage", "label": "Rated voltage", "idta_semantic_id": "https://example.com/semantic/rated-voltage"},
                    {"key": "protection_class", "label": "Protection class", "idta_semantic_id": "https://example.com/semantic/protection-class"},
                    {"key": "documentation_reference", "label": "Documentation", "idta_semantic_id": "https://example.com/semantic/documentation-reference"},
                ],
            },
        )
        BillOfMaterialPart.objects.filter(project=project).delete()
        root = BillOfMaterialPart.objects.create(project=project, name="Electric Motor Assembly Line", part_number="AML-500", level=1, sort_order=1, custom_fields={"supplier": "ShareMundo", "quantity": 1})
        station = BillOfMaterialPart.objects.create(project=project, parent=root, name="Drive and Control Station", part_number="STA-100", level=2, sort_order=1, custom_fields={"supplier": "ShareMundo", "quantity": 1})
        for index, product in enumerate(product_rows, start=1):
            BillOfMaterialPart.objects.create(
                project=project,
                parent=station,
                available_product=product,
                name=product.name,
                part_number=product.part_number,
                level=3,
                sort_order=index,
                custom_fields={"supplier": product.custom_fields["supplier"], "quantity": index, "supplier_aas_id": product.custom_fields["supplier_aas_id"]},
            )
            company_assembly, _ = CompanySolutionSpaceItem.objects.update_or_create(
                company_profile=company,
                aas_product=product,
                defaults={
                    "assembly_name": product.name,
                    "assembly_type": CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
                    "sort_order": index,
                    "level": 1,
                },
            )
            ProjectSolutionSpaceItem.objects.update_or_create(
                project=project,
                source_company_item=company_assembly,
                defaults={
                    "assembly_name": product.name,
                    "assembly_type": company_assembly.assembly_type,
                    "sort_order": index,
                    "level": 1,
                },
            )
        get_or_create_project_identity(project)
        self._edge_case_projects(company, user)
        if options["performance"]:
            self._large_project(company, user, 1000, "AAS Demo 1K BOM")
        if options["stress"]:
            self._large_project(company, user, 10000, "AAS Demo 10K BOM")
        self.stdout.write(self.style.SUCCESS(f"Seeded AAS demo project {project.id} with {len(product_rows) + 2} BOM rows."))

    def _ensure_demo_license(self, user, company):
        required_tables = {
            "package_license_product",
            "package_license_package",
            "package_license_license",
        }
        if not required_tables.issubset(set(connection.introspection.table_names())):
            return
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM package_license_product WHERE name = %s LIMIT 1",
                ["Energy Consultant Solution"],
            )
            product = cursor.fetchone()
            if not product:
                cursor.execute(
                    """
                    INSERT INTO package_license_product
                        (name, description, price, status)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    [
                        "Energy Consultant Solution",
                        "Local AAS demo fixture",
                        0,
                        "Active",
                    ],
                )
                product = cursor.fetchone()
            cursor.execute(
                """
                SELECT id FROM package_license_package
                WHERE company_profile_id = %s AND is_active = TRUE AND expires_at > %s
                ORDER BY expires_at DESC LIMIT 1
                """,
                [company.id, now],
            )
            package = cursor.fetchone()
            if not package:
                cursor.execute(
                    """
                    INSERT INTO package_license_package
                        (is_active, created_at, updated_at, company_profile_id, expires_at,
                         status, is_freetrial, billing_plan)
                    VALUES (TRUE, %s, %s, %s, %s, %s, TRUE, %s)
                    RETURNING id
                    """,
                    [now, now, company.id, now + timedelta(days=365), "Free Trial", "yearly"],
                )
                package = cursor.fetchone()
            cursor.execute(
                """
                SELECT id FROM package_license_license
                WHERE package_id = %s AND product_id = %s AND user_id = %s
                LIMIT 1
                """,
                [package[0], product[0], user.id],
            )
            if not cursor.fetchone():
                cursor.execute(
                    """
                    INSERT INTO package_license_license
                        (is_active, created_at, updated_at, package_id, product_id,
                         license_assigned_at, user_id)
                    VALUES (TRUE, %s, %s, %s, %s, %s, %s)
                    """,
                    [now, now, package[0], product[0], now, user.id],
                )

    def _ensure_demo_permissions(self, user, company):
        role, _ = Role.objects.get_or_create(
            name="AAS Demo Company Administrator",
            company=company,
            type="solution",
            scope="company",
            defaults={
                "description": "Local AAS verification role.",
                "is_default": False,
                "created_by": user,
            },
        )
        permission_definitions = (
            ("projects.full_access", "Projects full access", "projects"),
            ("available_product_parts.full_access", "Available products full access", "available_product_parts"),
            ("bill_of_material.full_access", "Bill of material full access", "bill_of_material"),
            ("aas_models.view", "AAS models view", "aas_models"),
            ("aas_models.full_access", "AAS models full access", "aas_models"),
        )
        permissions = [
            Permission.objects.get_or_create(
                key=key,
                defaults={"name": name, "module": module},
            )[0]
            for key, name, module in permission_definitions
        ]
        RolePermission.objects.bulk_create(
            [RolePermission(role=role, permission=permission) for permission in permissions],
            ignore_conflicts=True,
        )
        UserRole.objects.get_or_create(
            user=user,
            role=role,
            defaults={"company": company, "assigned_by": user},
        )

    def _edge_case_projects(self, company, user):
        fixtures = (
            ("AAS Demo Empty BOM", []),
            ("AAS Demo Single BOM", [("Standalone motor", "SINGLE-001", "Festo")]),
            (
                "AAS Demo Duplicate Part Numbers",
                [
                    ("Festo connector", "DUPLICATE-001", "Festo"),
                    ("WAGO connector", "DUPLICATE-001", "WAGO"),
                ],
            ),
            (
                "AAS Demo Missing Semantics",
                [("Unmapped sensor", "WARN-001", "SICK")],
            ),
        )
        for project_name, rows in fixtures:
            project, _ = Project.objects.get_or_create(
                project_name=project_name,
                company_profile=company,
                defaults={
                    "contract_value": Decimal("1.00"),
                    "project_start_date": date(2026, 1, 1),
                    "project_end_date": date(2026, 12, 31),
                    "user": user,
                    "solution": "Energy Consultant Solution",
                },
            )
            BillOfMaterialPart.objects.filter(project=project).delete()
            for index, (name, number, supplier) in enumerate(rows, start=1):
                custom_fields = {"supplier": supplier, "quantity": 1}
                if project_name == "AAS Demo Missing Semantics":
                    custom_fields["unmapped_supplier_property"] = "Requires semantic mapping"
                BillOfMaterialPart.objects.create(
                    project=project,
                    name=name,
                    part_number=number,
                    level=1,
                    sort_order=index,
                    custom_fields=custom_fields,
                )
            get_or_create_project_identity(project)

    def _large_project(self, company, user, count, name):
        project, _ = Project.objects.get_or_create(
            project_name=name,
            company_profile=company,
            defaults={
                "contract_value": Decimal("1.00"),
                "project_start_date": date(2026, 1, 1),
                "project_end_date": date(2026, 12, 31),
                "user": user,
                "solution": "Energy Consultant Solution",
            },
        )
        BillOfMaterialPart.objects.filter(project=project).delete()
        BillOfMaterialPart.objects.bulk_create(
            [
                BillOfMaterialPart(project=project, name=f"Part {index:05d}", part_number=f"DEMO-{index:05d}", level=1, sort_order=index, custom_fields={"quantity": 1})
                for index in range(1, count + 1)
            ],
            batch_size=1000,
        )
        get_or_create_project_identity(project)
