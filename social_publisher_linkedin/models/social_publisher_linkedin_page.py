from odoo import fields, models


class SocialPublisherLinkedinPage(models.Model):
    """A LinkedIn company page the connected member administers."""

    _name = "social.publisher.linkedin.page"
    _description = "LinkedIn Company Page"
    _order = "name"

    name = fields.Char(required=True)
    urn = fields.Char(string="URN", required=True)
    vanity_name = fields.Char(string="Handle")
    account_id = fields.Many2one(
        "social.publisher.account", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(related="account_id.company_id", store=True)

    _urn_account_uniq = models.Constraint(
        "UNIQUE(account_id, urn)", "This page is already listed for the account."
    )
