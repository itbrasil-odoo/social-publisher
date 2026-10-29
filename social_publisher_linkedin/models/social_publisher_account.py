import base64
import hmac
import re
import secrets
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.mimetypes import guess_mimetype

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API = "https://api.linkedin.com"
CALLBACK_PATH = "/social_publisher_linkedin/callback"

PUBLISH_SCOPES = "openid profile w_member_social"
# LinkedIn only grants the Community Management API to an app with no other
# product, so profile metrics usually come from a second app with its own token.
METRICS_SCOPE = "r_member_postAnalytics"
# A Community Management app that also holds w_member_social and r_basicprofile
# can run a personal profile on its own: one connection publishes and reads metrics.
SINGLE_APP_MEMBER_SCOPES = f"r_basicprofile w_member_social {METRICS_SCOPE}"
# Company pages live entirely on the Community Management API app.
ORGANIZATION_SCOPES = " ".join(
    (
        "rw_organization_admin",
        "w_organization_social",
        "r_organization_social",
        "r_organization_followers",
    )
)
POST_MAX_CHARS = 3000
TIMEOUT = 30
IMPORT_PAGE_SIZE = 100
# Posts per organizationalEntityShareStatistics call (a finder with a list
# parameter, allowed on the Development tier, unlike BATCH_GET).
STATS_BATCH_SIZE = 20

# memberCreatorPostAnalytics metric -> provider-neutral key
# (see METRIC_FIELDS in social_publisher)
MEMBER_METRICS = {
    "IMPRESSION": "impressions",
    "MEMBERS_REACHED": "members_reached",
    "REACTION": "reactions",
    "COMMENT": "comments",
    "RESHARE": "reshares",
    "POST_SAVE": "saves",
    "POST_SEND": "sends",
    "LINK_CLICKS": "link_clicks",
    "FOLLOWER_GAINED_FROM_CONTENT": "followers_gained",
    "PROFILE_VIEW_FROM_CONTENT": "profile_views",
}
# organizationalEntityShareStatistics totalShareStatistics -> provider-neutral key
ORGANIZATION_METRICS = {
    "impressionCount": "impressions",
    "uniqueImpressionsCount": "members_reached",
    "likeCount": "reactions",
    "commentCount": "comments",
    "shareCount": "reshares",
    "clickCount": "link_clicks",
}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif"}

_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")
_HASHTAG = re.compile(r"(?<![\w&/])#([^\W\d_][\w]*)", re.UNICODE)


def to_little_text(body):
    """Escape LinkedIn 'little text' reserved characters, keeping #hashtags live."""
    tags = []

    def stash(match):
        tags.append(match.group(1))
        return f"\x00{len(tags) - 1}\x00"

    escaped = _RESERVED.sub(r"\\\1", _HASHTAG.sub(stash, body))
    return re.sub(
        r"\x00(\d+)\x00",
        lambda m: "{hashtag|\\#|" + tags[int(m.group(1))] + "}",
        escaped,
    )


def from_little_text(commentary):
    """Plain text of a LinkedIn 'little text' commentary.

    Hashtags and mentions are kept readable.
    """
    text = re.sub(r"\{hashtag\|\\?#\|([^}]*)\}", r"#\1", commentary or "")
    text = re.sub(r"@\[([^\]]*)\]\([^)]*\)", r"@\1", text)
    return re.sub(r"\\(.)", r"\1", text)


class LinkedInError(UserError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def _linkedin_date(day):
    return f"(day:{day.day},month:{day.month},year:{day.year})"


def _epoch_ms(day):
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp() * 1000)


