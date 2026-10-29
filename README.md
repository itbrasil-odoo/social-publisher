

# Social Publisher
<!-- /!\ Non OCA Context : Set here the badge of your runbot / runboat instance. -->
[![Pre-commit Status](https://github.com/itbrasil-odoo/social-publisher/actions/workflows/pre-commit.yml/badge.svg?branch=19.0)](https://github.com/itbrasil-odoo/social-publisher/actions/workflows/pre-commit.yml?query=branch%3A19.0)
[![Build Status](https://github.com/itbrasil-odoo/social-publisher/actions/workflows/test.yml/badge.svg?branch=19.0)](https://github.com/itbrasil-odoo/social-publisher/actions/workflows/test.yml?query=branch%3A19.0)
[![codecov](https://codecov.io/gh/itbrasil-odoo/social-publisher/branch/19.0/graph/badge.svg)](https://codecov.io/gh/itbrasil-odoo/social-publisher)
<!-- /!\ Non OCA Context : Set here the badge of your translation instance. -->

<!-- /!\ do not modify above this line -->

Plan, approve, schedule and measure social media posts in Odoo, with a LinkedIn provider for profiles and company pages. Built on Odoo Community (no Enterprise module required) and multi-company.

### Approval

- **Writer** creates and edits drafts but cannot approve or publish: the right
  group for an AI agent connected to Odoo (for example through an MCP server).
- **Manager** approves and publishes. Editing the text, image or account of an
  approved post sends it back to draft.
- A failed publication becomes `failed` and is never retried automatically.

### LinkedIn setup

Create the apps at <https://www.linkedin.com/developers/apps> and add the
redirect URL shown on the account form (`https://<odoo>/social_publisher_linkedin/callback`)
to the **Auth** tab of each app.

| Account type | App(s) | Scopes requested |
|---|---|---|
| Personal profile | *Share on LinkedIn* + *Sign In with OpenID Connect* | `openid profile w_member_social` |
| Personal profile metrics | second app with *Community Management API* | `r_member_postAnalytics` |
| Personal profile, single app | only the *Community Management API* app, when it also has `w_member_social` and `r_basicprofile` | `r_basicprofile w_member_social r_member_postAnalytics` |
| Company page | the *Community Management API* app only | `rw_organization_admin w_organization_social r_organization_social r_organization_followers` |

LinkedIn only grants the Community Management API to an app with no other
product, hence the second app. A personal profile's past posts cannot be
listed through the API (`r_member_social` is restricted); a company page's can
(**Import Posts** on the account).

On the Community Management API **Development tier** LinkedIn allows 500 calls
per app and 100 per member a day, and no BATCH_GET. The module stays within
that: pages are looked up one by one, company page statistics are read in
batches, and the daily sync only refreshes posts published in the account's
metrics window (14 days by default; **Refresh Metrics** on a post updates an
older one). A profile's post costs 10 calls (one per metric), so keep the
window short until the app reaches the Standard tier.

Access tokens last about 60 days and are not refreshed; the responsible user
gets a to-do a week before they expire.

<!-- /!\ do not modify below this line -->

<!-- prettier-ignore-start -->

[//]: # (addons)

Available addons
----------------
addon | version | maintainers | summary
--- | --- | --- | ---
[social_publisher](social_publisher/) | 19.0.1.3.3 |  | Plan, approve, schedule and measure social media posts
[social_publisher_linkedin](social_publisher_linkedin/) | 19.0.1.3.4 |  | Publish and measure LinkedIn posts via the LinkedIn API

[//]: # (end addons)

<!-- prettier-ignore-end -->

## Licenses

This repository is licensed under [LGPL-3.0](LICENSE).

However, each module can have a totally different license, as long as they adhere to IT Brasil
policy. Consult each module's `__manifest__.py` file, which contains a `license` key
that explains its license.

----
This repository is maintained by [IT Brasil](https://github.com/itbrasil-odoo), an Odoo partner since 2018.
