"""The connection chain, against real Azure resources.

**Opt-in only.** Skipped unless the opt-in flag and the configuration are both
present:

    CONNECTIONS_INTEGRATION=1
    AZURE_SUBSCRIPTION_ID=<guid>
    SYNAPSE_RESOURCE_GROUP=<resource group>
    SYNAPSE_WORKSPACE_NAME=<workspace>
    SYNAPSE_SQL_POOL=<dedicated pool>

and, for the git leg, ``DISCOVERY_REPOSITORY_URL``. Authentication is
whatever ``az login`` already established; nothing here accepts a credential.

The opt-in flag is separate from the configuration on purpose, exactly as it
is for ``test_sql_dedicated_pool_integration.py``: having a workspace
configured for the dev command must never be enough to make the normal test
suite sign in and open network connections.

What only a real subscription can establish, and what the unit suite
therefore cannot: that ``AzureCliCredential`` returns a token these audiences
accept, that ARM's payloads really have the shape the mapping functions read,
and that the ODBC driver really accepts the token encoding. Everything else
is asserted offline in ``test_connections.py``.

These tests assert the *shape* of what comes back and never its content. The
subscription belongs to a client; a test that asserted a particular workspace
id would fail for everyone else.
"""

from __future__ import annotations

import json
import os

import pytest

from discovery_agent.connections.azure import ARM_SCOPE, SQL_SCOPE
from discovery_agent.connections.manager import ConnectionManager
from discovery_agent.connections.models import settings_from_environment
from discovery_agent.connections.validation import ValidationStatus
from discovery_agent.extractors.models import SourceType

ENABLED = os.environ.get("CONNECTIONS_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED
    or not os.environ.get("AZURE_SUBSCRIPTION_ID")
    or not os.environ.get("SYNAPSE_WORKSPACE_NAME")
    or not os.environ.get("SYNAPSE_RESOURCE_GROUP"),
    reason=(
        "live connection integration is opt-in: set CONNECTIONS_INTEGRATION=1 "
        "plus AZURE_SUBSCRIPTION_ID, SYNAPSE_RESOURCE_GROUP and "
        "SYNAPSE_WORKSPACE_NAME"
    ),
)


@pytest.fixture(scope="module")
def connections():
    """One manager for the module, so one sign-in and one token cache."""
    return ConnectionManager(settings_from_environment())


@pytest.fixture(scope="module")
def report(connections):
    """One full validation run, shared by every assertion below."""
    return connections.validate_all(probe_remote=bool(os.environ.get("DISCOVERY_REPOSITORY_URL")))


def test_the_azure_cli_credential_returns_tokens_for_both_audiences(connections):
    """The thing no fake can establish: that this tenant issues these tokens."""
    management = connections.azure().management_token()
    sql = connections.azure().sql_token()

    assert management.scope == ARM_SCOPE
    assert sql.scope == SQL_SCOPE
    assert management.is_valid()
    assert sql.is_valid()
    assert management.value != sql.value, "different audiences, different tokens"


def test_the_subscription_is_readable(report):
    result = report.result_for(SourceType.AZURE)

    assert result.status is ValidationStatus.OK, result.message
    assert result.details["subscription_state"]


def test_the_workspace_payload_has_the_shape_the_mapping_reads(connections):
    """Proves ``workspaceUID`` and ``connectivityEndpoints`` are really there.

    Both are documented, but only the service can say whether this API version
    returns them for this workspace. A None would mean the call succeeded and
    the field never arrived.
    """
    metadata = connections.synapse().metadata()

    assert metadata.workspace_id, "ARM did not return properties.workspaceUID"
    assert metadata.sql_endpoint
    assert metadata.development_endpoint
    assert metadata.provisioning_state


def test_the_dedicated_pool_is_readable_and_reports_a_status(connections):
    if not connections.settings.synapse.sql_pool_name:
        pytest.skip("no dedicated sql pool is configured")

    pool = connections.synapse().sql_pool_metadata()

    assert pool.name
    assert pool.status, "ARM did not return properties.status for the pool"


