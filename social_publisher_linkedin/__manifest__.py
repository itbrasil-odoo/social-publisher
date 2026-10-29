{
    "name": "Social Publisher - LinkedIn",
    "version": "19.0.1.3.2",
    "category": "Marketing/Social Marketing",
    "summary": "Publish to LinkedIn profiles and company pages, with analytics",
    "license": "LGPL-3",
    "author": "IT Brasil, Renan Teixeira",
    "website": "https://github.com/itbrasil-odoo/social-publisher",
    "depends": [
        "social_publisher",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/social_publisher_linkedin_security.xml",
        "views/social_publisher_account_views.xml",
    ],
    "images": [
        "static/description/banner.png",
        "static/description/account_screenshot.png",
        "static/description/post_screenshot.png",
    ],
    "installable": True,
    "auto_install": False,
}
