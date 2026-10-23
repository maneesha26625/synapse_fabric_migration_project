"""The consultation's facts: counts of what discovery recorded, and never a guess."""

from types import SimpleNamespace as NS

from discovery_agent.api import consult_facts


def record(kind, content):
    return NS(artifact=NS(value=kind), content=content)


def extra(source_type, name="x", **metadata):
    return NS(source_type=source_type, name=name, metadata=metadata)


def job():
    notebook = record("notebook", NS(
        findings=(NS(construct="mssparkutils.fs", category=NS(value="synapse_utils")), NS(construct="mssparkutils.credentials", category=NS(value="credential"))),
        all_resources=(NS(category=NS(value="storage"), scheme="abfss"),),
        packages=(NS(kind=NS(value="pip_install")),),
    ))
    plain = record("notebook", NS(findings=(), all_resources=(), packages=()))
    script = record("sqlscript", NS(features=(NS(kind=NS(value="cross_database")), NS(kind=NS(value="openrowset"))), dynamic_sql=("EXEC(...)",)))
    pipeline = record("pipeline", NS(all_activities=(NS(recognized=True), NS(recognized=False), NS(recognized=False))))
    ls_shir = record("linkedService", NS(integration_runtime="shir1", uses_key_vault=True, authentication=(NS(authentication_type=NS(value="managed_identity")),)))
    ls_plain = record("linkedService", NS(integration_runtime=None, uses_key_vault=False, authentication=()))
    tables = (
        NS(is_external=False, distribution=NS(value="hash"), storage=NS(value="clustered_columnstore"),
           columns=(NS(is_identity=True),), indexes=(NS(is_primary_key=True),)),
        NS(is_external=False, distribution=NS(value="round_robin"), storage=NS(value="heap"), columns=(), indexes=()),
        NS(is_external=True, distribution=NS(value="unknown"), storage=NS(value="unknown"), columns=(), indexes=()),
    )
    view = NS(dynamic_sql=(), referenced_objects=(NS(database="OtherDb"),))
    proc = NS(dynamic_sql=("sp_executesql",), referenced_objects=(NS(database=None),))
    security = NS(masks=(NS(column="email"), NS(column="phone")), permissions=(NS(column="salary"), NS(column=None)))
    catalog = NS(tables=tables, views=(view,), procedures=(proc,), functions=(), security=security)
    extras = [
        extra("Spark Library"), extra("Spark Pool", customLibraries=["a.whl"]), extra("Spark Pool"),
        extra("Integration Runtime", name="shir1", type="SelfHosted"), extra("Integration Runtime", name="auto", type="Managed", managedVirtualNetwork="default"),
        extra("Trigger", triggerType="ScheduleTrigger", runtimeState="Started"), extra("Trigger", triggerType="TumblingWindowTrigger", runtimeState="Stopped"),
        extra("Trigger", triggerType="BlobEventsTrigger", runtimeState="Started"),
    ]
    records = {str(i): r for i, r in enumerate([notebook, plain, script, pipeline, ls_shir, ls_plain])}
    return NS(records_by_id=records, extras_by_id={str(i): e for i, e in enumerate(extras)}, run=NS(catalog=catalog))


def test_counts_what_discovery_recorded():
    out = consult_facts.collect(job())
    f = out["facts"]
    assert out["unreadable"] == []
    assert f["mssparkutilsNotebooks"] == 1
    assert f["notebooksWithSecrets"] == 1
    assert f["notebooksWithStoragePaths"] == 1
    assert f["notebooksInstallingPackages"] == 1
    assert f["customLibraries"] == 2  # one library, one pool with custom libraries
    assert f["dynamicSqlObjects"] == 2  # the procedure and the script
    assert f["crossDatabaseObjects"] == 2  # the view's three-part name and the script
    assert f["distribution"] == {"hash": 1, "round_robin": 1}  # external tables have none
    assert f["columnstoreTables"] == 1
    assert f["identityTables"] == 1
    assert f["primaryKeyTables"] == 1
    assert f["externalTables"] == 1
    assert f["externalSqlFeatures"] == 1
    assert f["maskedColumns"] == 2
    assert f["columnGrants"] == 1
    assert f["selfHostedRuntimes"] == 1
    assert f["managedVnetRuntimes"] == 1
    assert f["linkedServicesOnSelfHostedRuntime"] == 1
    assert f["keyVaultLinkedServices"] == 1
    assert f["managedIdentityLinkedServices"] == 1
    assert f["unsupportedActivities"] == {"activities": 2, "pipelines": 1}
    assert f["triggers"] == {"schedule": 1, "tumblingWindow": 1, "event": 1, "other": 0, "started": 2}
    assert "materialized views" in out["notRecorded"]


def test_an_estate_without_a_pool_or_artifacts_reports_zeros():
    f = consult_facts.collect(NS(records_by_id={}, extras_by_id={}, run=NS(catalog=None)))["facts"]
    assert f["identityTables"] == 0 and f["distribution"] == {} and f["maskedColumns"] == 0
    assert f["triggers"]["schedule"] == 0


def test_a_fact_that_cannot_be_read_is_reported_not_guessed():
    broken = NS(records_by_id={"1": record("notebook", NS(findings=object(), all_resources=(), packages=()))}, extras_by_id={}, run=None)
    out = consult_facts.collect(broken)
    assert "mssparkutilsNotebooks" in out["unreadable"]
    assert "mssparkutilsNotebooks" not in out["facts"]
    assert out["facts"]["externalTables"] == 0
