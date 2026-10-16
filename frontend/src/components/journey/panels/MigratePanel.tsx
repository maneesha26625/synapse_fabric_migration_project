import { useSearchParams } from "react-router-dom";
import { Preflight, RunPanel } from "../../migrate/RunPanel";
import { RunHistory } from "../../migrate/RunHistory";
import { PlanBoard } from "../../orchestrator/PlanBoard";
import { Tabs } from "../../shared/Shared";
import { useMigration } from "../../../state/MigrationState";
import { useOrchestrator } from "../../../state/OrchestratorState";

/** The run: what must be true first, then the controls and every object's result; and the execution plan around it. */
export function MigratePanel() {
  const { execution } = useMigration();
  const { waiting } = useOrchestrator();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "plan" ? "plan" : "run";
  const choose = (next: "run" | "plan") => {
    const p = new URLSearchParams(params);
    if (next === "plan") p.set("view", "plan");
    else p.delete("view");
    setParams(p, { replace: true });
  };
  return (
    <div className="stack panel-body">
      <Tabs label="Migrate" value={view} onChange={choose} tabs={[
        { id: "run", label: "Run" },
        { id: "plan", label: "Execution plan", count: waiting || undefined },
      ]} />
      {view === "run" ? <>
        {/* Before the first run the checks matter most; afterwards the results do. */}
        {execution.state === "idle" && <Preflight />}
        <RunPanel />
        <RunHistory />
      </> : <PlanBoard />}
    </div>
  );
}
