To configure this module, you need to:

- Install a provider module, for example *Social Publisher LinkedIn*.
- Go to *Settings > Users & Companies > Users* and give each person a
  *Social Publisher* role:
  - **Writer** creates and edits drafts but cannot approve or publish. This is
    the right role for an assistant or an AI agent that drafts posts.
  - **Manager** approves and publishes posts and manages accounts.
- Go to *Social > Configuration > Accounts*, create an account for each
  profile or page and connect it. Leave the company empty to share the
  account with every company.
- Optionally, create tags in *Social > Configuration > Tags* to classify
  posts by topic.
- Odoo accepts uploads of up to 128 MB by default. To attach larger videos,
  set the system parameter `web.max_file_upload_size` (in bytes) and make sure
  the Odoo workers have enough memory for the upload.
- On each account, *Metrics Window (days)* limits the daily metrics sync to
  recent posts, which keeps the network's daily API limits in check.
