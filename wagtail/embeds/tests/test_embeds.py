import datetime
import json
import unittest
import urllib.request
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import responses
from django import template
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.timezone import make_aware, now
from responses import matchers

from wagtail import blocks
from wagtail.embeds import oembed_providers
from wagtail.embeds.blocks import EmbedBlock, EmbedValue
from wagtail.embeds.embeds import get_embed, get_embed_hash
from wagtail.embeds.exceptions import (
    EmbedNotFoundException,
    EmbedUnsupportedProviderException,
)
from wagtail.embeds.finders import get_finders
from wagtail.embeds.finders.embedly import (
    AccessDeniedEmbedlyException,
    EmbedlyException,
)
from wagtail.embeds.finders.embedly import EmbedlyFinder as EmbedlyFinder
from wagtail.embeds.finders.facebook import AccessDeniedFacebookOEmbedException
from wagtail.embeds.finders.facebook import FacebookOEmbedFinder as FacebookOEmbedFinder
from wagtail.embeds.finders.instagram import AccessDeniedInstagramOEmbedException
from wagtail.embeds.finders.instagram import (
    InstagramOEmbedFinder as InstagramOEmbedFinder,
)
from wagtail.embeds.finders.oembed import OEmbedFinder as OEmbedFinder
from wagtail.embeds.models import Embed
from wagtail.embeds.templatetags.wagtailembeds_tags import embed_tag
from wagtail.test.utils import WagtailTestUtils

try:
    import embedly  # noqa: F401

    no_embedly = False
except ImportError:
    no_embedly = True


class DummyFinder:
    def __init__(self, finder_func):
        self.finder_func = finder_func

    def accept(self, url):
        # Always accept for test purposes.
        return True

    def find_embed(self, url, max_width=None, **kwargs):
        return self.finder_func(url, max_width, **kwargs)


class TestGetFinders(TestCase):
    def test_defaults_to_oembed(self):
        finders = get_finders()

        self.assertEqual(len(finders), 1)
        self.assertIsInstance(finders[0], OEmbedFinder)

    # New WAGTAILEMBEDS_FINDERS setting

    @override_settings(
        WAGTAILEMBEDS_FINDERS=[{"class": "wagtail.embeds.finders.oembed"}]
    )
    def test_new_find_oembed(self):
        finders = get_finders()

        self.assertEqual(len(finders), 1)
        self.assertIsInstance(finders[0], OEmbedFinder)

    @override_settings(
        WAGTAILEMBEDS_FINDERS=[
            {
                "class": "wagtail.embeds.finders.embedly",
                "key": "foo",
            }
        ]
    )
    def test_new_find_embedly(self):
        finders = get_finders()

        self.assertEqual(len(finders), 1)
        self.assertIsInstance(finders[0], EmbedlyFinder)
        self.assertEqual(finders[0].get_key(), "foo")

    @override_settings(
        WAGTAILEMBEDS_FINDERS=[
            {"class": "wagtail.embeds.finders.oembed", "options": {"foo": "bar"}}
        ]
    )
    def test_new_find_oembed_with_options(self):
        finders = get_finders()

        self.assertEqual(len(finders), 1)
        self.assertIsInstance(finders[0], OEmbedFinder)
        self.assertEqual(finders[0].options, {"foo": "bar"})

    @override_settings(
        WAGTAILEMBEDS_FINDERS=[
            {
                "class": "wagtail.embeds.finders.instagram",
                "app_id": "1234567890",
                "app_secret": "abcdefghijklmnop",
            },
        ]
    )
    def test_find_instagram_oembed_with_options(self):
        finders = get_finders()

        self.assertEqual(len(finders), 1)
        self.assertIsInstance(finders[0], InstagramOEmbedFinder)
        self.assertEqual(finders[0].app_id, "1234567890")
        self.assertEqual(finders[0].app_secret, "abcdefghijklmnop")
        # omitscript defaults to False
        self.assertIs(finders[0].omitscript, False)

    @override_settings(
        WAGTAILEMBEDS_FINDERS=[
            {
                "class": "wagtail.embeds.finders.facebook",
                "app_id": "1234567890",
                "app_secret": "abcdefghijklmnop",
            },
        ]
    )
    def test_find_facebook_oembed_with_options(self):
        finders = get_finders()

        self.assertEqual(len(finders), 1)
        self.assertIsInstance(finders[0], FacebookOEmbedFinder)
        self.assertEqual(finders[0].app_id, "1234567890")
        self.assertEqual(finders[0].app_secret, "abcdefghijklmnop")
        # omitscript defaults to False
        self.assertIs(finders[0].omitscript, False)


