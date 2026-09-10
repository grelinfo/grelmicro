"""Tests for `grelmicro.types`."""

import pytest
from pydantic import (
    AnyUrl,
    BaseModel,
    PostgresDsn,
    RedisDsn,
    ValidationError,
)
from pydantic_core import MultiHostUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

from grelmicro.types import SecretUrl

pytestmark = [pytest.mark.timeout(1)]

REDIS_URL = "redis://app:hunter2@cache:6379/0"
REDIS_SAFE = "redis://app:***@cache:6379/0"
POSTGRES_URL = "postgresql://app:hunter2@a:5432,b:5432/db"
POSTGRES_SAFE = "postgresql://app:***@a:5432,b:5432/db"


class Model(BaseModel):
    """Model carrying every parametrization under test."""

    redis: SecretUrl[RedisDsn] | None = None
    postgres: SecretUrl[PostgresDsn] | None = None
    generic: SecretUrl | None = None
    text: SecretUrl[str] | None = None


class TestDisplay:
    """The credential must never reach a displayed or dumped value."""

    def test_repr_redacts_password(self) -> None:
        """`repr()` shows the URL with the password replaced."""
        model = Model(redis=REDIS_URL)

        assert repr(model.redis) == f"SecretUrl('{REDIS_SAFE}')"
        assert "hunter2" not in repr(model)

    def test_str_redacts_password(self) -> None:
        """`str()` shows the URL with the password replaced."""
        assert str(Model(redis=REDIS_URL).redis) == REDIS_SAFE

    def test_multi_host_redacts_every_password(self) -> None:
        """Each host of a multi-host DSN is redacted."""
        model = Model(postgres=POSTGRES_URL)

        assert str(model.postgres) == POSTGRES_SAFE

    def test_ipv6_multi_host_dsn_stays_parseable(self) -> None:
        """A plain IPv6 host is not mistaken for malformed userinfo."""
        model = Model(
            postgres=("postgresql://[::1]:5432,user:pw@db.example:5433/app")
        )

        rendered = str(model.postgres)
        assert rendered == (
            "postgresql://[::1]:5432,user:***@db.example:5433/app"
        )
        assert MultiHostUrl(rendered).hosts()

    def test_query_credentials_redacted(self) -> None:
        """Credential-like query parameters are redacted."""
        model = Model(generic="https://otlp:4318/v1?api_key=abc&region=eu")

        assert (
            str(model.generic) == "https://otlp:4318/v1?api_key=***&region=eu"
        )

    def test_encoded_query_credential_continuation_is_fully_redacted(
        self,
    ) -> None:
        """A structured URL masks ambiguous encoded credential continuation."""
        model = Model(
            generic=(
                "https://example.test/callback?access_token=FIRST%26part=SECOND"
            )
        )

        assert str(model.generic) == (
            "https://example.test/callback?access_token=***"
        )
        assert "FIRST" not in repr(model.generic)
        assert "SECOND" not in model.model_dump_json()

    @pytest.mark.parametrize(
        "key",
        [
            "db_password",
            "x-api-key",
            "private_key",
            "accessToken",
            "clientSecret",
        ],
    )
    def test_qualified_query_credentials_redacted(self, key: str) -> None:
        """Qualified and camel-case credential names stay out of displays."""
        model = Model(generic=f"https://otlp:4318/v1?{key}=sensitive&region=eu")

        assert str(model.generic) == (
            f"https://otlp:4318/v1?{key}=***&region=eu"
        )

    def test_url_without_credentials_stays_readable(self) -> None:
        """A URL with nothing to hide is displayed in full."""
        model = Model(generic="https://otlp.example.com:4318/v1/traces")

        assert str(model.generic) == "https://otlp.example.com:4318/v1/traces"


class TestSerialization:
    """Dumping in either mode must not leak the credential."""

    def test_model_dump_json_redacts(self) -> None:
        """`model_dump_json()` emits the redacted URL."""
        payload = Model(redis=REDIS_URL).model_dump_json()

        assert "hunter2" not in payload
        assert REDIS_SAFE in payload

    def test_model_dump_does_not_leak(self) -> None:
        """`model_dump()` keeps the wrapper, so printing it stays safe."""
        dumped = Model(redis=REDIS_URL).model_dump()

        assert "hunter2" not in repr(dumped)

    def test_model_dump_round_trips(self) -> None:
        """A python-mode dump revalidates back to the real URL.

        `reconfigure_from_mapping` rebuilds a config this way, so the
        credential has to survive the round trip.
        """
        model = Model(redis=REDIS_URL)

        reloaded = Model.model_validate(model.model_dump())

        assert reloaded.redis is not None
        assert str(reloaded.redis.get_secret_value()) == REDIS_URL

    def test_json_schema_marks_write_only(self) -> None:
        """The generated schema flags the field as write-only."""
        schema = Model.model_json_schema()["properties"]["generic"]

        assert schema["anyOf"][0]["writeOnly"] is True


