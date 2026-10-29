import base64
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged
from odoo.tests.common import TransactionCase

from ..models.social_publisher_account import from_little_text, to_little_text

MODULE = "odoo.addons.social_publisher_linkedin.models.social_publisher_account"
# 1x1 transparent PNG
PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
)


def _response(status=200, payload=None, headers=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload or {}
    response.content = b"{}" if payload is not None else b""
    response.text = str(payload)
    response.headers = headers or {}
    return response


@tagged("post_install", "-at_install")
class TestSocialPublisherLinkedin(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.writer = new_test_user(
            cls.env,
            login="sp_writer",
            groups="social_publisher.group_social_publisher_user",
        )
        cls.manager = new_test_user(
            cls.env,
            login="sp_manager",
            groups="social_publisher.group_social_publisher_manager",
        )
        cls.account = cls.env["social.publisher.account"].create(
            {
                "name": "Jane Doe",
                "provider": "linkedin",
                "linkedin_client_id": "cid",
                "linkedin_client_secret": "csecret",
                "linkedin_member_id": "abc123",
                "linkedin_access_token": "tok",
                "token_expires_at": fields.Datetime.now() + timedelta(days=50),
            }
        )
        cls.Post = cls.env["social.publisher.post"]

    def _draft(self, **vals):
        return self.Post.with_user(self.writer).create(
            {
                "account_id": self.account.id,
                "body": "Primeira linha do post.\n\n"
                "Segundo parágrafo com #Odoo e (parênteses).",
                **vals,
            }
        )

    def _no_commit(self):
        return patch.object(
            type(self.env["ir.cron"]), "_commit_progress", return_value=1.0
        )

    # --- approval ----------------------------------------------------------

    def test_writer_cannot_approve(self):
        post = self._draft()
        self.assertEqual(post.state, "draft")
        self.assertEqual(post.name, "Primeira linha do post.")
        with self.assertRaises(AccessError):
            post.action_approve()
        with self.assertRaises(AccessError):
            post.write({"state": "approved"})
        with self.assertRaises(AccessError):
            self.Post.with_user(self.writer).create(
                {"account_id": self.account.id, "body": "x", "state": "approved"}
            )
        with self.assertRaises(AccessError):
            post.with_user(self.manager).write({"state": "published"})

    def test_content_change_resets_approval(self):
        post = self._draft()
        post.with_user(self.manager).action_approve()
        self.assertEqual(post.state, "approved")
        post.with_user(self.manager).write({"publish_at": fields.Datetime.now()})
        self.assertEqual(
            post.state, "approved", "a manager may reschedule an approved post"
        )
        post.with_user(self.writer).write({"publish_at": fields.Datetime.now()})
        self.assertEqual(
            post.state, "draft", "a writer rescheduling needs a new approval"
        )
        post.with_user(self.manager).action_approve()
        post.with_user(self.manager).write({"body": "Outro texto"})
        self.assertEqual(post.state, "draft")

    def test_locked_first_line(self):
        post = self._draft(locked_first_line="Gancho fixo do kit.")
        with self.assertRaises(ValidationError):
            post.with_user(self.manager).action_approve()
        post.body = "Gancho  fixo do kit.\n\nResto adaptado."
        post.with_user(self.manager).action_approve()
        self.assertEqual(post.state, "approved")

    def test_max_length(self):
        post = self._draft(body="a" * 3001)
        with self.assertRaises(ValidationError):
            post.with_user(self.manager).action_approve()

    # --- publishing --------------------------------------------------------

    def test_little_text(self):
        self.assertEqual(
            to_little_text("Veja (isso) #Odoo_19 e C#"),
            "Veja \\(isso\\) {hashtag|\\#|Odoo_19} e C\\#",
        )

    def test_cron_publishes_due_posts(self):
        due = self._draft(publish_at=fields.Datetime.now() - timedelta(minutes=1))
        later = self._draft(publish_at=fields.Datetime.now() + timedelta(days=1))
        (due | later).with_user(self.manager).action_approve()
        created = _response(201, headers={"x-restli-id": "urn:li:share:42"})
        with (
            self._no_commit(),
            patch(f"{MODULE}.requests.request", return_value=created) as call,
        ):
            self.Post._cron_publish_due()
        self.assertEqual(due.state, "published")
        self.assertEqual(due.external_id, "urn:li:share:42")
        self.assertEqual(
            due.external_url, "https://www.linkedin.com/feed/update/urn:li:share:42/"
        )
        self.assertEqual(later.state, "approved")
        payload = call.call_args.kwargs["json"]
        self.assertEqual(payload["author"], "urn:li:person:abc123")
        self.assertIn("{hashtag|\\#|Odoo}", payload["commentary"])
        self.assertEqual(call.call_args.kwargs["headers"]["LinkedIn-Version"], "202609")
        with self.assertRaises(UserError):
            due.with_user(self.manager).write({"body": "editar depois de publicado"})

    def test_publish_with_image(self):
        post = self._draft(image=PNG, image_alt="Arte")
        post.with_user(self.manager).action_approve()
        init = _response(
            200, {"value": {"uploadUrl": "https://upload", "image": "urn:li:image:9"}}
        )
        created = _response(201, headers={"x-restli-id": "urn:li:share:43"})
        with (
            patch(f"{MODULE}.requests.request", side_effect=[init, created]) as call,
            patch(f"{MODULE}.requests.put", return_value=_response(201)) as put,
        ):
            post.with_user(self.manager).action_publish_now()
        self.assertEqual(post.state, "published")
        self.assertEqual(put.call_args.kwargs["headers"]["Content-Type"], "image/png")
        media = call.call_args.kwargs["json"]["content"]["media"]
        self.assertEqual(media, {"id": "urn:li:image:9", "altText": "Arte"})

    def test_failure_parks_post(self):
        post = self._draft(publish_at=fields.Datetime.now() - timedelta(minutes=1))
        post.with_user(self.manager).action_approve()
        refused = _response(422, {"message": "duplicate"})
        with (
            self._no_commit(),
            patch(f"{MODULE}.requests.request", return_value=refused),
        ):
            self.Post._cron_publish_due()
        self.assertEqual(post.state, "failed")
        self.assertIn("422", post.failure_reason)
        with self._no_commit(), patch(f"{MODULE}.requests.request") as call:
            self.Post._cron_publish_due()
        call.assert_not_called()

    # --- OAuth -------------------------------------------------------------

    def test_oauth_state_and_exchange(self):
        account = self.account.with_user(self.manager)
        url = account.action_connect()["url"]
        self.assertIn("client_id=cid", url)
        self.assertIn("scope=openid+profile+w_member_social", url)
        state = self.account.linkedin_oauth_state
        with self.assertRaises(UserError):
            account._linkedin_finish_connect("1:publish:forged", "code")
        token = _response(
            200,
            {
                "access_token": "new",
                "expires_in": 5184000,
                "scope": "openid,profile,w_member_social",
            },
        )
        userinfo = _response(200, {"sub": "xyz", "name": "Jane Doe"})
        with (
            patch(f"{MODULE}.requests.post", return_value=token),
            patch(f"{MODULE}.requests.request", return_value=userinfo),
        ):
            account._linkedin_finish_connect(state, "code")
        self.assertEqual(self.account.linkedin_access_token, "new")
        self.assertEqual(self.account.linkedin_member_name, "Jane Doe")
        self.assertEqual(self.account.connection_state, "connected")
        self.assertFalse(self.account.linkedin_oauth_state, "a state is single-use")

    def test_single_app_member(self):
        account = (
            self.env["social.publisher.account"]
            .create(
                {
                    "name": "Solo",
                    "provider": "linkedin",
                    "linkedin_metrics_client_id": "cm_id",
                    "linkedin_metrics_client_secret": "cm_secret",
                }
            )
            .with_user(self.manager)
        )
        self.assertTrue(account.linkedin_single_app)
        self.assertFalse(
            self.account.linkedin_single_app, "two-app profiles keep working"
        )
        url = account.action_connect()["url"]
        self.assertIn("client_id=cm_id", url)
        self.assertIn(
            "scope=r_basicprofile+w_member_social+r_member_postAnalytics", url
        )
        token = _response(
            200,
            {
                "access_token": "one",
                "expires_in": 5184000,
                "scope": "r_basicprofile,w_member_social,r_member_postAnalytics",
            },
        )
        me = _response(
            200, {"id": "p42", "localizedFirstName": "Jane", "localizedLastName": "Doe"}
        )
        with (
            patch(f"{MODULE}.requests.post", return_value=token) as post,
            patch(f"{MODULE}.requests.request", return_value=me) as call,
        ):
            account._linkedin_finish_connect(
                account.sudo().linkedin_oauth_state, "code"
            )
        self.assertEqual(post.call_args.kwargs["data"]["client_id"], "cm_id")
        self.assertTrue(call.call_args.args[1].endswith("/v2/me"))
        self.assertEqual(
            (account.linkedin_member_id, account.linkedin_member_name),
            ("p42", "Jane Doe"),
        )
        self.assertEqual(account.sudo().linkedin_metrics_access_token, "one")
        self.assertTrue(account._can_fetch_metrics())
        self.assertEqual(account._linkedin_author_urn(), "urn:li:person:p42")

    # --- metrics -----------------------------------------------------------

    def test_sync_metrics(self):
        post = self._draft()
        post._internal_write(
            {
                "state": "published",
                "external_id": "urn:li:share:42",
                "published_at": fields.Datetime.now(),
            }
        )
        self.account.write(
            {
                "linkedin_metrics_access_token": "mtok",
                "metrics_expires_at": fields.Datetime.now() + timedelta(days=60),
            }
        )

        def fake(method, url, **kwargs):
            if "queryType=FOLLOWER_GAINED_FROM_CONTENT" in url:
                return _response(400, {"message": "not supported"})
            return _response(200, {"elements": [{"count": 7}, {"count": 3}]})

        with (
            self._no_commit(),
            patch(f"{MODULE}.requests.request", side_effect=fake) as call,
        ):
            self.env["social.publisher.account"]._cron_sync_metrics()
        self.assertEqual(post.impressions, 10)
        self.assertEqual(post.followers_gained, 0)
        self.assertTrue(post.metrics_updated_at)
        self.assertEqual(
            call.call_args.kwargs["headers"]["Authorization"], "Bearer mtok"
        )
        snapshots = self.env["social.publisher.metric"].search(
            [("account_id", "=", self.account.id)]
        )
        self.assertEqual(sorted(snapshots.mapped("scope")), ["account", "post"])
        self.assertIn(
            "share%3A42",
            next(c.args[1] for c in call.call_args_list if "q=entity" in c.args[1]),
        )

    # --- company pages -----------------------------------------------------

    def _organization_account(self, **vals):
        return self.env["social.publisher.account"].create(
            {
                "name": "Example Corp",
                "provider": "linkedin",
                "linkedin_account_type": "organization",
                "linkedin_metrics_client_id": "cm_id",
                "linkedin_metrics_client_secret": "cm_secret",
                **vals,
            }
        )

    def test_organization_connect_lists_pages(self):
        account = self._organization_account().with_user(self.manager)
        url = account.action_connect()["url"]
        self.assertIn("client_id=cm_id", url)
        self.assertIn("w_organization_social", url)
        token = _response(
            200,
            {
                "access_token": "otok",
                "expires_in": 5184000,
                "scope": "rw_organization_admin,w_organization_social",
            },
        )
        acls = _response(
            200, {"elements": [{"organization": "urn:li:organization:777"}]}
        )
        orgs = _response(
            200, {"localizedName": "Example Corp", "vanityName": "example-corp"}
        )
        with (
            patch(f"{MODULE}.requests.post", return_value=token),
            patch(f"{MODULE}.requests.request", side_effect=[acls, orgs]) as call,
        ):
            account._linkedin_finish_connect(
                account.sudo().linkedin_oauth_state, "code"
            )
        self.assertTrue(
            call.call_args_list[1].args[1].endswith("/rest/organizations/777"),
            "one call per page: BATCH_GET is not allowed on the Development tier",
        )
        self.assertEqual(account.linkedin_page_id.name, "Example Corp")
        self.assertEqual(account.linkedin_page_id.urn, "urn:li:organization:777")
        self.assertEqual(account.connection_state, "connected")
        self.assertTrue(
            account._can_fetch_metrics(), "one token publishes and reads stats"
        )

    def _connected_organization(self):
        account = self._organization_account(
            linkedin_access_token="otok",
            linkedin_metrics_access_token="otok",
            token_expires_at=fields.Datetime.now() + timedelta(days=50),
            metrics_expires_at=fields.Datetime.now() + timedelta(days=50),
        )
        account.linkedin_page_id = self.env["social.publisher.linkedin.page"].create(
            {
                "account_id": account.id,
                "name": "Example Corp",
                "urn": "urn:li:organization:777",
            }
        )
        return account

    def test_organization_publish_as_page(self):
        account = self._connected_organization()
        post = self._draft(account_id=account.id)
        post.with_user(self.manager).action_approve()
        created = _response(201, headers={"x-restli-id": "urn:li:share:99"})
        with patch(f"{MODULE}.requests.request", return_value=created) as call:
            post.with_user(self.manager).action_publish_now()
        self.assertEqual(
            call.call_args.kwargs["json"]["author"], "urn:li:organization:777"
        )
        self.assertEqual(post.state, "published")

    def test_organization_import_posts(self):
        account = self._connected_organization()
        self.assertTrue(account.can_import_posts)
        self.assertFalse(
            self.account.can_import_posts, "a member's posts cannot be listed"
        )
        page1 = _response(
            200,
            {
                "elements": [
                    {
                        "id": f"urn:li:share:{i}",
                        "commentary": f"Post {i} \\(teste\\) {{hashtag|\\#|Odoo}}",
                        "publishedAt": 1760000000000,
                    }
                    for i in range(100)
                ]
            },
        )
        page2 = _response(
            200,
            {
                "elements": [
                    {"id": "urn:li:share:0", "commentary": "duplicado"},
                    {"id": "urn:li:share:500", "commentary": ""},
                    {
                        "id": "urn:li:ugcPost:501",
                        "commentary": "Último",
                        "createdAt": 1760000000000,
                    },
                ]
            },
        )
        with patch(f"{MODULE}.requests.request", side_effect=[page1, page2]) as call:
            account.with_user(self.manager).action_import_posts()
        self.assertIn("start=100", call.call_args_list[1].args[1])
        posts = self.Post.search([("account_id", "=", account.id)])
        self.assertEqual(len(posts), 101)
        first = posts.filtered(lambda p: p.external_id == "urn:li:share:0")
        self.assertEqual(first.body, "Post 0 (teste) #Odoo")
        self.assertEqual(first.state, "published")
        with patch(
            f"{MODULE}.requests.request", return_value=_response(200, {"elements": []})
        ):
            account.with_user(self.manager).action_import_posts()
        self.assertEqual(self.Post.search_count([("account_id", "=", account.id)]), 101)

    def test_organization_metrics(self):
        account = self._connected_organization()
        post = self._draft(account_id=account.id)
        post._internal_write(
            {
                "state": "published",
                "external_id": "urn:li:share:99",
                "published_at": fields.Datetime.now(),
            }
        )
        stats = _response(
            200,
            {
                "elements": [
                    {
                        "share": "urn:li:share:99",
                        "totalShareStatistics": {
                            "impressionCount": 50,
                            "uniqueImpressionsCount": 30,
                            "likeCount": 4,
                            "commentCount": 1,
                            "shareCount": 2,
                            "clickCount": 3,
                        },
                    }
                ]
            },
        )
        with (
            self._no_commit(),
            patch(f"{MODULE}.requests.request", return_value=stats) as call,
        ):
            account._sync_metrics()
        self.assertEqual(
            (
                post.impressions,
                post.members_reached,
                post.reactions,
                post.comments,
                post.reshares,
                post.link_clicks,
            ),
            (50, 30, 4, 1, 2, 3),
        )
        urls = [c.args[1] for c in call.call_args_list]
        self.assertTrue(any("shares=List(urn%3Ali%3Ashare%3A99)" in u for u in urls))
        self.assertTrue(any("timeGranularityType:DAY" in u for u in urls))

    def test_metrics_window_and_refresh(self):
        self.account.write(
            {
                "linkedin_metrics_access_token": "mtok",
                "metrics_expires_at": fields.Datetime.now() + timedelta(days=60),
            }
        )
        recent, old = self._draft(), self._draft()
        recent._internal_write(
            {
                "state": "published",
                "external_id": "urn:li:share:1",
                "published_at": fields.Datetime.now() - timedelta(days=3),
            }
        )
        old._internal_write(
            {
                "state": "published",
                "external_id": "urn:li:share:2",
                "published_at": fields.Datetime.now() - timedelta(days=40),
            }
        )
        counted = _response(200, {"elements": [{"count": 5}]})
        with (
            self._no_commit(),
            patch(f"{MODULE}.requests.request", return_value=counted),
        ):
            self.env["social.publisher.account"]._cron_sync_metrics()
        self.assertEqual(recent.impressions, 5)
        self.assertEqual(old.impressions, 0, "outside the 14-day window")
        with patch(f"{MODULE}.requests.request", return_value=counted) as call:
            old.with_user(self.writer).action_refresh_metrics()
        self.assertEqual(old.impressions, 5)
        self.assertTrue(all("share%3A2" in c.args[1] for c in call.call_args_list))

    def test_from_little_text(self):
        self.assertEqual(
            from_little_text(to_little_text("Veja (isso) #Odoo e C#")),
            "Veja (isso) #Odoo e C#",
        )
        self.assertEqual(
            from_little_text("Oi @[Acme Inc](urn:li:organization:1)"), "Oi @Acme Inc"
        )

    # --- multi-company -----------------------------------------------------

    def test_multi_company(self):
        other = self.env["res.company"].create({"name": "Acme Inc"})
        account_b = self._organization_account(name="Acme Inc", company_id=other.id)
        shared = self.account  # created in the main company; share it with everyone
        shared.company_id = False
        writer = new_test_user(
            self.env,
            login="sp_writer_a",
            groups="social_publisher.group_social_publisher_user",
        )
        Account = self.env["social.publisher.account"].with_user(writer)
        self.assertNotIn(account_b, Account.search([]))
        self.assertIn(shared, Account.search([]))
        post_b = self.Post.with_company(other).create(
            {"account_id": account_b.id, "body": "x"}
        )
        self.assertEqual(post_b.company_id, other)
        self.assertNotIn(post_b, self.Post.with_user(writer).search([]))
