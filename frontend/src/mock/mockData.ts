// DEMO DATA ONLY. Generated deterministically so the UI can be exercised (and
// paginated at realistic size) without a backend. None of it describes a real
// Synapse workspace, and the UI labels it as demo data wherever it is shown.

import type { ActivityMapping, DependencyRef, ObjectDetail, ObjectStatus } from "../types";
import { baseWave, categoryOf, componentOf, mockActivity, mockMap, mockReferenceTarget, stepsFor } from "./mockMapping";

function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const ENTITIES = [
  "Customer", "Product", "Sales", "Order", "Invoice", "Supplier", "Employee", "Store",
  "Region", "Inventory", "Shipment", "Payment", "Trip", "Fare", "Vendor", "Campaign",
  "Account", "Contract", "Ledger", "Session",
];

interface Def { key: string; type: string }
const DEFS: Record<string, Def> = {
  table: { key: "dedicated_sql_table", type: "Table" },
  xtable: { key: "dedicated_sql_table", type: "External Table" },
  view: { key: "sql_view", type: "View" },
  proc: { key: "stored_procedure", type: "Stored Procedure" },
  script: { key: "sqlscript", type: "SQL Script" },
  pipeline: { key: "pipeline", type: "Pipeline" },
  dataset: { key: "dataset", type: "Dataset" },
  ls: { key: "linkedService", type: "Linked Service" },
  notebook: { key: "notebook", type: "Notebook" },
  sjd: { key: "sparkJobDefinition", type: "Spark Job Definition" },
  pool: { key: "dedicated_sql_pool", type: "Dedicated SQL Pool" },
  schema: { key: "schema", type: "Schema" },
  spark: { key: "spark_pool", type: "Spark Pool" },
  lib: { key: "spark_library", type: "Spark Library" },
  trigger: { key: "trigger", type: "Trigger" },
  storage: { key: "storage", type: "Storage Reference" },
  ir: { key: "integration_runtime", type: "Integration Runtime" },
};

export interface MockOptions {
  workspace: string;
  /** Drop these categories, as if their endpoints could not be read. */
  omitCategories?: string[];
}

const SYNAPSE_API = ["mssparkutils.fs", "TokenLibrary", "spark.synapse.linkedService"];

