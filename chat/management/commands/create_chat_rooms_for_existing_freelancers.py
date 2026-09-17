from django.core.management.base import BaseCommand

from models.models import User
from chat.models import ChatRoom

class Command(BaseCommand):
    help = 'Creates chatrooms for all existing freelancers'

    def handle(self, *args, **kwargs):
        users = User.objects.all()
        freelancers = [user for user in users if user.is_freelancer]
        for freelancer in freelancers:
            if freelancer.is_freelancer:
                chatroom, created = ChatRoom.objects.get_or_create(
                    freelancer=freelancer,
                    name=freelancer.full_name,
                )
                if created:
                    self.stdout.write(self.style.SUCCESS(f"Created chatroom for {freelancer.full_name}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"Chatroom already exists for {freelancer.full_name}"))
