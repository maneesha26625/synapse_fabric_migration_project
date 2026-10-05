"""Tests for the connection layer, without Azure, a driver, or a network.

Every credential, every ARM call, every ODBC connection and every git
invocation here is a fake. The suite must stay runnable on a laptop with no
subscription, no ODBC driver and no internet, which is the same discipline
``test_sql_catalog.py`` follows for catalog discovery.

What cannot be established here: that ``AzureCliCredential`` really returns a
token this tenant accepts, that ARM really answers these payload shapes, and
that the ODBC driver really accepts the token encoding. Those need real
resources and live in ``test_connections_live_integration.py``, which is
opt-in and skipped by default.
"""

from __future__ import annotations

import json
import struct
import sys
import time

import pytest

from discovery_agent.connections.azure import (
    ARM_ENDPOINT,
    ARM_SCOPE,
    SQL_SCOPE,
    SUBSCRIPTION_API_VERSION,
    AccessToken,
    ArmResponse,
    ArmTransport,
    AzureCliCredentialProvider,
    AzureConnection,
    arm_url,
    credential_provider,
    credential_provider_for,
)
from discovery_agent.connections.git import (
    PROBE_TIMEOUT_SECONDS,
    GitConnection,
    connection_from_url,
)
from discovery_agent.connections.manager import ConnectionManager
from discovery_agent.connections.models import (
    AzureConnectionConfig,
    ConnectionSettings,
    CredentialMethod,
    GitRepositoryConfig,
    SynapseConnectionConfig,
    settings_from_environment,
)
from discovery_agent.connections.sql import SqlConnection
from discovery_agent.connections.synapse import (
    SQL_POOL_API_VERSION,
    WORKSPACE_API_VERSION,
    SynapseWorkspaceConnection,
    sql_pool_metadata_from_payload,
    workspace_metadata_from_payload,
)
from discovery_agent.connections.validation import (
    REDACTED,
    ConnectionValidation,
    ErrorCategory,
    ValidationStatus,
    redact,
)
from discovery_agent.errors import (
    AzureAuthenticationError,
    AzureConnectionError,
    AzureDependencyNotAvailableError,
    ConfigError,
    GitCommandError,
    SqlConnectionError,
    SynapseConnectionError,
)
from discovery_agent.extractors.models import SourceType
from discovery_agent.sql.auth import (
    AccessTokenAuthentication,
    EntraAuthMethod,
    authentication_for,
    encode_odbc_access_token,
)
from discovery_agent.sql.connection import (
    SQL_COPT_SS_ACCESS_TOKEN,
    PyodbcConnector,
    build_connection_string,
)
from discovery_agent.sql.dedicated_pool import DedicatedPoolSource

SUBSCRIPTION = "e65a4ad3-3054-4391-947c-2a601d0bbf99"
TENANT = "72f988bf-0000-0000-0000-000000000000"
RESOURCE_GROUP = "PRK_Synapse_Migration"
WORKSPACE = "prkjoqmsiitlpnoqpocws1"
WORKSPACE_UID = "47612886-44de-4a41-ba21-3488bf6c1430"
POOL = "prkjoqmsiitlpnoqpocws1p1"
SQL_ENDPOINT = f"{WORKSPACE}.sql.azuresynapse.net"
REPOSITORY = "https://github.com/Azure/Test-Drive-Azure-Synapse-with-a-1-click-POC"

#: The three ARM resource ids the chain reads, spelled out once. The fake
#: transport matches on the exact id rather than on a substring, because every
#: url below contains the subscription id and a substring match would answer a
#: workspace request with the subscription's payload.
SUBSCRIPTION_ID = f"/subscriptions/{SUBSCRIPTION}"
WORKSPACE_ID = (
    f"{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.Synapse/workspaces/{WORKSPACE}"
)
POOL_ID = f"{WORKSPACE_ID}/sqlPools/{POOL}"

#: Shaped like a real one -- three base64url segments starting ``eyJ`` -- so
#: the redaction tests are exercising the pattern a leak would actually match.
FAKE_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJodHRwcyJ9.c2lnbmF0dXJl"


# --- fakes -------------------------------------------------------------------


class FakeSdkToken:
    """What azure-identity's ``get_token`` returns."""

    def __init__(self, token, expires_on):
        self.token = token
        self.expires_on = expires_on


class FakeSdkCredential:
    """Stand-in for ``AzureCliCredential``. Records the scopes it was asked for."""

    def __init__(self, token=FAKE_TOKEN, lifetime=3600, raises=None):
        self.token = token
        self.lifetime = lifetime
        self.raises = raises
        self.scopes = []

    def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes[0] if scopes else None)
        if self.raises is not None:
            raise self.raises
        return FakeSdkToken(f"{self.token}", int(time.time()) + self.lifetime)


class FakeArmTransport(ArmTransport):
    """Replays canned ARM answers, keyed by exact resource id.

    Anything not primed comes back as a 404, which is what ARM does and what
    makes "the workspace does not exist" expressible by leaving it out.
    """

    def __init__(self, responses=None, raises=None):
        self.responses = dict(responses or {})
        self.raises = raises
        self.requests = []

    def get(self, url, token):
        self.requests.append((url, token))
        if self.raises is not None:
            raise self.raises
        resource_id = url[len(ARM_ENDPOINT) :].split("?", 1)[0]
        return self.responses.get(
            resource_id, ArmResponse(404, {"error": {"message": "not found"}})
        )


class FakeCursor:
    def __init__(self, row=None, raises=None):
        self.row = row
        self.raises = raises
        self.executed = []
        self.closed = False

    def execute(self, sql, *parameters):
        self.executed.append(sql)
        if self.raises is not None:
            raise self.raises

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


class FakeSqlConnection:
    def __init__(self, row=None, raises=None):
        self.cursors = []
        self.row = row
        self.raises = raises
        self.closed = False

    def cursor(self):
        cursor = FakeCursor(self.row, self.raises)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class FakeConnector:
    """A ``Connector`` that opens a fake session, or refuses to."""

    def __init__(self, row=("poolone", "user@contoso.com"), raises=None, cursor_raises=None):
        self.row = row
        self.raises = raises
        self.cursor_raises = cursor_raises
        self.connections = []

    def connect(self):
        if self.raises is not None:
            raise self.raises
        connection = FakeSqlConnection(self.row, self.cursor_raises)
        self.connections.append(connection)
        return connection

    def describe(self):
        return "fake connector"


class FakePyodbc:
    """Records exactly what the driver was handed."""

    def __init__(self):
        self.calls = []

    def connect(self, connection_string, **kwargs):
        self.calls.append((connection_string, kwargs))
        return FakeSqlConnection()


class FakeGit:
    """Stand-in for run_git: canned answers keyed on the argument prefix.

    Leading ``-c key=value`` pairs are stripped before matching, exactly as
    git treats them -- they are configuration for one invocation, not the
    subcommand. That keeps every existing response key working now that the
    visibility probe prefixes ``-c credential.helper=``.

    ``anonymous_failure`` is how a private repository is expressed: the
    authenticated listing succeeds and the credential-free one is refused.
    """

    def __init__(self, responses=None, failures=None, anonymous_failure=None):
        self.responses = dict(responses or {})
        self.failures = dict(failures or {})
        self.anonymous_failure = anonymous_failure
        self.calls = []

    def __call__(self, args, cwd=None, timeout=None):
        args = tuple(str(a) for a in args)
        self.calls.append(args)

        stripped = args
        while stripped[:1] == ("-c",):
            stripped = stripped[2:]
        # Every probe now carries -c credential.interactive=never, so the
        # config flags alone no longer mean "anonymous". Resetting the helper
        # list is what does.
        anonymous = "credential.helper=" in args
        if anonymous and self.anonymous_failure is not None:
            raise self.anonymous_failure

        for length in range(len(stripped), 0, -1):
            prefix = stripped[:length]
            if prefix in self.failures:
                raise self.failures[prefix]
            if prefix in self.responses:
                return self.responses[prefix]
        raise AssertionError(f"unexpected git call: {args}")

    @property
    def subcommands(self):
        """The git subcommand of each call, with config flags removed."""
        subcommands = []
        for call in self.calls:
            stripped = call
            while stripped[:1] == ("-c",):
                stripped = stripped[2:]
            subcommands.append(stripped[0] if stripped else "")
        return subcommands

    @property
    def stripped_calls(self):
        """Each call with its leading ``-c key=value`` pairs removed."""
        stripped = []
        for call in self.calls:
            rest = call
            while rest[:1] == ("-c",):
                rest = rest[2:]
            stripped.append(rest)
        return stripped

    @property
    def anonymous_calls(self):
        """Calls made with every credential helper switched off."""
        return [call for call in self.calls if "credential.helper=" in call]

    @property
    def interactive_calls(self):
        """Remote calls that could open a credential dialog and block."""
        return [
            call
            for call in self.calls
            if call[:1] != ("-c",) and call[:1] != ("--version",)
        ]


def git_ok(overrides=None, failures=None, anonymous_failure=None):
    responses = {
        ("--version",): "git version 2.45.1.windows.1",
        ("ls-remote",): "1111111111111111111111111111111111111111\trefs/heads/main",
    }
    responses.update(overrides or {})
    return FakeGit(responses, failures, anonymous_failure=anonymous_failure)


# --- builders ----------------------------------------------------------------


def azure_config(**overrides):
    fields = {"subscription_id": SUBSCRIPTION, "tenant_id": TENANT}
    fields.update(overrides)
    return AzureConnectionConfig(**fields)


def synapse_config(**overrides):
    fields = {
        "resource_group": RESOURCE_GROUP,
        "workspace_name": WORKSPACE,
        "sql_pool_name": POOL,
    }
    fields.update(overrides)
    return SynapseConnectionConfig(**fields)


def subscription_payload():
    return ArmResponse(
        200,
        {
            "id": f"/subscriptions/{SUBSCRIPTION}",
            "subscriptionId": SUBSCRIPTION,
            "displayName": "POC Subscription",
            "state": "Enabled",
            "tenantId": TENANT,
        },
    )


