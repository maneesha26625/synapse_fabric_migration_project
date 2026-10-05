import { Rocket } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { PlannerPanel, PlannerRuns } from "../components/migrate/PlannerPanel";
import { Preflight, RunPanel } from "../components/migrate/RunPanel";
import { StagesPanel } from "../components/migrate/StagesPanel";
import { StrategyPanel } from "../components/migrate/StrategyPanel";
import { Banner, Button, Card, EmptyState, ErrorState, LoadingState, PageHead, StatusBadge } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";

/**
 * One page for the whole migration phase: score the plan, choose how each kind
 * of object is migrated, check the target, then run it wave by wave.
 */
export function PlanMigrate() {
  const { mode, discovery, isConnected } = useAppState();
  const { graph, graphLoading, graphError, reloadGraph, plan, analysis, analysisBusy, analysisError, analyze } = useMigration();
  const navigate = useNavigate();
  const done = discovery.state === "completed" || discovery.state === "completed_with_warnings";

  let body;
  if (!done) {
    body = (
      <Card>
        <EmptyState icon={<Rocket size={22} />} title={isConnected ? "Discovery has not been executed." : "No Synapse workspace connected."}
          actions={<Button variant="primary" onClick={() => navigate(isConnected ? "/discovery" : "/synapse")}>{isConnected ? "Run Discovery" : "Connect Synapse"}</Button>}>
          The plan is built from the discovered objects and their dependency order.
        </EmptyState>
      </Card>
    );
  } else if (graphLoading && !graph) body = <Card><LoadingState label="Loading objects…" /></Card>;
  else if (graphError) body = <Card><ErrorState title="Could not load the objects" message={graphError} actions={<Button onClick={reloadGraph}>Retry</Button>} /></Card>;
  else {
    body = (
      <>
        <PlannerPanel analysis={analysis} busy={analysisBusy} error={analysisError} hasPlan={plan.length > 0} onRerun={() => void analyze(true)} />
        <StagesPanel />
        <StrategyPanel />
        <Preflight />
        <RunPanel />
        <PlannerRuns analysis={analysis} />
      </>
    );
  }

  return (
    <div className="page wide">
      <PageHead icon={Rocket} title="Plan & Migrate" badge={mode === "mock" ? <StatusBadge tone="warning">DEMO DATA</StatusBadge> : undefined}>
        Score the plan, choose how each kind of object is migrated, check the target, then run the migration wave by wave. Every step reads the same plan.
      </PageHead>
      {done && !plan.length && <Banner tone="info" title="Start with a plan">Add every discovered object in its suggested wave under The plan, then the planner scores it.</Banner>}
      {body}
    </div>
  );
}