class TestAccess:
    """`get_secret_value` is the only way back to the credential."""

    def test_get_secret_value_returns_parsed_url(self) -> None:
        """The unwrapped value keeps its parametrized type."""
        model = Model(redis=REDIS_URL)

        assert model.redis is not None
        value = model.redis.get_secret_value()

        assert isinstance(value, RedisDsn)
        assert value.unicode_string() == REDIS_URL


class TestValidation:
    """Parametrizing must keep the inner type's validation."""

    def test_parametrized_type_rejects_wrong_scheme(self) -> None:
        """`SecretUrl[RedisDsn]` refuses a non-Redis scheme."""
        with pytest.raises(ValidationError):
            Model(redis="https://example.com")

    def test_bare_type_validates_as_any_url(self) -> None:
        """An unparametrized `SecretUrl` still requires a valid URL."""
        with pytest.raises(ValidationError):
            Model(generic="not-a-url")

    def test_bare_type_parses_to_any_url(self) -> None:
        """An unparametrized `SecretUrl` carries an `AnyUrl`."""
        model = Model(generic="https://example.com")

        assert model.generic is not None
        assert isinstance(model.generic.get_secret_value(), AnyUrl)

    def test_str_parametrization_accepts_host_port(self) -> None:
        """`SecretUrl[str]` takes the scheme-less OTLP gRPC endpoint form."""
        model = Model(text="localhost:4318")

        assert model.text is not None
        assert model.text.get_secret_value() == "localhost:4318"