def workspace_payload(**overrides):
    payload = {
        "id": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
            f"/providers/Microsoft.Synapse/workspaces/{WORKSPACE}"
        ),
        "name": WORKSPACE,
        "location": "eastus",
        "identity": {"principalId": "aaaa-bbbb", "type": "SystemAssigned"},
        "properties": {
            "workspaceUID": WORKSPACE_UID,
            "provisioningState": "Succeeded",
            "connectivityEndpoints": {
                "sql": SQL_ENDPOINT,
                "dev": f"https://{WORKSPACE}.dev.azuresynapse.net",
            },
            "defaultDataLakeStorage": {
                "accountUrl": "https://poc.dfs.core.windows.net",
                "filesystem": "workspace",
            },
        },
    }
    payload["properties"].update(overrides.pop("properties", {}))
    payload.update(overrides)
    return ArmResponse(200, payload)


def pool_payload(status="Online"):
    return ArmResponse(
        200,
        {
            "id": (
                f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
                f"/providers/Microsoft.Synapse/workspaces/{WORKSPACE}/sqlPools/{POOL}"
            ),
            "name": POOL,
            "sku": {"name": "DW100c"},
            "properties": {
                "status": status,
                "collation": "SQL_Latin1_General_CP1_CI_AS",
                "maxSizeBytes": 263882790666240,
            },
        },
    )


def working_transport(pool_status="Online"):
    """An ARM transport where every step of the chain succeeds."""
    return FakeArmTransport(
        {
            SUBSCRIPTION_ID: subscription_payload(),
            WORKSPACE_ID: workspace_payload(),
            POOL_ID: pool_payload(pool_status),
        }
    )


def provider(**kwargs):
    return AzureCliCredentialProvider(credential=FakeSdkCredential(**kwargs))


def azure_connection(transport=None, credential=None, **config_overrides):
    return AzureConnection(
        azure_config(**config_overrides),
        credential=credential or provider(),
        transport=transport or working_transport(),
    )


def manager(
    settings=None, transport=None, sql_connector=None, git=None, credential=None
):
    if settings is None:
        settings = ConnectionSettings(
            azure=azure_config(),
            synapse=synapse_config(),
            git=GitRepositoryConfig(repository_url=REPOSITORY, ref="main"),
        )
    return ConnectionManager(
        settings,
        credential=credential or provider(),
        transport=transport or working_transport(),
        sql_connector=sql_connector or FakeConnector(row=(POOL, "user@contoso.com")),
        git=git or git_ok(),
    )


# --- 1: configuration models -------------------------------------------------


def test_the_azure_configuration_names_a_subscription_and_a_mechanism():
    config = azure_config()

    assert config.credential_method is CredentialMethod.AZURE_CLI
    assert config.subscription_resource_id == f"/subscriptions/{SUBSCRIPTION}"
    assert config.safe_description() == (
        f"subscription={SUBSCRIPTION} tenant={TENANT} credential=azure_cli"
    )


@pytest.mark.parametrize(
    "factory, overrides",
    [
        (AzureConnectionConfig, {"subscription_id": ""}),
        (SynapseConnectionConfig, {"resource_group": "", "workspace_name": WORKSPACE}),
        (SynapseConnectionConfig, {"resource_group": RESOURCE_GROUP, "workspace_name": ""}),
        (GitRepositoryConfig, {"repository_url": ""}),
    ],
)
def test_incomplete_configuration_is_rejected(factory, overrides):
    with pytest.raises(ConfigError):
        factory(**overrides).validate()


def test_the_synapse_endpoint_is_derived_from_the_workspace_name():
    assert synapse_config().resolved_sql_endpoint == SQL_ENDPOINT
    assert synapse_config().development_endpoint == (
        f"https://{WORKSPACE}.dev.azuresynapse.net"
    )


def test_an_explicit_endpoint_wins_over_the_derived_one():
    """A workspace behind a private endpoint does not follow the naming rule."""
    config = synapse_config(sql_endpoint="private.sql.example.net")

    assert config.resolved_sql_endpoint == "private.sql.example.net"


def test_the_database_defaults_to_the_pool_name_but_can_differ():
    assert synapse_config().resolved_database == POOL
    assert synapse_config(database_name="other").resolved_database == "other"


def test_the_arm_resource_ids_are_well_formed():
    config = synapse_config()

    assert config.workspace_resource_id(SUBSCRIPTION) == (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.Synapse/workspaces/{WORKSPACE}"
    )
    assert config.sql_pool_resource_id(SUBSCRIPTION).endswith(f"/sqlPools/{POOL}")


def test_a_workspace_with_no_pool_has_no_pool_resource_id():
    with pytest.raises(ConfigError, match="no sql pool is configured"):
        synapse_config(sql_pool_name=None).sql_pool_resource_id(SUBSCRIPTION)


def test_the_sql_connection_configuration_is_derived_not_duplicated():
    """One definition of how to reach a SQL endpoint, in the sql package."""
    config = synapse_config().sql_connection_config()

    assert config.server == SQL_ENDPOINT
    assert config.database == POOL
    assert config.authentication is EntraAuthMethod.ACCESS_TOKEN
    assert config.driver == "ODBC Driver 18 for SQL Server"
    assert config.encrypt is True


def test_a_workspace_with_no_pool_and_no_database_cannot_produce_a_connection():
    with pytest.raises(ConfigError, match="no sql pool or database"):
        synapse_config(sql_pool_name=None).sql_connection_config()


def test_settings_validate_only_what_is_configured():
    """A repository-only run must not be made to invent an Azure subscription."""
    settings = ConnectionSettings(git=GitRepositoryConfig(repository_url=REPOSITORY))

    settings.validate()

    assert settings.azure is None
    assert settings.to_dict()["azure"] is None


def test_settings_come_from_the_environment_for_developer_testing():
    environment = {
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_TENANT_ID": TENANT,
        "SYNAPSE_RESOURCE_GROUP": RESOURCE_GROUP,
        "SYNAPSE_WORKSPACE_NAME": WORKSPACE,
        "SYNAPSE_SQL_POOL": POOL,
        "DISCOVERY_REPOSITORY_URL": REPOSITORY,
        "DISCOVERY_REPOSITORY_REF": "main",
    }

    settings = settings_from_environment(environment)

    assert settings.azure.subscription_id == SUBSCRIPTION
    assert settings.synapse.sql_pool_name == POOL
    assert settings.git.ref == "main"


def test_a_half_configured_section_is_absent_rather_than_broken(monkeypatch):
    """A workspace name with no resource group cannot address anything."""
    settings = settings_from_environment({"SYNAPSE_WORKSPACE_NAME": WORKSPACE})

    assert settings.synapse is None


def test_an_unknown_credential_method_is_rejected():
    with pytest.raises(ConfigError, match="unknown credential method"):
        settings_from_environment(
            {"AZURE_SUBSCRIPTION_ID": SUBSCRIPTION, "AZURE_CREDENTIAL_METHOD": "kerberos"}
        )


# --- 2: azure credentials and tokens -----------------------------------------


def test_the_configured_mechanism_is_the_azure_cli():
    credential = credential_provider_for(azure_config())

    assert isinstance(credential, AzureCliCredentialProvider)
    assert credential.method is CredentialMethod.AZURE_CLI
    assert credential.describe() == f"Azure CLI sign-in (tenant {TENANT})"


def test_a_service_principal_needs_all_three_parts_and_never_shows_its_secret():
    from discovery_agent.connections.azure import ServicePrincipalCredentialProvider

    with pytest.raises(AzureAuthenticationError, match="client secret"):
        credential_provider(CredentialMethod.SERVICE_PRINCIPAL, tenant_id=TENANT, client_id="c")

    sdk = FakeSdkCredential()
    provider = credential_provider(
        CredentialMethod.SERVICE_PRINCIPAL,
        tenant_id=TENANT,
        client_id="app-id",
        client_secret="TOP-SECRET-VALUE",
        credential=sdk,
    )
    assert isinstance(provider, ServicePrincipalCredentialProvider)
    assert provider.token(ARM_SCOPE).value
    for text in (repr(provider), str(provider), provider.describe()):
        assert "TOP-SECRET-VALUE" not in text
    assert "app-id" in provider.describe()


def test_a_managed_identity_needs_no_secret_and_names_which_kind():
    from discovery_agent.connections.azure import ManagedIdentityCredentialProvider

    system = credential_provider(CredentialMethod.MANAGED_IDENTITY, credential=FakeSdkCredential())
    user = credential_provider(CredentialMethod.MANAGED_IDENTITY, client_id="uami", credential=FakeSdkCredential())
    assert isinstance(system, ManagedIdentityCredentialProvider)
    assert system.describe() == "Managed identity (system-assigned)"
    assert "uami" in user.describe()

def test_a_management_token_is_acquired_for_the_arm_audience():
    sdk = FakeSdkCredential()
    connection = azure_connection(credential=AzureCliCredentialProvider(credential=sdk))

    token = connection.management_token()

    assert sdk.scopes == [ARM_SCOPE]
    assert token.scope == ARM_SCOPE
    assert token.value == FAKE_TOKEN


def test_a_sql_token_is_acquired_for_the_database_audience():
    """The audience is database.windows.net even for a Synapse pool."""
    sdk = FakeSdkCredential()
    connection = azure_connection(credential=AzureCliCredentialProvider(credential=sdk))

    token = connection.sql_token()

    assert sdk.scopes == [SQL_SCOPE]
    assert token.scope == SQL_SCOPE


def test_tokens_are_cached_per_audience_rather_than_re_acquired():
    """Shelling out to `az` once per catalog query would be visibly slow."""
    sdk = FakeSdkCredential()
    credential = AzureCliCredentialProvider(credential=sdk)

    credential.token(ARM_SCOPE)
    credential.token(ARM_SCOPE)
    credential.token(SQL_SCOPE)

    assert sdk.scopes == [ARM_SCOPE, SQL_SCOPE]


def test_an_expiring_token_is_replaced_before_it_expires():
    """A token valid when checked and expired when the server sees it fails a
    login for no discoverable reason, so the margin is not optional."""
    sdk = FakeSdkCredential(lifetime=60)  # inside the 300s margin
    credential = AzureCliCredentialProvider(credential=sdk)

    credential.token(ARM_SCOPE)
    credential.token(ARM_SCOPE)

    assert sdk.scopes == [ARM_SCOPE, ARM_SCOPE]


def test_a_credential_failure_is_reported_with_the_remedy():
    sdk = FakeSdkCredential(raises=RuntimeError("Please run 'az login'"))
    credential = AzureCliCredentialProvider(credential=sdk)

    with pytest.raises(AzureAuthenticationError, match="az login"):
        credential.token(ARM_SCOPE)


