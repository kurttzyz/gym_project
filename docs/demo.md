# Two-gym local demo (Windows)

This example demonstrates Powerhouse as the technology provider, with IronFit
and Titans as separate customer gyms. It does not configure public DNS or use a
real payment provider. PostgreSQL and your existing platform superuser are required.

Stop Django with Ctrl+C, then from your activated PowerShell environment:

```powershell
git pull origin main
python manage.py migrate --noinput
python manage.py migrate_gym_databases
python manage.py setup_demo_gyms
```

The command requires DJANGO_DEBUG=true. It prompts twice for a password of your
choice (input is hidden); both newly created sample owner accounts use that password.
No hardcoded password is included. Existing owner passwords are never reset on rerun.

It creates:

| Gym | Owner username | Local domain |
|---|---|---|
| IronFit Demo | demo_ironfit_owner | demo-ironfit.test |
| Titans Demo | demo_titans_owner | demo-titans.test |

Each gym has its own UUID-based PostgreSQL database/login, website settings,
active owner membership, and locally verified test-domain mapping. No platform
superuser is created. The accounts cannot administer Powerhouse.

Open Notepad as administrator and edit:

```text
C:\Windows\System32\drivers\etc\hosts
```

Add and save this line without changing the file's extension:

```text
127.0.0.1 demo-ironfit.test demo-titans.test
```

Start Django:

```powershell
python manage.py runserver
```

## Try the two portals

1. Open http://demo-ironfit.test:8000/accounts/login/ and sign in as
   `demo_ironfit_owner` with the password you selected.
2. You land at http://demo-ironfit.test:8000/manage/. Select Customize website.
3. Change IronFit's heading or color, save, and open http://demo-ironfit.test:8000/.
4. Open http://demo-titans.test:8000/ and observe it retains its own branding/content.
5. Sign in at http://demo-titans.test:8000/accounts/login/ as `demo_titans_owner`
   using the selected demo password. Its dashboard/editor belong only to Titans.
6. IronFit credentials are rejected on Titans' login because that owner has no
   active Titans membership. Public websites are intentionally readable by anyone.

Your existing platform administrator still uses http://127.0.0.1:8000/. Both gym
registrations appear there; their owners do not get platform administration rights.

Rerunning setup_demo_gyms migrates existing demo databases and preserves website
edits and owner passwords. It refuses conflicting reserved demo identities/domains
and will not re-enable disabled accounts or domains. Provisioning is retryable if
a database step fails; central records remain. This command only seeds local sample
gyms and is disabled when DEBUG is false. Production tenants use normal onboarding
with individual passwords, verified real domains, and HTTPS.
