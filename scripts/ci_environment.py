"""Synthetic values for the disposable consolidation CI job only; no real credentials."""
import base64
values = {
 'DJANGO_SETTINGS_MODULE':'ecpmpapp.settings.recette','CELERY_ENV':'recette','ENV':'local',
 'DB_NAME':'links_recette_ci','DB_USER':'links_ci','DB_PASSWORD':'ci-only-disposable',
 'DB_HOST':'links-recette-db','DB_PORT':'5432','REDIS_HOST':'links-recette-redis',
 'RECETTE_SECRET_KEY':'ci-only-application-key-not-for-live-use-'*2,
 'RECETTE_ALLOWED_HOSTS':'testserver,localhost,127.0.0.1,recette.sharemundolinks.fr',
 'RECETTE_ALLOWED_ORIGINS':'https://recette.sharemundolinks.fr',
 'HTTP_PROXY':'http://links-recette-redis:3128','HTTPS_PROXY':'http://links-recette-redis:3128',
 'NO_PROXY':'links-recette-db,links-recette-redis,localhost,127.0.0.1',
 'HEALTH_CHECK_API_KEY':'ci-only-health-key-not-for-live-use',
 'MS_TOKEN_ENCRYPTION_KEYS':base64.urlsafe_b64encode(b'c'*32).decode(),
 'AUTH0_AUDIENCE':'https://ci-api.invalid','AUTH0_ISSUER_BASE_URL':'https://ci.eu.auth0.com/',
 'AUTH0_DOMAIN':'ci.eu.auth0.com','AUTH0_CLIENT_ID':'ci-only-client','AUTH0_CLIENT_SECRET':'ci-only-no-provider-access',
 'AUTH0_SECRET':'c'*64,'API_SECRET_KEY':'ci-only-api-key-not-for-live-use',
 'MS_REDIRECT_URI':'api/user/auth/outlook/callback','AZURE_REDIRECT_URI':'https://recette.sharemundolinks.fr/api/user/auth/azure/callback',
 'RECETTE_MAIL_SEND_ENABLED':'false','RECETTE_ALLOW_ANY_RECIPIENT':'false',
 'SUPPLIER_PORTAL_ENABLED':'true','SUPPLIER_REGISTER_ENABLED':'true','TRANSMITTAL_PROOF_ENABLED':'true',
 'AWS_EC2_METADATA_DISABLED':'true','PYTHONDONTWRITEBYTECODE':'1',
}
for key in ['BACKEND_URL','FRONTEND_URL','FRONTEND_URL_CPM','FRONTEND_URL_CORE','FRONTEND_URL_ENTERPRISE','FRONTEND_URL_BACK_OFFICE']:
 values[key]='https://recette.sharemundolinks.fr'
values['BACKEND_URL']='https://recette.sharemundolinks.fr/'
for key,value in values.items():print(key+'='+value)