class TestEmbeds(TestCase):
    def setUp(self):
        self.hit_count = 0

    def dummy_finder(self, url, max_width=None, max_height=None):
        # Up hit count
        self.hit_count += 1

        # Return a pretend record
        return {
            "title": "Test: " + url,
            "type": "video",
            "width": max_width if max_width else 640,
            "height": 480,
            "html": "<p>Blah blah blah</p>",
        }

    @override_settings(WAGTAILEMBEDS_RESPONSIVE_HTML=True)
    def test_get_embed_responsive(self):
        with patch("wagtail.embeds.embeds.get_finders") as get_finders_mock:
            # Configure get_finders to return our dummy finder properly
            dummy_finder = DummyFinder(self.dummy_finder)
            get_finders_mock.return_value = [dummy_finder]

            embed = get_embed("www.test.com/1234", max_width=400)

        # Check that the embed is correct
        self.assertEqual(embed.title, "Test: www.test.com/1234")
        self.assertEqual(embed.type, "video")
        self.assertEqual(embed.width, 400)
        self.assertEqual(embed.thumbnail_url, "")

        # Check ratio calculations
        self.assertEqual(embed.ratio, 480 / 400)
        self.assertEqual(embed.ratio_css, "120.0%")
        self.assertTrue(embed.is_responsive)

        # Check that there has only been one hit to the backend
        self.assertEqual(self.hit_count, 1)

        # Look for the same embed again and check the hit count hasn't increased
        with patch("wagtail.embeds.embeds.get_finders") as get_finders_mock:
            get_finders_mock.return_value = [DummyFinder(self.dummy_finder)]
            get_embed("www.test.com/1234", max_width=400)
        self.assertEqual(self.hit_count, 1)

        # Look for a different embed, hit count should increase
        with patch("wagtail.embeds.embeds.get_finders") as get_finders_mock:
            get_finders_mock.return_value = [DummyFinder(self.dummy_finder)]
            get_embed("www.test.com/4321", max_width=400)
        self.assertEqual(self.hit_count, 2)

        # Look for the same embed with a different width, this should also increase hit count
        with patch("wagtail.embeds.embeds.get_finders") as get_finders_mock:
            get_finders_mock.return_value = [DummyFinder(self.dummy_finder)]
            get_embed("www.test.com/4321")
        self.assertEqual(self.hit_count, 3)

    def test_get_embed_nonresponsive(self):
        with patch("wagtail.embeds.embeds.get_finders") as get_finders:
            get_finders.return_value = [DummyFinder(self.dummy_finder)]
            embed = get_embed("www.test.com/1234", max_width=400)

        # Check that the embed is correct
        self.assertEqual(embed.title, "Test: www.test.com/1234")
        self.assertEqual(embed.type, "video")
        self.assertEqual(embed.width, 400)
        self.assertFalse(embed.is_responsive)
        self.assertIsNone(embed.cache_until)

    def dummy_cache_until_finder(self, url, max_width=None, max_height=None):
        # Up hit count
        self.hit_count += 1

        # Return a pretend record
        return {
            "title": "Test: " + url,
            "type": "video",
            "width": max_width if max_width else 640,
            "height": 480,
            "html": "<p>Blah blah blah</p>",
            "cache_until": make_aware(datetime.datetime(2001, 2, 3)),
        }

    def test_get_embed_cache_until(self):
        # Patch get_finders to always return our dummy_cache_until_finder
        with patch("wagtail.embeds.embeds.get_finders") as get_finders:
            get_finders.return_value = [DummyFinder(self.dummy_cache_until_finder)]
            embed = get_embed("www.test.com/1234", max_width=400)
            self.assertEqual(
                embed.cache_until, make_aware(datetime.datetime(2001, 2, 3))
            )
            self.assertEqual(self.hit_count, 1)

            # expired cache_until should be ignored
            embed_2 = get_embed("www.test.com/1234", max_width=400)
            self.assertEqual(self.hit_count, 2)

            # future cache_until should not be ignored
            future_dt = now() + datetime.timedelta(minutes=1)
            embed.cache_until = future_dt
            embed.save()
            embed_3 = get_embed("www.test.com/1234", max_width=400)
            self.assertEqual(self.hit_count, 2)

            # ensure we've received the same embed
            self.assertEqual(embed, embed_2)
            self.assertEqual(embed, embed_3)
            self.assertEqual(embed_3.cache_until, future_dt)

    def dummy_finder_invalid_width(self, url, max_width=None, max_height=None):
        # Return a record with an invalid width
        return {
            "title": "Test: " + url,
            "type": "video",
            "thumbnail_url": "",
            "width": "100%",
            "height": 480,
            "html": "<p>Blah blah blah</p>",
        }

    def test_invalid_width(self):
        with patch("wagtail.embeds.embeds.get_finders") as get_finders:
            get_finders.return_value = [DummyFinder(self.dummy_finder_invalid_width)]
            embed = get_embed("www.test.com/1234", max_width=400)

        # Width must be set to None
        self.assertIsNone(embed.width)

    def test_no_html(self):
        def no_html_finder(url, max_width=None, max_height=None):
            """
            A finder which returns everything but HTML
            """
            embed = self.dummy_finder(url, max_width)
            embed["html"] = None
            return embed

        with patch("wagtail.embeds.embeds.get_finders") as get_finders:
            get_finders.return_value = [DummyFinder(no_html_finder)]
            embed = get_embed("www.test.com/1234", max_width=400)

        self.assertEqual(embed.html, "")

    @override_settings(WAGTAILEMBEDS_FINDERS=[])
    def test_no_finders_available(self):
        with self.assertRaises(EmbedUnsupportedProviderException):
            get_embed("www.test.com/1234", max_width=400)


