/**
 * The platforms a migration can start from and land on. Only the pair this
 * build supports end to end is available; the rest are listed so the route
 * picker shows where the product is heading, and cannot be chosen.
 */
export interface Platform {
  id: string;
  name: string;
  /** Two letters for the platform tile. Monograms, not vendor logos. */
  mark: string;
  /** What the accelerator reads or writes there, in a few words. */
  caption: string;
  available: boolean;
  /** Tile colour. */
  tint: string;
}

export const SOURCES: Platform[] = [
  { id: "synapse", name: "Azure Synapse Analytics", mark: "AS", caption: "SQL pools, Spark, pipelines, notebooks", available: true, tint: "#4f8cff" },
  { id: "adf", name: "Azure Data Factory", mark: "DF", caption: "Pipelines, data flows, linked services", available: false, tint: "#3a7bd5" },
  { id: "sqlserver", name: "SQL Server", mark: "SQ", caption: "Databases, SSIS packages, Agent jobs", available: false, tint: "#c0504d" },
  { id: "databricks", name: "Azure Databricks", mark: "DB", caption: "Notebooks, jobs, Delta tables", available: false, tint: "#e8603c" },
  { id: "snowflake", name: "Snowflake", mark: "SF", caption: "Warehouses, tasks, stages", available: false, tint: "#29b5e8" },
  { id: "teradata", name: "Teradata", mark: "TD", caption: "Databases, BTEQ scripts, macros", available: false, tint: "#f37440" },
  { id: "oracle", name: "Oracle Database", mark: "OR", caption: "Schemas, PL/SQL, scheduler jobs", available: false, tint: "#d63b2f" },
  { id: "redshift", name: "Amazon Redshift", mark: "RS", caption: "Clusters, stored procedures, Spectrum", available: false, tint: "#8c4fff" },
  { id: "bigquery", name: "Google BigQuery", mark: "BQ", caption: "Datasets, scheduled queries", available: false, tint: "#4285f4" },
];

export const DESTINATIONS: Platform[] = [
  { id: "fabric", name: "Microsoft Fabric", mark: "MF", caption: "Warehouse, Lakehouse, Data Factory, Spark", available: true, tint: "#14b8a6" },
  { id: "databricks", name: "Azure Databricks", mark: "DB", caption: "Unity Catalog, workflows, Delta", available: false, tint: "#e8603c" },
  { id: "snowflake", name: "Snowflake", mark: "SF", caption: "Warehouses, tasks, Snowpark", available: false, tint: "#29b5e8" },
  { id: "azuresql", name: "Azure SQL Database", mark: "SQ", caption: "Databases, elastic jobs", available: false, tint: "#0078d4" },
  { id: "bigquery", name: "Google BigQuery", mark: "BQ", caption: "Datasets, Dataform", available: false, tint: "#4285f4" },
];

export const platformById = (list: Platform[], id: string | null | undefined) => list.find((p) => p.id === id) ?? null;