def test_an_empty_token_is_refused_rather_than_passed_on():
    sdk = FakeSdkCredential(token="")
    credential = AzureCliCredentialProvider(credential=sdk)

    with pytest.raises(AzureAuthenticationError, match="empty token"):
        credential.token(ARM_SCOPE)


def test_a_missing_azure_identity_is_a_dependency_error_not_a_crash(monkeypatch):
    """The offline repository run must work on a machine without the SDK."""
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    credential = AzureCliCredentialProvider()

    with pytest.raises(AzureDependencyNotAvailableError, match="azure-identity"):
        credential.token(ARM_SCOPE)


def test_the_sql_token_provider_is_a_callable_not_a_token():
    """What the SQL layer holds between connections: a function, never a secret."""
    connection = azure_connection()

    provide = connection.sql_token_provider()

    assert callable(provide)
    assert provide() == FAKE_TOKEN


# --- 3: azure validation -----------------------------------------------------


def test_azure_validation_reports_the_subscription_it_reached():
    result = azure_connection().validate()

    assert result.ok
    assert result.connection is SourceType.AZURE
    assert "POC Subscription" in result.message
    assert result.details["subscription_state"] == "Enabled"
    assert result.details["tenant_id"] == TENANT


def test_azure_validation_asks_arm_for_the_configured_subscription():
    transport = working_transport()

    azure_connection(transport=transport).validate()

    url, _ = transport.requests[0]
    assert url == arm_url(f"/subscriptions/{SUBSCRIPTION}", SUBSCRIPTION_API_VERSION)


def test_not_being_signed_in_is_an_authentication_failure():
    """Distinct from authorization: the remedy is `az login`, not a role."""
    sdk = FakeSdkCredential(raises=RuntimeError("no account found"))
    connection = azure_connection(credential=AzureCliCredentialProvider(credential=sdk))

    result = connection.validate()

    assert result.status is ValidationStatus.FAILED
    assert result.category is ErrorCategory.AUTHENTICATION


@pytest.mark.parametrize(
    "status, expected",
    [
        (401, ErrorCategory.AUTHENTICATION),
        (403, ErrorCategory.AUTHORIZATION),
        (404, ErrorCategory.NOT_FOUND),
        (500, ErrorCategory.NETWORK),
    ],
)
def test_an_arm_refusal_keeps_its_own_category(status, expected):
    """403 and 404 need opposite remedies, so they must not collapse together."""
    transport = FakeArmTransport(
        {SUBSCRIPTION_ID: ArmResponse(status, {"error": {"message": "no"}})}
    )

    result = azure_connection(transport=transport).validate()

    assert result.category is expected
    assert result.details["status_code"] == str(status)


def test_an_unreachable_endpoint_is_a_network_failure():
    transport = FakeArmTransport(raises=AzureConnectionError("could not reach ARM"))

    result = azure_connection(transport=transport).validate()

    assert result.category is ErrorCategory.NETWORK


# --- 4: synapse workspace ----------------------------------------------------


def workspace_connection(transport=None, config=None):
    return SynapseWorkspaceConnection(
        config or synapse_config(), azure_connection(transport=transport)
    )


def test_workspace_metadata_is_mapped_from_the_arm_payload():
    """Pure mapping: no subscription needed to test what a payload means."""
    config = synapse_config()

    metadata = workspace_metadata_from_payload(
        workspace_payload().payload, config, config.workspace_resource_id(SUBSCRIPTION)
    )

    assert metadata.workspace_id == WORKSPACE_UID
    assert metadata.sql_endpoint == SQL_ENDPOINT
    assert metadata.location == "eastus"
    assert metadata.managed_identity_principal_id == "aaaa-bbbb"
    assert metadata.default_filesystem == "workspace"


def test_a_missing_workspace_id_stays_missing_rather_than_being_invented():
    config = synapse_config()
    payload = workspace_payload().payload
    del payload["properties"]["workspaceUID"]

    metadata = workspace_metadata_from_payload(payload, config, "resource-id")

    assert metadata.workspace_id is None
    assert metadata.name == WORKSPACE, "the name is still known from configuration"


def test_the_metadata_object_carries_only_the_fields_it_declares():
    """A key or connection string added by a future API version cannot arrive."""
    payload = workspace_payload().payload
    payload["properties"]["someFutureSecret"] = "shhh"

    metadata = workspace_metadata_from_payload(payload, synapse_config(), "id")

    assert "shhh" not in json.dumps(metadata.to_dict())


def test_pool_metadata_is_mapped_from_the_arm_payload():
    metadata = sql_pool_metadata_from_payload(pool_payload().payload, POOL, "id")

    assert metadata.name == POOL
    assert metadata.sku == "DW100c"
    assert metadata.is_online


def test_a_paused_pool_is_not_online():
    metadata = sql_pool_metadata_from_payload(
        pool_payload("Paused").payload, POOL, "id"
    )

    assert not metadata.is_online


def test_workspace_validation_reports_the_workspace_and_the_pool():
    result = workspace_connection().validate()

    assert result.ok
    assert result.details["workspace_id"] == WORKSPACE_UID
    assert result.details["sql_pool"] == POOL
    assert result.details["sql_pool_status"] == "Online"


def test_workspace_validation_reads_both_arm_resources():
    transport = working_transport()

    workspace_connection(transport).validate()

    urls = [url for url, _ in transport.requests]
    assert any(WORKSPACE_API_VERSION in url and "/workspaces/" in url for url in urls)
    assert any(SQL_POOL_API_VERSION in url and "/sqlPools/" in url for url in urls)


def test_a_paused_pool_is_reported_before_the_sql_connection_times_out():
    """An ARM fact, discovered where it is cheap to discover.

    UNAVAILABLE rather than CONFIGURATION: nothing is misconfigured, so an
    operator sent to re-check the pool name would find it correct.
    """
    result = workspace_connection(working_transport("Paused")).validate()

    assert result.status is ValidationStatus.FAILED
    assert result.category is ErrorCategory.UNAVAILABLE
    assert "resume the pool" in result.message


def test_a_missing_workspace_is_not_found_rather_than_unauthorized():
    transport = FakeArmTransport({SUBSCRIPTION_ID: subscription_payload()})
    # the workspace is simply not there, so the lookup 404s

    result = workspace_connection(transport).validate()

    assert result.category is ErrorCategory.NOT_FOUND


def test_a_workspace_we_may_not_read_is_an_authorization_failure():
    transport = working_transport()
    transport.responses[WORKSPACE_ID] = ArmResponse(
        403, {"error": {"message": "does not have authorization to perform action"}}
    )

    result = workspace_connection(transport).validate()

    assert result.category is ErrorCategory.AUTHORIZATION


def test_a_workspace_id_that_disagrees_with_arm_stops_the_run():
    """A reused workspace name would attribute artifacts to the wrong workspace."""
    config = synapse_config(workspace_id="00000000-0000-0000-0000-000000000000")

    result = workspace_connection(config=config).validate()

    assert result.status is ValidationStatus.FAILED
    assert result.category is ErrorCategory.CONFIGURATION
    assert "name may have been reused" in result.message


def test_a_workspace_without_a_configured_pool_still_validates():
    result = workspace_connection(config=synapse_config(sql_pool_name=None)).validate()

    assert result.ok
    assert "no dedicated sql pool is configured" in result.message


def test_the_resolved_configuration_carries_what_arm_confirmed():
    """What a UI saves after a successful validation."""
    resolved = workspace_connection().resolved_config()

    assert resolved.workspace_id == WORKSPACE_UID
    assert resolved.sql_endpoint == SQL_ENDPOINT
    assert resolved.workspace_name == WORKSPACE


def test_an_unreachable_workspace_leaves_the_configuration_as_entered():
    transport = FakeArmTransport({SUBSCRIPTION_ID: subscription_payload()})

    resolved = workspace_connection(transport).resolved_config()

    assert resolved == synapse_config()


def test_reading_a_missing_workspace_raises_rather_than_returning_a_stand_in():
    """A caller could not tell a real workspace id from one built locally."""
    transport = FakeArmTransport({SUBSCRIPTION_ID: subscription_payload()})

    with pytest.raises(SynapseConnectionError):
        workspace_connection(transport).metadata()


# --- 5: the SQL connection ---------------------------------------------------


def sql_connection(connector=None, config=None, credential=None):
    """A SQL connection on a credential provider -- never on an AzureConnection.

    The signature is the point: a SQL session needs a token, so this class is
    handed the identity and nothing else. There is no subscription and no ARM
    transport within its reach.
    """
    return SqlConnection(
        config or synapse_config().sql_connection_config(),
        credential or provider(),
        connector=connector or FakeConnector(row=(POOL, "user@contoso.com")),
    )


def test_the_sql_connection_authenticates_with_an_entra_access_token():
    authentication = sql_connection().authentication()

    assert isinstance(authentication, AccessTokenAuthentication)
    assert authentication.method is EntraAuthMethod.ACCESS_TOKEN
    assert authentication.describe() == "Entra ID (access_token)"


def test_the_token_reaches_the_driver_through_the_attribute_not_the_string():
    """The whole reason the ODBC token path exists: a connection string is
    logged and an attribute is not."""
    driver = FakePyodbc()
    connection = sql_connection()
    connector = PyodbcConnector(connection.config, connection.authentication())
    connector._driver_module = lambda: driver

    connector.connect()

    connection_string, kwargs = driver.calls[0]
    attributes = kwargs["attrs_before"]
    assert SQL_COPT_SS_ACCESS_TOKEN in attributes
    assert attributes[SQL_COPT_SS_ACCESS_TOKEN] == encode_odbc_access_token(FAKE_TOKEN)
    assert FAKE_TOKEN not in connection_string


def test_the_token_is_encoded_the_way_the_driver_requires():
    """A length-prefixed UTF-16-LE blob. Getting this wrong fails the login
    with an opaque message rather than a decoding error."""
    encoded = encode_odbc_access_token("abc")

    assert encoded == struct.pack("<i", 6) + "abc".encode("utf-16-le")


def test_an_empty_token_never_reaches_the_driver():
    authentication = authentication_for(
        EntraAuthMethod.ACCESS_TOKEN, token_provider=lambda: ""
    )

    with pytest.raises(Exception, match="empty access token"):
        authentication.access_token()


