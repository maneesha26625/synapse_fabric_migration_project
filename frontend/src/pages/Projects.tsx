import { FolderKanban, Plus } from "lucide-react";
import { Link } from "react-router-dom";
import { MetricCard } from "../components/shared/Metrics";
import { Banner, Card, PageHead, StatusBadge } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";

export function Projects() {
  const { isConnected, connection, discovery, backendError, mode } = useAppState();
  const { projects, project, selectProject, plan, fabric } = useMigration();
  const done = discovery.state === "completed" || discovery.state === "completed_with_warnings";

  return (
    <div className="page">
      <PageHead icon={FolderKanban} title="Projects" badge={<StatusBadge tone={mode === "mock" ? "warning" : "success"}>{mode === "mock" ? "DEMO DATA" : "LIVE"}</StatusBadge>}>
        A project names one Synapse → Fabric migration. Work through the stages in order: connect the source, discover, assess, plan, then migrate and validate.
      </PageHead>

      {backendError && <Banner tone="error" title="Backend unavailable">{backendError}</Banner>}

      <div className="grid cols-4">
        <MetricCard label="Source" value={isConnected ? "Connected" : "Not connected"} hint={isConnected ? connection.workspace : "Connect a Synapse workspace"} />
        <MetricCard label="Objects discovered" value={done && discovery.summary ? discovery.summary.total.toLocaleString() : "—"} hint={done ? undefined : "Run discovery"} />
        <MetricCard label="Objects in plan" value={plan.length ? plan.length.toLocaleString() : "—"} hint="Migration Plan" />
        <MetricCard label="Fabric target" value={fabric.status === "connected" ? "Connected" : "Not connected"} hint={fabric.workspaceName} />
      </div>

      <Card title="Projects" subtitle="Select the project you are working on. Use New Project in the header to add one.">
        <div className="stack">
          {projects.map((p) => (
            <button key={p.id} type="button" className="project-row" aria-pressed={p.id === project.id} onClick={() => selectProject(p.id)}>
              <span className="page-ico"><FolderKanban size={18} aria-hidden="true" /></span>
              <span style={{ flex: 1, textAlign: "left" }}>
                <strong>{p.name}</strong>
                <span className="faint" style={{ display: "block", fontSize: 13 }}>{p.id === "default" ? "Default project" : `Created ${new Date(p.createdAt).toLocaleDateString()}`}</span>
              </span>
              {p.id === project.id && <StatusBadge tone="info">Current</StatusBadge>}
            </button>
          ))}
        </div>
        <p className="faint" style={{ marginTop: 12 }}>Projects are labels kept in this browser. The backend holds one connection and one discovery at a time, so switching project does not switch the connected workspace.</p>
      </Card>

      <Card title="Next step">
        {!isConnected
          ? <p>Start by connecting your Synapse workspace. <Link to="/synapse"><Plus size={13} aria-hidden="true" /> Open Synapse Source</Link></p>
          : !done
            ? <p>Workspace connected. Discovery has not been executed. <Link to="/discovery">Open Discovery</Link></p>
            : <p>Discovery is complete. Review the <Link to="/assessment">Assessment</Link> and the <Link to="/dependencies">Dependencies &amp; Waves</Link>, then build the <Link to="/plan">Migration Plan</Link>.</p>}
      </Card>
    </div>
  );
}
