import { ArrowRight, Check, ChevronDown, ChevronRight, CircleDashed, GitBranch, Route } from "lucide-react";
import { useState } from "react";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";

/** The usual order a solution is promoted in. */
const ENVIRONMENTS = ["Dev", "Test", "Prod"];

type StageState = "done" | "current" | "todo";

interface Stage {
  key: string;
  label: string;
  detail: string;
  state: StageState;
}

/** What comes after this migration, ticked off per project in this browser. */
const CHECKLIST: { id: string; title: string; detail: string }[] = [
  { id: "git", title: "Connect the Fabric workspace to Git",
    detail: "Workspace settings → Git integration, on the main branch. From then on the workspace's items are versioned in Fabric's own repository (not the Synapse one)." },
  { id: "workspaces", title: "Create the Test and Prod workspaces",
    detail: "One Fabric workspace per environment, each on a capacity. Nothing reaches Prod without passing Test." },
  { id: "connections", title: "Create each environment's connections",
    detail: "Same names as in Dev, pointing at that environment's servers. Connections are never stored in Git. Prefer workspace identity or a Key Vault reference over typed secrets." },
  { id: "pipeline", title: "Set up the deployment (CD)",
    detail: "A Fabric deployment pipeline, or Azure DevOps / GitHub Actions, that deploys Dev → Test → Prod and swaps in each environment's connections and settings." },
  { id: "branches", title: "Work in feature branches",
    detail: "Each change starts on a feature branch made from main, goes through a pull request with checks and a review (CI), and is merged into main." },
  { id: "schedules", title: "Switch schedules on only where they should run",
    detail: "The migration creates schedules switched off. Turn them on in the environment that runs them, usually Prod." },
];

const GLOSSARY: [string, string][] = [
  ["Git repository", "Where definitions (pipelines, notebooks, …) are stored with their full history. It holds no data."],
  ["Branch", "A separate copy to work in: main is the shared one, a feature branch is one change, a release branch is what goes to Prod."],
  ["Pull request (PR)", "A request to review a branch and merge it into main; automated checks run on it (CI)."],
  ["Environment", "A separate workspace with its own servers and data: Dev to build, Test to check, Prod for the business."],
  ["Deployment (CD)", "Copying the approved version into the next environment, with that environment's settings swapped in."],
];

function storageKey(project: string): string {
  return `ma.releasePath.${project}`;
}

