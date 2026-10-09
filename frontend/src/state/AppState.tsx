import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { apiFor, initialMode, rememberMode } from "../services";
import {
  ApiRequestError,
  type ApiMode,
  type ConnectionConfig,
  type ConnectionError,
  type ConnectionState,
  type DiscoveryStatus,
  type Health,
  type MigrationApi,
  type ObjectDetail,
  type RepositoryConfig,
  type ResultsPage,
  type ResultsQuery,
  type ZipUpload,
} from "../types";

const DISCONNECTED: ConnectionState = { status: "disconnected", ok: true, checks: [] };
const IDLE: DiscoveryStatus = {
  state: "idle",
  startedAt: null,
  finishedAt: null,
  error: null,
  workspace: null,
  progress: null,
  summary: null,
};

type Busy = "authenticate" | "test" | "disconnect" | null;

interface AppStateValue {
  /** The active data layer (real or demo). Pages call it through hooks, never directly. */
  api: MigrationApi;
  mode: ApiMode;
  setMode: (mode: ApiMode) => void;
  ready: boolean;
  health: Health | null;
  /** Re-read what the backend supports, e.g. after it was restarted. */
  refreshHealth: () => Promise<void>;
  /** Set when the real backend cannot be reached. */
  backendError: string | null;
  connection: ConnectionState;
  connectionBusy: Busy;
  connectionError: ConnectionError | null;
  clearConnectionError: () => void;
  authenticate: (config: ConnectionConfig) => Promise<void>;
  testConnection: (config: ConnectionConfig) => Promise<void>;
  /** Connect one environment's repository (Git or an uploaded ZIP) as the source. */
  connectRepository: (config: RepositoryConfig) => Promise<void>;
  /** Upload a ZIP export; the caller shows what was found, or the error. */
  uploadZip: (file: File) => Promise<ZipUpload>;
  disconnect: () => Promise<void>;
  isConnected: boolean;
  discovery: DiscoveryStatus;
  discoveryStartError: string | null;
  startDiscovery: () => Promise<void>;
  /** Re-read the discovery status and drop cached pages, so the table reloads. */
  refreshDiscovery: () => Promise<void>;
  /** Forget the discovery's results; the connection is kept. Throws when refused (discovery running). */
  resetDiscovery: () => Promise<void>;
  /** Load everything again from the backend, e.g. after it was started. */
  reload: () => void;
  getResults: (query: ResultsQuery) => Promise<ResultsPage>;
  getObject: (id: string) => Promise<ObjectDetail>;
  /** Dropdown contents for the Azure source form. */
  azureLists: Pick<import("../types").MigrationApi, "listResourceGroups" | "listWorkspaces" | "listSqlPools">;
}

const AppState = createContext<AppStateValue | null>(null);

export function useAppState(): AppStateValue {
  const value = useContext(AppState);
  if (!value) throw new Error("useAppState must be used inside <AppStateProvider>");
  return value;
}

/** Turn any thrown value into something a person can read. Never a stack. */
function describe(error: unknown): ConnectionError {
  if (error instanceof ApiRequestError) {
    const titles: Record<string, string> = {
      backend_unavailable: "Backend unavailable",
      invalid_configuration: "Invalid configuration",
      method_not_implemented: "Not available in this backend build",
      network: "Network failure",
      not_connected: "Not connected",
    };
    return {
      code: error.code,
      title: titles[error.code] ?? "Request failed",
      hint: "",
      message: error.message,
    };
  }
  return { code: "unknown", title: "Something went wrong", hint: "", message: "An unexpected error occurred." };
}

