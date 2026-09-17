from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("project_management", "0187_merge_contact_expertise_and_company_root")]
    operations = [migrations.AddField(
        model_name="projectworkstationnode", name="approval_status",
        field=models.CharField(max_length=24, default="open", choices=[
            ("open", "Open"), ("under_discussion", "Under discussion"), ("agreed", "Agreed"),
        ]),
    )]