function loadDone(project: string): Set<string> {
  try {
    const raw = localStorage.getItem(storageKey(project));
    const list = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(list) ? list.filter((x) => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function saveDone(project: string, done: Set<string>): void {
  try { localStorage.setItem(storageKey(project), JSON.stringify([...done])); } catch { /* storage unavailable: kept for this visit */ }
}

/** The environment this migration's definitions are for, as one of Dev/Test/Prod when it is one. */
function environmentOf(name: string | undefined): { name: string; index: number } {
  const n = (name ?? "").trim();
  const index = ENVIRONMENTS.findIndex((e) => e.toLowerCase() === n.toLowerCase());
  return { name: n || "Dev", index: n ? index : 0 };
}

/**
 * Where this migration sits on the way from Dev to Prod. The six steps move one environment's
 * definitions into one Fabric workspace, once; promoting them to the other environments is the
 * team's usual Git and deployment work, shown here with what is still to do.
 */
export function ReleasePath({ stepsDone, stepsTotal }: { stepsDone: number; stepsTotal: number }) {
  const { connection } = useAppState();
  const { fabric, project } = useMigration();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<Set<string>>(() => loadDone(project.id));
  const [shownFor, setShownFor] = useState(project.id);
  if (shownFor !== project.id) { setShownFor(project.id); setDone(loadDone(project.id)); }

  const repo = connection.repository ?? null;
  const env = environmentOf(repo?.environment);
  const migrated = stepsDone >= stepsTotal;
  const later = env.index >= 0 ? ENVIRONMENTS.slice(env.index + 1) : ["Test", "Prod"];
  const fabricName = fabric.workspaceName ?? "Fabric workspace";

  const stages: Stage[] = [
    {
      key: "source", state: "done",
      label: repo ? `Synapse ${env.name}` : "Synapse workspace",
      detail: repo
        ? `${repo.kind === "git" ? `Git · ${repo.ref ?? "default branch"}` : "ZIP export"}${repo.parametersName ? ` · ${repo.parametersName}` : ""}`
        : `Live workspace ${connection.workspace ?? ""}`.trim(),
    },
    { key: "app", state: migrated ? "done" : "current", label: "This migration", detail: `${stepsDone} of ${stepsTotal} steps done` },
    { key: "target", state: migrated ? "done" : "todo", label: `Fabric ${env.name}`, detail: fabricName },
    { key: "git", state: done.has("git") ? "done" : "todo", label: "Fabric Git", detail: "Workspace connected to Git" },
    ...later.map((e): Stage => ({
      // Whether anything was deployed there is not something this app can see, so it stays "to do".
      key: `env-${e}`, state: "todo",
      label: `Fabric ${e}`, detail: e === "Prod" ? "Deployed from a release branch" : "Deployed after review",
    })),
  ];

  const toggle = (id: string) => {
    setDone((d) => {
      const next = new Set(d);
      if (next.has(id)) next.delete(id); else next.add(id);
      saveDone(project.id, next);
      return next;
    });
  };
  const ticked = CHECKLIST.filter((c) => done.has(c.id)).length;

  return (
    <section className="release-path" aria-label="Release path">
      <div className="release-head">
        <span className="release-title"><Route size={15} aria-hidden="true" /> Release path</span>
        <ol className="release-stages" aria-label="From the source to production">
          {stages.map((s, i) => (
            <li key={s.key} className={`release-stage ${s.state}`}>
              {i > 0 && <ArrowRight size={13} className="release-arrow" aria-hidden="true" />}
              <span className="release-chip" title={s.detail}>
                <span className="release-icon" aria-hidden="true">
                  {s.state === "done" ? <Check size={12} /> : s.state === "current" ? <span className="release-dot" /> : <CircleDashed size={12} />}
                </span>
                <span className="release-label">{s.label}</span>
                <span className="release-detail">{s.detail}</span>
                <span className="sr-only">{s.state === "done" ? " (done)" : s.state === "current" ? " (in progress)" : " (to do)"}</span>
              </span>
            </li>
          ))}
        </ol>
        <button type="button" className="release-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          {open ? "Hide the flow" : `What comes next · ${ticked}/${CHECKLIST.length}`}
        </button>
      </div>

      {open && (
        <div className="release-body">
          <p className="muted" style={{ margin: 0 }}>
            The six steps move <strong>one environment's</strong> definitions into <strong>one</strong> Fabric workspace, once.
            {env.index === 2
              ? " These definitions are for Prod: this migration is the production copy, so there is nothing to promote after it."
              : ` Getting them to ${later.join(" and ")} is not a second migration: it is your team's usual Git and deployment work, below.`}
          </p>

          <div className="release-lanes">
            <div className="release-lane">
              <span className="eyebrow">One-time migration · this app</span>
              <div className="release-flow">
                <span>Synapse {repo ? `${env.name} (${repo.kind === "git" ? `branch ${repo.ref ?? "default"}` : "ZIP"})` : "workspace"}</span>
                <ArrowRight size={13} aria-hidden="true" />
                <span>Discover → Assess → Waves → Plan → Migrate → Validate</span>
                <ArrowRight size={13} aria-hidden="true" />
                <span>Fabric {env.name}: {fabricName}</span>
              </div>
            </div>
            <div className="release-lane">
              <span className="eyebrow"><GitBranch size={12} aria-hidden="true" /> Ongoing development · CI/CD</span>
              <div className="release-flow">
                <span>feature branch (made from main)</span>
                <ArrowRight size={13} aria-hidden="true" />
                <span>pull request: checks + review</span>
                <ArrowRight size={13} aria-hidden="true" />
                <span>main</span>
                <ArrowRight size={13} aria-hidden="true" />
                <span>deploy to Test</span>
                <ArrowRight size={13} aria-hidden="true" />
                <span>release branch</span>
                <ArrowRight size={13} aria-hidden="true" />
                <span>deploy to Prod</span>
              </div>
              <span className="faint">Each environment has its own workspace and its own connections; Git stores the definitions once and never the connections.</span>
            </div>
          </div>

          <div>
            <span className="eyebrow">What comes next</span>
            <ul className="release-checklist">
              {CHECKLIST.map((c) => (
                <li key={c.id}>
                  <label className="check-row" style={{ marginTop: 8 }}>
                    <input type="checkbox" checked={done.has(c.id)} onChange={() => toggle(c.id)} />
                    <span><strong>{c.title}</strong><span className="muted"> — {c.detail}</span></span>
                  </label>
                </li>
              ))}
            </ul>
          </div>

          <details className="release-glossary">
            <summary>What do these words mean?</summary>
            <dl>
              {GLOSSARY.map(([term, meaning]) => (<div key={term}><dt>{term}</dt><dd>{meaning}</dd></div>))}
            </dl>
          </details>
        </div>
      )}
    </section>
  );
}
