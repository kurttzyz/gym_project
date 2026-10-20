# Two-gym example through the real UI

No demo command or hosts-file edit is needed for normal local testing. Use modern
Chrome/Edge and the running Powerhouse platform on your PC.

1. Sign in at http://127.0.0.1:8000/ with your existing platform superuser.
2. Select Add another gym and create:

   | Field | IronFit | Titans |
   |---|---|---|
   | Name | IronFit Gym | Titans Fitness |
   | Internal identifier | ironfit | titans |
   | Owner username | ironfit_owner | titans_owner |
   | Accent color | #28734e | #7145a3 |

   Enter an owner email and a strong initial password for each. Leave the custom
   domain blank for local testing. Powerhouse automatically provisions each gym's
   database, owner membership, default website, and local .localhost portal.
3. On each gym's platform detail page, click Owner login. The actual URL is shown
   there, normally `http://ironfit.localhost:8000/accounts/login/` and
   `http://titans.localhost:8000/accounts/login/` for the names above.
4. Sign in as the respective owner. You land on `/manage/` on that gym's domain.
5. Edit IronFit through Customize website, save, and open both public websites.
   IronFit changes; Titans retains its own content and database.
6. IronFit's owner cannot sign in to Titans without a separate active membership.
   Public websites are intentionally visible to everyone; management pages are private.

If you already created these gyms, open each detail page and select Enable local
portal instead of recreating the gym or owner. If setup could not finish, use Retry
setup on that same page. These actions preserve passwords and existing edits.

The legacy `setup_demo_gyms` management command remains an optional developer tool,
with separate demo-* identities. It is not needed for this UI workflow. For real
public domains, use Register domain and Verify domain on the gym detail page and
configure DNS/HTTPS through your provider; see custom-domains.md.