def test_the_connection_string_carries_no_credential_and_no_sql_login():
    connection = sql_connection()

    connection_string = build_connection_string(
        connection.config, connection.authentication()
    )

    assert f"Server=tcp:{SQL_ENDPOINT},1433" in connection_string
    assert f"Database={POOL}" in connection_string
    assert "Encrypt=yes" in connection_string
    for forbidden in (
        "PWD=",
        "Password",
        "UID=",
        "AccessToken",
        "Bearer",
        "Authentication=",
        FAKE_TOKEN,
    ):
        assert forbidden not in connection_string


def test_access_token_authentication_contributes_no_connection_string_keywords():
    """Nothing is carefully omitted -- there is nothing to omit."""
    assert sql_connection().authentication().odbc_keywords() == {}


def test_a_connection_configured_for_interactive_sign_in_is_refused():
    """Routing interactive auth through here would connect as whoever answered
    the browser prompt, which need not be the run's identity."""
    interactive = synapse_config().sql_connection_config()
    interactive = type(interactive)(
        server=interactive.server,
        database=interactive.database,
        authentication=EntraAuthMethod.INTERACTIVE,
    )

    with pytest.raises(ConfigError, match="access token"):
        SqlConnection(interactive, provider())


def test_access_token_authentication_cannot_be_built_without_a_provider():
    with pytest.raises(Exception, match="needs a token provider"):
        authentication_for(EntraAuthMethod.ACCESS_TOKEN)


def test_sql_validation_reports_the_database_and_the_principal():
    result = sql_connection().validate()

    assert result.ok
    assert result.details["connected_database"] == POOL
    assert result.details["principal"] == "user@contoso.com"
    assert result.details["token_audience"] == SQL_SCOPE
    assert result.details["authentication"] == "access_token"


def test_sql_validation_asks_the_server_where_the_session_landed():
    connector = FakeConnector(row=(POOL, "user@contoso.com"))

    sql_connection(connector).validate()

    executed = connector.connections[0].cursors[0].executed
    assert executed == ["SELECT DB_NAME() AS database_name, SUSER_SNAME() AS principal"]


def test_sql_validation_closes_what_it_opened():
    connector = FakeConnector(row=(POOL, "user@contoso.com"))

    sql_connection(connector).validate()

    assert connector.connections[0].closed
    assert connector.connections[0].cursors[0].closed


def test_landing_in_a_different_database_is_a_failure_not_a_success():
    """Otherwise everything discovered afterwards is attributed to the wrong
    database."""
    result = sql_connection(FakeConnector(row=("master", "user@contoso.com"))).validate()

    assert result.status is ValidationStatus.FAILED
    assert result.category is ErrorCategory.CONFIGURATION
    assert "not" in result.message


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Login failed for user", ErrorCategory.AUTHORIZATION),
        ("Login timeout expired; unable to connect", ErrorCategory.NETWORK),
        ("something the driver has never said before", ErrorCategory.UNKNOWN),
    ],
)
def test_a_connection_failure_is_categorised_for_the_operator(message, expected):
    connector = FakeConnector(raises=SqlConnectionError(message))

    result = sql_connection(connector).validate()

    assert result.status is ValidationStatus.FAILED
    assert result.category is expected


def test_a_credential_failure_stops_before_the_driver_is_touched():
    """No point opening a socket when there is no identity to present."""
    sdk = FakeSdkCredential(raises=RuntimeError("no account found"))
    connector = FakeConnector()
    connection = sql_connection(
        connector=connector, credential=AzureCliCredentialProvider(credential=sdk)
    )

    result = connection.validate()

    assert result.category is ErrorCategory.AUTHENTICATION
    assert connector.connections == []


def test_the_connection_hands_back_the_existing_catalog_source():
    """The hand-off to discovery: an unchanged DedicatedPoolSource."""
    connection = sql_connection()

    source = connection.source()

    assert isinstance(source, DedicatedPoolSource)
    assert source.config is connection.config
    assert source.source_type is SourceType.SQL


# --- 6: the git connection ---------------------------------------------------


def git_connection(git=None, **config_overrides):
    fields = {"repository_url": REPOSITORY, "ref": "main"}
    fields.update(config_overrides)
    return GitConnection(GitRepositoryConfig(**fields), git=git or git_ok())


def test_the_git_configuration_records_provider_url_and_ref():
    connection = git_connection()

    assert connection.provider == "github"
    assert connection.ref == "main"
    assert connection.repository_url == REPOSITORY


def test_a_declared_provider_must_match_the_url():
    with pytest.raises(ConfigError, match="github"):
        GitRepositoryConfig(repository_url=REPOSITORY, provider="azuredevops").validate()


def test_an_unsupported_host_is_a_configuration_failure():
    connection = GitConnection.__new__(GitConnection)  # skip __init__'s validate
    connection.config = GitRepositoryConfig(repository_url="https://example.com/a/b")
    connection.providers = ()
    connection.git = git_ok()

    result = connection.validate()

    assert result.category is ErrorCategory.CONFIGURATION


def test_git_validation_does_not_clone():
    git = git_ok()

    git_connection(git).validate()

    assert all(call[0] != "clone" for call in git.calls)


def test_git_validation_proves_the_remote_answers_with_the_users_credentials():
    git = git_ok()

    result = git_connection(git).validate()

    assert result.ok
    assert result.details["refs_visible"] == "1"
    assert ("ls-remote", "--heads", "--tags", REPOSITORY, "main") in git.stripped_calls


def test_the_remote_probe_can_be_skipped_for_an_offline_check():
    git = git_ok()

    result = git_connection(git).validate(probe_remote=False)

    assert result.ok
    assert "not contacted" in result.message
    assert all(call[0] != "ls-remote" for call in git.calls)


def test_a_ref_the_remote_does_not_have_is_not_found_not_unreachable():
    git = git_ok(overrides={("ls-remote",): ""})

    result = git_connection(git).validate()

    assert result.status is ValidationStatus.FAILED
    assert result.category is ErrorCategory.NOT_FOUND


@pytest.mark.parametrize(
    "stderr, expected",
    [
        ("fatal: Authentication failed for ...", ErrorCategory.AUTHENTICATION),
        ("remote: Permission denied", ErrorCategory.AUTHORIZATION),
        ("fatal: repository not found", ErrorCategory.NOT_FOUND),
        ("fatal: could not resolve host: github.com", ErrorCategory.NETWORK),
    ],
)
def test_a_git_failure_is_categorised_for_the_operator(stderr, expected):
    git = git_ok(
        failures={("ls-remote",): GitCommandError(["ls-remote"], 128, stderr)}
    )

    result = git_connection(git).validate()

    assert result.category is expected


def test_a_missing_git_client_is_a_dependency_failure():
    git = git_ok(
        failures={("--version",): GitCommandError(["--version"], None, "not found")}
    )

    result = git_connection(git).validate()

    assert result.category is ErrorCategory.DEPENDENCY


def test_acquisition_behaviour_is_unchanged_and_still_does_the_cloning(tmp_path):
    """The connection passes its configuration through; acquire_repository is
    still the only implementation of the clone and reuse rules."""
    git = FakeGit(
        {
            ("clone",): "",
            ("checkout", "main"): "",
            ("rev-parse", "HEAD"): "1" * 40,
        }
    )
    connection = git_connection(git)

    source = connection.acquire(input_root=tmp_path)

    assert source.provider == "github"
    assert source.repository_url == REPOSITORY
    assert source.ref == "main"
    assert source.commit_sha == "1" * 40
    assert source.reused is False
    assert ("clone", REPOSITORY, str(tmp_path / connection.config.repository_name)) in git.calls


def test_a_bare_url_can_be_turned_into_a_connection():
    connection = connection_from_url(REPOSITORY, ref="main", git=git_ok())

    assert connection.provider == "github"


# --- 7: the connection manager -----------------------------------------------


def test_each_connection_can_be_validated_on_its_own():
    running = manager()

    assert running.validate_azure().ok
    assert running.validate_synapse().ok
    assert running.validate_sql().ok
    assert running.validate_git().ok


def test_validate_all_walks_the_chain_in_dependency_order():
    report = manager().validate_all()

    assert report.ok
    assert [result.connection for result in report.results] == [
        SourceType.AZURE,
        SourceType.SYNAPSE,
        SourceType.SQL,
        SourceType.REPOSITORY,
    ]


def test_a_failed_azure_check_skips_synapse_and_sql_rather_than_repeating_itself():
    """Three red lines with one root cause make an operator guess which to fix."""
    sdk = FakeSdkCredential(raises=RuntimeError("no account found"))
    report = manager(credential=AzureCliCredentialProvider(credential=sdk)).validate_all()

    assert report.result_for(SourceType.AZURE).category is ErrorCategory.AUTHENTICATION
    assert report.result_for(SourceType.SYNAPSE).status is ValidationStatus.SKIPPED
    assert report.result_for(SourceType.SQL).status is ValidationStatus.SKIPPED
    assert "azure validation did not succeed" in report.result_for(SourceType.SQL).message


def test_git_is_still_attempted_when_azure_fails():
    """It depends on the user's git credentials, not on Azure."""
    sdk = FakeSdkCredential(raises=RuntimeError("no account found"))
    report = manager(credential=AzureCliCredentialProvider(credential=sdk)).validate_all()

    assert report.result_for(SourceType.REPOSITORY).ok


def test_a_paused_pool_skips_the_sql_connection_it_would_have_refused():
    report = manager(transport=working_transport("Paused")).validate_all()

    assert report.result_for(SourceType.SYNAPSE).status is ValidationStatus.FAILED
    assert report.result_for(SourceType.SQL).status is ValidationStatus.SKIPPED


def test_an_unconfigured_connection_is_skipped_not_failed():
    settings = ConnectionSettings(git=GitRepositoryConfig(repository_url=REPOSITORY))

    report = ConnectionManager(settings, git=git_ok()).validate_all()

    assert report.result_for(SourceType.AZURE).status is ValidationStatus.SKIPPED
    assert report.result_for(SourceType.REPOSITORY).ok


def test_a_report_with_a_skip_does_not_call_itself_fine():
    """The most misleading thing this package could produce."""
    settings = ConnectionSettings(git=GitRepositoryConfig(repository_url=REPOSITORY))

    report = ConnectionManager(settings, git=git_ok()).validate_all()

    assert not report.ok


def test_synapse_configured_without_azure_is_a_configuration_failure():
    settings = ConnectionSettings(synapse=synapse_config())

    result = ConnectionManager(settings).validate_synapse()

    assert result.category is ErrorCategory.CONFIGURATION
    assert "no azure connection is configured" in result.message


