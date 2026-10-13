import { HardDrive } from "lucide-react";
import { useEffect } from "react";
import { useMigration } from "../../state/MigrationState";
import type { StorageRoute, StorageSuggestion } from "../../types";
import { Banner } from "../shared/Shared";

const key = (r: { account: string; container: string }) => `${r.account}/${r.container}`;
const usable = (s: StorageSuggestion) => !!(s.connectionId || s.linkedService);
const route = (s: StorageSuggestion): StorageRoute => ({
  account: s.account, container: s.container,
  ...(s.connectionId ? { connectionId: s.connectionId } : s.linkedService ? { linkedService: s.linkedService } : {}),
});

/** Storage the plan's code reads directly, sent through OneLake shortcuts so Fabric reaches it with a connection. */
export function StorageRoutesPanel() {
  const { analysis, options, setOptions } = useMigration();
  const suggestions = analysis?.storage ?? [];

  // First time a plan meets storage: route everything a connection can reach. After that the operator decides.
  useEffect(() => {
    if (options.storageRoutes === undefined && suggestions.length) {
      setOptions({ storageRoutes: suggestions.filter(usable).map(route) });
    }
  }, [suggestions, options.storageRoutes, setOptions]);

  if (!suggestions.length) return null;
  const chosen = new Set((options.storageRoutes ?? []).map(key));
  const toggle = (s: StorageSuggestion) => {
    const current = options.storageRoutes ?? [];
    setOptions({ storageRoutes: chosen.has(key(s)) ? current.filter((r) => key(r) !== key(s)) : [...current, route(s)] });
  };
  const unrouted = suggestions.filter((s) => !chosen.has(key(s)));

  return (
    <section className="stack" style={{ gap: 10 }} aria-label="External storage">
      <h3 className="eyebrow" style={{ margin: 0 }}><HardDrive size={13} aria-hidden="true" /> External storage → OneLake</h3>
      <p className="muted" style={{ margin: 0 }}>
        Notebooks, Spark jobs and SQL in this plan read these storage locations directly. In Synapse that ran as the
        workspace's managed identity; in Fabric it runs as the signed-in user, which a storage account in another Microsoft
        Entra tenant always rejects (401). Routed locations get a OneLake shortcut in the run's Lakehouse, reached with the
        connection shown, and every path to them is rewritten to go through it, including paths built from variables. Spark
        jobs run a rewritten copy of their main file; the original is left as it is.
      </p>
      <ul className="checklist" style={{ gap: 8 }}>
        {suggestions.map((s) => (
          <li key={key(s)}>
            <label className="row" style={{ gap: 8, alignItems: "flex-start" }}>
              <input type="checkbox" checked={chosen.has(key(s))} disabled={!usable(s) && !chosen.has(key(s))} onChange={() => toggle(s)} />
              <span>
                <strong>Route {s.container} on {s.account}</strong>
                <span className="muted"> — {s.uses ? `named ${s.uses} time${s.uses === 1 ? "" : "s"} in code` : "built from variables in code"}; </span>
                {s.connectionId ? <span className="muted">through the Fabric connection <code>{s.connectionName}</code>.</span>
                  : s.linkedService ? <span className="muted">through the connection the Connections stage makes from <code>{s.linkedService}</code> (keep that stage on, with its credentials).</span>
                    : <span className="tone-warning">no Fabric connection reaches it yet: create one to it (account key or SAS works across tenants), then score the plan again.</span>}
              </span>
            </label>
          </li>
        ))}
      </ul>
      {unrouted.length > 0 && (
        <Banner tone="warning" title="Not routed">
          Code that reads {unrouted.map((s) => `${s.container} on ${s.account}`).join(", ")} keeps doing so directly, and fails in Fabric
          unless the signed-in user can reach that storage account itself.
        </Banner>
      )}
    </section>
  );
}
