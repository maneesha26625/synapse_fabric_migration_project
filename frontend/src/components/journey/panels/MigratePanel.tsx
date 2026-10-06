import { Preflight, RunPanel } from "../../migrate/RunPanel";
import { useMigration } from "../../../state/MigrationState";

/** The run: what must be true first, then the controls and every object's result. */
export function MigratePanel() {
  const { execution } = useMigration();
  return (
    <div className="stack panel-body">
      {/* Before the first run the checks matter most; afterwards the results do. */}
      {execution.state === "idle" && <Preflight />}
      <RunPanel />
    </div>
  );
}