def test_an_entra_token_opens_a_real_session_on_the_pool(report):
    """The whole point of the refactor: a SQL session with no password, no
    browser prompt, and no pasted token -- on the same sign-in Azure used."""
    pytest.importorskip("pyodbc", reason="pyodbc is required for a live session")
    result = report.result_for(SourceType.SQL)

    assert result.status is ValidationStatus.OK, result.message
    assert result.details["connected_database"]
    assert result.details["principal"] != "unknown"
    assert result.details["authentication"] == "access_token"


def test_the_source_handed_to_discovery_connects_the_same_way(connections):
    """The hand-off, exercised end to end: the existing CatalogSource, opened
    with the central credential."""
    pytest.importorskip("pyodbc", reason="pyodbc is required for a live session")

    with connections.sql().source() as source:
        database = source.database()

    assert database.name
    assert database.server


def test_the_repository_is_reachable_with_the_users_own_git(report):
    if not os.environ.get("DISCOVERY_REPOSITORY_URL"):
        pytest.skip("no repository is configured")
    result = report.result_for(SourceType.REPOSITORY)

    assert result.status is ValidationStatus.OK, result.message


def test_no_live_result_carries_a_credential(report):
    """The assertion that matters most here: these results hold real tokens'
    worth of context and must still contain none of them."""
    serialized = json.dumps(report.to_dict())

    for forbidden in ("eyJ", "Bearer", "PWD=", "password", "access_token="):
        assert forbidden not in serialized


# --- the consolidation, against real resources ------------------------------


def test_one_identity_serves_every_connection_in_a_real_run(connections):
    """The "log in once" rule, on the live path.

    Not a mock: these are the objects a real run uses, and they must be
    holding the same credential provider -- one `az login`, one session, one
    token cache.
    """
    identities = {
        id(connections.credential()),
        id(connections.azure().credential),
        id(connections.synapse().azure.credential),
        id(connections.sql().credential),
    }

    assert len(identities) == 1


def test_a_second_token_for_the_same_audience_comes_from_the_cache(connections):
    """Shelling out to `az` per query would make discovery visibly slow, and
    the cache is what makes one sign-in serve a whole run."""
    first = connections.credential().token(ARM_SCOPE)
    second = connections.credential().token(ARM_SCOPE)

    assert second is first, "the token was re-acquired rather than reused"
    assert first.is_valid()


def test_a_real_sql_query_executes_over_the_entra_session(connections):
    """Requirement M: an actual statement, on the real pool, read-only.

    ``validate()`` runs its own check; this executes a query through the
    connection directly, which is what discovery will do.
    """
    pytest.importorskip("pyodbc", reason="pyodbc is required for a live session")

    connection = connections.sql().connect()
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT DB_NAME()")
            row = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        connection.close()

    assert row is not None
    assert row[0] == connections.settings.synapse.resolved_database


def test_the_sql_session_needs_no_password_and_no_pasted_token(connections):
    """The connection string that reached the driver, checked on the real one."""
    from discovery_agent.sql.connection import build_connection_string

    connection = connections.sql()
    rendered = build_connection_string(connection.config, connection.authentication())

    for forbidden in ("PWD=", "UID=", "Password", "Authentication=", "eyJ"):
        assert forbidden not in rendered


def test_the_configured_git_ref_is_actually_present_on_the_remote(connections):
    """Requirement: ref access, not merely repository reachability."""
    if not os.environ.get("DISCOVERY_REPOSITORY_URL"):
        pytest.skip("no repository is configured")

    result = connections.git().validate(probe_remote=True)

    assert result.status is ValidationStatus.OK, result.message
    assert int(result.details["refs_visible"]) > 0


def test_the_whole_chain_reports_ready(report):
    """The end-to-end acceptance check, as an operator would read it."""
    from discovery_agent.connections.__main__ import render

    rendered = render(report)

    assert "Overall: READY" in rendered, rendered
    for forbidden in ("eyJ", "Bearer", "PWD=", "password"):
        assert forbidden not in rendered