export function AppStateProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ApiMode>(initialMode);
  const api = useMemo(() => apiFor(mode), [mode]);

  const [ready, setReady] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>(DISCONNECTED);
  const [connectionBusy, setBusy] = useState<Busy>(null);
  const [connectionError, setConnectionError] = useState<ConnectionError | null>(null);
  const [discovery, setDiscovery] = useState<DiscoveryStatus>(IDLE);
  const [discoveryStartError, setDiscoveryStartError] = useState<string | null>(null);

  // Cache of results and object details for the current discovery run. A new
  // run (or a mode change) invalidates it; a repeated page or drawer open does
  // not hit the backend again.
  const cache = useRef(new Map<string, Promise<unknown>>());
  const [epoch, setEpoch] = useState(0);
  const [loadEpoch, setLoadEpoch] = useState(0);
  const runToken = `${mode}|${discovery.startedAt ?? ""}|${discovery.finishedAt ?? ""}|${epoch}`;
  useEffect(() => {
    cache.current.clear();
  }, [runToken]);

  // Initial load, and again whenever the data layer is switched.
  useEffect(() => {
    let cancelled = false;
    setReady(false);
    setHealth(null);
    setBackendError(null);
    setConnection(DISCONNECTED);
    setConnectionError(null);
    setDiscovery(IDLE);
    (async () => {
      try {
        const [h, c, d] = await Promise.all([api.health(), api.getConnection(), api.getDiscoveryStatus()]);
        if (cancelled) return;
        setHealth(h);
        setConnection(c);
        setDiscovery(d);
      } catch (e) {
        if (!cancelled) setBackendError(describe(e).message);
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, loadEpoch]);

  // Poll only while a run is in flight.
  useEffect(() => {
    if (discovery.state !== "running") return;
    const id = setInterval(async () => {
      try {
        setDiscovery(await api.getDiscoveryStatus());
      } catch (e) {
        setBackendError(describe(e).message);
      }
    }, 1200);
    return () => clearInterval(id);
  }, [api, discovery.state]);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
      setBackendError(null);
    } catch (e) {
      setBackendError(describe(e).message);
    }
  }, [api]);

  const setMode = useCallback((next: ApiMode) => {
    rememberMode(next);
    setModeState(next);
  }, []);

  const run = useCallback(
    async (kind: Exclude<Busy, null>, action: () => Promise<ConnectionState>) => {
      setBusy(kind);
      setConnectionError(null);
      try {
        const next = await action();
        setBackendError(null);
        setConnection(next);
        if (next.error) setConnectionError(next.error);
        if (next.status !== "connected") setDiscovery(IDLE);
      } catch (e) {
        setConnectionError(describe(e));
        if (e instanceof ApiRequestError && e.code === "backend_unavailable") setBackendError(e.message);
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const authenticate = useCallback((c: ConnectionConfig) => run("authenticate", () => api.authenticate(c)), [api, run]);
  const testConnection = useCallback((c: ConnectionConfig) => run("test", () => api.testConnection(c)), [api, run]);
  const connectRepository = useCallback((c: RepositoryConfig) => run("test", () => api.connectRepository(c)), [api, run]);
  const uploadZip = useCallback((file: File) => api.uploadZip(file), [api]);
  const disconnect = useCallback(async () => {
    await run("disconnect", () => api.disconnect());
    setDiscovery(IDLE);
  }, [api, run]);

  const startDiscovery = useCallback(async () => {
    setDiscoveryStartError(null);
    try {
      setDiscovery(await api.startDiscovery());
    } catch (e) {
      setDiscoveryStartError(describe(e).message);
    }
  }, [api]);

  const refreshDiscovery = useCallback(async () => {
    try {
      setDiscovery(await api.getDiscoveryStatus());
      setBackendError(null);
    } catch (e) {
      setBackendError(describe(e).message);
    }
    setEpoch((n) => n + 1);
  }, [api]);

  const resetDiscovery = useCallback(async () => {
    setDiscoveryStartError(null);
    setDiscovery(await api.resetDiscovery());
    setEpoch((n) => n + 1);
  }, [api]);

  const memo = useCallback(<T,>(key: string, load: () => Promise<T>): Promise<T> => {
    const hit = cache.current.get(key) as Promise<T> | undefined;
    if (hit) return hit;
    const pending = load();
    cache.current.set(key, pending);
    pending.catch(() => cache.current.delete(key)); // never cache a failure
    return pending;
  }, []);

  const getResults = useCallback(
    (q: ResultsQuery) => memo(`r|${JSON.stringify(q)}`, () => api.getResults(q)),
    // The epoch is part of the identity so a refresh makes the table reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [api, memo, epoch],
  );
  const getObject = useCallback((id: string) => memo(`o|${id}`, () => api.getObject(id)), [api, memo]);

  const value: AppStateValue = {
    api,
    mode,
    setMode,
    ready,
    health,
    refreshHealth,
    backendError,
    connection,
    connectionBusy,
    connectionError,
    clearConnectionError: () => setConnectionError(null),
    authenticate,
    testConnection,
    connectRepository,
    uploadZip,
    disconnect,
    isConnected: connection.status === "connected",
    discovery,
    discoveryStartError,
    startDiscovery,
    refreshDiscovery,
    resetDiscovery,
    reload: () => setLoadEpoch((n) => n + 1),
    getResults,
    getObject,
    azureLists: api,
  };
  return <AppState.Provider value={value}>{children}</AppState.Provider>;
}

