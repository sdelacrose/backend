from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0080_alter_invoice_source_invoice"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ActionItemComment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("content", models.TextField(blank=True, default="")),
                ("attachments", models.JSONField(blank=True, default=list)),
                ("linked_objects", models.JSONField(blank=True, default=list)),
                ("is_deleted", models.BooleanField(default=False)),
                ("is_edited", models.BooleanField(default=False)),
                ("edited_at", models.DateTimeField(blank=True, null=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "action_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="project_management.actionitem",
                    ),
                ),
                (
                    "deleted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="deleted_action_item_comments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "replied_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="replies",
                        to="project_management.actionitemcomment",
                    ),
                ),
                (
                    "sender",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="action_item_comments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ActionItemCommentReaction",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("key", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "comment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reactions",
                        to="project_management.actionitemcomment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="action_item_comment_reactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "unique_together": {("comment", "user", "key")},
            },
        ),
        migrations.AddIndex(
            model_name="actionitemcomment",
            index=models.Index(
                fields=["action_item", "created_at"],
                name="project_mana_action__38d9c4_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="actionitemcomment",
            index=models.Index(
                fields=["action_item", "id"],
                name="project_mana_action__86cf45_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="actionitemcomment",
            index=models.Index(
                fields=["replied_to", "created_at"],
                name="project_mana_replied_355561_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="actionitemcomment",
            index=models.Index(
                fields=["sender", "created_at"],
                name="project_mana_sender__c25410_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="actionitemcommentreaction",
            index=models.Index(
                fields=["comment", "key"],
                name="project_mana_comment_1a25f5_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="actionitemcommentreaction",
            index=models.Index(
                fields=["user", "created_at"],
                name="project_mana_user_id_8db62b_idx",
            ),
        ),
    ]