class SocialPublisherAccount(models.Model):
    _inherit = "social.publisher.account"

    provider = fields.Selection(
        selection_add=[("linkedin", "LinkedIn")],
        ondelete={"linkedin": "cascade"},
    )
    linkedin_account_type = fields.Selection(
        [("member", "Personal Profile"), ("organization", "Company Page")],
        string="LinkedIn Account",
        default="member",
        help="A company page publishes and reads metrics through the Community "
        "Management "
        "API app only.",
    )
    linkedin_client_id = fields.Char(string="Client ID", groups="base.group_system")
    linkedin_client_secret = fields.Char(
        string="Client Secret", groups="base.group_system"
    )
    linkedin_metrics_client_id = fields.Char(
        string="Community Management Client ID",
        groups="base.group_system",
        help="App with the Community Management API product: profile metrics and "
        "company "
        "pages. Leave empty if the publishing app also has it.",
    )
    linkedin_metrics_client_secret = fields.Char(
        string="Community Management Client Secret", groups="base.group_system"
    )
    linkedin_api_version = fields.Char(
        string="API Version",
        default="202609",
        help="LinkedIn versioned API (YYYYMM). LinkedIn retires versions after about "
        "a year.",
    )
    linkedin_redirect_uri = fields.Char(
        string="Redirect URL",
        compute="_compute_linkedin_redirect_uri",
        help="Add this URL to 'Authorized redirect URLs' in the Auth tab of each "
        "LinkedIn app.",
    )
    linkedin_single_app = fields.Boolean(
        string="Single App",
        compute="_compute_linkedin_single_app",
        help="Personal profile run by the Community Management API app alone: fill in "
        "only "
        "that app and the profile connects once for publishing and metrics.",
    )
    linkedin_member_id = fields.Char(string="Member ID", readonly=True, copy=False)
    linkedin_member_name = fields.Char(string="Connected As", readonly=True, copy=False)
    linkedin_page_ids = fields.One2many(
        "social.publisher.linkedin.page",
        "account_id",
        string="Administered Pages",
        readonly=True,
    )
    linkedin_page_id = fields.Many2one(
        "social.publisher.linkedin.page",
        string="Company Page",
        domain="[('account_id', '=', id)]",
        copy=False,
        tracking=True,
    )
    linkedin_access_token = fields.Char(
        groups="base.group_system", copy=False, readonly=True
    )
    linkedin_scopes = fields.Char(string="Granted Scopes", readonly=True, copy=False)
    linkedin_metrics_access_token = fields.Char(
        groups="base.group_system", copy=False, readonly=True
    )
    linkedin_metrics_scopes = fields.Char(
        string="Metrics Scopes", readonly=True, copy=False
    )
    linkedin_oauth_state = fields.Char(
        groups="base.group_system", copy=False, readonly=True
    )

    def _compute_linkedin_redirect_uri(self):
        url = self.get_base_url() + CALLBACK_PATH
        for account in self:
            account.linkedin_redirect_uri = url

    @api.depends(
        "linkedin_account_type", "linkedin_client_id", "linkedin_metrics_client_id"
    )
    def _compute_linkedin_single_app(self):
        for account in self:
            app = account.sudo()
            account.linkedin_single_app = bool(
                account.linkedin_account_type == "member"
                and app.linkedin_metrics_client_id
                and not app.linkedin_client_id
            )

    @api.constrains("linkedin_page_id")
    def _check_linkedin_page(self):
        for account in self:
            if (
                account.linkedin_page_id
                and account.linkedin_page_id.account_id != account
            ):
                raise ValidationError(
                    self.env._("Pick one of the pages listed for this account.")
                )

    def _linkedin_is_organization(self):
        return self.linkedin_account_type == "organization"

    def _linkedin_author_urn(self):
        self.ensure_one()
        if self._linkedin_is_organization():
            if not self.linkedin_page_id:
                raise UserError(
                    self.env._("Choose the company page of %s first.", self.name)
                )
            return self.linkedin_page_id.urn
        if not self.linkedin_member_id:
            raise UserError(self.env._("Connect the LinkedIn account first."))
        return f"urn:li:person:{self.linkedin_member_id}"

    # --- OAuth -------------------------------------------------------------

    def _linkedin_app(self, kind):
        self.ensure_one()
        account = self.sudo()
        if (
            kind in ("metrics", "organization", "member")
            and account.linkedin_metrics_client_id
        ):
            return (
                account.linkedin_metrics_client_id,
                account.linkedin_metrics_client_secret,
            )
        return account.linkedin_client_id, account.linkedin_client_secret

    def _linkedin_authorize_url(self, kind):
        self.ensure_one()
        client_id, client_secret = self._linkedin_app(kind)
        if not client_id or not client_secret:
            raise UserError(
                self.env._(
                    "Fill in the Client ID and Client Secret of the LinkedIn app first."
                )
            )
        state = f"{self.id}:{kind}:{secrets.token_urlsafe(24)}"
        self.sudo().linkedin_oauth_state = state
        if kind == "organization":
            scope = ORGANIZATION_SCOPES
        elif kind == "member":
            scope = SINGLE_APP_MEMBER_SCOPES
        elif kind == "metrics" and self.sudo().linkedin_metrics_client_id:
            scope = METRICS_SCOPE
        elif kind == "metrics":
            scope = f"{PUBLISH_SCOPES} {METRICS_SCOPE}"
        else:
            scope = PUBLISH_SCOPES
        return (
            AUTH_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": self.linkedin_redirect_uri,
                    "state": state,
                    "scope": scope,
                }
            )
        )

    def _linkedin_action_connect(self):
        if self._linkedin_is_organization():
            kind = "organization"
        elif self.linkedin_single_app:
            kind = "member"
        else:
            kind = "publish"
        return {
            "type": "ir.actions.act_url",
            "url": self._linkedin_authorize_url(kind),
            "target": "self",
        }

    def action_linkedin_connect_metrics(self):
        return {
            "type": "ir.actions.act_url",
            "url": self._linkedin_authorize_url("metrics"),
            "target": "self",
        }

    def _linkedin_check_state(self, state):
        """Return the connection kind encoded in ``state`` after checking it."""
        self.ensure_one()
        expected = self.sudo().linkedin_oauth_state or ""
        if not expected or not hmac.compare_digest(expected, state or ""):
            raise UserError(
                self.env._(
                    "This authorization link is no longer valid. Start again from "
                    "the Connect button."
                )
            )
        return expected.split(":")[1]

    def _linkedin_finish_connect(self, state, code):
        """Exchange the authorization code and store the token."""
        self.ensure_one()
        kind = self._linkedin_check_state(state)
        client_id, client_secret = self._linkedin_app(kind)
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.linkedin_redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            raise UserError(
                self.env._(
                    "LinkedIn refused the code (%(status)s): %(body)s",
                    status=response.status_code,
                    body=response.text[:500],
                )
            )
        token = response.json()
        expires_at = fields.Datetime.now() + timedelta(
            seconds=token.get("expires_in", 0)
        )
        scopes = (token.get("scope") or "").replace(",", " ")
        metrics_vals = {
            "linkedin_metrics_access_token": token["access_token"],
            "linkedin_metrics_scopes": scopes,
            "metrics_expires_at": expires_at,
        }
        vals = {"linkedin_oauth_state": False}
        if kind == "metrics":
            vals.update(metrics_vals)
        elif kind == "organization":
            # One token publishes as the page and reads its statistics.
            vals.update(
                {
                    "linkedin_access_token": token["access_token"],
                    "linkedin_scopes": scopes,
                    "token_expires_at": expires_at,
                    **metrics_vals,
                }
            )
        else:
            member_id, member_name = self._linkedin_identify_member(
                token["access_token"], kind
            )
            vals.update(
                {
                    "linkedin_access_token": token["access_token"],
                    "linkedin_scopes": scopes,
                    "token_expires_at": expires_at,
                    "linkedin_member_id": member_id,
                    "linkedin_member_name": member_name,
                }
            )
            # One app holding both products: the same token reads metrics.
            if METRICS_SCOPE in scopes.split():
                vals.update(metrics_vals)
        self.sudo().write(vals)
        if kind == "organization":
            self._linkedin_sync_pages(token["access_token"])
        self.message_post(
            body=self.env._(
                "LinkedIn %(kind)s access connected; valid until %(date)s.",
                kind=kind,
                date=fields.Datetime.to_string(expires_at),
            )
        )

    def _linkedin_identify_member(self, token, kind):
        """Return (member id, name) of the member who authorized ``token``."""
        if kind == "member":
            # The Community Management app has no OpenID product:
            # read the basic profile.
            me = self._linkedin_request(
                "GET", "/v2/me", token=token, versioned=False
            ).payload
            name = " ".join(
                filter(
                    None, (me.get("localizedFirstName"), me.get("localizedLastName"))
                )
            )
            return me["id"], name
        info = self._linkedin_request(
            "GET", "/v2/userinfo", token=token, versioned=False
        ).payload
        return info["sub"], info.get("name")

    def _linkedin_sync_pages(self, token):
        """List the company pages the connected member administers."""
        self.ensure_one()
        acls = self._linkedin_request(
            "GET",
            "/rest/organizationAcls?q=roleAssignee&role=ADMINISTRATOR&state=APPROVED",
            token=token,
        ).payload.get("elements", [])
        urns = sorted({acl["organization"] for acl in acls if acl.get("organization")})
        # One call per page: the Development tier forbids BATCH_GET (ids=List(...)).
        names = {
            urn: self._linkedin_request(
                "GET", f"/rest/organizations/{urn.rsplit(':', 1)[1]}", token=token
            ).payload
            for urn in urns
        }
        Page = self.env["social.publisher.linkedin.page"].sudo()
        existing = {page.urn: page for page in self.sudo().linkedin_page_ids}
        for urn in urns:
            info = names.get(urn, {})
            vals = {
                "name": info.get("localizedName") or urn,
                "vanity_name": info.get("vanityName"),
            }
            if urn in existing:
                existing.pop(urn).write(vals)
            else:
                Page.create({"account_id": self.id, "urn": urn, **vals})
        for page in existing.values():
            if page != self.linkedin_page_id:
                page.unlink()
        if not self.linkedin_page_id and len(urns) == 1:
            self.linkedin_page_id = self.sudo().linkedin_page_ids
        if not urns:
            self.message_post(
                body=self.env._("The connected member administers no company page.")
            )

    # --- HTTP --------------------------------------------------------------

    def _linkedin_request(
        self, method, path, *, token, json_body=None, versioned=True, expected=(200,)
    ):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if versioned:
            headers["LinkedIn-Version"] = self.linkedin_api_version or "202609"
            headers["X-Restli-Protocol-Version"] = "2.0.0"
        url = path if path.startswith("http") else API + path
        response = requests.request(
            method, url, headers=headers, json=json_body, timeout=TIMEOUT
        )
        if response.status_code not in expected:
            raise LinkedInError(
                self.env._(
                    "LinkedIn %(method)s %(path)s failed (%(status)s): %(body)s",
                    method=method,
                    path=path.split("?")[0],
                    status=response.status_code,
                    body=response.text[:500],
                ),
                response.status_code,
            )
        response.payload = response.json() if response.content else {}
        return response

    # --- publishing --------------------------------------------------------

    def _linkedin_max_length(self):
        return POST_MAX_CHARS

    def _linkedin_upload_image(self, token, post):
        raw = base64.b64decode(post.image)
        mime = guess_mimetype(raw)
        if mime not in IMAGE_MIMES:
            raise UserError(
                self.env._("Unsupported image format %s (use PNG, JPEG or GIF).", mime)
            )
        init = self._linkedin_request(
            "POST",
            "/rest/images?action=initializeUpload",
            token=token,
            json_body={
                "initializeUploadRequest": {"owner": self._linkedin_author_urn()}
            },
        ).payload["value"]
        upload = requests.put(
            init["uploadUrl"],
            data=raw,
            timeout=120,
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
        )
        if upload.status_code not in (200, 201):
            raise UserError(
                self.env._(
                    "Image upload failed (%(status)s): %(body)s",
                    status=upload.status_code,
                    body=upload.text[:300],
                )
            )
        return init["image"]

    def _linkedin_publish(self, post):
        self.ensure_one()
        token = self.sudo().linkedin_access_token
        if not token:
            raise UserError(self.env._("Connect the LinkedIn account first."))
        body = post.body.strip()
        payload = {
            "author": self._linkedin_author_urn(),
            "commentary": to_little_text(body),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if post.image:
            payload["content"] = {
                "media": {
                    "id": self._linkedin_upload_image(token, post),
                    "altText": (post.image_alt or body.splitlines()[0])[:4000],
                }
            }
        response = self._linkedin_request(
            "POST", "/rest/posts", token=token, json_body=payload, expected=(201,)
        )
        urn = response.headers.get("x-restli-id", "")
        return {
            "external_id": urn,
            "url": f"https://www.linkedin.com/feed/update/{urn}/",
        }

    # --- importing ---------------------------------------------------------
    # Company pages only: LinkedIn restricts listing a member's posts.

    def _linkedin_can_import_posts(self):
        return bool(
            self._linkedin_is_organization()
            and self.linkedin_page_id
            and self._linkedin_metrics_token()
        )

    def _linkedin_import_posts(self):
        self.ensure_one()
        token = self._linkedin_metrics_token()
        author = urllib.parse.quote(self._linkedin_author_urn(), safe="")
        Post = self.env["social.publisher.post"]
        known = set(
            Post.with_context(active_test=False)
            .search([("account_id", "=", self.id), ("external_id", "!=", False)])
            .mapped("external_id")
        )
        vals_list, start = [], 0
        while True:
            elements = self._linkedin_request(
                "GET",
                f"/rest/posts?q=author&author={author}&count={IMPORT_PAGE_SIZE}"
                f"&start={start}&sortBy=CREATED",
                token=token,
            ).payload.get("elements", [])
            for element in elements:
                urn = element.get("id")
                body = from_little_text(element.get("commentary")).strip()
                if not urn or urn in known or not body:
                    continue  # already in Odoo, or a reshare without text
                published = element.get("publishedAt") or element.get("createdAt")
                vals_list.append(
                    {
                        "account_id": self.id,
                        "body": body,
                        "state": "published",
                        "external_id": urn,
                        "external_url": f"https://www.linkedin.com/feed/update/{urn}/",
                        "published_at": datetime.fromtimestamp(
                            published / 1000, timezone.utc
                        ).replace(tzinfo=None)
                        if published
                        else False,
                    }
                )
                known.add(urn)
            if len(elements) < IMPORT_PAGE_SIZE:
                break
            start += IMPORT_PAGE_SIZE
        return Post.create(vals_list)

    # --- metrics -----------------------------------------------------------

    def _linkedin_metrics_token(self):
        account = self.sudo()
        if (
            account.metrics_expires_at
            and account.metrics_expires_at > fields.Datetime.now()
        ):
            return account.linkedin_metrics_access_token
        return False

    def _linkedin_can_fetch_metrics(self):
        if self._linkedin_is_organization() and not self.linkedin_page_id:
            return False
        return bool(self._linkedin_metrics_token())

    def _linkedin_fetch_metrics(self, post=None, days=None):
        self.ensure_one()
        token = self._linkedin_metrics_token()
        if not token:
            raise UserError(self.env._("Connect the LinkedIn metrics access first."))
        if self._linkedin_is_organization():
            return self._linkedin_fetch_organization_metrics(
                token, post=post, days=days
            )
        return self._linkedin_fetch_member_metrics(token, post=post, days=days)

    def _linkedin_fetch_metrics_batch(self, posts):
        """Company pages read many posts per call; profiles go post by post (None)."""
        self.ensure_one()
        if not self._linkedin_is_organization():
            return None
        token = self._linkedin_metrics_token()
        base = (
            "/rest/organizationalEntityShareStatistics?q=organizationalEntity"
            "&organizationalEntity="
            + urllib.parse.quote(self._linkedin_author_urn(), safe="")
        )
        by_urn = {post.external_id: post.id for post in posts}
        result = {
            post.id: dict.fromkeys(ORGANIZATION_METRICS.values(), 0) for post in posts
        }
        for param, key in (("shares", "share"), ("ugcPosts", "ugcPost")):
            urns = [urn for urn in by_urn if (":share:" in urn) == (param == "shares")]
            for start in range(0, len(urns), STATS_BATCH_SIZE):
                chunk = ",".join(
                    urllib.parse.quote(u, safe="")
                    for u in urns[start : start + STATS_BATCH_SIZE]
                )
                elements = self._linkedin_request(
                    "GET",
                    f"{base}&{param}=List({chunk})",
                    token=token,
                ).payload.get("elements", [])
                for element in elements:
                    post_id = by_urn.get(element.get(key))
                    if not post_id:
                        continue
                    stats = element.get("totalShareStatistics", {})
                    for source, target in ORGANIZATION_METRICS.items():
                        result[post_id][target] += stats.get(source, 0)
        return result

    def _linkedin_fetch_member_metrics(self, token, post=None, days=None):
        if post:
            urn = post.external_id
            kind = "share" if ":share:" in urn else "ugc"
            query = f"q=entity&entity=({kind}:{urllib.parse.quote(urn, safe='')})"
        else:
            query = "q=me"
        if days and not post:
            end = date.today() + timedelta(days=1)
            query += (
                f"&dateRange=(start:{_linkedin_date(end - timedelta(days=days))},"
                f"end:{_linkedin_date(end)})"
            )
        out = {}
        for metric, key in MEMBER_METRICS.items():
            try:
                response = self._linkedin_request(
                    "GET",
                    f"/rest/memberCreatorPostAnalytics?{query}&queryType={metric}"
                    "&aggregation=TOTAL",
                    token=token,
                )
            except LinkedInError as error:
                if error.status in (401, 403):
                    raise
                out[key] = None  # metric not offered for this post type or API version
                continue
            out[key] = sum(
                e.get("count", 0) for e in response.payload.get("elements", [])
            )
        return out

    def _linkedin_fetch_organization_metrics(self, token, post=None, days=None):
        query = "q=organizationalEntity&organizationalEntity=" + urllib.parse.quote(
            self._linkedin_author_urn(), safe=""
        )
        if post:
            urn = post.external_id
            param = "shares" if ":share:" in urn else "ugcPosts"
            query += f"&{param}=List({urllib.parse.quote(urn, safe='')})"
        elif days:
            end = date.today() + timedelta(days=1)
            query += (
                f"&timeIntervals=(timeRange:"
                f"(start:{_epoch_ms(end - timedelta(days=days))},"
                f"end:{_epoch_ms(end)}),timeGranularityType:DAY)"
            )
        elements = self._linkedin_request(
            "GET",
            f"/rest/organizationalEntityShareStatistics?{query}",
            token=token,
        ).payload.get("elements", [])
        out = dict.fromkeys(ORGANIZATION_METRICS.values(), 0)
        for element in elements:
            stats = element.get("totalShareStatistics", {})
            for source, key in ORGANIZATION_METRICS.items():
                out[key] += stats.get(source, 0)
        if post is None:
            # uniqueImpressionsCount is per day: a sum over days is not a reach.
            if days:
                out["members_reached"] = None
            out["followers_gained"] = None
        return out
