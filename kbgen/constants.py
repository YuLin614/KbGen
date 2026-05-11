from __future__ import annotations

TOOL_VERSION = "kbgen@0.1"
DEFAULT_KEY_PATH_LIMIT = 0
SOFT_TOKEN_TARGET = 12000
SOFT_TOKEN_MAX = 22000
ROUTE_INDEX_LIMIT = 200
DB_SCHEMA_LIMIT = 320
BLUEPRINT_PREFIX_CACHE: dict[str, dict[str, str]] = {}

SCHEMA_TEXT = """Keys:
m = modules
r = role / responsibility
s = one-line module semantic
e = exports
a = export anchors (symbol@path:line)
d = depends_on
u = used_by
i = invariant / expectation
p = key file paths
f = directional flow (mostly acyclic)
fd = file dependency digest src>dst(count)
cy = mutual module dependency cycles (a<->b)
ri = route index summary
db = db schema index (table/column/relation summary)
ac = auth chain summary
no = negative knowledge (discourage paths)
h = decision hints
hf = decision hint file targets
hr = machine-readable task read plan for hf targets
ls = loop sentinels (bias only)

hr format:
- each item: {"s":"S1|S2|S3...","t":"anchor_or_path","r":"reason_code"}

hr reason codes:
- EP_ENTRY = endpoint/route entrypoint
- EP_WRITE = endpoint write-flow core logic
- EP_VERIFY = endpoint test/verification target
- EP_NAV = endpoint-adjacent navigation fallback
- BUG_REPRO = bug reproduction/assertion target
- BUG_PATH = bug failure-path/guard logic
- BUG_HOT = bug hotspot fallback
- REF_SHARED = shared abstraction target
- REF_CORE = core logic target
- REF_STRUCT = structural cleanup target
- UI_ENTRY = UI component/page entrypoint
- UI_STATE = UI state/store/hook target
- UI_FLOW = UI interaction flow target
- AUTH_PATH = auth/session/token target
- NAV = generic navigation fallback

task keys may include ui_bugfix, ui_feature, ui_refactor when UI-focused modules are detected.

All entries are heuristic, not authoritative.
f may be empty when dependency direction cannot be inferred safely.
Use snapshot to guide WHERE to explore, not to replace reading code.
"""

IGNORED_DIR_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".ai",
    ".next",
    "coverage",
    "lcov-report",
    "out",
    "tmp",
}

SUPPORTED_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".java",
    ".rb",
    ".rs",
    ".cs",
    ".php",
}

ENTRYPOINT_NAMES = {
    "main.py",
    "app.py",
    "index.js",
    "index.ts",
    "server.js",
    "server.ts",
    "manage.py",
}

# Quality scoring
QUALITY_WEIGHTS = {"coverage": 0.6, "freshness": 0.4}
QUALITY_GRADES = [
    (85, "A"),
    (70, "B"),
    (50, "C"),
    (0,  "D"),
]