def test_connections_are_built_once_and_share_one_credential():
    """One identity per run: two token caches would mean two `az` sign-ins."""
    running = manager()

    assert running.azure() is running.azure()
    assert running.credential() is running.credential()
    assert running.azure().credential is running.credential()
    assert running.synapse().azure.credential is running.credential()
    assert running.sql().credential is running.credential()


def test_a_repository_only_run_never_builds_a_credential():
    settings = ConnectionSettings(git=GitRepositoryConfig(repository_url=REPOSITORY))
    running = ConnectionManager(settings, git=git_ok())

    running.validate_git(probe_remote=False)

    assert running._azure is None


def test_the_manager_returns_a_ready_source_for_discovery():
    source = manager().sql().source()

    assert isinstance(source, DedicatedPoolSource)


def test_a_validation_failure_is_a_result_not_a_traceback():
    """One red line is useful; a traceback halfway through four checks is not."""
    settings = ConnectionSettings(synapse=synapse_config(sql_pool_name=None))

    report = ConnectionManager(settings).validate_all()

    assert not report.ok
    assert all(isinstance(r, ConnectionValidation) for r in report.results)


# --- 8: security -------------------------------------------------------------


def test_a_token_is_never_in_its_own_repr():
    token = AccessToken(FAKE_TOKEN, expires_on=1, scope=ARM_SCOPE)

    for rendering in (repr(token), str(token), f"{token}", format(token)):
        assert FAKE_TOKEN not in rendering
        assert ARM_SCOPE in rendering
    assert token.value == FAKE_TOKEN, "the value is still reachable, deliberately"


def test_a_credential_provider_is_never_in_a_position_to_print_a_token():
    credential = AzureCliCredentialProvider(credential=FakeSdkCredential())
    credential.token(ARM_SCOPE)

    rendering = repr(credential)

    assert FAKE_TOKEN not in rendering
    assert "azure_cli" in rendering


def test_sql_authentication_holds_a_callable_rather_than_a_token():
    authentication = sql_connection().authentication()

    assert FAKE_TOKEN not in repr(authentication)
    assert not hasattr(authentication, "token")
    for forbidden in ("password", "secret", "client_secret", "pat"):
        assert not hasattr(authentication, forbidden)


@pytest.mark.parametrize(
    "model",
    [AzureConnectionConfig, SynapseConnectionConfig, GitRepositoryConfig, ConnectionSettings],
)
def test_no_configuration_model_has_a_field_a_secret_could_live_in(model):
    """Structural, not a redaction step someone could forget."""
    forbidden = (
        "password",
        "secret",
        "token",
        "pat",
        "key",
        "credential_value",
        "connection_string",
    )
    for name in model.__dataclass_fields__:
        assert not any(word in name.lower() for word in forbidden), name


@pytest.mark.parametrize(
    "model, kwargs",
    [
        (AzureConnectionConfig, {"subscription_id": SUBSCRIPTION, "client_secret": "s"}),
        (SynapseConnectionConfig, {"resource_group": "a", "workspace_name": "b", "password": "s"}),
        (GitRepositoryConfig, {"repository_url": REPOSITORY, "personal_access_token": "s"}),
    ],
)
def test_a_secret_cannot_be_passed_to_a_configuration_model(model, kwargs):
    with pytest.raises(TypeError):
        model(**kwargs)


def test_a_repository_url_with_a_pat_in_it_is_refused_at_construction():
    """The one way a secret could reach the git configuration."""
    with pytest.raises(ConfigError, match="must not embed credentials"):
        GitRepositoryConfig(
            repository_url="https://user:ghp_secrettoken@github.com/contoso/repo"
        )


def test_a_configuration_model_never_prints_a_secret_because_it_holds_none():
    for config in (azure_config(), synapse_config(), GitRepositoryConfig(REPOSITORY)):
        rendered = repr(config) + json.dumps(config.to_dict())
        assert FAKE_TOKEN not in rendered
        assert "secret" not in rendered.lower()


@pytest.mark.parametrize(
    "leak",
    [
        f"Authorization: Bearer {FAKE_TOKEN}",
        f"token {FAKE_TOKEN} was rejected",
        "connect failed: access_token=abc123def456",
        "https://user:ghp_secret@github.com/contoso/repo is unreachable",
        "login failed, PWD=hunter2",
    ],
)
def test_anything_credential_shaped_is_redacted_out_of_a_result(leak):
    result = ConnectionValidation(
        connection=SourceType.SQL,
        status=ValidationStatus.FAILED,
        message=leak,
        details={"detail": leak},
        category=ErrorCategory.UNKNOWN,
    )

    assert REDACTED in result.message
    assert FAKE_TOKEN not in result.message
    assert FAKE_TOKEN not in result.details["detail"]
    assert "ghp_secret" not in json.dumps(result.to_dict())
    assert "hunter2" not in json.dumps(result.to_dict())


def test_a_driver_error_quoting_a_token_cannot_reach_a_report():
    """The backstop that matters: an error text we did not write."""
    connector = FakeConnector(
        raises=SqlConnectionError(f"login failed; token was Bearer {FAKE_TOKEN}")
    )

    result = sql_connection(connector).validate()

    assert FAKE_TOKEN not in json.dumps(result.to_dict())


def test_no_token_appears_anywhere_in_a_full_validation_report():
    report = manager().validate_all()

    serialized = json.dumps(report.to_dict())
    assert FAKE_TOKEN not in serialized
    for forbidden in ("Bearer", "eyJ", "password", "PWD="):
        assert forbidden not in serialized


def test_a_token_expiry_is_reported_but_the_token_is_not():
    """Expiry is the useful, non-secret half of a token."""
    result = azure_connection().validate()

    assert result.details["token_expires_on"].isdigit()
    assert FAKE_TOKEN not in json.dumps(result.to_dict())


def test_redaction_leaves_ordinary_text_alone():
    """A blunt instrument must not mangle the message an operator needs."""
    message = f"workspace {WORKSPACE!r} in resource group {RESOURCE_GROUP!r} is readable"

    assert redact(message) == message


def test_a_failure_must_say_why_it_failed():
    with pytest.raises(ValueError, match="must say why"):
        ConnectionValidation(
            connection=SourceType.AZURE,
            status=ValidationStatus.FAILED,
            message="it broke",
        )


def test_a_success_cannot_carry_an_error_category():
    with pytest.raises(ValueError, match="cannot carry an error category"):
        ConnectionValidation(
            connection=SourceType.AZURE,
            status=ValidationStatus.OK,
            message="fine",
            category=ErrorCategory.UNKNOWN,
        )


# --- 9: the dev command ------------------------------------------------------


def test_the_command_prints_the_whole_chain(capsys):
    from discovery_agent.connections import __main__ as dev

    exit_code = dev.main([], manager=manager())
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert "[OK] Azure" in printed
    assert "[OK] Synapse" in printed
    assert "[OK] SQL" in printed
    assert "[OK] Git" in printed
    assert "Overall: READY" in printed


def test_the_command_names_what_failed_and_what_was_not_attempted(capsys):
    from discovery_agent.connections import __main__ as dev

    sdk = FakeSdkCredential(raises=RuntimeError("no account found"))
    exit_code = dev.main(
        [], manager=manager(credential=AzureCliCredentialProvider(credential=sdk))
    )
    printed = capsys.readouterr().out

    assert exit_code == 1
    assert "[FAIL] Azure" in printed
    assert "category: AUTHENTICATION" in printed
    assert "Overall: NOT READY" in printed
    assert "failed: Azure" in printed
    assert "not attempted: SQL, Synapse" in printed


def test_the_command_never_prints_a_token_or_a_connection_string(capsys):
    from discovery_agent.connections import __main__ as dev

    dev.main([], manager=manager())
    printed = capsys.readouterr().out

    for forbidden in (FAKE_TOKEN, "Bearer", "eyJ", "PWD", "Driver=", "Encrypt="):
        assert forbidden not in printed


def test_the_command_can_emit_the_structured_report(capsys):
    from discovery_agent.connections import __main__ as dev

    dev.main(["--json"], manager=manager())
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert {r["connection"] for r in payload["results"]} == {
        "azure",
        "synapse",
        "sql",
        "repository",
    }


def test_the_command_reports_the_useful_non_secret_metadata(capsys):
    from discovery_agent.connections import __main__ as dev

    dev.main([], manager=manager())
    printed = capsys.readouterr().out

    assert WORKSPACE_UID in printed
    assert POOL in printed
    assert "user@contoso.com" in printed


def test_an_unconfigured_run_exits_non_zero_and_says_so(capsys, monkeypatch):
    from discovery_agent.connections import __main__ as dev

    for name in (
        "AZURE_SUBSCRIPTION_ID",
        "SYNAPSE_WORKSPACE_NAME",
        "SYNAPSE_RESOURCE_GROUP",
        "DISCOVERY_REPOSITORY_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    assert dev.main([]) == 2
    assert "nothing is configured" in capsys.readouterr().err


def test_command_line_arguments_win_over_the_environment(monkeypatch):
    from discovery_agent.connections import __main__ as dev

    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "from-env")
    args = dev.build_parser().parse_args(["--subscription-id", "from-args"])

    settings = dev.settings_from_args(args)

    assert settings.azure.subscription_id == "from-args"


def test_a_configuration_error_exits_non_zero(capsys, monkeypatch):
    from discovery_agent.connections import __main__ as dev

    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    monkeypatch.delenv("SYNAPSE_WORKSPACE_NAME", raising=False)

    assert dev.main(["--repository-url", "https://user:pat@github.com/a/b"]) == 2
    assert "must not embed credentials" in capsys.readouterr().err


# --- 10: consolidation ------------------------------------------------------
#
# These are the audit's tests. They do not check that a connection works --
# sections 1 to 9 do that. They check that there is exactly *one* way for a
# connection to be made, and that the way cannot be bypassed. A regression
# here is not a broken connection; it is a second connection path growing
# back, which is the thing this consolidation existed to prevent.


def source_of(module_name):
    import importlib
    import inspect as inspect_module

    return inspect_module.getsource(importlib.import_module(module_name))


def repository_python_files():
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent / "src" / "discovery_agent"
    return sorted(root.rglob("*.py"))


