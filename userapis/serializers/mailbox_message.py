"""MAIL-1.3D-B: what the review API shows of a stored mailbox message -- only what reviewing needs.

Never: a Graph id, an Internet/conversation id, a body, an attachment (only the fact that one
exists), a token, a cursor. The link to a LINKS correspondence is shown as its bare id and a flag:
the correspondence's own content stays behind the correspondence permissions (holding a mailbox
right never opens a correspondence, and the reverse).
"""

from rest_framework import serializers

from userapis.models import MailboxMessage


class MailboxMessageReviewSerializer(serializers.ModelSerializer):
    mailbox = serializers.SerializerMethodField()
    is_linked = serializers.SerializerMethodField()

    class Meta:
        model = MailboxMessage
        fields = (
            "id", "mailbox", "folder", "sender_address", "sender_name", "to_addresses",
            "cc_addresses", "subject", "received_at", "has_attachments", "state",
            "is_linked", "correspondence_id", "created_at",
        )
        read_only_fields = fields

    def get_mailbox(self, obj):
        mailbox = obj.mailbox
        return {
            "id": mailbox.pk,
            "email_address": mailbox.email_address,
            "display_name": mailbox.display_name,
            "connection_type": mailbox.connection_type,
        }

    def get_is_linked(self, obj):
        return obj.correspondence_id is not None
