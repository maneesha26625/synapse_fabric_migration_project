import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useAppState } from "../../state/AppState";
import type { DependencyRef, ObjectDetail } from "../../types";
import { Button, Skeleton } from "../shared/Shared";

const KIND_LABEL: Record<string, string> = {
  artifact: "artifact",
  compute: "compute",
  secret: "secret",
  storage_path: "storage",
  sql_object: "sql object",
  external_endpoint: "endpoint",
  unknown: "unknown",
};

type View = "source" | "target";

interface NodeProps {
  dep: DependencyRef;
  /** Object ids already on the path from the root, to stop circular references. */
  ancestors: ReadonlySet<string>;
  view: View;
  onOpen: (id: string) => void;
}

/** One dependency. Children load only when the node is expanded. */
function DependencyNode({ dep, ancestors, view, onOpen }: NodeProps) {
  const { getObject } = useAppState();
  const [open, setOpen] = useState(false);
  const [child, setChild] = useState<ObjectDetail | "loading" | "error" | null>(null);
  const id = dep.objectId;
  const circular = id !== null && ancestors.has(id);
  const expandable = id !== null && !circular;

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && child === null && id) {
      setChild("loading");
      try {
        setChild(await getObject(id));
      } catch {
        setChild("error");
      }
    }
  };

  const typeLabel = dep.type ?? KIND_LABEL[dep.kind] ?? dep.kind;
  return (
    <li>
      <div className="tree-node">
        {expandable ? (
          <Button variant="ghost" size="small" icon className="toggle" onClick={toggle} aria-expanded={open} aria-label={`${open ? "Collapse" : "Expand"} ${dep.name}`}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </Button>
        ) : (
          <span className="leaf-pad" />
        )}
        {view === "target" ? (
          <>
            <span className="name target-name" title={dep.fabricTarget ?? "No Fabric target recorded"}>{dep.fabricTarget ?? "No Fabric target recorded"}</span>
            <span className="faint">← {dep.name}</span>
          </>
        ) : (
          <>
            {id ? (
              <button type="button" className="link name" onClick={() => onOpen(id)} title={`Open ${dep.name}`}>{dep.name}</button>
            ) : (
              <span className="name" title={dep.name}>{dep.name}</span>
            )}
            <span className="type">{typeLabel}</span>
            {dep.fabricTarget && <span className="faint">→ {dep.fabricTarget}</span>}
          </>
        )}
        {circular && <span className="faint">circular reference</span>}
        {!id && view === "source" && <span className="faint">not an object in this discovery run</span>}
      </div>
      {open && expandable && (
        <ul>
          {child === "loading" && <li><Skeleton width={180} /></li>}
          {child === "error" && <li className="faint">Could not load dependencies.</li>}
          {child && typeof child === "object" && (
            child.dependencies.length === 0 ? (
              <li className="faint" style={{ padding: "3px 0" }}>No dependencies discovered.</li>
            ) : (
              child.dependencies.map((d) => (
                <DependencyNode key={`${d.kind}|${d.name}`} dep={d} view={view} ancestors={new Set([...ancestors, id as string])} onOpen={onOpen} />
              ))
            )
          )}
        </ul>
      )}
    </li>
  );
}

/**
 * The dependencies discovery observed, as a tree, in two readings: the Synapse
 * objects as they are, and the Fabric components they conceptually land on.
 * Both are pictures of what references what. Nothing is created and nothing is
 * judged.
 */
export function DependencyView({ root, onOpen }: { root: ObjectDetail; onOpen: (id: string) => void }) {
  const [view, setView] = useState<View>("source");
  return (
    <div className="stack">
      <div className="mode-switch" role="group" aria-label="Dependency view" style={{ alignSelf: "flex-start" }}>
        <button type="button" aria-pressed={view === "source"} onClick={() => setView("source")}>Synapse source</button>
        <button type="button" aria-pressed={view === "target"} onClick={() => setView("target")}>Fabric target (conceptual)</button>
      </div>
      {view === "target" && <p className="faint">A mapping picture only. No Fabric object is created by Discovery.</p>}

      {root.dependencies.length === 0 ? (
        <p className="muted">No dependencies were discovered for this object.</p>
      ) : (
        <div>
          <div className="tree-node">
            <strong className="name">{view === "target" ? root.fabricTarget : root.name}</strong>
            <span className="type">{view === "target" ? root.targetType : root.type}</span>
          </div>
          <ul className="tree" aria-label={`Dependencies of ${root.name}`}>
            <li>
              <ul>
                {root.dependencies.map((d) => (
                  <DependencyNode key={`${d.kind}|${d.name}`} dep={d} view={view} ancestors={new Set([root.id])} onOpen={onOpen} />
                ))}
              </ul>
            </li>
          </ul>
        </div>
      )}

      {root.referencedBy.length > 0 && (
        <div>
          <h3 style={{ marginBottom: 6 }}>Referenced by</h3>
          <ul className="tree" aria-label="Objects that reference this one">
            {root.referencedBy.slice(0, 100).map((r) => (
              <li key={r.objectId}>
                <div className="tree-node">
                  <span className="leaf-pad" />
                  <button type="button" className="link name" onClick={() => onOpen(r.objectId)}>{r.name}</button>
                  <span className="type">{r.type}</span>
                </div>
              </li>
            ))}
          </ul>
          {root.referencedBy.length > 100 && <p className="faint">Showing 100 of {root.referencedBy.length}.</p>}
        </div>
      )}
    </div>
  );
}