export function generateObjects({ workspace, omitCategories = [] }: MockOptions): ObjectDetail[] {
  const rand = rng(20240607);
  const pick = <T,>(xs: T[]): T => xs[Math.floor(rand() * xs.length)];
  const objects: ObjectDetail[] = [];
  const byKind: Record<string, ObjectDetail[]> = {};
  const used = new Set<string>();
  const discoveredAt = new Date().toISOString();

  const dep = (o: ObjectDetail, kind: string, location: string): DependencyRef => ({
    name: o.name, kind, type: o.type, objectId: o.id, location, fabricTarget: o.fabricTarget,
  });
  const external = (name: string, kind: string, location: string): DependencyRef => ({
    name, kind, type: null, objectId: null, location, fabricTarget: mockReferenceTarget(kind, null),
  });

  function add(
    kind: keyof typeof DEFS,
    name: string,
    build: (self: ObjectDetail) => { deps: DependencyRef[]; config: Record<string, unknown>; specific?: boolean; activities?: ActivityMapping[] },
  ) {
    const def = DEFS[kind];
    if (omitCategories.includes(categoryOf(def.type))) return;
    const isSql = def.type === "Table" || def.type === "External Table" || def.type === "View" || def.type === "Stored Procedure";
    let uniq = name;
    for (let n = 2; used.has(`${kind}:${uniq}`); n++) uniq = `${name}_${n}`;
    used.add(`${kind}:${uniq}`);
    const id = isSql ? `sql://mockpool/${uniq.replace(".", "/")}` : `synapse://${def.key}/${uniq}`;
    const roll = rand();
    const status: ObjectStatus = roll < 0.04 ? "Warning" : roll < 0.06 ? "Partial" : roll < 0.07 ? "Failed" : "Discovered";
    const self: ObjectDetail = {
      id, name: uniq, type: def.type, category: categoryOf(def.type), workspace, status,
      dependencyCount: 0, sources: isSql ? ["sql"] : ["synapse"],
      schema: isSql && uniq.includes(".") ? uniq.split(".")[0] : "",
      size: "",
      component: componentOf(def.type),
      wave: 1,
      steps: stepsFor(def.type),
      actions: [],
      ...mockMap(def.type),
      discoveredAt,
      target: { platform: "Microsoft Fabric", component: "", componentType: "" },
      migration: { path: "Direct Target", route: "", assessmentRequired: true, automationPotential: "", mappingStatus: "Mapped", workstream: "", classification: "DIRECT", action: "" },
      notes: [],
      overview: { logicalId: id, sources: isSql ? ["sql"] : ["synapse"], note: "Demo data" },
      configuration: null, dependencies: [], referencedBy: [], activities: [],
      issues:
        status === "Warning" ? [{ code: "unsupported_construct", message: "A construct in this definition was not fully understood (demo).", source: "synapse", facet: "definition" }]
        : status === "Failed" ? [{ code: "opaque_definition", message: "The definition exists but its body was withheld (demo).", source: "sql", facet: "definition" }] : [],
      rawMetadata: null,
    };
    const { deps, config, specific, activities } = build(self);
    Object.assign(self, mockMap(def.type, !!specific));
    self.dependencies = deps;
    self.dependencyCount = deps.length;
    self.activities = activities ?? [];
    self.configuration = status === "Failed" ? null : config;
    self.target = { platform: "Microsoft Fabric", component: self.fabricTarget, componentType: self.targetType };
    self.migration = {
      path: self.migrationPath, route: `Synapse ${def.type} → ${self.fabricTarget}`, assessmentRequired: true,
      automationPotential: self.automationPotential, mappingStatus: self.mappingStatus, workstream: self.workstream,
      classification: self.classification, action: self.action,
    };
    self.size = self.type === "Table" || self.type === "External Table" ? `${(config.columnCount as number) ?? 0} columns`
      : self.type === "Pipeline" ? `${activities?.length ?? 0} activities`
      : self.type === "Notebook" ? `${(config.cells as number) ?? 0} cells` : "";
    self.notes = [
      "Preliminary mapping from the object type. Compatibility is decided in Assessment.",
      ...(specific ? [`Synapse-specific constructs were found: ${SYNAPSE_API.join(", ")}. They are identified only; nothing is changed.`] : []),
    ];
    if (specific) self.overview.synapseSpecificConstructs = SYNAPSE_API;
    self.rawMetadata = { identity: { logical_id: id, name: uniq, artifact: def.key, workspace }, dependencies: deps.map((d) => d.name), demo: true };
    objects.push(self);
    (byKind[kind] ??= []).push(self);
  }

  // Order matters: things that are depended on come first.
  add("pool", "TransportDW", () => ({ deps: [], config: { status: "Online", collation: "SQL_Latin1_General_CP1_CI_AS", sku: { name: "DW500c" } } }));
  add("ir", "AutoResolveIntegrationRuntime", () => ({ deps: [], config: { type: "Managed", state: "Started" } }));
  for (let i = 0; i < 3; i++) add("spark", `sparkpool0${i + 1}`, () => ({ deps: [], config: { sparkVersion: "3.4", nodeSize: pick(["Small", "Medium", "Large"]), nodeCount: 3 + i, autoScale: { enabled: true, minNodeCount: 3, maxNodeCount: 10 }, autoPause: { enabled: true, delayInMinutes: 15 } } }));
  for (let i = 0; i < 6; i++) add("lib", pick(["pandas", "scikit-learn", "great_expectations", "custom_utils", "pyarrow", "delta-spark"]) + `-${i}.whl`, () => ({ deps: [], config: { scope: "Workspace", type: "Python wheel" } }));
  const lsNames = ["AdlsGen2_Raw", "AdlsGen2_Curated", "KeyVault", "SynapseDW", "AzureSql_Ops", "Blob_Landing", "Rest_Partner", "Http_OpenData"];
  for (let i = 0; i < 25; i++) {
    const n = lsNames[i % lsNames.length] + (i >= lsNames.length ? `_${Math.floor(i / lsNames.length) + 1}` : "");
    add("ls", `LS_${n}`, () => ({
      deps: i % 3 === 0 ? [external("kv-migration-demo", "secret", "typeProperties.password")] : [],
      config: { type: pick(["AzureBlobFS", "AzureKeyVault", "AzureSqlDW", "HttpServer"]), authentication: pick(["ManagedIdentity", "AccountKey (Key Vault reference)"]) },
    }));
  }
  const schemas = ["dbo", "stg", "dw"];
  for (let i = 0; i < 800; i++) {
    const e = ENTITIES[i % ENTITIES.length];
    const prefix = pick(["Dim", "Fact", "Stg", "Ref"]);
    const schema = pick(schemas);
    const external_ = i % 37 === 0;
    add(external_ ? "xtable" : "table", `${schema}.${prefix}${e}${i >= ENTITIES.length ? i : ""}`, () => ({
      deps: external_ ? [external("abfss://raw@demostorage.dfs.core.windows.net/" + e.toLowerCase(), "storage_path", "location")] : [],
      config: { distribution: pick(["HASH", "ROUND_ROBIN", "REPLICATE"]), index: pick(["CLUSTERED COLUMNSTORE", "HEAP"]), columnCount: 4 + Math.floor(rand() * 40), isExternal: external_ },
    }));
  }
  const tables = byKind.table ?? [];
  for (let i = 0; i < 120; i++) {
    add("view", `dbo.vw_${ENTITIES[i % ENTITIES.length]}${i}`, () => ({
      deps: tables.length ? dedupe([dep(pick(tables), "sql_object", "definition"), dep(pick(tables), "sql_object", "definition")]) : [],
      config: { language: "T-SQL", columns: 5 + Math.floor(rand() * 20) },
    }));
  }
  const views = byKind.view ?? [];
  for (let i = 0; i < 90; i++) {
    add("proc", `dbo.usp_Load${ENTITIES[i % ENTITIES.length]}${i}`, () => {
      const deps: DependencyRef[] = [];
      for (let k = 0; k < 1 + Math.floor(rand() * 4); k++) {
        const pool = rand() < 0.7 || !views.length ? tables : views;
        if (pool.length) deps.push(dep(pick(pool), "sql_object", "definition"));
      }
      return { deps: dedupe(deps), config: { parameters: Math.floor(rand() * 5), language: "T-SQL" } };
    });
  }
  for (let i = 0; i < 40; i++) {
    add("script", `SQL_Script_${ENTITIES[i % ENTITIES.length]}_${i}`, () => ({
      deps: tables.length && rand() < 0.7 ? [dep(pick(tables), "sql_object", "properties.content.query")] : [],
      config: { language: "T-SQL", targetPool: "TransportDW" },
    }));
  }
  const lss = byKind.ls ?? [];
  for (let i = 0; i < 300; i++) {
    add("dataset", `DS_${ENTITIES[i % ENTITIES.length]}_${pick(["Parquet", "Csv", "SqlTable", "Json"])}${i}`, () => ({
      deps: lss.length ? [dep(pick(lss), "artifact", "properties.linkedServiceName")] : [],
      config: { type: pick(["Parquet", "DelimitedText", "AzureSqlDWTable", "Json"]), declaredSchemaColumns: Math.floor(rand() * 12) },
    }));
  }
  const sparks = byKind.spark ?? [];
  for (let i = 0; i < 70; i++) {
    const specific = i % 3 === 0;
    add("notebook", `NB_${ENTITIES[i % ENTITIES.length]}_${pick(["Transform", "Clean", "Enrich", "Aggregate"])}${i}`, () => ({
      deps: [
        sparks.length ? dep(pick(sparks), "compute", "properties.bigDataPool") : external("sparkpool01", "compute", "properties.bigDataPool"),
        ...(rand() < 0.5 ? [external("abfss://raw@demostorage.dfs.core.windows.net/", "storage_path", "cells[2]")] : []),
      ],
      config: { language: "PySpark", cells: 3 + Math.floor(rand() * 25), sessionExecutors: pick([2, 4, 8]) },
      specific,
    }));
  }
  for (let i = 0; i < 12; i++) {
    add("sjd", `SJD_${ENTITIES[i % ENTITIES.length]}Batch${i}`, () => ({
      deps: [sparks.length ? dep(pick(sparks), "compute", "properties.targetBigDataPool") : external("sparkpool01", "compute", "properties.targetBigDataPool")],
      config: { language: pick(["PySpark", "Scala"]), mainFile: "abfss://jobs@demostorage.dfs.core.windows.net/main.py" },
    }));
  }
  const datasets = byKind.dataset ?? [];
  const notebooks = byKind.notebook ?? [];
  const sjds = byKind.sjd ?? [];
  for (let i = 0; i < 60; i++) {
    add("pipeline", `PL_${i === 0 ? "Master" : ENTITIES[i % ENTITIES.length] + "_" + pick(["Ingest", "Load", "Publish"]) + i}`, () => {
      const deps: DependencyRef[] = [];
      const earlier = byKind.pipeline ?? [];
      const activities: ActivityMapping[] = [];
      const act = (type: string, ref?: string) => {
        const m = mockActivity(type);
        activities.push({ name: `${type}_${activities.length + 1}`, type, parent: null, source: type === "Copy" ? "DelimitedTextSource" : null, sink: type === "Copy" ? "SqlDWSink" : null, dependsOn: activities.length ? [activities[activities.length - 1].name] : [], references: ref ? [ref] : [], expressionCount: Math.floor(rand() * 3), ...m });
      };
      for (let k = 0; k < 2 + Math.floor(rand() * 4); k++) if (datasets.length) { const d = pick(datasets); deps.push(dep(d, "artifact", `activities[${k}].inputs`)); act("Copy", d.name); }
      if (notebooks.length && rand() < 0.6) { const n = pick(notebooks); deps.push(dep(n, "artifact", "activities[1].typeProperties.notebook")); act("SynapseNotebook", n.name); }
      if (sjds.length && rand() < 0.2) { const s = pick(sjds); deps.push(dep(s, "artifact", "activities.sparkJob")); act("SparkJob", s.name); }
      if (earlier.length && rand() < 0.5) { const p = pick(earlier); deps.push(dep(p, "artifact", "activities[0].typeProperties.pipeline")); act("ExecutePipeline", p.name); }
      if (rand() < 0.15) act("DataFlow");
      deps.push((byKind.ir ?? [])[0] ? dep((byKind.ir ?? [])[0], "compute", "activities[0].connectVia") : external("AutoResolveIntegrationRuntime", "compute", "activities[0].connectVia"));
      return { deps: dedupe(deps), config: { activities: activities.length, parameters: Math.floor(rand() * 4) }, activities };
    });
  }
  const pipelines = byKind.pipeline ?? [];
  for (let i = 0; i < 8; i++) {
    add("trigger", `TR_${pick(["Daily", "Hourly", "Weekly", "OnBlobCreate"])}_${i}`, () => ({
      deps: pipelines.length ? [dep(pipelines[(i * 7) % pipelines.length], "artifact", "properties.pipelines")] : [],
      config: { triggerType: pick(["ScheduleTrigger", "BlobEventsTrigger", "TumblingWindowTrigger"]), runtimeState: pick(["Started", "Stopped"]), frequency: "Day", interval: 1 },
    }));
  }
  for (const [account, container] of [["demostorage", "raw"], ["demostorage", "curated"], ["demolake", "landing"]]) {
    add("storage", `${account}/${container}`, () => ({ deps: [], config: { account, container, pathCount: 12 } }));
  }
  const bySchema = new Map<string, ObjectDetail[]>();
  for (const o of objects) if (o.id.startsWith("sql://mockpool/")) { const s = o.name.split(".")[0]; (bySchema.get(s) ?? bySchema.set(s, []).get(s)!).push(o); }
  for (const [s, members] of bySchema) {
    add("schema", `TransportDW.${s}`, () => ({ deps: members.slice(0, 200).map((m) => dep(m, "sql_object", "contains")), config: { schemaName: s, objectCounts: { total: members.length } } }));
  }

  // "Referenced by" is the reverse of the dependencies that joined.
  const byId = new Map(objects.map((o) => [o.id, o]));
  for (const o of objects) for (const d of o.dependencies) {
    if (d.objectId && d.location !== "contains") byId.get(d.objectId)?.referencedBy.push({ name: o.name, type: o.type, objectId: o.id });
  }
  for (const o of objects) {
    o.actions = [
      { label: "Definition discovered", state: o.status === "Failed" ? "warn" : "ok" },
      { label: o.dependencies.length ? `${o.dependencies.length} dependencies discovered` : "No dependencies discovered", state: "ok" },
      ...(o.dependencies.some((d) => d.type === "Linked Service" || d.kind === "secret") ? [{ label: "Connection requires recreation", state: "warn" as const }] : []),
      ...(o.referencedBy.some((r) => r.type === "Trigger") ? [{ label: "Trigger requires recreation", state: "warn" as const }] : []),
    ];
  }

  // Dependency-ordered waves (demo copy of the backend rule).
  const wave = new Map<string, number>();
  const visiting = new Set<string>();
  const waveOf = (o: ObjectDetail): number => {
    const known = wave.get(o.id);
    if (known) return known;
    const base = baseWave(o.type);
    visiting.add(o.id);
    let best = base;
    for (const d of o.dependencies) {
      if (!d.objectId || d.location === "contains" || visiting.has(d.objectId)) continue;
      const dep = byId.get(d.objectId);
      if (!dep) continue;
      const w = waveOf(dep);
      best = Math.max(best, w >= base ? w + 1 : base);
    }
    visiting.delete(o.id);
    wave.set(o.id, best);
    return best;
  };
  for (const o of objects) o.wave = waveOf(o);
  return objects;
}

function dedupe(deps: DependencyRef[]): DependencyRef[] {
  const seen = new Set<string>();
  return deps.filter((d) => {
    const k = `${d.kind}|${d.name}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
