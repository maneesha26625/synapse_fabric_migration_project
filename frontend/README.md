# Migration Accelerator UI

Dark enterprise UI for the Synapse → Fabric accelerator. **Scope: Connections and
Discovery only.** Assessment, Execution and Validation are visible in the
navigation as *Coming soon* and have no logic behind them. There is no Fabric
(target) connection anywhere in this UI; it belongs to a later phase.

Stack: React 18 + TypeScript + Vite, `react-router-dom`, `lucide-react`. Plain
CSS with design tokens (`src/styles/global.css`). No state library and no UI kit.

## Run it

Backend (from the project root, with `az login` done):

```
pip install -e ".[live]"
python -m discovery_agent.api            # http://127.0.0.1:8001
```

Frontend, development:

```
cd frontend
npm install
npm run dev                              # http://localhost:5173, proxies /api -> :8001
```

Frontend, single process (the API serves the built UI):

```
cd frontend && npm run build
python -m discovery_agent.api            # open http://127.0.0.1:8001
```

Checks: `npm run typecheck`, `npm test`, and `pytest tests/test_api.py` for the API.

## Data layer: Live API vs Demo data

The header switch chooses the implementation of one interface, `MigrationApi`
(`src/types.ts`):

| Mode | Implementation | Notes |
|---|---|---|
| **Live API** | `services/realApi.ts` → `/api/*` | Real Synapse data. Default. |
| **Demo data** | `mock/mockApi.ts` | Generated sample data, ~1,500 objects. A banner says so on every page. |

Demo scenarios are chosen by workspace name: containing `denied`, `notfound`,
`expired`, `offline` fails the connection test; `empty`, `partial`, `fail`,
`timeout` change what discovery returns. Anything else succeeds.

## Environment

| Variable | Where | Purpose |
|---|---|---|
| `VITE_API_MODE` | frontend | `real` (default) or `mock`; the header switch overrides it per browser |
| `VITE_BACKEND_URL` | frontend dev | where Vite proxies `/api` (default `http://127.0.0.1:8001`) |

No secret is read from, or written to, any environment variable or storage.

## Security notes

- The Synapse source offers **Azure CLI** and **Interactive browser**. Its form
  has no secret field and no optional fields: Interactive browser asks only for
  Tenant ID and Subscription ID, and the backend refuses any sign-in field it
  does not expect.
- The UI reads `/api/health` (`authMethods`, `authMethodDetails`) and disables a
  method the running backend does not support.
- Tokens never reach the browser: the backend holds them, and its responses are
  redacted.
- Only the API mode preference is kept in `localStorage`.

## Layout

```
src/
  services/     realApi, mode selection            (the only code that calls fetch)
  mock/         mockApi, mockData                  (demo only)
  state/        AppState: connection, discovery, request cache
  components/   layout/, shared/, connections/, discovery/
  pages/        Dashboard, Connections, Discovery, ComingSoon
```