class TestEmbedHash(TestCase):
    def test_get_embed_hash(self):
        url = "www.test.com/1234"
        self.assertEqual(get_embed_hash(url), "9a4cfc187266026cd68160b5db572629")
        self.assertEqual(get_embed_hash(url, 0), "946fb9597a6c74ab3cef1699eff7fde7")
        self.assertEqual(get_embed_hash(url, 1), "427830227a86093b50417e11dbd2f28e")


class TestChooser(WagtailTestUtils, TestCase):
    def setUp(self):
        # login
        self.login()

    def test_chooser(self):
        r = self.client.get("/admin/embeds/chooser/")
        self.assertEqual(r.status_code, 200)

    def test_chooser_with_edit_params(self):
        r = self.client.get("/admin/embeds/chooser/?url=http://example2.com")
        self.assertEqual(r.status_code, 200)
        response_json = json.loads(r.content.decode())
        self.assertEqual(response_json["step"], "chooser")
        self.assertIn('value="http://example2.com"', response_json["html"])

    @patch("wagtail.embeds.embeds.get_embed")
    def test_submit_valid_embed(self, get_embed):
        get_embed.return_value = Embed(
            html='<img src="http://www.example.com" />', title="An example embed"
        )

        response = self.client.post(
            reverse("wagtailembeds:chooser_upload"),
            {"embed-chooser-url": "http://www.example.com/"},
        )
        self.assertEqual(response.status_code, 200)
        response_json = json.loads(response.content.decode())
        self.assertEqual(response_json["step"], "embed_chosen")
        self.assertEqual(response_json["embed_data"]["title"], "An example embed")

    @patch("wagtail.embeds.embeds.get_embed")
    def test_submit_unrecognised_embed(self, get_embed):
        get_embed.side_effect = EmbedNotFoundException

        response = self.client.post(
            reverse("wagtailembeds:chooser_upload"),
            {"embed-chooser-url": "http://www.example.com/"},
        )
        self.assertEqual(response.status_code, 200)

        response_json = json.loads(response.content.decode())
        self.assertEqual(response_json["step"], "chooser")
        self.assertIn("Cannot find an embed for this URL.", response_json["html"])