def test_only_the_connection_layer_can_create_an_azure_credential():
    """Requirement H, checked across the whole tree rather than by convention."""
    offenders = []
    for path in repository_python_files():
        if path.parent.name == "connections":
            continue
        text = path.read_text(encoding="utf-8")
        if "azure.identity" in text or "AzureCliCredential(" in text:
            offenders.append(str(path))

    assert not offenders, (
        f"these modules reach for an Azure credential directly: {offenders}. "
        f"Every identity comes from discovery_agent.connections.azure."
    )


def test_only_the_sql_package_opens_an_odbc_connection():
    """One ODBC implementation.

    ``PyodbcConnector`` is a protocol component, not a duplicate connection
    layer, so it stays in ``sql/`` -- but it must be the only thing that calls
    the driver.
    """
    offenders = [
        str(path)
        for path in repository_python_files()
        if "pyodbc.connect(" in path.read_text(encoding="utf-8")
        and path.name != "connection.py"
    ]

    assert not offenders, f"these modules call the ODBC driver directly: {offenders}"


# The Fabric target (api/fabric.py) drives the `az` and `fab` CLIs for sign-in and is
# the one sanctioned non-git subprocess user. These guards are about git only.
_FABRIC_CLI_MODULE = "fabric.py"


def test_only_the_acquisition_package_runs_a_git_subprocess():
    """One git implementation, wrapped by one connection."""
    offenders = [
        str(path)
        for path in repository_python_files()
        if "subprocess.run(" in path.read_text(encoding="utf-8")
        and path.name not in ("git.py", _FABRIC_CLI_MODULE)
    ]

    assert not offenders, f"these modules shell out to git directly: {offenders}"


