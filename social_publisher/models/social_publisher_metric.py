from odoo import fields, models

# Provider-neutral metric keys. Providers return a dict with these keys
# (missing or None means the network does not report that metric).
METRIC_FIELDS = (
    "impressions",
    "members_reached",
    "reactions",
    "comments",
    "reshares",
    "saves",
    "sends",
    "link_clicks",
    "followers_gained",
    "profile_views",
)


class SocialPublisherMetricMixin(models.AbstractModel):
    _name = "social.publisher.metric.mixin"
    _description = "Social Publisher Metric Values"

    impressions = fields.Integer(aggregator="sum", readonly=True)
    members_reached = fields.Integer(string="Reach", aggregator="sum", readonly=True)
    reactions = fields.Integer(aggregator="sum", readonly=True)
    comments = fields.Integer(aggregator="sum", readonly=True)
    reshares = fields.Integer(aggregator="sum", readonly=True)
    saves = fields.Integer(aggregator="sum", readonly=True)
    sends = fields.Integer(aggregator="sum", readonly=True)
    link_clicks = fields.Integer(aggregator="sum", readonly=True)
    followers_gained = fields.Integer(aggregator="sum", readonly=True)
    profile_views = fields.Integer(aggregator="sum", readonly=True)

    def _metric_values(self, data):
        return {key: data.get(key) or 0 for key in METRIC_FIELDS}


class SocialPublisherMetric(models.Model):
    """Point-in-time snapshot of a post's (or an account's) metrics."""

    _name = "social.publisher.metric"
    _inherit = "social.publisher.metric.mixin"
    _description = "Social Publisher Metric Snapshot"
    _order = "date desc, id desc"

    date = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, readonly=True
    )
    scope = fields.Selection(
        [("post", "Post"), ("account", "Account")],
        required=True,
        default="post",
        readonly=True,
    )
    period_days = fields.Integer(
        help="Window covered by an account snapshot, in days.", readonly=True
    )
    account_id = fields.Many2one(
        "social.publisher.account",
        required=True,
        ondelete="cascade",
        index=True,
        readonly=True,
    )
    post_id = fields.Many2one(
        "social.publisher.post", ondelete="cascade", index=True, readonly=True
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, index=True
    )
    tag_ids = fields.Many2many(related="post_id.tag_ids", string="Tags")