class TestEmbedly(TestCase):
    @unittest.skipIf(no_embedly, "Embedly is not installed")
    def test_embedly_oembed_called_with_correct_arguments(self):
        with patch("embedly.Embedly.oembed") as oembed:
            oembed.return_value = {"type": "photo", "url": "http://www.example.com"}

            EmbedlyFinder(key="foo").find_embed("http://www.example.com")
            oembed.assert_called_with("http://www.example.com", better=False)

            EmbedlyFinder(key="foo").find_embed("http://www.example.com", max_width=100)
            oembed.assert_called_with(
                "http://www.example.com", maxwidth=100, better=False
            )

    @unittest.skipIf(no_embedly, "Embedly is not installed")
    def test_embedly_401(self):
        with patch("embedly.Embedly.oembed") as oembed:
            oembed.return_value = {
                "type": "photo",
                "url": "http://www.example.com",
                "error": True,
                "error_code": 401,
            }
            self.assertRaises(
                AccessDeniedEmbedlyException,
                EmbedlyFinder(key="foo").find_embed,
                "http://www.example.com",
            )

    @unittest.skipIf(no_embedly, "Embedly is not installed")
    def test_embedly_403(self):
        with patch("embedly.Embedly.oembed") as oembed:
            oembed.return_value = {
                "type": "photo",
                "url": "http://www.example.com",
                "error": True,
                "error_code": 403,
            }
            self.assertRaises(
                AccessDeniedEmbedlyException,
                EmbedlyFinder(key="foo").find_embed,
                "http://www.example.com",
            )

    @unittest.skipIf(no_embedly, "Embedly is not installed")
    def test_embedly_404(self):
        with patch("embedly.Embedly.oembed") as oembed:
            oembed.return_value = {
                "type": "photo",
                "url": "http://www.example.com",
                "error": True,
                "error_code": 404,
            }
            self.assertRaises(
                EmbedNotFoundException,
                EmbedlyFinder(key="foo").find_embed,
                "http://www.example.com",
            )

    @unittest.skipIf(no_embedly, "Embedly is not installed")
    def test_embedly_other_error(self):
        with patch("embedly.Embedly.oembed") as oembed:
            oembed.return_value = {
                "type": "photo",
                "url": "http://www.example.com",
                "error": True,
                "error_code": 999,
            }
            self.assertRaises(
                EmbedlyException,
                EmbedlyFinder(key="foo").find_embed,
                "http://www.example.com",
            )

    @unittest.skipIf(no_embedly, "Embedly is not installed")
    def test_embedly_html_conversion(self):
        with patch("embedly.Embedly.oembed") as oembed:
            oembed.return_value = {"type": "photo", "url": "http://www.example.com"}
            result = EmbedlyFinder(key="foo").find_embed("http://www.example.com")
            self.assertEqual(
                result["html"], '<img src="http://www.example.com" alt="">'
            )

            oembed.return_value = {"type": "something else", "html": "<foo>bar</foo>"}
            result = EmbedlyFinder(key="foo").find_embed("http://www.example.com")
            self.assertEqual(result["html"], "<foo>bar</foo>")

    @unittest.skipIf(no_embedly, "Embedly is not installed")
    def test_embedly_return_value(self):
        with patch("embedly.Embedly.oembed") as oembed:
            oembed.return_value = {"type": "something else", "html": "<foo>bar</foo>"}
            result = EmbedlyFinder(key="foo").find_embed("http://www.example.com")
            self.assertEqual(
                result,
                {
                    "title": "",
                    "author_name": "",
                    "provider_name": "",
                    "type": "something else",
                    "thumbnail_url": None,
                    "width": None,
                    "height": None,
                    "html": "<foo>bar</foo>",
                },
            )

            oembed.return_value = {
                "type": "something else",
                "author_name": "Alice",
                "provider_name": "Bob",
                "title": "foo",
                "thumbnail_url": "http://www.example.com",
                "width": 100,
                "height": 100,
                "html": "<foo>bar</foo>",
            }
            result = EmbedlyFinder(key="foo").find_embed("http://www.example.com")
            self.assertEqual(
                result,
                {
                    "type": "something else",
                    "author_name": "Alice",
                    "provider_name": "Bob",
                    "title": "foo",
                    "thumbnail_url": "http://www.example.com",
                    "width": 100,
                    "height": 100,
                    "html": "<foo>bar</foo>",
                },
            )


