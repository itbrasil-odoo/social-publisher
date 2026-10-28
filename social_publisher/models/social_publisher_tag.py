from odoo import fields, models


class SocialPublisherTag(models.Model):
    _name = "social.publisher.tag"
    _description = "Social Publisher Tag"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    color = fields.Integer()

    _name_uniq = models.Constraint("UNIQUE(name)", "A tag with this name already exists.")