class TestSettings:
    """The environment path must wrap the value the same way."""

    def test_env_value_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A URL read from the environment is wrapped and redacted."""
        monkeypatch.setenv("TEST_URL", REDIS_URL)

        class Settings(BaseSettings):
            model_config = SettingsConfigDict(
                env_prefix="TEST_", extra="ignore"
            )

            url: SecretUrl[RedisDsn] | None = None

        settings = Settings()

        assert settings.url is not None
        assert str(settings.url) == REDIS_SAFE
        assert settings.url.get_secret_value().unicode_string() == REDIS_URL


class TestFailClosed:
    """A value the redactor cannot reason about must be masked whole."""

    def test_non_url_inner_type_is_fully_masked(self) -> None:
        """A parametrization that is not URL-shaped shows nothing at all."""

        class Weird(BaseModel):
            value: SecretUrl[dict[str, str]] | None = None

        weird = Weird(value={"password": "hunter2"})

        assert "hunter2" not in repr(weird)
        assert str(weird.value) == "**********"

    def test_scheme_less_endpoint_with_userinfo_is_redacted(self) -> None:
        """The `user:password@host:port` endpoint form is still redacted."""
        model = Model(text="user:hunter2@collector:4317")

        assert "hunter2" not in repr(model)
        assert str(model.text) == "user:***@collector:4317"

    def test_scheme_less_password_with_colons_is_fully_redacted(self) -> None:
        """Every segment after the first userinfo colon is password material."""
        model = Model(text="user:PART1:PART2@collector:4317")

        assert "PART1" not in repr(model)
        assert "PART2" not in repr(model)
        assert str(model.text) == "user:***@collector:4317"

    def test_scheme_less_endpoint_with_query_credential_is_redacted(
        self,
    ) -> None:
        """A query on a non-URL endpoint is redacted by the fallback path."""
        model = Model(text="collector/path?accessToken=sensitive")

        assert "sensitive" not in repr(model)
        assert str(model.text) == "collector/path?accessToken=***"

    def test_scheme_less_userinfo_and_query_are_both_redacted(self) -> None:
        """Structured query rebuilding cannot expose path-like userinfo."""
        model = Model(
            text="user:hunter2@collector:4317/path?accessToken=sensitive"
        )

        assert "hunter2" not in repr(model)
        assert "sensitive" not in repr(model)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (
                "https://example/#/callback?state=x;token=SECRET",
                "https://example/#/callback?state=x;token=***",
            ),
            (
                "https://example/?state=x/access_token=SECRET",
                "https://example/?state=x/access_token=***",
            ),
            (
                "https://example/#/callback?state=x/access_token=SECRET",
                "https://example/#/callback?state=x/access_token=***",
            ),
            (
                "https://example/?state=x/access%5Ftoken=SECRET",
                "https://example/?state=x/access_token=***",
            ),
            (
                "https://example/?state=x;token=SECRET",
                "https://example/?state=x;token=***",
            ),
            (
                "https://example/#/callback/access%5Ftoken=SECRET",
                "https://example/#/callback/access_token=***",
            ),
            (
                "https://example.test/?state=x%2Faccess%5Ftoken=SECRET",
                "https://example.test/?state=x%2Faccess_token=***",
            ),
            (
                "https://example.test/#/callback?state=x%3btoken=SECRET",
                "https://example.test/#/callback?state=x%3btoken=***",
            ),
            (
                "https://example.test/#/callback?state=x%26token=SECRET",
                "https://example.test/#/callback?state=x%26token=***",
            ),
        ],
    )
    def test_string_url_assignment_variants_are_redacted(
        self, value: str, expected: str
    ) -> None:
        """String endpoints mask separator and encoded credential variants."""
        model = Model(text=value)

        assert str(model.text) == expected
        assert "SECRET" not in repr(model.text)
        assert "SECRET" not in model.model_dump_json()

    @pytest.mark.parametrize(
        "value",
        [
            "https://example.test/callback?access_token=FIRST%26part=SECOND",
            "https://example.test/#access_token=FIRST%3bpart=SECOND",
            "https://example.test/#/callback?token=FIRST%2Fpart=SECOND",
        ],
    )
    def test_encoded_credential_continuation_is_hidden_everywhere(
        self, value: str
    ) -> None:
        """String, repr, and JSON mask the whole ambiguous credential value."""
        model = Model(text=value)

        assert "FIRST" not in str(model.text)
        assert "SECOND" not in str(model.text)
        assert "FIRST" not in repr(model.text)
        assert "SECOND" not in repr(model.text)
        assert "FIRST" not in model.model_dump_json()
        assert "SECOND" not in model.model_dump_json()

    @pytest.mark.parametrize(
        ("value", "secret"),
        [
            (
                "https://example.test/#state=ok%3Faccess_token=FRAGMENT_SECRET",
                "FRAGMENT_SECRET",
            ),
            (
                "https://example.test/?redirect=callback%3Faccess_token=QUERY_SECRET",
                "QUERY_SECRET",
            ),
        ],
    )
    def test_encoded_question_mark_credentials_are_hidden_everywhere(
        self, value: str, secret: str
    ) -> None:
        """Nested redirect credentials stay out of every display sink."""
        model = Model(text=value)

        assert secret not in str(model.text)
        assert secret not in repr(model)
        assert secret not in repr(model.model_dump())
        assert secret not in model.model_dump_json()

    def test_protocol_relative_userinfo_and_query_are_both_redacted(
        self,
    ) -> None:
        """A network-path endpoint masks its password and query credential."""
        model = Model(
            text="//user:hunter2@collector:4317/path?accessToken=sensitive"
        )

        assert "hunter2" not in repr(model)
        assert "sensitive" not in repr(model)

    def test_malformed_scheme_userinfo_is_redacted(self) -> None:
        """Malformed userinfo and parameters remain secret in every display."""
        model = Model(
            text=(
                "http:/user:PART1@PART2@bad host/path"
                "?accessToken=QUERYSECRET#access_token=FRAGSECRET"
            )
        )

        rendered = str(model.text)
        assert "PART1" not in repr(model)
        assert "PART2" not in rendered
        assert "QUERYSECRET" not in rendered
        assert "FRAGSECRET" not in rendered
        assert rendered == (
            "http:/user:***@bad host/path?accessToken=***#access_token=***"
        )

    def test_malformed_username_at_sign_is_redacted(self) -> None:
        """An extra at sign before the password cannot evade redaction."""
        model = Model(text="http:/user@realm:PWSECRET@bad host/path")

        assert "PWSECRET" not in repr(model)
        assert str(model.text) == "http:/user@realm:***@bad host/path"

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://example.test/callback#access_token=TOKENVALUE",
            "https://example.test/#access_token=TOKENVALUE?state=x",
            "https://example.test/#/callback/token=TOKENVALUE?state=x",
            "collector/path#accessToken=TOKENVALUE",
            "collector/path#/callback?clientSecret=TOKENVALUE",
        ],
    )
    def test_fragment_credential_is_redacted(self, endpoint: str) -> None:
        """OAuth-style fragment parameters never appear in display output."""
        model = Model(text=endpoint)

        assert "TOKENVALUE" not in repr(model)
        assert "TOKENVALUE" not in str(model.text)
