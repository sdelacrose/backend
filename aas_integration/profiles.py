from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AASFieldDefinition:
    key: str
    label: str
    submodel: str
    data_type: str = "string"
    semantic_id: str = ""
    required: bool = False
    source: str = "standard_fields"
    system: bool = True
    ownership_rule: str = "manufactured_editable"


STANDARD_PRODUCT_AAS_FIELDS = (
    AASFieldDefinition(
        key="ProductName",
        label="Product Name",
        submodel="Nameplate",
        required=True,
        source="product.name",
    ),
    AASFieldDefinition(
        key="ArticleNumber",
        label="Article Number",
        submodel="Nameplate",
        required=True,
        source="product.part_number",
    ),
    AASFieldDefinition(
        key="ManufacturerName",
        label="Manufacturer Name",
        submodel="Nameplate",
        semantic_id="https://admin-shell.io/idta/Nameplate/3/0/ManufacturerName",
        source="profile.manufacturer_name",
    ),
)


LINKS_ONLY_PRODUCT_COLUMN_KEYS = frozenset({"description", "quantity"})


def is_product_aas_column(column: dict) -> bool:
    key = str(column.get("key") or column.get("id") or "").strip()
    ownership = str(column.get("aas_ownership") or "").strip().lower()
    if ownership:
        return ownership == "aas"
    return key not in LINKS_ONLY_PRODUCT_COLUMN_KEYS


def product_aas_field_contract() -> list[dict]:
    return [asdict(field) for field in STANDARD_PRODUCT_AAS_FIELDS]
