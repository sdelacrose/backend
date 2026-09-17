from django.contrib import admin

from models.models import CompanyProfile, User, CompanyConfig, GlobalConfig
from .models import Opportunity, NoticeBoard

admin.site.register(CompanyProfile)
admin.site.register(User)
admin.site.register(CompanyConfig)
admin.site.register(GlobalConfig)
admin.site.register(Opportunity)
admin.site.register(NoticeBoard)
