"""Tests for `grelmicro.types`."""

import pytest
from pydantic import (
    AnyUrl,
    BaseModel,
    PostgresDsn,
    RedisDsn,
    ValidationError,
)
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

    def test_query_credentials_redacted(self) -> None:
        """Credential-like query parameters are redacted."""
        model = Model(generic="https://otlp:4318/v1?api_key=abc&region=eu")

        assert (
            str(model.generic) == "https://otlp:4318/v1?api_key=***&region=eu"
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
