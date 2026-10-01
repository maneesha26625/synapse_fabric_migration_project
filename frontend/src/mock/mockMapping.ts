// DEMO ONLY. A small copy of the backend's mapping table, actions and wave
// ordering so demo objects carry plausible values. The source of truth is the
// Python backend (src/discovery_agent/mapping); the live UI never uses this
// file and no mapping logic exists anywhere else in the UI.

import type { Classification, ComponentRow, MappingStatus, MigrationPath, ObjectRow } from "../types";

type Rule = [target: string, targetType: string, workstream: string, path: MigrationPath, automation: string, action: string];

const RULES: Record<string, Rule> = {
  "Dedicated SQL Pool": ["Fabric Data Warehouse", "Warehouse", "Data Warehouse", "Target With Transformation", "Partial", "Migrate schema + tables + SQL"],
  "Serverless SQL": ["Fabric SQL analytics endpoint / Warehouse / Lakehouse SQL endpoint", "SQL endpoint", "Data Warehouse", "Requires Assessment", "Not determined", "Review workload and redesign queries where required"],
  Schema: ["Fabric Warehouse Schema", "Warehouse Schema", "Data Warehouse", "Direct Target", "Candidate", "Create the schema in the Fabric Warehouse"],
  Table: ["Fabric Warehouse Table", "Warehouse Table", "Data Warehouse", "Target With Transformation", "Partial", "Recreate the table definition and load its data"],
  View: ["Fabric Warehouse View", "Warehouse View", "Data Warehouse", "Direct Target", "Candidate", "Recreate the view after its tables exist"],
  "Stored Procedure": ["Fabric Warehouse Stored Procedure", "Warehouse Stored Procedure", "Data Warehouse", "Target With Refactoring", "Partial", "Recreate the procedure, adjusting T-SQL where Fabric differs"],
  "SQL Script": ["Fabric Data Warehouse (SQL query)", "Warehouse SQL Query", "Data Warehouse", "Requires Assessment", "Not determined", "Review the script and decide where it runs in Fabric"],
  "External Table": ["Requires Assessment (Fabric Warehouse or Fabric Lakehouse)", "Undetermined", "OneLake / Storage", "Requires Assessment", "Not determined", "Assess storage and access pattern"],
  "Spark Pool": ["Fabric Spark pool / Environment configuration", "Environment", "Data Engineering", "Target With Transformation", "Partial", "Reconfigure compute/runtime"],
  "Spark Library": ["Fabric Environment libraries", "Environment library", "Data Engineering", "Requires Assessment", "Not determined", "Add the package to a Fabric Environment"],
  Notebook: ["Fabric Notebook", "Notebook", "Data Engineering", "Direct Target", "Candidate", "Migrate notebook + dependencies"],
  "Spark Job Definition": ["Fabric Spark Job Definition", "Spark Job Definition", "Data Engineering", "Direct Target", "Candidate", "Reconfigure execution"],
  Pipeline: ["Fabric Data Factory Pipeline", "Data Pipeline", "Data Factory", "Target With Transformation", "Partial", "Migrate pipeline activities and connections"],
  Dataset: ["Fabric Connection / pipeline activity configuration", "Connection settings", "Connections", "Requires Reconfiguration", "Partial", "Review based on usage (pipeline, Dataflow Gen2 or connection)"],
  "Linked Service": ["Fabric Connection", "Connection", "Connections", "Requires Reconfiguration", "Partial", "Recreate connection"],
  Trigger: ["Fabric Data Factory pipeline schedule / trigger", "Schedule or trigger", "Data Factory", "Requires Assessment", "Partial", "Recreate schedule or event trigger"],
  "Storage Reference": ["OneLake (shortcut or copy)", "OneLake", "OneLake / Storage", "Requires Assessment", "Partial", "Choose between an OneLake shortcut and a copy"],
  "Security Object": ["Fabric security model", "Security", "Security & Governance", "Requires Assessment", "Manual", "Remap users, groups and permissions"],
  "Integration Runtime": ["Fabric connection / data gateway configuration", "Network configuration", "Connections", "Manual / Special Handling", "Manual", "Manual / reconfiguration assessment"],
  "Networking Configuration": ["Fabric network configuration", "Network configuration", "Security & Governance", "Manual / Special Handling", "Manual", "Manual / reconfiguration assessment"],
};

const STATUS: Record<MigrationPath, MappingStatus> = {
  "Direct Target": "Mapped",
  "Target With Transformation": "Mapped with Transformation",
  "Target With Refactoring": "Mapped with Transformation",
  "Requires Reconfiguration": "Mapped with Transformation",
  "Requires Assessment": "Requires Assessment",
  "Manual / Special Handling": "No Automatic Mapping",
};

const CLASS: Record<MigrationPath, Classification> = {
  "Direct Target": "DIRECT",
  "Target With Transformation": "TRANSFORM",
  "Target With Refactoring": "TRANSFORM",
  "Requires Reconfiguration": "RECONFIGURE",
  "Requires Assessment": "REVIEW",
  "Manual / Special Handling": "MANUAL",
};

const CATEGORY: Record<string, string> = {
  "Dedicated SQL Pool": "SQL", Schema: "SQL", Table: "SQL", "External Table": "SQL", View: "SQL",
  "Stored Procedure": "SQL", "SQL Script": "SQL", "Spark Pool": "Spark", "Spark Library": "Spark",
  Notebook: "Spark", "Spark Job Definition": "Spark", Pipeline: "Integration", Dataset: "Integration",
  "Linked Service": "Integration", Trigger: "Integration", "Storage Reference": "Storage",
  "Integration Runtime": "Networking",
};

