# UI service boundary

The browser calls named operations only. `scripts/run_ui.py` constructs the
services and injects them into `arbx.ui.app.ServiceRegistry`; UI routes never
construct venue or capture clients directly.

Every operation returns the envelope defined by `arbx.ui.envelope`: `ok`, `data`,
`error`, and versioned `meta`. Reads use `GET /api/{operation}`. Mutations use
`POST /api/{operation}` with JSON request bodies. List operations use bounded
limits and opaque cursors.

| Area | Service | Main operations |
|---|---|---|
| Documents | `DocStore`, `NotesStore` | List/read Markdown and save local notes. |
| Data | `DataService` | List soaks, inspect quality metadata, and page through rows. |
| Pairs | `PairRegistryService` | List reviewed/candidate pairs and record review decisions. |
| Scanner | `ScannerController` | Start, stop, and inspect a managed paper scanner. |
| Analysis | `AnalysisService` | Run background soak analysis and retrieve summaries. |
| Tests | `TestSuiteRunner` | Run the repository test suite and read its output. |
| Safety | `LiveController` | Read safety status, engage the kill switch, and store external credential references. |

`arbx.services.contracts.SEAM_OPERATIONS` is the machine-readable operation list
used by the FastAPI application and regression tests. The safety controller has no
order-placement, cancellation, or mode-changing operation.
