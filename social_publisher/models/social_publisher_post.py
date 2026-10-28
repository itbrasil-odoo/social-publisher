import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

# Changing any of these on an approved post sends it back to draft: approval
# is given to a specific text, image and destination, not to the record.
CONTENT_FIELDS = {"body", "image", "image_alt", "account_id", "locked_first_line"}
LOCKED_WHEN_PUBLISHED = CONTENT_FIELDS | {"publish_at", "calendar_date"}
# Calendar colour per state (Odoo colour picker indexes).
STATE_COLORS = {"draft": 3, "approved": 4, "publishing": 7, "published": 10, "failed": 1}
MANAGER_GROUP = "social_publisher.group_social_publisher_manager"


def first_paragraph(text):
    return " ".join((text or "").strip().split("\n\n")[0].split())


class SocialPublisherPost(models.Model):
    _name = "social.publisher.post"
    _inherit = ["mail.thread", "mail.activity.mixin", "social.publisher.metric.mixin"]
    _description = "Social Publisher Post"
    _order = "publish_at desc, id desc"
    _check_company_auto = True

    @api.model
    def _lang_get(self):
        return self.env["res.lang"].get_installed()

    name = fields.Char(compute="_compute_name", store=True)
    body = fields.Text(
        required=True,
        tracking=True,
        help="Text exactly as it should appear on the network.",
    )
    char_count = fields.Integer(string="Characters", compute="_compute_char_count")
    max_length = fields.Integer(related="account_id.max_length")
    account_id = fields.Many2one(
        "social.publisher.account",
        required=True,
        tracking=True,
        index=True,
        check_company=True,
        default=lambda self: self.env["social.publisher.account"].search(
            self.env["social.publisher.account"]._check_company_domain(self.env.company), limit=1
        ),
    )
    company_id = fields.Many2one(related="account_id.company_id", store=True, index=True)
    provider = fields.Selection(related="account_id.provider")
    color = fields.Integer(related="account_id.color")
    state_color = fields.Integer(compute="_compute_state_color")
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("approved", "Approved"),
            ("publishing", "Publishing"),
            ("published", "Published"),
            ("failed", "Failed"),
        ],
        default="draft",
        required=True,
        tracking=True,
        index=True,
        copy=False,
        group_expand="_group_expand_states",
    )
    publish_at = fields.Datetime(
        string="Scheduled For",
        tracking=True,
        copy=False,
        help="Approved posts are published automatically once this time has passed. "
        "Leave empty to publish by hand.",
    )
    published_at = fields.Datetime(readonly=True, copy=False)
    calendar_date = fields.Datetime(
        compute="_compute_calendar_date",
        inverse="_inverse_calendar_date",
        store=True,
        help="Scheduled time, or publication time once published.",
    )
    image = fields.Image(max_width=4096, max_height=4096, copy=False)
    image_alt = fields.Char(string="Image Description")
    locked_first_line = fields.Char(
        string="Required Opening",
        help="When set, the post is only published if its first paragraph matches this text "
        "exactly (for example, a hook fixed by an agency kit).",
    )
    tag_ids = fields.Many2many("social.publisher.tag", string="Tags")
    lang = fields.Selection(_lang_get, string="Language", default=lambda self: self.env.lang)
    reference = fields.Char(
        help="Free reference, such as the kit number or the hook formula used.",
    )
    note = fields.Text(string="Internal Notes")
    external_id = fields.Char(readonly=True, copy=False, index=True)
    external_url = fields.Char(string="Link", readonly=True, copy=False)
    failure_reason = fields.Text(readonly=True, copy=False)
    metrics_updated_at = fields.Datetime(readonly=True, copy=False)
    metric_ids = fields.One2many("social.publisher.metric", "post_id", string="Metric History")
    engagement_rate = fields.Float(compute="_compute_engagement_rate", digits=(16, 2))

    def _group_expand_states(self, states, domain):
        # "publishing" only lasts a few seconds: show it only when it holds a post.
        return [key for key, _label in self._fields["state"].selection
                if key != "publishing" or key in states]

    @api.depends("body")
    def _compute_name(self):
        for post in self:
            hook = first_paragraph(post.body)
            post.name = (hook[:77] + "...") if len(hook) > 80 else hook or _("New post")

    @api.depends("body")
    def _compute_char_count(self):
        for post in self:
            post.char_count = len((post.body or "").strip())

    @api.depends("publish_at", "published_at", "state")
    def _compute_calendar_date(self):
        for post in self:
            post.calendar_date = post.published_at if post.state == "published" else post.publish_at

    def _inverse_calendar_date(self):
        # Dragging a post in the calendar reschedules it.
        for post in self:
            if post.state != "published":
                post.publish_at = post.calendar_date

    @api.depends("state")
    def _compute_state_color(self):
        for post in self:
            post.state_color = STATE_COLORS.get(post.state, 0)

    @api.depends("impressions", "reactions", "comments", "reshares")
    def _compute_engagement_rate(self):
        for post in self:
            interactions = post.reactions + post.comments + post.reshares
            post.engagement_rate = 100.0 * interactions / post.impressions if post.impressions else 0.0

    # --- guards ------------------------------------------------------------

    def _is_manager(self):
        return self.env.su or self.env.user.has_group(MANAGER_GROUP)

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_manager() and any(
            vals.get("state", "draft") != "draft" for vals in vals_list
        ):
            raise AccessError(_("Only a Social Publisher manager can approve posts."))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get("social_publisher_internal"):
            return super().write(vals)
        if "state" in vals and vals["state"] != "draft":
            if vals["state"] != "approved" or not self._is_manager():
                raise AccessError(_("Only a Social Publisher manager can approve posts; "
                                    "publishing states are set by the system."))
        locked = LOCKED_WHEN_PUBLISHED.intersection(vals)
        if locked and self.filtered(lambda p: p.state in ("published", "publishing")):
            raise UserError(_("A published post can no longer be changed on the network "
                              "from here."))
        resets = CONTENT_FIELDS.intersection(vals) or (
            "publish_at" in vals and not self._is_manager()
        )
        if resets and "state" not in vals:
            approved = self.filtered(lambda p: p.state == "approved")
            if approved:
                for post in approved:
                    post.message_post(body=_("Content changed after approval: back to draft."))
                super(SocialPublisherPost, approved).write({"state": "draft"})
        return super().write(vals)

    def _internal_write(self, vals):
        return self.with_context(social_publisher_internal=True).sudo().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_published(self):
        if self.filtered(lambda p: p.state in ("published", "publishing")):
            raise UserError(_("Published posts cannot be deleted; archive them on the network."))

    def _check_publishable(self):
        for post in self:
            body = (post.body or "").strip()
            if not body:
                raise ValidationError(_("The post is empty."))
            if post.max_length and len(body) > post.max_length:
                raise ValidationError(
                    _("The post has %(count)s characters; %(network)s allows %(max)s.",
                      count=len(body), network=post.account_id.name, max=post.max_length)
                )
            if post.locked_first_line and first_paragraph(body) != first_paragraph(
                post.locked_first_line
            ):
                raise ValidationError(
                    _("The first paragraph was changed. It must be exactly:\n%s",
                      post.locked_first_line)
                )
            if post.account_id.connection_state == "disconnected":
                raise ValidationError(_("Connect the account %s first.", post.account_id.name))

    # --- actions -----------------------------------------------------------

    def action_approve(self):
        if not self._is_manager():
            raise AccessError(_("Only a Social Publisher manager can approve posts."))
        drafts = self.filtered(lambda p: p.state in ("draft", "failed"))
        drafts._check_publishable()
        drafts.write({"state": "approved"})

    def action_reset_draft(self):
        self.filtered(lambda p: p.state in ("approved", "failed")).write({"state": "draft"})

    def action_publish_now(self):
        if not self._is_manager():
            raise AccessError(_("Only a Social Publisher manager can publish posts."))
        if self.filtered(lambda p: p.state != "approved"):
            raise UserError(_("Only approved posts can be published."))
        self._publish()

    def action_refresh_metrics(self):
        posts = self.filtered(lambda p: p.state == "published" and p.external_id)
        for account in posts.account_id:
            if not account._can_fetch_metrics():
                raise UserError(_("Connect the metrics access of %s first.", account.name))
            account._store_post_metrics(posts.filtered(lambda p: p.account_id == account))

    def action_open_external(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.external_url, "target": "new"}

    # --- publishing --------------------------------------------------------

    def _publish(self, commit=False):
        """Publish approved posts one by one.

        A post is never retried blindly: when the network call fails the post
        becomes ``failed`` and waits for a human. With ``commit`` (cron), the
        ``publishing`` state is committed before calling the network, so a
        crash midway can't lead to a second publication.
        """
        for post in self:
            if post.state != "approved":
                continue
            try:
                post._check_publishable()
            except ValidationError as error:
                post._mark_failed(str(error))
                if commit:
                    self.env["ir.cron"]._commit_progress(processed=1)
                continue
            post._internal_write({"state": "publishing", "failure_reason": False})
            if commit:
                self.env["ir.cron"]._commit_progress()
            try:
                result = post.account_id._publish(post)
            except Exception as error:  # noqa: BLE001 - any failure parks the post
                _logger.warning("Publishing post %s failed", post.id, exc_info=True)
                post._mark_failed(str(error))
            else:
                post._internal_write({
                    "state": "published",
                    "published_at": fields.Datetime.now(),
                    "external_id": result.get("external_id"),
                    "external_url": result.get("url"),
                })
                post.message_post(body=_("Published: %s", result.get("url") or result.get("external_id")))
            if commit:
                self.env["ir.cron"]._commit_progress(processed=1)

    def _mark_failed(self, reason):
        self._internal_write({"state": "failed", "failure_reason": reason})
        self.message_post(body=_("Publication failed: %s", reason))

    @api.model
    def _cron_publish_due(self):
        due = self.search([
            ("state", "=", "approved"),
            ("publish_at", "!=", False),
            ("publish_at", "<=", fields.Datetime.now()),
        ], order="publish_at")
        self.env["ir.cron"]._commit_progress(remaining=len(due))
        due._publish(commit=True)
