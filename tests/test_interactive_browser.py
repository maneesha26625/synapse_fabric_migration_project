"""The interactive browser sign-in: one window, kept safely, never repeated needlessly.

Offline. A fake SDK credential stands in for ``InteractiveBrowserCredential``,
so no browser ever opens. What cannot be established here -- that Microsoft's
window really opens, that the redirect to localhost completes, that the OS
keyring accepts the cache -- needs a person at a real machine.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from discovery_agent.connections import azure as azure_module
from discovery_agent.connections.azure import (
    ARM_SCOPE,
    DEVELOPER_SIGN_IN_CLIENT_ID,
    INTERACTIVE_CACHE_NAME,
    INTERACTIVE_TIMEOUT_SECONDS,
    MAX_HELD_CREDENTIALS,
    SQL_SCOPE,
    SYNAPSE_SCOPE,
    AzureCliCredentialProvider,
    InteractiveBrowserCredentialProvider,
    auth_record_path,
    credential_provider,
    credential_provider_for,
    forget_auth_record,
    load_auth_record,
    reset_credentials,
    save_auth_record,
)
from discovery_agent.connections.models import AzureConnectionConfig, CredentialMethod
from discovery_agent.errors import AzureAuthenticationError

identity = pytest.importorskip("azure.identity")

TENANT = "72f988bf-0000-0000-0000-000000000000"
OTHER_TENANT = "8a24d8ed-7a4b-45b3-b56b-d781dd225aa1"
CLIENT = "11111111-2222-3333-4444-555555555555"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
FAKE_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJodHRwcyJ9.c2lnbmF0dXJl"


def record_for(tenant=TENANT, client=DEVELOPER_SIGN_IN_CLIENT_ID):
    return identity.AuthenticationRecord(
        tenant_id=tenant,
        client_id=client,
        authority="login.microsoftonline.com",
        home_account_id=f"00000000-0000-0000-0000-000000000001.{tenant}",
        username="operator@contoso.example",
    )


class FakeToken:
    def __init__(self, token, expires_on):
        self.token = token
        self.expires_on = expires_on


class FakeBrowserCredential:
    """Counts windows. ``authenticate`` is the window; ``get_token`` is silent after it."""

    def __init__(self, record=None, authenticate_raises=None, get_token_raises=None, delay=0.0):
        self.record = record or record_for()
        self.authenticate_raises = authenticate_raises
        self.get_token_raises = get_token_raises or {}
        self.delay = delay
        self.authenticate_calls = 0
        self.scopes = []
        self._lock = threading.Lock()

    def authenticate(self, **kwargs):
        with self._lock:
            self.authenticate_calls += 1
        time.sleep(self.delay)
        if self.authenticate_raises is not None:
            raise self.authenticate_raises
        return self.record

    def get_token(self, *scopes, **kwargs):
        with self._lock:
            self.scopes.append(scopes[0])
        if scopes[0] in self.get_token_raises:
            raise self.get_token_raises[scopes[0]]
        return FakeToken(FAKE_TOKEN, int(time.time()) + 3600)


def browser(credential=None, **kwargs):
    return InteractiveBrowserCredentialProvider(
        tenant_id=TENANT, credential=credential or FakeBrowserCredential(), **kwargs
    )


# --- what is sent to the SDK -------------------------------------------------


def test_the_provider_sends_timeout_tenant_and_client_and_never_a_login_hint():
    args = InteractiveBrowserCredentialProvider(tenant_id=TENANT, client_id=CLIENT)._sdk_arguments()

    assert args["timeout"] == INTERACTIVE_TIMEOUT_SECONDS == 120
    assert args["tenant_id"] == TENANT
    assert args["client_id"] == CLIENT
    assert "login_hint" not in args
    assert not any("secret" in k or "password" in k for k in args)


def test_the_cache_is_named_encrypted_only_and_the_stored_record_is_offered_back():
    save_auth_record(record_for())
    args = InteractiveBrowserCredentialProvider(tenant_id=TENANT)._sdk_arguments()

    options = args["cache_persistence_options"]
    assert options.name == INTERACTIVE_CACHE_NAME
    assert options.allow_unencrypted_storage is False
    assert args["authentication_record"].username == "operator@contoso.example"


def test_a_record_for_another_tenant_or_application_is_not_offered():
    save_auth_record(record_for(tenant=OTHER_TENANT))
    assert "authentication_record" not in InteractiveBrowserCredentialProvider(tenant_id=TENANT)._sdk_arguments()

    save_auth_record(record_for(client=CLIENT))
    assert "authentication_record" not in InteractiveBrowserCredentialProvider(tenant_id=TENANT)._sdk_arguments()
    assert "authentication_record" in InteractiveBrowserCredentialProvider(
        tenant_id=TENANT, client_id=CLIENT
    )._sdk_arguments()


def test_without_a_keyring_it_falls_back_to_memory_and_never_to_plaintext(monkeypatch):
    built = []

    class NoKeyring:
        def __init__(self, **kwargs):
            options = kwargs.get("cache_persistence_options")
            assert options is None or options.allow_unencrypted_storage is False
            built.append(kwargs)
            if options is not None:
                raise ValueError("Cache encryption is impossible because libsecret ...")

    monkeypatch.setattr(identity, "InteractiveBrowserCredential", NoKeyring)
    provider = InteractiveBrowserCredentialProvider(tenant_id=TENANT)
    provider._build_credential()

    assert "cache_persistence_options" in built[0]
    assert "cache_persistence_options" not in built[-1]
    assert provider._persistent is False


def test_a_cache_that_fails_on_first_use_is_retried_in_memory(monkeypatch):
    """azure-identity opens its persistent cache lazily, on the first request."""
    built = []

    class LazyFailure(FakeBrowserCredential):
        def __init__(self, **kwargs):
            super().__init__()
            self.persistent = "cache_persistence_options" in kwargs
            built.append(self)

        def authenticate(self, **kwargs):
            if self.persistent:
                raise ValueError("Cache encryption is impossible because libsecret ...")
            return super().authenticate(**kwargs)

    monkeypatch.setattr(identity, "InteractiveBrowserCredential", LazyFailure)
    provider = InteractiveBrowserCredentialProvider(tenant_id=TENANT)

    assert provider.token(ARM_SCOPE).value == FAKE_TOKEN
    assert [c.persistent for c in built] == [True, False]
    assert built[-1].authenticate_calls == 1


# --- the authentication record -----------------------------------------------


def test_the_record_file_names_the_account_and_contains_no_token():
    provider = browser()
    provider.token(ARM_SCOPE)

    text = auth_record_path().read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text
    assert not any("token" in key.lower() for key in json.loads(text))
    assert json.loads(text)["username"] == "operator@contoso.example"


def test_an_unreadable_record_costs_one_prompt_not_a_crash(monkeypatch):
    path = auth_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert load_auth_record() is None

    sdk = FakeBrowserCredential()
    built = []
    monkeypatch.setattr(identity, "InteractiveBrowserCredential", lambda **kw: built.append(kw) or sdk)
    InteractiveBrowserCredentialProvider(tenant_id=TENANT).token(ARM_SCOPE)

    assert "authentication_record" not in built[0]
    assert sdk.authenticate_calls == 1
    assert load_auth_record().username == "operator@contoso.example"  # replaced by a good one


def test_a_stored_record_means_a_silent_sign_in(monkeypatch):
    save_auth_record(record_for())
    sdk = FakeBrowserCredential()
    monkeypatch.setattr(identity, "InteractiveBrowserCredential", lambda **kw: sdk)

    InteractiveBrowserCredentialProvider(tenant_id=TENANT).token(ARM_SCOPE)

    assert sdk.authenticate_calls == 0
    assert sdk.scopes == [ARM_SCOPE]


def test_only_the_remembering_browser_method_writes_a_record():
    AzureCliCredentialProvider(credential=FakeBrowserCredential()).token(ARM_SCOPE)
    assert not auth_record_path().exists()

    browser(remember=False).token(ARM_SCOPE)
    assert not auth_record_path().exists()

    browser().token(ARM_SCOPE)
    assert auth_record_path().exists()


def test_forgetting_deletes_the_record_and_reset_drops_held_identities():
    held = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT)
    save_auth_record(record_for())

    assert forget_auth_record() is True
    assert not auth_record_path().exists()
    assert forget_auth_record() is False
    reset_credentials()
    assert credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT) is not held


# --- one window, at most once ------------------------------------------------


def test_four_audiences_acquired_concurrently_open_exactly_one_window():
    sdk = FakeBrowserCredential(delay=0.2)
    provider = browser(sdk)
    scopes = [ARM_SCOPE, SYNAPSE_SCOPE, SQL_SCOPE, FABRIC_SCOPE]
    barrier = threading.Barrier(len(scopes))
    results, errors = {}, []

    def acquire(scope):
        barrier.wait()
        try:
            results[scope] = provider.token(scope).value
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(s,)) for s in scopes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert not errors
    assert sdk.authenticate_calls == 1
    assert sorted(results) == sorted(scopes)


def test_a_refused_sign_in_is_not_retried_for_the_other_audiences_of_the_run():
    sdk = FakeBrowserCredential(authenticate_raises=RuntimeError("Timed out waiting for the user"))
    provider = browser(sdk)

    with pytest.raises(AzureAuthenticationError, match="within 120 seconds"):
        provider.token(ARM_SCOPE)
    for scope in (SYNAPSE_SCOPE, SQL_SCOPE):
        with pytest.raises(AzureAuthenticationError):
            provider.token(scope)
    assert sdk.authenticate_calls == 1


def test_a_later_separate_attempt_does_try_again():
    sdk = FakeBrowserCredential(authenticate_raises=RuntimeError("access_denied"))
    provider = browser(sdk)
    with pytest.raises(AzureAuthenticationError):
        provider.token(ARM_SCOPE)

    sdk.authenticate_raises = None
    provider.start_attempt()

    assert provider.token(ARM_SCOPE).value == FAKE_TOKEN
    assert sdk.authenticate_calls == 2
    assert auth_record_path().exists()


def test_a_failure_after_one_audience_worked_is_not_memoised():
    sdk = FakeBrowserCredential(get_token_raises={SQL_SCOPE: RuntimeError("AADSTS65001 consent")})
    provider = browser(sdk)
    provider.token(ARM_SCOPE)

    with pytest.raises(AzureAuthenticationError):
        provider.token(SQL_SCOPE)
    with pytest.raises(AzureAuthenticationError):
        provider.token(SQL_SCOPE)
    assert sdk.scopes.count(SQL_SCOPE) == 2  # asked again, not answered from memory
    assert sdk.authenticate_calls == 1


# --- the credential registry -------------------------------------------------


def test_the_same_identity_gets_the_same_provider_across_requests():
    first = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT)
    again = credential_provider_for(
        AzureConnectionConfig(
            subscription_id="e65a4ad3-3054-4391-947c-2a601d0bbf99",
            tenant_id=TENANT,
            credential_method=CredentialMethod.INTERACTIVE_BROWSER,
        )
    )
    assert first is again


def test_a_different_tenant_or_client_gets_a_different_provider():
    base = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT)
    assert credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=OTHER_TENANT) is not base
    other_app = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT, client_id=CLIENT)
    assert other_app is not base
    assert other_app.client_id == CLIENT


def test_injected_and_keep_nothing_providers_are_never_held():
    injected = credential_provider(
        CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT, credential=FakeBrowserCredential()
    )
    fresh = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT, remember=False)
    held = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT)

    assert held is not injected and held is not fresh
    assert fresh.remember is False and fresh.timeout_seconds == 300
    assert credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=TENANT, remember=False) is not fresh


def test_the_registry_is_bounded():
    tenants = [f"00000000-0000-0000-0000-{i:012d}" for i in range(MAX_HELD_CREDENTIALS + 2)]
    first = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=tenants[0])
    for tenant in tenants[1:]:
        credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=tenant)

    assert len(azure_module._HELD) == MAX_HELD_CREDENTIALS
    assert credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=tenants[0]) is not first


def test_the_config_carries_a_client_id_and_it_is_not_a_secret():
    config = AzureConnectionConfig(
        subscription_id="e65a4ad3-3054-4391-947c-2a601d0bbf99",
        tenant_id=TENANT,
        credential_method=CredentialMethod.INTERACTIVE_BROWSER,
        client_id=CLIENT,
    )
    assert config.to_dict()["client_id"] == CLIENT
    assert credential_provider_for(config).client_id == CLIENT


# --- guidance ----------------------------------------------------------------


@pytest.mark.parametrize(
    "message, expected",
    [
        ("AADSTS50020: User account from identity provider does not exist in tenant", "Use another account"),
        ("access_denied: the user cancelled", "ask the tenant's administrator"),
        ("AADSTS50011: The redirect URI specified in the request does not match", "Mobile and desktop applications"),
        ("Timed out waiting for authentication", "machine running the server"),
        ("Failed to open a browser", "headless host"),
        ("no display available", "headless host"),
        ("something nobody anticipated", "complete the sign-in within 120 seconds"),
    ],
)
def test_each_failure_maps_to_its_specific_fix(message, expected):
    assert expected in InteractiveBrowserCredentialProvider.guidance_for(RuntimeError(message))


def test_the_guidance_reaches_the_error_the_operator_sees():
    sdk = FakeBrowserCredential(authenticate_raises=RuntimeError("AADSTS50020 ... does not exist in tenant"))
    with pytest.raises(AzureAuthenticationError, match="Use another account"):
        browser(sdk).token(ARM_SCOPE)
