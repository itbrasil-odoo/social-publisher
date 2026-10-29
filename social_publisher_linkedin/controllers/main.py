import logging

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.social_publisher_account import CALLBACK_PATH

_logger = logging.getLogger(__name__)


class SocialPublisherLinkedin(http.Controller):
    @http.route(CALLBACK_PATH, type="http", auth="user", methods=["GET"])
    def linkedin_callback(
        self, state=None, code=None, error=None, error_description=None, **kw
    ):
        account_id = (state or "").split(":")[0]
        if not account_id.isdigit():
            return request.not_found()
        account = (
            request.env["social.publisher.account"].browse(int(account_id)).exists()
        )
        if not account:
            return request.not_found()
        if not request.env.user.has_group(
            "social_publisher.group_social_publisher_manager"
        ):
            raise AccessError(
                request.env._("Only a Social Publisher manager can connect accounts.")
            )
        if error:
            account._linkedin_check_state(state)
            account.message_post(
                body=request.env._(
                    "LinkedIn refused the connection: %(error)s %(desc)s",
                    error=error,
                    desc=error_description or "",
                )
            )
        else:
            try:
                account._linkedin_finish_connect(state, code)
            except UserError as exc:
                _logger.warning(
                    "LinkedIn connection of account %s failed: %s", account.id, exc
                )
                account.message_post(
                    body=request.env._("LinkedIn connection failed: %s", exc)
                )
        return request.redirect(
            f"/odoo/action-social_publisher.social_publisher_account_action/{account.id}"
        )
