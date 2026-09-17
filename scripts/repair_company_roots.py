"""Run through manage.py shell with COMPANY_ROOT_IDS and optional APPLY=1."""
import json
import os
from project_management.services.company_root_repair import repair_company_roots

selected = os.environ.get("COMPANY_ROOT_IDS")
selected = [int(value) for value in selected.split(",")] if selected else None
print(json.dumps({"apply": os.environ.get("APPLY") == "1", "repairs": repair_company_roots(selected, os.environ.get("APPLY") == "1")}))