class TestOembed(TestCase):
    def test_oembed_invalid_provider(self):
        self.assertRaises(EmbedNotFoundException, OEmbedFinder().find_embed, "foo")

    @responses.activate
    def test_oembed_invalid_request(self):
        # no response set up, so responses will raise a ConnectionError
        self.assertRaises(
            EmbedNotFoundException,
            OEmbedFinder().find_embed,
            "https://www.youtube.com/watch/",
        )

    @responses.activate
    def test_oembed_non_json_response(self):
        responses.get(
            url="https://www.youtube.com/oembed",
            match=[
                matchers.query_param_matcher(
                    {
                        "url": "https://www.youtube.com/watch?v=ReblZ7o7lu4",
                        "format": "json",
                    }
                ),
            ],
            body="foo",
        )
        self.assertRaises(
            EmbedNotFoundException,
            OEmbedFinder().find_embed,
            "https://www.youtube.com/watch?v=ReblZ7o7lu4",
        )

    @responses.activate
    def test_oembed_photo_request(self):
        responses.get(
            url="https://www.youtube.com/oembed",
            match=[
                matchers.query_param_matcher(
                    {"url": "https://www.youtube.com/watch/", "format": "json"}
                ),
            ],
            json={"type": "photo", "url": "http://www.example.com"},
        )
        result = OEmbedFinder().find_embed("https://www.youtube.com/watch/")
        self.assertEqual(result["type"], "photo")
        self.assertEqual(result["html"], '<img src="http://www.example.com" alt="">')

    @responses.activate
    def test_oembed_return_values(self):
        responses.get(
            url="https://www.youtube.com/oembed",
            match=[
                matchers.query_param_matcher(
                    {"url": "https://www.youtube.com/watch/", "format": "json"}
                ),
            ],
            json={
                "type": "something",
                "url": "http://www.example.com",
                "title": "test_title",
                "author_name": "test_author",
                "provider_name": "test_provider_name",
                "thumbnail_url": "test_thumbail_url",
                "width": "test_width",
                "height": "test_height",
                "html": "test_html",
            },
        )
        result = OEmbedFinder().find_embed("https://www.youtube.com/watch/")
        self.assertEqual(
            result,
            {
                "type": "something",
                "title": "test_title",
                "author_name": "test_author",
                "provider_name": "test_provider_name",
                "thumbnail_url": "test_thumbail_url",
                "width": "test_width",
                "height": "test_height",
                "html": "test_html",
            },
        )

    @patch("django.utils.timezone.now")
    @responses.activate
    def test_oembed_cache_until(self, now):
        responses.get(
            url="https://www.youtube.com/oembed",
            match=[
                matchers.query_param_matcher(
                    {"url": "https://www.youtube.com/watch/", "format": "json"}
                ),
            ],
            json={
                "type": "something",
                "url": "http://www.example.com",
                "title": "test_title",
                "author_name": "test_author",
                "provider_name": "test_provider_name",
                "thumbnail_url": "test_thumbail_url",
                "width": "test_width",
                "height": "test_height",
                "html": "test_html",
                "cache_age": 3600,
            },
        )
        now.return_value = make_aware(datetime.datetime(2001, 2, 3))
        result = OEmbedFinder().find_embed("https://www.youtube.com/watch/")
        self.assertEqual(
            result,
            {
                "type": "something",
                "title": "test_title",
                "author_name": "test_author",
                "provider_name": "test_provider_name",
                "thumbnail_url": "test_thumbail_url",
                "width": "test_width",
                "height": "test_height",
                "html": "test_html",
                "cache_until": make_aware(datetime.datetime(2001, 2, 3, hour=1)),
            },
        )

    @patch("django.utils.timezone.now")
    @responses.activate
    def test_oembed_cache_until_as_string(self, now):
        responses.get(
            url="https://www.youtube.com/oembed",
            match=[
                matchers.query_param_matcher(
                    {"url": "https://www.youtube.com/watch/", "format": "json"}
                ),
            ],
            json={
                "type": "something",
                "url": "http://www.example.com",
                "title": "test_title",
                "author_name": "test_author",
                "provider_name": "test_provider_name",
                "thumbnail_url": "test_thumbail_url",
                "width": "test_width",
                "height": "test_height",
                "html": "test_html",
                "cache_age": "3600",
            },
        )
        now.return_value = make_aware(datetime.datetime(2001, 2, 3))
        result = OEmbedFinder().find_embed("https://www.youtube.com/watch/")
        self.assertEqual(
            result,
            {
                "type": "something",
                "title": "test_title",
                "author_name": "test_author",
                "provider_name": "test_provider_name",
                "thumbnail_url": "test_thumbail_url",
                "width": "test_width",
                "height": "test_height",
                "html": "test_html",
                "cache_until": make_aware(datetime.datetime(2001, 2, 3, hour=1)),
            },
        )

    def test_oembed_accepts_known_provider(self):
        finder = OEmbedFinder(providers=[oembed_providers.youtube])
        self.assertTrue(finder.accept("https://www.youtube.com/watch/"))

    def test_oembed_doesnt_accept_unknown_provider(self):
        finder = OEmbedFinder(providers=[oembed_providers.twitter])
        self.assertFalse(finder.accept("https://www.youtube.com/watch/"))

    @responses.activate
    def test_endpoint_with_format_param(self):
        responses.get(
            url="https://www.vimeo.com/api/oembed.json",
            match=[
                matchers.query_param_matcher(
                    {"url": "https://vimeo.com/217403396", "format": "json"}
                ),
            ],
            json={"type": "video", "url": "http://www.example.com"},
        )
        result = OEmbedFinder().find_embed("https://vimeo.com/217403396")
        self.assertEqual(result["type"], "video")


