"""Tests for RPlay API client module."""

from unittest.mock import MagicMock, patch

import pytest
from requests.exceptions import ConnectionError, HTTPError, JSONDecodeError, Timeout

from core.rplay import (
    RPlayAPI,
    RPlayAPIError,
    RPlayAuthError,
    RPlayConnectionError,
)
from models.rplay import CreatorStreamState


@pytest.fixture(autouse=True)
def _skip_token_renewal(monkeypatch):
    """These tests cover request handling, not JWT renewal (see test_token_refresh)."""
    monkeypatch.setattr(RPlayAPI, "_ensure_valid_token", lambda self: None)


@pytest.fixture
def api():
    return RPlayAPI(
        base_url="https://api.rplay.live", user_oid="test", refresh_token="test"
    )


def ok_response(body):
    """A successful response whose JSON body is `body`."""
    response = MagicMock()
    response.json.return_value = body
    return response


def stub_get(api, **kwargs):
    """Replace the HTTP GET the client makes. Pass return_value or side_effect."""
    return patch.object(api._session, "get", **kwargs)


def status_response(status_code, body=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    return response


LIVE_URLS = {
    "mode": "signed",
    "playUrls": {
        "hd": "https://play.example/live/c1_abrhd.flv?sign=s&expire=1",
        "sd": "https://play.example/live/c1_abrsd.flv?sign=s&expire=1",
    },
}
OFFLINE_URLS = {"mode": None, "playUrls": None}
METADATA = {
    "_id": "stream-doc",
    "oid": "stream-doc",
    "creatorOid": "c1",
    "title": "Test Stream",
    "hashtags": ["tag"],
    "isAdultContent": False,
    # Masked without a browser pass; the client must not rely on it.
    "streamState": "offline",
    "streamStartTime": None,
}


def route(play_urls=OFFLINE_URLS, metadata=METADATA, play_status=200):
    """GET stub dispatching on path, like the real API."""

    def _get(url, **kwargs):
        if url.endswith("/live/bytedance/play-urls"):
            return status_response(play_status, play_urls)
        if url.endswith("/live/play"):
            return status_response(200, metadata)
        raise AssertionError(f"unexpected GET {url}")

    return _get


class TestGetLivestreamStatus:
    """Live detection via playback URLs, since the public list needs a browser check."""

    def test_uses_custom_base_url(self):
        api = RPlayAPI(
            base_url="https://api.example.com/", user_oid="test", refresh_token="test"
        )

        with stub_get(api, side_effect=route()) as mock_get:
            api.get_livestream_status(["c1"])

        assert (
            mock_get.call_args.args[0]
            == "https://api.example.com/live/bytedance/play-urls"
        )

    def test_live_creator_is_reported_with_metadata(self, api):
        with stub_get(api, side_effect=route(LIVE_URLS)):
            streams = api.get_livestream_status(["c1"])

        assert len(streams) == 1
        assert streams[0].creator_oid == "c1"
        assert streams[0].title == "Test Stream"
        assert streams[0].oid == "stream-doc"
        assert streams[0].stream_state.value == "live"

    def test_offline_creator_is_not_reported_or_looked_up(self, api):
        with stub_get(api, side_effect=route()) as mock_get:
            assert api.get_livestream_status(["c1"]) == []

        assert mock_get.call_count == 1

    def test_start_time_is_stable_while_live(self, api):
        with stub_get(api, side_effect=route(LIVE_URLS)):
            first = api.get_livestream_status(["c1"])[0].stream_start_time
            second = api.get_livestream_status(["c1"])[0].stream_start_time

        assert first == second

    def test_single_missed_poll_keeps_start_time(self, api):
        with stub_get(api, side_effect=route(LIVE_URLS)):
            first = api.get_livestream_status(["c1"])[0].stream_start_time
        with stub_get(api, side_effect=route()):
            api.get_livestream_status(["c1"])
        with stub_get(api, side_effect=route(LIVE_URLS)):
            again = api.get_livestream_status(["c1"])[0].stream_start_time

        assert again == first

    def test_confirmed_offline_starts_a_new_stream(self, api):
        with stub_get(api, side_effect=route(LIVE_URLS)):
            api.get_livestream_status(["c1"])
        with stub_get(api, side_effect=route()):
            api.get_livestream_status(["c1"])
            api.get_livestream_status(["c1"])

        assert "c1" not in api._live_since

    def test_playback_denied_skips_creator_without_auth_error(self, api):
        with stub_get(api, side_effect=route(play_status=403)):
            assert api.get_livestream_status(["c1"]) == []

    def test_unauthorized_raises_auth_error(self, api):
        with stub_get(api, side_effect=route(play_status=401)):
            with pytest.raises(RPlayAuthError):
                api.get_livestream_status(["c1"])

    def test_timeout_raises_connection_error(self, api):
        with stub_get(api, side_effect=Timeout()), patch("time.sleep"):
            with pytest.raises(RPlayConnectionError, match="timed out"):
                api.get_livestream_status(["c1"])

    def test_connection_error_raises_connection_error(self, api):
        with stub_get(api, side_effect=ConnectionError("down")), patch("time.sleep"):
            with pytest.raises(RPlayConnectionError, match="Connection failed"):
                api.get_livestream_status(["c1"])

    def test_http_error_raises_api_error(self, api):
        mock_response = status_response(400)
        mock_response.raise_for_status.side_effect = HTTPError("400 Bad Request")

        with stub_get(api, return_value=mock_response):
            with pytest.raises(RPlayAPIError, match="HTTP error"):
                api.get_livestream_status(["c1"])


class TestGetStreamUrl:
    """get_stream_url returns a fresh playback URL from play-urls."""

    def test_signed_mode_returns_hd_url(self, api):
        with stub_get(api, side_effect=route(LIVE_URLS)):
            url = api.get_stream_url("c1")

        assert url == LIVE_URLS["playUrls"]["hd"]

    def test_falls_back_to_lower_quality(self, api):
        urls = {"mode": "signed", "playUrls": {"hd": None, "sd": "https://p/sd.flv"}}
        with stub_get(api, side_effect=route(urls)):
            assert api.get_stream_url("c1") == "https://p/sd.flv"

    def test_legacy_mode_appends_viewer_params(self, api):
        api.auth_token = "jwt"
        urls = {"mode": "legacy", "playUrls": {"hd": "https://p/live/c1.m3u8"}}
        with stub_get(api, side_effect=route(urls)):
            url = api.get_stream_url("c1")

        assert url.startswith("https://p/live/c1.m3u8?")
        assert "creatorId=c1" in url and "token=jwt" in url
        assert url.endswith("&resolution=hd")

    def test_not_live_raises_api_error(self, api):
        with stub_get(api, side_effect=route()):
            with pytest.raises(RPlayAPIError, match="not live"):
                api.get_stream_url("c1")

    def test_playback_denied_raises_api_error(self, api):
        with stub_get(api, side_effect=route(play_status=403)):
            with pytest.raises(RPlayAPIError, match="denied"):
                api.get_stream_url("c1")


class TestValidateCredentials:
    """Tests for the public credential-validation seam."""

    def test_auth_failure_raises_auth_error(self, api):
        """Test 401 from key2 raises RPlayAuthError without API-layer ERROR log."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with stub_get(api, return_value=mock_response):
            with patch.object(api.logger, "error") as mock_error:
                with pytest.raises(RPlayAuthError, match="Authentication failed"):
                    api.validate_credentials()

        mock_error.assert_not_called()


class TestGetStreamKey:
    """Tests for _get_stream_key method."""

    def test_successful_key_retrieval(self, api):
        """Test successful stream key retrieval."""
        with stub_get(api, return_value=ok_response({"authKey": "my_stream_key"})):
            key = api._get_stream_key()

        assert key == "my_stream_key"

    @pytest.mark.parametrize(
        "body", [{"authKey": None}, {"authKey": ""}, {"other": "data"}]
    )
    def test_null_empty_or_missing_auth_key_raises_auth_error(self, api, body):
        with stub_get(api, return_value=ok_response(body)):
            with pytest.raises(RPlayAuthError, match="Invalid authentication"):
                api._get_stream_key()

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_auth_error(self, api, status):
        """Test that 401 and 403 responses raise RPlayAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = status
        http_error = HTTPError(f"{status} error")
        http_error.response = mock_response
        mock_response.raise_for_status.side_effect = http_error

        with stub_get(api, return_value=mock_response):
            with pytest.raises(RPlayAuthError, match="Authentication failed"):
                api._get_stream_key()

    def test_timeout_raises_connection_error(self, api):
        """Test that timeout raises RPlayConnectionError."""
        with stub_get(api, side_effect=Timeout()):
            with pytest.raises(RPlayConnectionError, match="timed out"):
                api._get_stream_key()

    def test_json_decode_error_raises_api_error(self, api):
        """Test malformed JSON body raises RPlayAPIError, not the raw decode error."""
        mock_response = MagicMock()
        mock_response.json.side_effect = JSONDecodeError("Expecting value", "doc", 0)

        with stub_get(api, return_value=mock_response):
            with pytest.raises(RPlayAPIError, match="Unexpected error"):
                api._get_stream_key()

    def test_unexpected_exception_does_not_leak_secret(self, api, caplog):
        """Exception messages may embed Authorization; must not reach logs or RPlayAPIError."""
        secret = "Bearer sekrit-token"

        with stub_get(api, side_effect=RuntimeError(secret)):
            with caplog.at_level("ERROR"):
                with pytest.raises(RPlayAPIError) as exc_info:
                    api._get_stream_key()

        assert secret not in str(exc_info.value)
        assert all(secret not in record.getMessage() for record in caplog.records)
        assert "RuntimeError" in str(exc_info.value)


class TestCreatorStreamState:
    """Tests for CreatorStreamState dataclass."""

    def test_default_initialization(self):
        """Test CreatorStreamState default values."""
        state = CreatorStreamState()
        assert state.is_current_stream_blocked is False

    def test_mark_blocked(self):
        """Test mark_blocked sets the blocked flag."""
        state = CreatorStreamState()
        state.mark_blocked()
        assert state.is_current_stream_blocked is True


class TestTransientRetry:
    """Tests for retrying transient failures on the surviving API calls."""

    def test_get_livestream_status_retries_transient_connection_errors(self, api):
        """Test transient API connection failures are retried before succeeding."""
        with (
            stub_get(
                api,
                side_effect=[
                    ConnectionError("boom"),
                    ConnectionError("boom"),
                    status_response(200, OFFLINE_URLS),
                ],
            ) as mock_get,
            patch("time.sleep") as mock_sleep,
        ):
            streams = api.get_livestream_status(["c1"])

        assert streams == []
        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2

    def test_get_stream_key_retries_transient_connection_errors(self, api):
        """Test transient key-fetch failures are retried before succeeding."""
        with (
            stub_get(
                api,
                side_effect=[
                    ConnectionError("boom"),
                    ConnectionError("boom"),
                    ok_response({"authKey": "my_stream_key"}),
                ],
            ) as mock_get,
            patch("time.sleep") as mock_sleep,
        ):
            key = api._get_stream_key()

        assert key == "my_stream_key"
        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2
