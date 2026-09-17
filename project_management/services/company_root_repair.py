"""Transactional company document-root repair without storage side effects."""
from django.db import connection, transaction
from django.db.models import Count
from project_management.models import Document, Folder


def repair_company_roots(company_ids=None, apply=False):
    report = []
    with transaction.atomic():
        # Serialize tree edits with ordinary application INSERT/UPDATE/DELETE.
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('LOCK TABLE project_management_folder, project_management_document IN SHARE ROW EXCLUSIVE MODE')
        roots = Folder.objects.filter(parent=None, company_profile__isnull=False,
                                      project__isnull=True, freelancer__isnull=True)
        if company_ids is not None:
            roots = roots.filter(company_profile_id__in=company_ids)
        groups = list(roots.values('company_profile_id', 'name').annotate(n=Count('id')).filter(n__gt=1))
        for group in groups:
            company_id = group['company_profile_id']
            if group['name'] != f'{company_id}_company_root_folder':
                continue
            candidates = list(roots.filter(company_profile_id=company_id, name=group['name']).order_by('-id'))
            target = candidates[0]
            tree_ids = [f.pk for f in candidates]
            frontier = tree_ids[:]
            while frontier:
                frontier = list(Folder.objects.filter(parent_id__in=frontier).values_list('pk', flat=True))
                tree_ids.extend(frontier)
            before = dict(Document.objects.filter(folder_id__in=tree_ids).values_list('pk', 'file'))
            entry = {'company_id': company_id, 'keep_root': target.pk, 'merge': [], 'move': [], 'documents': len(before)}

            def merge(source, destination):
                if (source.company_profile_id, source.project_id, source.freelancer_id) != (destination.company_profile_id, destination.project_id, destination.freelancer_id):
                    raise ValueError('Folder owner mismatch')
                if source.system_key and destination.system_key and source.system_key != destination.system_key:
                    raise ValueError('Conflicting system folder keys')
                if source.system_key and not destination.system_key:
                    Folder.objects.filter(pk=destination.pk).update(system_key=source.system_key)
                    destination.system_key = source.system_key
                for child in list(Folder.objects.filter(parent=source)):
                    match = Folder.objects.filter(parent=destination, name=child.name).first()
                    if match:
                        merge(child, match)
                    else:
                        Folder.objects.filter(pk=child.pk).update(parent=destination)
                        entry['move'].append({'folder': child.pk, 'parent': destination.pk})
                Document.objects.filter(folder=source).update(folder=destination)
                for relation in Folder._meta.related_objects:
                    if relation.many_to_many:
                        field = relation.field
                        through = field.remote_field.through
                        folder_field = field.m2m_reverse_field_name()
                        owner_field = field.m2m_field_name()
                        if not through._meta.auto_created:
                            raise ValueError('Custom folder relationship requires explicit repair')
                        for link in list(through.objects.filter(**{folder_field: source})):
                            through.objects.get_or_create(**{
                                folder_field: destination,
                                owner_field + '_id': getattr(link, owner_field + '_id'),
                            })
                            through.objects.filter(pk=link.pk)._raw_delete(through.objects.db)
                    elif relation.related_model not in (Folder, Document):
                        raise ValueError('Unknown folder relationship requires explicit repair')
                # Preserve generic references such as existing notifications.
                from django.contrib.contenttypes.fields import GenericForeignKey
                from django.contrib.contenttypes.models import ContentType
                from django.apps import apps
                ct = ContentType.objects.get_for_model(Folder)
                for model in apps.get_models():
                    for field in model._meta.private_fields:
                        if isinstance(field, GenericForeignKey):
                            model.objects.filter(**{field.ct_field: ct, field.fk_field: source.pk}).update(**{field.fk_field: destination.pk})
                if Folder.objects.filter(parent=source).exists() or Document.objects.filter(folder=source).exists():
                    raise ValueError('Refusing to delete non-empty folder')
                Folder.objects.filter(pk=source.pk)._raw_delete(Folder.objects.db)
                entry['merge'].append({'from': source.pk, 'to': destination.pk})

            for duplicate in candidates[1:]:
                merge(duplicate, target)
            after = dict(Document.objects.filter(pk__in=before).values_list('pk', 'file'))
            if before != after:
                raise ValueError('Document IDs or file paths changed')
            if roots.filter(company_profile_id=company_id, name=group['name']).count() != 1:
                raise ValueError('Root repair invariant failed')
            report.append(entry)
        if not apply:
            transaction.set_rollback(True)
    return report
