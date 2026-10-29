from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

TOKEN_WARNING_DAYS = 7
ACCOUNT_METRICS_DAYS = 28


class SocialPublisherAccount(models.Model):
    """A profile or page on a social network.

    Provider modules add their value to ``provider`` and implement the
    ``_<provider>_*`` hooks called by the generic methods below.
    """

    _name = "social.publisher.account"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Social Publisher Account"
    _order = "sequence, name"
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        index=True,
        tracking=True,
        help="Leave empty to share the account (for example a personal profile) with "
        "every "
        "company.",
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer()
    provider = fields.Selection([], required=True, tracking=True)
    user_id = fields.Many2one(
        "res.users",
        string="Responsible",
        default=lambda self: self.env.user,
        help="Receives the reminder to reconnect before the access expires.",
    )
    token_expires_at = fields.Datetime(
        string="Access Expires", readonly=True, copy=False
    )
    metrics_expires_at = fields.Datetime(
        string="Metrics Access Expires",
        readonly=True,
        copy=False,
        help="Some networks grant metrics through a separate connection.",
    )
    connection_state = fields.Selection(
        [
            ("disconnected", "Disconnected"),
            ("expiring", "Expiring Soon"),
            ("connected", "Connected"),
        ],
        compute="_compute_connection_state",
    )
    metrics_window_days = fields.Integer(
        string="Metrics Window (days)",
        default=14,
        help="The daily sync refreshes the posts published in this many days; older "
        "posts "
        "keep their last numbers (use Refresh Metrics on a post to update it). "
        "Networks limit "
        "API calls per day, and a post's numbers settle after a week or two. 0 = all "
        "posts.",
    )
    max_length = fields.Integer(compute="_compute_max_length")
    can_import_posts = fields.Boolean(compute="_compute_can_import_posts")
    post_ids = fields.One2many("social.publisher.post", "account_id", readonly=True)
    post_count = fields.Integer(compute="_compute_post_count")

    @api.depends("token_expires_at")
    def _compute_connection_state(self):
        now = fields.Datetime.now()
        for account in self:
            expires = account.token_expires_at
            if not expires or expires <= now:
                account.connection_state = "disconnected"
            elif expires - now < timedelta(days=TOKEN_WARNING_DAYS):
                account.connection_state = "expiring"
            else:
                account.connection_state = "connected"

    @api.depends("provider")
    def _compute_max_length(self):
        for account in self:
            account.max_length = account._call_provider("max_length", default=0)

    def _compute_can_import_posts(self):
        for account in self:
            account.can_import_posts = account._call_provider(
                "can_import_posts", default=False
            )

    def _compute_post_count(self):
        counts = dict(
            self.env["social.publisher.post"]._read_group(
                [("account_id", "in", self.ids)], ["account_id"], ["__count"]
            )
        )
        for account in self:
            account.post_count = counts.get(account, 0)

    # --- provider dispatch -------------------------------------------------

    def _call_provider(self, hook, *args, default=None, **kwargs):
        self.ensure_one()
        method = (
            getattr(self, f"_{self.provider}_{hook}", None) if self.provider else None
        )
        if method is None:
            if default is not None:
                return default
            raise UserError(
                self.env._(
                    "The %(provider)s provider does not support '%(hook)s'.",
                    provider=self.provider,
                    hook=hook,
                )
            )
        return method(*args, **kwargs)

    def action_connect(self):
        """Start the provider's authorization flow (returns an action)."""
        return self._call_provider("action_connect")

    def action_view_posts(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "social_publisher.social_publisher_post_action"
        )
        action["domain"] = [("account_id", "=", self.id)]
        action["context"] = {"default_account_id": self.id}
        return action

    def action_import_posts(self):
        """Bring the posts already on the network into Odoo as published."""
        self.ensure_one()
        if not self.can_import_posts:
            raise UserError(
                self.env._("This account cannot list its posts on the network.")
            )
        posts = self._call_provider("import_posts")
        self.message_post(
            body=self.env._("%s posts imported from the network.", len(posts))
        )
        return self.action_view_posts()

    def _publish(self, post):
        """Publish ``post`` and return ``{"external_id": ..., "url": ...}``."""
        return self._call_provider("publish", post)

    def _fetch_metrics(self, post=None, days=None):
        """Return a dict keyed by METRIC_FIELDS for one post or the account."""
        return self._call_provider("fetch_metrics", post=post, days=days)

    def _can_fetch_metrics(self):
        return self._call_provider("can_fetch_metrics", default=False)

    # --- metrics -----------------------------------------------------------

    def _metrics_posts(self):
        """Published posts the daily sync refreshes (see metrics_window_days)."""
        self.ensure_one()
        posts = self.post_ids.filtered(
            lambda p: p.state == "published" and p.external_id
        )
        if self.metrics_window_days > 0:
            since = fields.Datetime.now() - timedelta(days=self.metrics_window_days)
            posts = posts.filtered(lambda p: p.published_at and p.published_at >= since)
        return posts

    def _fetch_posts_metrics(self, posts):
        """Return {post id: metrics dict}, in one call when the provider can batch."""
        self.ensure_one()
        batch = getattr(self, f"_{self.provider}_fetch_metrics_batch", None)
        result = batch(posts) if batch else None
        if result is None:
            result = {post.id: self._fetch_metrics(post=post) for post in posts}
        return result

    def _store_post_metrics(self, posts, now=None):
        self.ensure_one()
        Metric = self.env[
            "social.publisher.metric"
        ].sudo()  # system data, writers may refresh
        now = now or fields.Datetime.now()
        for post_id, data in self._fetch_posts_metrics(posts).items():
            post = posts.browse(post_id)
            values = Metric._metric_values(data)
            Metric.create(
                {
                    "scope": "post",
                    "account_id": self.id,
                    "post_id": post.id,
                    "date": now,
                    **values,
                }
            )
            post.write({**values, "metrics_updated_at": now})

    def _sync_metrics(self):
        """Snapshot the account and its recent published posts into metrics."""
        Metric = self.env["social.publisher.metric"]
        now = fields.Datetime.now()
        for account in self:
            data = account._fetch_metrics(days=ACCOUNT_METRICS_DAYS)
            Metric.create(
                {
                    "scope": "account",
                    "account_id": account.id,
                    "date": now,
                    "period_days": ACCOUNT_METRICS_DAYS,
                    **Metric._metric_values(data),
                }
            )
            account._store_post_metrics(account._metrics_posts(), now=now)

    def action_sync_metrics(self):
        if not all(self.mapped(lambda a: a._can_fetch_metrics())):
            raise UserError(
                self.env._("Connect the metrics access of this account first.")
            )
        self._sync_metrics()

    @api.model
    def _cron_sync_metrics(self):
        accounts = self.search([("provider", "!=", False)]).filtered(
            lambda a: a._can_fetch_metrics()
        )
        for account in accounts:
            account._sync_metrics()
            self.env["ir.cron"]._commit_progress(processed=1)

    # --- access expiry -----------------------------------------------------

    @api.model
    def _cron_warn_expiring_access(self):
        """Leave a to-do for the responsible before access runs out."""
        limit = fields.Datetime.now() + timedelta(days=TOKEN_WARNING_DAYS)
        todo = self.env.ref("mail.mail_activity_data_todo")
        for account in self.search([("provider", "!=", False)]):
            expiring = [
                label
                for label, when in (
                    (self.env._("publishing"), account.token_expires_at),
                    (self.env._("metrics"), account.metrics_expires_at),
                )
                if when and when <= limit
            ]
            if not expiring or not account.user_id:
                continue
            summary = self.env._("Reconnect %s", account.name)
            if account.activity_ids.filtered_domain([("summary", "=", summary)]):
                continue
            account.activity_schedule(
                activity_type_id=todo.id,
                summary=summary,
                note=self.env._(
                    "Access for %(scopes)s expires in less than %(days)s days.",
                    scopes=", ".join(expiring),
                    days=TOKEN_WARNING_DAYS,
                ),
                user_id=account.user_id.id,
            )
