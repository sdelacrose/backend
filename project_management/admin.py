from django.contrib import admin

from project_management.models import Project, ECPMPConfig, Contact, Folder, \
     Document, Milestone, ProjectTime, ProjectWork, GeneralExpense, GeneralWork, Billing, \
     AzureToken, OAuthState, ActionItem, ActionItemComment, ActionItemCommentReaction, ProjectTeamMembers, ProjectExpense, \
     ProjectExternalUsers, SiteReport, Observation, TimeCard, ExpenseCard, InvoiceLineItem, Invoice, PurchaseOrder, ContactUser, ContactCompany, \
     ProjectMatrixConfig, SolutionDemand, Requirement, MatrixComment, MatrixCommentReaction, EquipmentConfig, Equipment, PartPurchaseIntent

admin.site.register(Project)
admin.site.register(ECPMPConfig)
admin.site.register(Contact)
admin.site.register(ContactUser)
admin.site.register(ContactCompany)
admin.site.register(Folder)
admin.site.register(Document)
admin.site.register(Milestone)
admin.site.register(ProjectTime)
admin.site.register(ProjectWork)
admin.site.register(ProjectExpense)
admin.site.register(GeneralExpense)
admin.site.register(GeneralWork)
admin.site.register(Billing)
admin.site.register(AzureToken)
admin.site.register(OAuthState)
admin.site.register(ActionItem)
admin.site.register(ActionItemComment)
admin.site.register(ActionItemCommentReaction)
admin.site.register(ProjectTeamMembers)
admin.site.register(ProjectExternalUsers)
admin.site.register(SiteReport)
admin.site.register(Observation)
admin.site.register(TimeCard)
admin.site.register(ExpenseCard)
admin.site.register(Invoice)
admin.site.register(InvoiceLineItem)
admin.site.register(PurchaseOrder)
admin.site.register(ProjectMatrixConfig)
admin.site.register(SolutionDemand)
admin.site.register(Requirement)
admin.site.register(EquipmentConfig)
admin.site.register(Equipment)
admin.site.register(MatrixComment)
admin.site.register(MatrixCommentReaction)
admin.site.register(PartPurchaseIntent)