const COMPONENT: Record<string, string> = {
  "Dedicated SQL Pool": "Dedicated SQL Pool", Schema: "Dedicated SQL Pool", Table: "Dedicated SQL Pool",
  "External Table": "Dedicated SQL Pool", View: "Dedicated SQL Pool", "Stored Procedure": "Dedicated SQL Pool",
  "SQL Script": "Synapse SQL", "Spark Pool": "Apache Spark", "Spark Library": "Apache Spark",
  Notebook: "Apache Spark", "Spark Job Definition": "Apache Spark", Pipeline: "Synapse Pipelines",
  Dataset: "Synapse Pipelines", "Linked Service": "Synapse Pipelines", Trigger: "Synapse Pipelines",
  "Storage Reference": "Storage (ADLS Gen2)", "Integration Runtime": "Integration Runtime",
};

const STEPS: Record<string, string[]> = {
  Pipeline: ["Export the Synapse pipeline definition", "Convert supported activities", "Map linked services to Fabric connections", "Recreate unsupported activities", "Deploy to Fabric", "Validate execution"],
  Notebook: ["Export the notebook", "Replace Synapse-specific APIs where present", "Attach a Fabric Environment and Lakehouse", "Import into the Fabric workspace", "Validate a run"],
  Table: ["Export the table definition", "Create the table in the Fabric Warehouse", "Load the data (copy or shortcut)", "Compare row counts and data types"],
};
const GENERIC_STEPS = ["Review the discovered definition", "Recreate or reconfigure the object in Fabric", "Validate the result against the source"];

const BASE_WAVE: Record<string, number> = {
  "Linked Service": 1, "Integration Runtime": 1, "Storage Reference": 1, "Dedicated SQL Pool": 1, "Spark Pool": 1,
  "Spark Library": 1, Schema: 1, Table: 2, "External Table": 2, Dataset: 2, View: 3, "Stored Procedure": 3,
  "SQL Script": 3, Notebook: 4, "Spark Job Definition": 4, Pipeline: 4, Trigger: 5,
};

export const categoryOf = (type: string) => CATEGORY[type] ?? "Other";
export const componentOf = (type: string) => COMPONENT[type] ?? "Other";
export const baseWave = (type: string) => BASE_WAVE[type] ?? 3;
export const stepsFor = (type: string) => STEPS[type] ?? GENERIC_STEPS;

type MappingFields = Pick<ObjectRow, "fabricTarget" | "targetType" | "migrationPath" | "automationPotential" | "assessmentRequired" | "mappingStatus" | "workstream" | "classification" | "action">;

export function mockMap(type: string, synapseSpecific = false): MappingFields {
  const r = RULES[type] ?? (["Requires Assessment", "Undetermined", "Unassigned", "Requires Assessment", "Not determined", "Review the object"] as Rule);
  const path: MigrationPath = type === "Notebook" && synapseSpecific ? "Target With Refactoring" : r[3];
  return {
    fabricTarget: r[0], targetType: r[1], workstream: r[2], migrationPath: path, automationPotential: r[4],
    assessmentRequired: true, mappingStatus: STATUS[path], classification: CLASS[path], action: r[5],
  };
}

export function mockComponents(): ComponentRow[] {
  return Object.keys(RULES).map((t) => {
    const m = mockMap(t);
    return { sourceType: t, fabricTarget: m.fabricTarget, targetType: m.targetType, migrationPath: m.migrationPath, classification: m.classification, action: m.action, workstream: m.workstream, notes: ["Preliminary mapping from the object type (demo)."] };
  });
}

/** Fabric label for a dependency that is not itself an object (demo). */
export function mockReferenceTarget(kind: string, type: string | null): string | null {
  if (type && RULES[type]) return RULES[type][0];
  return { compute: "Fabric Spark / Environment (or Fabric-managed compute)", storage_path: "OneLake", secret: "Fabric Connection credential", sql_object: "Fabric Warehouse object" }[kind] ?? null;
}

const ACTIVITY: Record<string, [string, boolean, boolean, string]> = {
  Copy: ["Copy activity", true, false, "Connection references are re-pointed to Fabric connections."],
  Lookup: ["Lookup activity", true, false, "Connection references are re-pointed to Fabric connections."],
  ExecutePipeline: ["Invoke pipeline activity", true, false, "Points at another pipeline, which is migrated separately."],
  SynapseNotebook: ["Notebook activity", true, true, "References a Fabric Notebook, which must exist as a migrated target."],
  SparkJob: ["Spark Job Definition activity", true, true, "References a Fabric Spark Job Definition, which must exist as a migrated target."],
  ForEach: ["ForEach activity", false, false, "Control-flow activity; its children are mapped individually."],
};

export function mockActivity(type: string) {
  const a = ACTIVITY[type];
  return a
    ? { fabricEquivalent: a[0], equivalence: "Known equivalent" as const, requiresTransformation: a[1], requiresManualReview: a[2], note: a[3] }
    : { fabricEquivalent: "Requires Assessment", equivalence: "Requires Assessment" as const, requiresTransformation: false, requiresManualReview: true, note: "No established Fabric equivalent is recorded for this activity type." };
}
