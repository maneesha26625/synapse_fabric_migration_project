// Demo LLM-assisted transformation: drafts are canned and nothing is sent anywhere.
import type { ObjectDetail, TransformKind, TransformObject, TransformSettings } from "../types";

export const KIND_OF_TYPE: Record<string, TransformKind> = {
  "Stored Procedure": "procedure", View: "view", Table: "table", "SQL Script": "script", Pipeline: "pipeline", Notebook: "notebook",
};

export const demoTransformSettings: TransformSettings = {
  enabled: true, available: true, model: "Demo (no model is called)", timeoutSeconds: 180, message: null, disabledReason: null,
  kinds: ["notebook", "pipeline", "procedure", "script", "table", "view"],
  promptsDir: "prompts/transform", storeDir: "transforms",
};

const short = (name: string) => name.split(".").pop() ?? name;
const schemaOf = (o: ObjectDetail) => o.schema || "dbo";

function sources(o: ObjectDetail, kind: TransformKind) {
  const n = short(o.name);
  const q = `${schemaOf(o)}.${n}`;
  switch (kind) {
    case "procedure":
      return {
        code: `CREATE PROCEDURE ${q} @RunDate date AS\nBEGIN\n    SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;\n    SELECT * INTO #nodes FROM sys.pdw_nodes_tables;\n    MERGE ${schemaOf(o)}.Target AS t USING ${schemaOf(o)}.Staging AS s ON t.Id = s.Id\n    WHEN MATCHED THEN UPDATE SET t.Amount = s.Amount\n    WHEN NOT MATCHED THEN INSERT (Id, Amount) VALUES (s.Id, s.Amount);\nEND`,
        rule: `CREATE PROCEDURE ${q} @RunDate date AS\nBEGIN\n    /* removed for Fabric: SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; */\n    SELECT * INTO #nodes FROM sys.pdw_nodes_tables;\n    MERGE ${schemaOf(o)}.Target AS t USING ${schemaOf(o)}.Staging AS s ON t.Id = s.Id\n    WHEN MATCHED THEN UPDATE SET t.Amount = s.Amount\n    WHEN NOT MATCHED THEN INSERT (Id, Amount) VALUES (s.Id, s.Amount);\nEND`,
        draft: `CREATE PROCEDURE ${q} @RunDate date AS\nBEGIN\n    -- Fabric: isolation level removed; a Warehouse always uses snapshot isolation.\n    -- Fabric: sys.pdw_nodes_tables has no equivalent; the unused #nodes copy is dropped.\n    BEGIN TRANSACTION;\n    -- Fabric: MERGE rewritten as UPDATE + INSERT with the same match on Id.\n    UPDATE t SET t.Amount = s.Amount\n    FROM ${schemaOf(o)}.Target AS t JOIN ${schemaOf(o)}.Staging AS s ON t.Id = s.Id;\n    INSERT INTO ${schemaOf(o)}.Target (Id, Amount)\n    SELECT s.Id, s.Amount FROM ${schemaOf(o)}.Staging AS s\n    WHERE NOT EXISTS (SELECT 1 FROM ${schemaOf(o)}.Target AS t WHERE t.Id = s.Id);\n    COMMIT TRANSACTION;\nEND`,
        notes: ["Removed SET TRANSACTION ISOLATION LEVEL: Fabric uses snapshot isolation.", "Dropped the copy of sys.pdw_nodes_tables into #nodes: nothing reads #nodes afterwards.", "Rewrote MERGE as UPDATE + INSERT in one transaction, with the same match on Id."],
        unconverted: [] as string[], problems: ["Still uses Synapse system views (sys.pdw_*), which a Fabric Warehouse rejects."],
        language: "sql" as const,
      };
    case "view":
      return {
        code: `CREATE VIEW ${q} AS\nSELECT o.Id, o.Amount, n.pdw_node_id\nFROM ${schemaOf(o)}.Orders AS o WITH (NOLOCK)\nJOIN sys.pdw_nodes_tables AS n ON n.object_id = OBJECT_ID('${schemaOf(o)}.Orders')`,
        rule: "",
        draft: `CREATE VIEW ${q} AS\nSELECT o.Id, o.Amount, CAST(NULL AS int) AS pdw_node_id -- Fabric: no distributions in a Warehouse; column kept for callers\nFROM ${schemaOf(o)}.Orders AS o -- Fabric: NOLOCK removed`,
        notes: ["Removed the NOLOCK hint.", "pdw_node_id has no meaning in Fabric; it is kept as NULL so callers still get the column."],
        unconverted: ["Check whether any report uses pdw_node_id; if not, drop the column."], problems: ["Still uses Synapse system views (sys.pdw_*), which a Fabric Warehouse rejects."],
        language: "sql" as const,
      };
    case "table":
      return {
        code: `-- Rebuilt from the pool catalog: a dedicated pool keeps no CREATE TABLE text.\nCREATE TABLE [${schemaOf(o)}].[${n}] (\n    [Id] int NOT NULL,\n    [Payload] sql_variant NULL,\n    [LoadedAt] datetime NOT NULL\n)\nWITH (DISTRIBUTION = HASH([Id]));`,
        rule: "",
        draft: `CREATE TABLE [${schemaOf(o)}].[${n}] (\n    [Id] int NOT NULL,\n    [Payload] varchar(8000) NULL, -- Fabric: sql_variant is not supported; stored as text\n    [LoadedAt] datetime2(3) NOT NULL\n);`,
        notes: ["sql_variant has no Fabric type; Payload is stored as varchar(8000).", "datetime stored as datetime2(3), as the rules do."],
        unconverted: ["Check the values in Payload: anything that is not text must be converted in the load."], problems: ["Column Payload has type sql_variant, which Fabric Warehouse cannot store."],
        language: "sql" as const,
      };
    case "script":
      return {
        code: `USE [SalesPool];\nRENAME OBJECT ${schemaOf(o)}.Stage TO Stage_old;\nSELECT TOP 10 * FROM sys.dm_pdw_exec_requests;`,
        rule: `USE [SalesPool];\nEXEC sp_rename '${schemaOf(o)}.Stage', 'Stage_old';\nSELECT TOP 10 * FROM sys.dm_pdw_exec_requests;`,
        draft: `-- Fabric: USE removed; the notebook cell is bound to the Warehouse.\nEXEC sp_rename '${schemaOf(o)}.Stage', 'Stage_old';\n-- Fabric: sys.dm_pdw_exec_requests -> sys.dm_exec_requests\nSELECT TOP 10 * FROM sys.dm_exec_requests;`,
        notes: ["Removed USE.", "Replaced sys.dm_pdw_exec_requests with sys.dm_exec_requests (fewer columns)."],
        unconverted: [] as string[], problems: ["Still uses Synapse system views (sys.pdw_*), which a Fabric Warehouse rejects."],
        language: "sql" as const,
      };
    case "notebook": {
      const cells = { language: "python", cells: [
        { cell_type: "markdown", source: `# ${n}` },
        { cell_type: "code", source: `from notebookutils import mssparkutils
key = TokenLibrary.getSecret('kv-sales', 'storage-key', 'ls_keyvault')
df = spark.read.synapsesql('SalesPool.dbo.Orders')
mssparkutils.fs.ls('/raw')` },
      ] };
      const rule = { language: "python", cells: [
        cells.cells[0],
        { cell_type: "code", source: `# notebookutils is built into Fabric notebooks
key = TokenLibrary.getSecret('kv-sales', 'storage-key', 'ls_keyvault')
df = spark.read.synapsesql('SalesPool.dbo.Orders')
notebookutils.fs.ls('/raw')` },
      ] };
      const draft = { language: "python", cells: [
        cells.cells[0],
        { cell_type: "code", source: `# Fabric: TokenLibrary -> notebookutils.credentials (no linked services in Fabric)
key = notebookutils.credentials.getSecret('https://kv-sales.vault.azure.net/', 'storage-key')
# Fabric: reads the migrated Warehouse
df = spark.read.synapsesql('SalesPool.dbo.Orders')
notebookutils.fs.ls('/raw')` },
      ] };
      return {
        code: JSON.stringify(cells, null, 2), rule: JSON.stringify(rule, null, 2), draft: JSON.stringify(draft, null, 2),
        notes: ["TokenLibrary.getSecret via the linked service is replaced by notebookutils.credentials.getSecret on the Key Vault URL."],
        unconverted: ["Confirm the Key Vault URL https://kv-sales.vault.azure.net/ and that the Fabric workspace identity can read it."],
        problems: ["Review before running, uses: TokenLibrary (use notebookutils.credentials in Fabric)."], language: "json" as const,
      };
    }
    default: {
      const resource = { name: n, properties: { activities: [
        { name: "Wait", type: "Wait", typeProperties: { waitTimeInSeconds: 5 } },
        { name: "Transform", type: "ExecuteDataFlow", dependsOn: [{ activity: "Wait", dependencyConditions: ["Succeeded"] }], typeProperties: { dataflow: { referenceName: `df_${n}`, type: "DataFlowReference" } } },
      ] } };
      const draft = { name: n, properties: { activities: [
        resource.properties.activities[0],
        { name: "Transform", type: "SynapseNotebook", description: "Fabric: replaces mapping data flow df_" + n, dependsOn: [{ activity: "Wait", dependencyConditions: ["Succeeded"] }], typeProperties: { notebook: { referenceName: `df_${n}`, type: "NotebookReference" } } },
      ] } };
      return {
        code: JSON.stringify(resource, null, 2), rule: "", draft: JSON.stringify(draft, null, 2),
        notes: [`The mapping data flow is replaced by a notebook activity calling df_${n}; activity name and dependsOn are unchanged.`],
        unconverted: [`Write notebook df_${n}: read the flow's sources, apply its transformations, write its sink.`],
        problems: ["No Fabric equivalent the converter knows: Transform (ExecuteDataFlow)"], language: "json" as const,
      };
    }
  }
}