def test_no_extractor_can_authenticate_or_connect():
    """The architectural boundary, as an assertion.

    Extractors receive already-connected sources. Nothing under
    ``extractors/`` may import a connection package, and that has to be
    checkable rather than merely documented -- it is the rule a future agent
    is most likely to break.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent / "src" / "discovery_agent"
    offenders = []
    for package in ("extractors", "artifacts", "writers"):
        for path in (root / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                "discovery_agent.connections",
                "discovery_agent.sql",
                "azure.identity",
                "pyodbc",
                "subprocess",
            ):
                if f"import {forbidden}" in text or f"from {forbidden}" in text:
                    offenders.append(f"{path.name} imports {forbidden}")

    assert not offenders, offenders


def test_the_sql_package_cannot_acquire_a_credential():
    """``sql/`` speaks TDS and reads catalogs.

    It has no identity of its own, and removing the interactive path is what
    made that true.
    """
    for module in (
        "discovery_agent.sql.auth",
        "discovery_agent.sql.config",
        "discovery_agent.sql.connection",
        "discovery_agent.sql.dedicated_pool",
    ):
        source = source_of(module)
        assert "azure.identity" not in source
        assert "AzureCliCredential" not in source
        # An import, not a mention: these modules document the arrangement
        # in prose, and prose is not a dependency.
        assert "from discovery_agent.connections" not in source, (
            f"{module} imports the connection layer; the dependency runs the "
            f"other way -- connections depends on sql"
        )
        assert "import discovery_agent.connections" not in source


def test_the_interactive_sign_in_path_is_gone_and_stays_gone():
    """The duplicate that the audit removed.

    It worked. That was the problem: the ODBC driver signed in separately, so
    a run could hold one identity for ARM and a different one for SQL with
    nothing reporting the discrepancy.
    """
    import discovery_agent.sql.auth as auth_module

    assert not hasattr(auth_module, "InteractiveAuthentication")
    assert "ActiveDirectoryInteractive" not in source_of("discovery_agent.sql.auth")

    with pytest.raises(Exception, match="not available"):
        authentication_for(EntraAuthMethod.INTERACTIVE)


def test_the_sql_dev_command_uses_the_central_credential():
    """Requirement G, at the entry point that used to bypass it."""
    source = source_of("discovery_agent.sql.__main__")

    assert "credential_provider" in source
    assert "PyodbcConnector" not in source, (
        "the SQL dev command assembles no connector of its own; it asks the "
        "connection layer for one"
    )


def test_the_acquisition_command_uses_the_central_git_connection():
    """Requirement I: one git connectivity path, including from the CLI."""
    source = source_of("discovery_agent.acquisition.__main__")

    assert "connection_from_url" in source
    assert "acquire_repository(" not in source


def test_one_credential_provider_serves_every_azure_backed_connection():
    """Requirement A, stated as identity rather than as equality."""
    running = manager()

    identities = {
        id(running.credential()),
        id(running.azure().credential),
        id(running.synapse().azure.credential),
        id(running.sql().credential),
    }

    assert len(identities) == 1, "more than one credential provider in one run"


def test_the_manager_builds_its_own_credential_exactly_once():
    """Nothing is injected here, so this is the production path."""
    settings = ConnectionSettings(azure=azure_config(), synapse=synapse_config())
    running = ConnectionManager(settings)

    first = running.credential()
    second = running.credential()

    assert first is second
    assert first.method is CredentialMethod.AZURE_CLI


def test_a_sql_only_run_needs_no_subscription_to_obtain_an_identity():
    """Why ``SqlConnection`` takes a credential and not an ``AzureConnection``.

    A SQL token is scoped to an audience, not to a subscription. Requiring one
    would push a SQL-only caller into inventing a subscription id -- or into
    building its own credential, which is the hole the old interactive path
    went through.
    """
    from discovery_agent.connections.azure import credential_provider

    credential = credential_provider(credential=FakeSdkCredential())
    connection = SqlConnection(
        synapse_config().sql_connection_config(),
        credential,
        connector=FakeConnector(row=(POOL, "user@contoso.com")),
    )

    assert connection.validate().ok
    assert not hasattr(connection, "azure")


def test_different_audiences_get_different_tokens_from_the_same_identity():
    """Requirement C. One sign-in, two audiences, two tokens -- as OAuth works."""

    class PerScopeCredential(FakeSdkCredential):
        def get_token(self, *scopes, **kwargs):
            self.scopes.append(scopes[0])
            return FakeSdkToken(
                f"{FAKE_TOKEN}.{scopes[0][-12:]}", int(time.time()) + 3600
            )

    sdk = PerScopeCredential()
    credential = AzureCliCredentialProvider(credential=sdk)

    management = credential.token(ARM_SCOPE)
    sql = credential.token(SQL_SCOPE)

    assert management.value != sql.value
    assert management.scope == ARM_SCOPE
    assert sql.scope == SQL_SCOPE
    assert sorted(sdk.scopes) == sorted([ARM_SCOPE, SQL_SCOPE])


def test_an_already_expired_token_is_reacquired_without_asking_anyone():
    """Requirement D. No prompt, no pasted token -- the provider just refreshes."""
    sdk = FakeSdkCredential(lifetime=-10)  # already expired on arrival
    credential = AzureCliCredentialProvider(credential=sdk)

    first = credential.token(ARM_SCOPE)
    second = credential.token(ARM_SCOPE)

    assert not first.is_valid()
    assert sdk.scopes == [ARM_SCOPE, ARM_SCOPE], "the expired token was reused"
    assert second is not first


def test_a_long_run_picks_up_a_refreshed_token_between_connections():
    """The SQL layer holds a callable, so an expiry mid-run costs a call rather
    than a failed login."""
    sdk = FakeSdkCredential(lifetime=-10)
    connection = sql_connection(credential=AzureCliCredentialProvider(credential=sdk))
    authentication = connection.authentication()

    authentication.access_token()
    authentication.access_token()

    assert sdk.scopes == [SQL_SCOPE, SQL_SCOPE]


def test_settings_cannot_carry_a_token_of_any_kind():
    """No secret-bearing configuration, checked by field name."""
    for model in (AzureConnectionConfig, SynapseConnectionConfig, GitRepositoryConfig):
        for name in model.__dataclass_fields__:
            assert "token" not in name.lower()
            assert "secret" not in name.lower()
            assert "password" not in name.lower()


def test_every_failure_category_the_ui_needs_exists():
    """The UI renders a different message per category, so the set is a
    contract rather than an implementation detail."""
    required = {
        "authentication",
        "authorization",
        "network",
        "configuration",
        "dependency",
        "not_found",
        "unavailable",
        "unsupported",
    }

    assert required <= {category.value for category in ErrorCategory}


def test_validate_all_exercises_the_real_path_rather_than_the_configuration():
    """Requirement O: a health check that only read its own settings back
    would pass on a machine with no network and no sign-in."""
    transport = working_transport()
    connector = FakeConnector(row=(POOL, "user@contoso.com"))
    git = git_ok()

    manager(transport=transport, sql_connector=connector, git=git).validate_all()

    assert transport.requests, "ARM was never called"
    assert connector.connections, "no SQL session was opened"
    assert connector.connections[0].cursors[0].executed, "no query was executed"
    assert "ls-remote" in git.subcommands, "the remote was not probed"


# --- 11: multi-provider git ---------------------------------------------------
#
# Production repositories are private and are not all on GitHub. These tests
# cover the five providers and the two transports, and -- more importantly --
# the rule that holds across all of them: this application never handles a git
# credential. It names the machine's mechanism and lets git use it.

GITHUB_HTTPS = "https://github.com/contoso/synapse-workspace"
GITHUB_SSH = "git@github.com:contoso/synapse-workspace.git"
ADO_HTTPS = "https://dev.azure.com/contoso/Analytics/_git/synapse-workspace"
ADO_SSH = "git@ssh.dev.azure.com:v3/contoso/Analytics/synapse-workspace"
ADO_LEGACY_HTTPS = "https://contoso.visualstudio.com/Analytics/_git/synapse-workspace"
GITLAB_HTTPS = "https://gitlab.com/contoso/data/synapse-workspace"
GITLAB_SSH = "git@gitlab.com:contoso/synapse-workspace.git"
BITBUCKET_HTTPS = "https://bitbucket.org/contoso/synapse-workspace"
SELF_HOSTED_HTTPS = "https://git.corp.example.com/data/synapse-workspace.git"
SELF_HOSTED_SSH = "git@git.corp.example.com:data/synapse-workspace.git"
SELF_HOSTED_SSH_URL = "ssh://git@git.corp.example.com:2222/data/synapse-workspace.git"


def code_of(module):
    """A module's source with every docstring removed.

    These tests assert what a module *does*, and a module that explains in
    prose that it holds no OAuth client must not fail for saying so.
    """
    import ast as ast_module
    import inspect as inspect_module

    tree = ast_module.parse(inspect_module.getsource(module))
    for node in ast_module.walk(tree):
        if isinstance(
            node, (ast_module.Module, ast_module.ClassDef, ast_module.FunctionDef)
        ):
            if (
                node.body
                and isinstance(node.body[0], ast_module.Expr)
                and isinstance(node.body[0].value, ast_module.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast_module.unparse(tree)


def git_for(url, ref="main", git_override=None, **kwargs):
    """A git connection for one URL, on a fake runner."""
    return GitConnection(
        GitRepositoryConfig(repository_url=url, ref=ref),
        git=git_override or git_ok(**kwargs),
    )


@pytest.mark.parametrize(
    "url, provider, transport, authentication",
    [
        (GITHUB_HTTPS, "github", "https", "git_credential_manager"),
        (GITHUB_SSH, "github", "ssh", "ssh"),
        (ADO_HTTPS, "azure_devops", "https", "git_credential_manager"),
        (ADO_SSH, "azure_devops", "ssh", "ssh"),
        (ADO_LEGACY_HTTPS, "azure_devops", "https", "git_credential_manager"),
        (GITLAB_HTTPS, "gitlab", "https", "git_credential_manager"),
        (GITLAB_SSH, "gitlab", "ssh", "ssh"),
        (BITBUCKET_HTTPS, "bitbucket", "https", "git_credential_manager"),
        (SELF_HOSTED_HTTPS, "generic", "https", "git_credential_manager"),
        (SELF_HOSTED_SSH, "generic", "ssh", "ssh"),
        (SELF_HOSTED_SSH_URL, "generic", "ssh", "ssh"),
    ],
)
def test_the_provider_transport_and_mechanism_are_read_off_the_url(
    url, provider, transport, authentication
):
    """The whole reporting contract, one case per supported combination.

    The mechanism follows from the transport and nothing else: there is no
    provider-specific authentication anywhere, because all five are reached by
    the same git binary through the same two credential systems.
    """
    connection = git_for(url)

    assert connection.provider == provider
    assert connection.transport.value == transport
    assert connection.authentication.value == authentication


@pytest.mark.parametrize(
    "url, provider",
    [
        (GITHUB_HTTPS.upper().replace("HTTPS://", "https://"), "github"),
        ("https://GitHub.com/Contoso/Repo", "github"),
        ("https://DEV.AZURE.COM/contoso/Analytics/_git/Repo", "azure_devops"),
        ("git@GitLab.COM:contoso/Repo.git", "gitlab"),
        ("https://BitBucket.ORG/contoso/Repo", "bitbucket"),
        ("https://GIT.CORP.EXAMPLE.COM/data/Repo.git", "generic"),
    ],
)
def test_provider_detection_ignores_hostname_case(url, provider):
    """Hostnames are case-insensitive, and a pasted URL is whatever the user
    had on their clipboard."""
    assert git_for(url).provider == provider


def test_detection_uses_the_hostname_and_not_the_repository_name():
    """A repository called ``gitlab`` on a corporate host is not GitLab.

    Guessing a provider from the path is how a tool ends up applying the wrong
    API, the wrong URL shape, or the wrong assumptions about permissions.
    """
    assert git_for("https://git.corp.example.com/infra/gitlab.git").provider == "generic"
    assert git_for("https://git.corp.example.com/infra/github").provider == "generic"
    assert git_for("https://github.com/contoso/gitlab-mirror").provider == "github"


def test_a_self_hosted_host_is_generic_rather_than_guessed():
    """``gitlab.corp.example.com`` is almost certainly GitLab, and we still do
    not say so: nothing in the hostname proves it, and generic is handled
    identically anyway."""
    assert git_for("https://gitlab.corp.example.com/team/repo.git").provider == "generic"


def test_a_known_host_with_a_malformed_path_is_not_adopted_as_generic():
    """``github.com/contoso`` is a user page. Accepting it as a self-hosted
    remote would defer the failure to clone time with a worse message."""
    with pytest.raises(ConfigError):
        GitRepositoryConfig(repository_url="https://github.com/contoso").validate()


@pytest.mark.parametrize(
    "url",
    [
        "git://github.com/contoso/repo.git",
        "ftp://example.com/repo.git",
    ],
)
def test_transports_that_cannot_carry_authentication_are_refused(url):
    """``git://`` is anonymous and unencrypted; it cannot reach a private
    repository at all, so accepting it would only defer the failure."""
    with pytest.raises(ConfigError):
        GitRepositoryConfig(repository_url=url).validate()


@pytest.mark.parametrize(
    "url",
    [
        "https://token@github.com/contoso/repo",
        "https://user:pass@github.com/contoso/repo",
        "https://user:ghp_secretvalue@dev.azure.com/org/proj/_git/repo",
        "https://x-token-auth:secret@bitbucket.org/contoso/repo",
        "https://oauth2:glpat-secret@gitlab.com/contoso/repo",
        "https://user:pass@git.corp.example.com/team/repo.git",
    ],
)
def test_a_url_carrying_a_credential_is_refused_for_every_provider(url):
    """The one way a secret could enter this layer, closed on all five."""
    with pytest.raises(ConfigError, match="must not embed credentials"):
        GitRepositoryConfig(repository_url=url)


def test_the_rejection_message_does_not_quote_the_credential_it_rejected():
    """An error that echoed the URL would write the token into the very report
    that refused it, and into whatever log holds that report."""
    with pytest.raises(ConfigError) as raised:
        GitRepositoryConfig(
            repository_url="https://user:ghp_verysecretvalue@github.com/contoso/repo"
        )

    message = str(raised.value)
    assert "ghp_verysecretvalue" not in message
    assert "user:" not in message


def test_an_ssh_user_is_not_mistaken_for_an_embedded_credential():
    """``git@host`` is the SSH login name, not a secret. Refusing it would
    make every SSH remote unusable."""
    connection = git_for(GITHUB_SSH)

    assert connection.transport.value == "ssh"
    assert connection.authentication.value == "ssh"


@pytest.mark.parametrize(
    "url, transport",
    [
        (GITHUB_HTTPS, "https"),
        (GITHUB_SSH, "ssh"),
        (ADO_HTTPS, "https"),
        (ADO_SSH, "ssh"),
        (GITLAB_HTTPS, "https"),
        (BITBUCKET_HTTPS, "https"),
        (SELF_HOSTED_HTTPS, "https"),
        (SELF_HOSTED_SSH, "ssh"),
    ],
)
def test_validation_reports_the_chain_the_user_needs_to_see(url, transport):
    """provider, transport, authentication, ref, reachable -- for all eight
    of the required cases."""
    result = git_for(url).validate()

    assert result.ok, result.message
    assert result.details["transport"] == transport
    assert result.details["authentication"] == (
        "ssh" if transport == "ssh" else "git_credential_manager"
    )
    assert result.details["reachable"] == "true"
    assert result.details["ref"] == "main"
    assert result.details["provider"] in {
        "github",
        "azure_devops",
        "gitlab",
        "bitbucket",
        "generic",
    }


def test_a_public_repository_is_reported_as_public():
    """Determined by asking, not assumed: a second listing with every
    credential helper switched off."""
    git = git_ok()

    result = git_for(GITHUB_HTTPS, git_override=git).validate()

    assert result.details["repository"] == "public"
    assert git.anonymous_calls, "the credential-free probe was never made"


def test_a_private_repository_is_reported_as_private():
    """The authenticated listing succeeds; the credential-free one is refused."""
    git = git_ok(
        anonymous_failure=GitCommandError(
            ["ls-remote"], 128, "fatal: Authentication failed"
        )
    )

    result = git_for(GITHUB_HTTPS, git_override=git).validate()

    assert result.ok
    assert result.details["repository"] == "private"
    assert result.details["reachable"] == "true"


def test_visibility_over_ssh_is_unknown_rather_than_guessed():
    """There is no "without a key" SSH request to make, so there is no answer
    to give. Reporting ``public`` because we could not tell would be worse
    than reporting nothing."""
    git = git_ok()

    result = git_for(GITHUB_SSH, git_override=git).validate()

    assert result.details["repository"] == "unknown"
    assert not git.anonymous_calls, "SSH has no credential-free form to probe"


def test_the_credential_free_probe_really_disables_every_helper():
    """If the flag were wrong the probe would use the helper and every
    private repository would be reported as public."""
    git = git_ok()

    git_for(GITHUB_HTTPS, git_override=git).visibility()

    probe = git.anonymous_calls[0]
    assert "credential.helper=" in probe
    assert "ls-remote" in probe


def test_validation_never_clones_whatever_the_provider():
    """A probe that cloned would leave a snapshot the reuse path would adopt."""
    for url in (GITHUB_HTTPS, ADO_SSH, GITLAB_HTTPS, SELF_HOSTED_SSH):
        git = git_ok()
        git_for(url, git_override=git).validate()
        assert "clone" not in git.subcommands


@pytest.mark.parametrize(
    "stderr, expected",
    [
        ("fatal: Authentication failed for ...", ErrorCategory.AUTHENTICATION),
        ("could not read Username for 'https://github.com'", ErrorCategory.AUTHENTICATION),
        ("git: terminal prompts disabled", ErrorCategory.AUTHENTICATION),
        ("git@github.com: Permission denied (publickey).", ErrorCategory.AUTHENTICATION),
        ("Host key verification failed.", ErrorCategory.AUTHENTICATION),
        ("remote: TF401019: access denied", ErrorCategory.AUTHORIZATION),
        ("remote: Repository not found.", ErrorCategory.NOT_FOUND),
        ("ssh: connect to host ... Connection refused", ErrorCategory.NETWORK),
        ("fatal: could not resolve host: git.corp.example.com", ErrorCategory.NETWORK),
    ],
)
def test_ssh_and_https_failures_are_categorised_for_the_operator(stderr, expected):
    """The wordings differ per transport and per host, so both are covered.

    An SSH key that is not authorised reads nothing like an HTTPS 403, and
    collapsing them would tell the user to fix the wrong thing.
    """
    git = git_ok(failures={("ls-remote",): GitCommandError(["ls-remote"], 128, stderr)})

    result = git_for(SELF_HOSTED_SSH, git_override=git).validate()

    assert result.category is expected


def test_no_credential_reaches_a_validation_result_or_a_repr():
    """Requirement 6 and 11 together, over a git error that quotes a secret.

    The message here is one git could plausibly produce from a misconfigured
    credential helper, which prints its protocol output on failure.
    """
    leaked = "fatal: helper returned password=hunter2 token=ghp_secretvalue"
    git = git_ok(failures={("ls-remote",): GitCommandError(["ls-remote"], 128, leaked)})
    connection = git_for(GITHUB_HTTPS, git_override=git)

    result = connection.validate()
    serialized = json.dumps(result.to_dict())

    assert "hunter2" not in serialized
    assert "password=" not in serialized
    for rendering in (repr(connection.config), str(connection.config), connection.describe()):
        assert "hunter2" not in rendering
        assert "ghp_" not in rendering


def test_the_configuration_carries_the_derived_facts_and_no_secret():
    """What a UI shows back after the user pastes a URL."""
    payload = GitRepositoryConfig(repository_url=ADO_HTTPS, ref="main").to_dict()

    assert payload["detected_provider"] == "azure_devops"
    assert payload["transport"] == "https"
    assert payload["authentication"] == "git_credential_manager"
    assert not any(
        key in payload for key in ("password", "token", "pat", "secret", "credential")
    )


def test_a_declared_provider_is_checked_case_insensitively():
    GitRepositoryConfig(repository_url=ADO_HTTPS, provider="AZURE_DEVOPS").validate()

    with pytest.raises(ConfigError, match="azure_devops"):
        GitRepositoryConfig(repository_url=ADO_HTTPS, provider="github").validate()


def test_azure_devops_url_spellings_are_the_same_repository():
    """The reuse check compares an existing clone's ``origin`` against the
    configured URL. A clone made over SSH must not look like a different
    repository to a run configured with HTTPS."""
    from discovery_agent.acquisition.providers import normalize_for_compare

    assert normalize_for_compare(ADO_HTTPS) == normalize_for_compare(ADO_SSH)


@pytest.mark.parametrize(
    "url",
    [GITHUB_HTTPS, GITHUB_SSH, ADO_HTTPS, GITLAB_HTTPS, BITBUCKET_HTTPS, SELF_HOSTED_SSH],
)
def test_acquisition_still_clones_every_provider_the_same_way(url, tmp_path):
    """Requirement 12: existing acquisition behaviour is untouched.

    The connection passes its configuration through; ``acquire_repository`` is
    still the only implementation of the clone and reuse rules, and it handles
    all five providers because it never knew which one it had.
    """
    git = FakeGit(
        {
            ("clone",): "",
            ("checkout", "main"): "",
            ("rev-parse", "HEAD"): "1" * 40,
        }
    )
    connection = GitConnection(
        GitRepositoryConfig(repository_url=url, ref="main"), git=git
    )

    source = connection.acquire(input_root=tmp_path)

    assert source.repository_url == url
    assert source.ref == "main"
    assert source.commit_sha == "1" * 40
    assert source.local_path.name == "synapse-workspace"
    assert "clone" in git.subcommands


def test_git_has_exactly_one_execution_boundary():
    """Requirement 13 and 10 together.

    One ``subprocess.run`` in the codebase, and the connection reaches it only
    through the injected runner. A second execution path would be a second
    place where credentials, environment and timeouts have to be got right.
    """
    import inspect as inspect_module

    from discovery_agent.acquisition import git as git_module
    from discovery_agent.connections import git as connection_module

    subprocess_sites = [
        str(path)
        for path in repository_python_files()
        if "subprocess.run(" in path.read_text(encoding="utf-8")
        and path.name != _FABRIC_CLI_MODULE
    ]
    assert subprocess_sites == [str(inspect_module.getfile(git_module))]

    # Code, not prose: the module documents the arrangement in its docstring,
    # and a docstring is not an execution path.
    code = code_of(connection_module)
    assert "subprocess" not in code
    assert "Popen" not in code
    # Every git call this class makes funnels through one helper.
    assert code.count("self.git(") == 1


def test_the_git_connection_holds_no_azure_credential():
    """Requirement 8: the two authentication systems are independent.

    Git credentials belong to the machine and are held by git; nothing here
    can reach an Azure identity, and nothing about Azure is needed to clone.
    """
    import inspect as inspect_module

    from discovery_agent.connections import git as connection_module

    source = inspect_module.getsource(connection_module)
    for forbidden in ("AzureCredentialProvider", "azure.identity", "AccessToken", "ARM_SCOPE"):
        assert forbidden not in source

    connection = git_for(GITHUB_HTTPS)
    assert not hasattr(connection, "credential")
    assert not hasattr(connection, "azure")


def test_no_provider_specific_token_handling_was_introduced():
    """No OAuth client, no PAT prompt, no provider API. Five providers, one
    mechanism per transport, and git does the authenticating."""
    import inspect as inspect_module

    from discovery_agent.acquisition import providers as providers_module
    from discovery_agent.connections import git as connection_module

    for module in (connection_module, providers_module):
        code = code_of(module)
        for forbidden in (
            "oauth",
            "OAuth",
            "client_id",
            "client_secret",
            "personal_access_token",
            "PRIVATE-TOKEN",
            "Authorization",
        ):
            assert forbidden not in code, f"{module.__name__} implements {forbidden}"


def test_the_authenticated_probe_leaves_the_credential_helper_alone():
    """The regression this test exists to prevent, found against a real
    private repository.

    ``credential.interactive=never`` was added here to stop a probe blocking,
    and it broke private repositories outright: in Git Credential Manager it
    is a kill switch evaluated *before* the credential store is consulted, so
    GCM aborted rather than returning the token it already held. The probe
    that authenticates must therefore override no credential config at all --
    non-interactivity comes from ``GIT_TERMINAL_PROMPT=0``, which suppresses
    the prompt without suppressing the helper.
    """
    git = git_ok()

    git_for(GITHUB_HTTPS, git_override=git).validate()

    authenticated = [
        call
        for call in git.calls
        if "ls-remote" in call and "credential.helper=" not in call
    ]
    assert authenticated, "nothing probed the remote with the user's credentials"
    for call in authenticated:
        assert not any(argument == "-c" for argument in call), (
            f"the authenticated probe overrides git config: {call}. Anything "
            f"that disables interactivity also disables the credential helper "
            f"that private repositories depend on."
        )


def test_non_interactivity_comes_from_the_environment_not_from_a_flag():
    """``GIT_TERMINAL_PROMPT=0`` is git's own switch and is set for every
    invocation, including clones. It stops git asking; it does not stop a
    helper answering."""
    import inspect as inspect_module

    from discovery_agent.acquisition import git as git_module

    source = inspect_module.getsource(git_module)

    assert 'env["GIT_TERMINAL_PROMPT"] = "0"' in source


def test_no_probe_overrides_credential_interactivity_anywhere():
    """Stated once, over the module, so the flag cannot come back by a
    different route."""
    from discovery_agent.connections import git as connection_module

    code = code_of(connection_module)

    assert "credential.interactive" not in code
    assert "guiPrompt" not in code


def test_every_probe_is_bounded_by_a_timeout():
    """The second half of the same guarantee: a probe that cannot be answered
    fails, rather than holding a validation screen open indefinitely."""
    recorded = []

    def recording_git(args, cwd=None, timeout=None):
        recorded.append(timeout)
        return "1" * 40 + "\trefs/heads/main"

    git_for(GITHUB_HTTPS, git_override=recording_git).validate()

    assert recorded, "no git call was made"
    assert all(t == PROBE_TIMEOUT_SECONDS for t in recorded), recorded


def test_acquisition_is_left_interactive_so_a_real_clone_can_still_prompt(tmp_path):
    """The non-interactive flags constrain the probe only.

    A user running a clone themselves should still be able to sign in to their
    credential helper; suppressing that would turn a first-time clone into an
    unexplained failure.
    """
    git = FakeGit(
        {("clone",): "", ("checkout", "main"): "", ("rev-parse", "HEAD"): "1" * 40}
    )

    git_for(GITHUB_HTTPS, git_override=git).acquire(input_root=tmp_path)

    for call in git.calls:
        assert "credential.interactive=never" not in call


@pytest.mark.parametrize(
    "stderr, expected_hint",
    [
        (
            # What git says once the helper returns nothing and
            # GIT_TERMINAL_PROMPT=0 stops it asking -- the wording a real
            # uncached host now produces.
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled",
            "sign in to your git credential helper",
        ),
        (
            "fatal: Cannot prompt because user interactivity has been disabled.",
            "sign in to your git credential helper",
        ),
        (
            "git@github.com: Permission denied (publickey).",
            "add your key to the agent",
        ),
        ("Host key verification failed.", "known_hosts"),
    ],
)
def test_a_suppressed_prompt_is_explained_in_terms_of_the_users_problem(
    stderr, expected_hint
):
    """Git says "cannot prompt", which describes our probe rather than the
    user's problem.

    The problem is that this machine has no usable credential for the host,
    and the fix is to sign in to the mechanism the report already named -- so
    that is what the message says.
    """
    git = git_ok(failures={("ls-remote",): GitCommandError(["ls-remote"], 128, stderr)})

    result = git_for(GITHUB_HTTPS, git_override=git).validate()

    assert result.category is ErrorCategory.AUTHENTICATION
    assert expected_hint in result.message
    assert "never accepts a token" in result.message or "retry" in result.message


def test_the_hint_never_suggests_pasting_a_token():
    """The remedy is always "sign in to the mechanism you already have", never
    "give this application a PAT"."""
    git = git_ok(
        failures={
            ("ls-remote",): GitCommandError(
                ["ls-remote"], 128, "fatal: could not read Username: terminal prompts disabled"
            )
        }
    )

    message = git_for(ADO_HTTPS, git_override=git).validate().message.lower()

    for forbidden in ("paste", "enter your token", "provide a pat", "set a password"):
        assert forbidden not in message


def test_interactive_browser_is_implemented_and_asks_rather_than_inheriting_az_login():
    from discovery_agent.connections.azure import InteractiveBrowserCredentialProvider

    sdk = FakeSdkCredential()
    provider = credential_provider_for(
        azure_config(credential_method=CredentialMethod.INTERACTIVE_BROWSER)
    )
    assert isinstance(provider, InteractiveBrowserCredentialProvider)
    assert not isinstance(provider, AzureCliCredentialProvider)
    provider = InteractiveBrowserCredentialProvider(tenant_id=TENANT, credential=sdk)
    assert provider.token(ARM_SCOPE).value
    assert provider.describe() == f"Interactive Azure sign-in (tenant {TENANT})"


