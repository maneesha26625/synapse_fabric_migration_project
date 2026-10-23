import type { Capabilities, ConnectionState, ConsultFacts, ConsultFeatureFacts, DependencyGraph, DiscoveryStatus, FabricGitState, FabricTarget, StageDef } from "../types";

/**
 * What discovery and the connections say about this migration, in the terms the consultation's
 * recommendations are written in. Everything here is read, never guessed: a count of zero means
 * discovery found none, and `null` means it is not known yet.
 */
export interface Facts {
  discovered: boolean;
  workspace: string | null;
  total: number;
  /** Discovered objects by type ("Table", "Pipeline", ...). */
  byType: Record<string, number>;
  /** By preliminary classification (DIRECT, TRANSFORM, MANUAL, ...). */
  byClassification: Record<string, number>;
  /** Objects with no automatic route: MANUAL or NOT SUPPORTED. */
  noAutomaticRoute: number;
  waves: number;
  tables: number;
  /** The SQL pool is connected, so table rows can be read. */
  sqlPool: string | null;
  /** The definitions come from a Git repository or ZIP rather than the live workspace. */
  repositorySource: boolean;
  environment: string | null;
  linkedServices: number;
  /** The migration stages the backend offers, and those with something discovered to move. */
  stages: StageDef[];
  stagesWithObjects: StageDef[];
  fabricConnected: boolean;
  fabricGitConnected: boolean;
  /** Read from the configuration rather than asked: the Fabric workspace, its capacity, who is signed in. */
  fabricWorkspace: string | null;
  /** True or false after the Fabric connection was tested; null when not known. */
  capacityAssigned: boolean | null;
  account: string | null;
  signInMethod: string | null;
  /** Features discovery recorded in code and table definitions; null until read (or when the API cannot say). */
  features: Partial<ConsultFeatureFacts> | null;
  /** What discovery cannot see at all. */
  notRecorded: string[];
}

/** A feature's count, 0 when discovery found none or could not say. */
export function feature(f: Facts, name: Exclude<keyof ConsultFeatureFacts, "distribution" | "unsupportedActivities" | "triggers">): number {
  return f.features?.[name] ?? 0;
}

const count = (by: Record<string, number>, ...types: string[]) => types.reduce((s, t) => s + (by[t] ?? 0), 0);

export function readFacts(input: {
  discovery: DiscoveryStatus;
  connection: ConnectionState;
  graph: DependencyGraph | null;
  capabilities: Capabilities | null;
  fabricConnected: boolean;
  fabricGit: FabricGitState | null;
  fabric?: FabricTarget;
  consultFacts?: ConsultFacts | null;
}): Facts {
  const { discovery, connection, graph, capabilities } = input;
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  // The graph is the most exact count; the discovery summary stands in until it is read.
  const byType: Record<string, number> = {};
  const byClassification: Record<string, number> = {};
  if (graph?.nodes.length) {
    for (const n of graph.nodes) {
      byType[n.type] = (byType[n.type] ?? 0) + 1;
      byClassification[n.classification] = (byClassification[n.classification] ?? 0) + 1;
    }
  } else if (discovery.summary) {
    Object.assign(byType, discovery.summary.byType);
    Object.assign(byClassification, discovery.summary.byClassification);
  }
  const total = Object.values(byType).reduce((a, b) => a + b, 0);
  const stages = capabilities?.stages ?? [];
  return {
    discovered,
    workspace: connection.workspace ?? discovery.workspace ?? null,
    total,
    byType,
    byClassification,
    noAutomaticRoute: count(byClassification, "MANUAL", "NOT SUPPORTED"),
    waves: graph?.waves.length ?? 0,
    tables: count(byType, "Table"),
    sqlPool: connection.sqlPool ?? null,
    repositorySource: !!connection.repository,
    environment: connection.repository?.environment ?? null,
    linkedServices: capabilities?.linkedServices.length ?? count(byType, "Linked Service"),
    stages,
    stagesWithObjects: stages.filter((s) => s.types.some((t) => (byType[t] ?? 0) > 0)),
    fabricConnected: input.fabricConnected,
    fabricGitConnected: input.fabricGit?.state === "connected",
    fabricWorkspace: input.fabricConnected ? input.fabric?.workspaceName ?? null : null,
    capacityAssigned: input.fabricConnected ? input.fabric?.capacityAssigned ?? null : null,
    account: connection.account ?? null,
    signInMethod: connection.signInMethod ?? connection.method ?? null,
    features: input.consultFacts?.facts ?? null,
    notRecorded: input.consultFacts?.notRecorded ?? [],
  };
}

/** "3 pipelines", "1 table": a count and its noun, for recommendation reasons. */
export function plural(n: number, noun: string, many = `${noun}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? noun : many}`;
}