const hash = (text: string) => {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (Math.imul(31, h) + text.charCodeAt(i)) | 0;
  return (h >>> 0).toString(16);
};

export function demoSource(o: ObjectDetail): TransformObject["source"] & { draft: string; notes: string[]; unconverted: string[] } {
  const kind = KIND_OF_TYPE[o.type];
  const src = sources(o, kind);
  return { code: src.code, language: src.language, hash: hash(src.code), ruleOutput: src.rule, ruleNotes: src.rule ? ["Rule-based changes applied (demo)."] : [], problems: src.problems, draft: src.draft, notes: src.notes, unconverted: src.unconverted };
}

export function demoPrompt(o: ObjectDetail): string {
  const src = demoSource(o);
  return [
    "# Synapse → Fabric transformation", "",
    `Convert the one ${o.type} below so it works on its Fabric target, **${o.fabricTarget}**, without changing what it does.`, "",
    "## Source", "", "```" + src.language, src.code, "```", "",
    ...(src.problems.length ? ["## Problems the rules could not solve", "", ...src.problems.map((p) => `- ${p}`), ""] : []),
    "## Project requirements", "", "(from prompts/transform/project.md)", "",
    "Demo mode: this is a shortened preview. The real prompt is built on the server from prompts/transform/*.md.",
  ].join("\n");
}

/** Demo: about a third of the transformable objects have something the rules cannot convert. */
export function demoNeedsWork(o: { id: string; type: string }): boolean {
  return o.type === "Notebook" || o.type === "Pipeline" ? parseInt(hash(o.id), 16) % 3 === 0 : parseInt(hash(o.id), 16) % 3 !== 2;
}

export const DEMO_PROJECT = `<!--
  PROJECT REQUIREMENTS (demo). Everything below this comment goes into every prompt.
-->

- Target Warehouse: \`{{target.warehouse}}\`. Keep every schema name as it is in Synapse.
- Every changed line in SQL gets a \`-- Fabric:\` comment that says why.
- Keep T-SQL keywords in upper case and 4-space indentation, as in the source.
`;