class TestInstagramOEmbed(TestCase):
    def setUp(self):
        class DummyResponse:
            def read(self):
                return b"""{
                    "type": "something",
                    "url": "http://www.example.com",
                    "title": "test_title",
                    "author_name": "test_author",
                    "provider_name": "Instagram",
                    "thumbnail_url": "test_thumbail_url",
                    "width": "test_width",
                    "height": "test_height",
                    "html": "<blockquote class=\\\"instagram-media\\\">Content</blockquote>"
                }"""

        self.dummy_response = DummyResponse()

    def test_instagram_oembed_only_accepts_new_url_patterns(self):
        finder = InstagramOEmbedFinder()
        self.assertTrue(
            finder.accept(
                "https://www.instagram.com/p/CHeRxmnDSYe/?utm_source=ig_embed"
            )
        )
        self.assertTrue(
            finder.accept(
                "https://www.instagram.com/tv/CZMkxGaIXk3/?utm_source=ig_embed"
            )
        )
        self.assertTrue(
            finder.accept(
                "https://www.instagram.com/reel/CZMs3O_I22w/?utm_source=ig_embed"
            )
        )
        self.assertFalse(
            finder.accept("https://instagr.am/p/CHeRxmnDSYe/?utm_source=ig_embed")
        )

    @patch("urllib.request.urlopen")
    def test_instagram_oembed_return_values(self, urlopen):
        urlopen.return_value = self.dummy_response
        result = InstagramOEmbedFinder(app_id="123", app_secret="abc").find_embed(
            "https://instagram.com/p/CHeRxmnDSYe/"
        )
        self.assertEqual(
            result,
            {
                "type": "something",
                "title": "test_title",
                "author_name": "test_author",
                "provider_name": "Instagram",
                "thumbnail_url": "test_thumbail_url",
                "width": "test_width",
                "height": "test_height",
                "html": '<blockquote class="instagram-media">Content</blockquote>',
            },
        )
        # check that a request was made with the expected URL / authentication
        request = urlopen.call_args[0][0]
        self.assertEqual(
            request.get_full_url(),
            "https://graph.facebook.com/v11.0/instagram_oembed?url=https%3A%2F%2Finstagram.com%2Fp%2FCHeRxmnDSYe%2F&format=json",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer 123|abc")

    def test_instagram_request_denied_401(self):
        err = HTTPError(
            "https://instagram.com/p/CHeRxmnDSYe/",
            code=401,
            msg="invalid credentials",
            hdrs={},
            fp=None,
        )
        config = {"side_effect": err}
        with patch.object(urllib.request, "urlopen", **config):
            self.assertRaises(
                AccessDeniedInstagramOEmbedException,
                InstagramOEmbedFinder().find_embed,
                "https://instagram.com/p/CHeRxmnDSYe/",
            )

    def test_instagram_request_not_found(self):
        err = HTTPError(
            "https://instagram.com/p/badrequest/",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        config = {"side_effect": err}
        with patch.object(urllib.request, "urlopen", **config):
            self.assertRaises(
                EmbedNotFoundException,
                InstagramOEmbedFinder().find_embed,
                "https://instagram.com/p/CHeRxmnDSYe/",
            )

    def test_instagram_failed_request(self):
        config = {"side_effect": URLError(reason="Testing error handling")}
        with patch.object(urllib.request, "urlopen", **config):
            self.assertRaises(
                EmbedNotFoundException,
                InstagramOEmbedFinder().find_embed,
                "https://instagram.com/p/CHeRxmnDSYe/",
            )


class TestFacebookOEmbed(TestCase):
    def setUp(self):
        class DummyResponse:
            def read(self):
                return b"""{
                    "type": "something",
                    "url": "http://www.example.com",
                    "title": "test_title",
                    "author_name": "test_author",
                    "provider_name": "Facebook",
                    "width": "test_width",
                    "height": "test_height",
                    "html": "<blockquote class=\\\"facebook-media\\\">Content</blockquote>"
                }"""

        self.dummy_response = DummyResponse()

    def test_facebook_oembed_accepts_various_url_patterns(self):
        finder = FacebookOEmbedFinder()
        self.assertTrue(
            finder.accept("https://www.facebook.com/testuser/posts/10157389310497085")
        )
        self.assertTrue(finder.accept("https://fb.watch/ABC123eew/"))

    @patch("urllib.request.urlopen")
    def test_facebook_oembed_return_values(self, urlopen):
        urlopen.return_value = self.dummy_response
        result = FacebookOEmbedFinder(app_id="123", app_secret="abc").find_embed(
            "https://fb.watch/ABC123eew/"
        )
        self.assertEqual(
            result,
            {
                "type": "something",
                "title": "test_title",
                "author_name": "test_author",
                "provider_name": "Facebook",
                "thumbnail_url": None,
                "width": "test_width",
                "height": "test_height",
                "html": '<blockquote class="facebook-media">Content</blockquote>',
            },
        )
        # check that a request was made with the expected URL / authentication
        request = urlopen.call_args[0][0]
        self.assertEqual(
            request.get_full_url(),
            "https://graph.facebook.com/v11.0/oembed_video?url=https%3A%2F%2Ffb.watch%2FABC123eew%2F&format=json",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer 123|abc")

    def test_facebook_request_denied_401(self):
        err = HTTPError(
            "https://fb.watch/ABC123eew/",
            code=401,
            msg="invalid credentials",
            hdrs={},
            fp=None,
        )
        config = {"side_effect": err}
        with patch.object(urllib.request, "urlopen", **config):
            self.assertRaises(
                AccessDeniedFacebookOEmbedException,
                FacebookOEmbedFinder().find_embed,
                "https://fb.watch/ABC123eew/",
            )

    def test_facebook_request_not_found(self):
        err = HTTPError(
            "https://fb.watch/ABC123eew/", code=404, msg="Not Found", hdrs={}, fp=None
        )
        config = {"side_effect": err}
        with patch.object(urllib.request, "urlopen", **config):
            self.assertRaises(
                EmbedNotFoundException,
                FacebookOEmbedFinder().find_embed,
                "https://fb.watch/ABC123eew/",
            )

    def test_facebook_failed_request(self):
        config = {"side_effect": URLError(reason="Testing error handling")}
        with patch.object(urllib.request, "urlopen", **config):
            self.assertRaises(
                EmbedNotFoundException,
                FacebookOEmbedFinder().find_embed,
                "https://fb.watch/ABC123eew/",
            )


class TestEmbedTag(TestCase):
    @patch("wagtail.embeds.embeds.get_embed")
    def test_direct_call(self, get_embed):
        get_embed.return_value = Embed(html='<img src="http://www.example.com" />')

        result = embed_tag("http://www.youtube.com/watch/")

        self.assertEqual(result, '<img src="http://www.example.com" />')

    @patch("wagtail.embeds.embeds.get_embed")
    def test_call_from_template(self, get_embed):
        get_embed.return_value = Embed(html='<img src="http://www.example.com" />')

        temp = template.Template(
            '{% load wagtailembeds_tags %}{% embed "http://www.youtube.com/watch/" %}'
        )
        result = temp.render(template.Context())

        self.assertEqual(result, '<img src="http://www.example.com" />')

    @patch("wagtail.embeds.embeds.get_embed")
    def test_catches_embed_not_found(self, get_embed):
        get_embed.side_effect = EmbedNotFoundException

        temp = template.Template(
            '{% load wagtailembeds_tags %}{% embed "http://www.youtube.com/watch/" %}'
        )
        result = temp.render(template.Context())

        self.assertEqual(result, "")


class TestEmbedBlock(TestCase):
    def set_up_embed_response(self):
        responses.get(
            url="https://www.youtube.com/oembed",
            match=[
                matchers.query_param_matcher(
                    {"url": "https://www.youtube.com/watch/", "format": "json"}
                ),
            ],
            json={
                "type": "something",
                "url": "http://www.example.com",
                "title": "test_title",
                "width": "640",
                "height": "480",
                "html": "<h1>Hello world!</h1>",
            },
        )

    def test_deserialize(self):
        """
        Deserialising the JSONish value of an EmbedBlock (a URL) should give us an EmbedValue
        for that URL
        """
        block = EmbedBlock(required=False)

        block_val = block.to_python("http://www.example.com/foo")
        self.assertIsInstance(block_val, EmbedValue)
        self.assertEqual(block_val.url, "http://www.example.com/foo")

        # empty values should yield None
        empty_block_val = block.to_python("")
        self.assertIsNone(empty_block_val)

    def test_serialize(self):
        block = EmbedBlock(required=False)

        block_val = EmbedValue("http://www.example.com/foo")
        serialized_val = block.get_prep_value(block_val)
        self.assertEqual(serialized_val, "http://www.example.com/foo")

        serialized_empty_val = block.get_prep_value(None)
        self.assertEqual(serialized_empty_val, "")

    @responses.activate
    def test_render(self):
        self.set_up_embed_response()

        block = EmbedBlock()
        block_val = block.to_python("https://www.youtube.com/watch/")

        temp = template.Template("embed: {{ embed }}")
        context = template.Context({"embed": block_val})
        result = temp.render(context)

        # Check that the embed was in the returned HTML
        self.assertIn("<h1>Hello world!</h1>", result)

    @responses.activate
    def test_render_within_structblock(self):
        """
        When rendering the value of an EmbedBlock directly in a template
        (as happens when accessing it as a child of a StructBlock), the
        proper embed output should be rendered, not the URL.
        """
        self.set_up_embed_response()

        block = blocks.StructBlock(
            [
                ("title", blocks.CharBlock()),
                ("embed", EmbedBlock()),
            ]
        )

        block_val = block.to_python(
            {"title": "A test", "embed": "https://www.youtube.com/watch/"}
        )

        temp = template.Template("embed: {{ self.embed }}")
        context = template.Context({"self": block_val})
        result = temp.render(context)

        self.assertIn("<h1>Hello world!</h1>", result)

    def test_value_from_form(self):
        """
        EmbedBlock should be able to turn a URL submitted as part of a form
        back into an EmbedValue
        """
        block = EmbedBlock(required=False)

        block_val = block.value_from_datadict(
            {"myembed": "http://www.example.com/foo"}, {}, prefix="myembed"
        )
        self.assertIsInstance(block_val, EmbedValue)
        self.assertEqual(block_val.url, "http://www.example.com/foo")

        # empty value should result in None
        empty_val = block.value_from_datadict({"myembed": ""}, {}, prefix="myembed")
        self.assertIsNone(empty_val)

    def test_default(self):
        block1 = EmbedBlock()
        self.assertIsNone(block1.get_default())

        block2 = EmbedBlock(default="")
        self.assertIsNone(block2.get_default())

        block3 = EmbedBlock(default=None)
        self.assertIsNone(block3.get_default())

        block4 = EmbedBlock(default="http://www.example.com/foo")
        self.assertIsInstance(block4.get_default(), EmbedValue)
        self.assertEqual(block4.get_default().url, "http://www.example.com/foo")

        block5 = EmbedBlock(default=EmbedValue("http://www.example.com/foo"))
        self.assertIsInstance(block5.get_default(), EmbedValue)
        self.assertEqual(block5.get_default().url, "http://www.example.com/foo")

        def callable_default():
            return EmbedValue("http://www.example.com/foo")

        block6 = EmbedBlock(default=callable_default)
        self.assertIsInstance(block6.get_default(), EmbedValue)
        self.assertEqual(block6.get_default().url, "http://www.example.com/foo")

    @responses.activate
    def test_clean_required(self):
        self.set_up_embed_response()

        block = EmbedBlock()

        cleaned_value = block.clean(
            block.normalize(EmbedValue("https://www.youtube.com/watch/"))
        )
        self.assertIsInstance(cleaned_value, EmbedValue)
        self.assertEqual(cleaned_value.url, "https://www.youtube.com/watch/")

        with self.assertRaisesMessage(ValidationError, ""):
            block.clean(block.normalize(None))

    @responses.activate
    def test_clean_non_required(self):
        self.set_up_embed_response()

        block = EmbedBlock(required=False)

        cleaned_value = block.clean(
            block.normalize(EmbedValue("https://www.youtube.com/watch/"))
        )
        self.assertIsInstance(cleaned_value, EmbedValue)
        self.assertEqual(cleaned_value.url, "https://www.youtube.com/watch/")

        cleaned_value = block.clean(block.normalize(None))
        self.assertIsNone(cleaned_value)

    @responses.activate
    def test_clean_invalid_url(self):
        # no response set up, so responses will raise a ConnectionError

        non_required_block = EmbedBlock(required=False)

        with self.assertRaises(ValidationError):
            non_required_block.clean(
                non_required_block.normalize(
                    EmbedValue("http://no-oembed-here.com/something")
                )
            )

        required_block = EmbedBlock()

        with self.assertRaises(ValidationError):
            required_block.clean(
                non_required_block.normalize(
                    EmbedValue("http://no-oembed-here.com/something")
                )
            )
