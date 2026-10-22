# Resource choices — 2026-10-21

Consulted useful-dev-resources/AGENTS.md and the builds/packages and Windows build-tool categories, plus the supporting Windows and general catalogs. Existing AppTrail choices were retained; no runtime dependency was added.

| Resource | Need and choice | Alternative / constraint | Verified evidence |
|---|---|---|---|
| [CPython subprocess / Tk](https://docs.python.org/3/library/subprocess.html) | Standard-library process capture and Windows GUI; matches AppTrail | Qt adds a runtime dependency; Tk is sufficient for this first release | Tested locally on Python 3.15/Windows 11 x64; CI covers 3.11/3.15. Python and bundled Tcl/Tk notices included with binary |
| [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) | Own the entire build process tree, with kill-on-close | Killing only the root process leaves compiler children; jobs constrain persistent daemons | Microsoft documentation checked 2026-10-21; suspended launch, assignment, resume, cancellation, normal-exit descendant cleanup tested on x64 |
| [AppTrail 0.16.0](https://github.com/SeanShaffer88/AppTrail/releases/tag/v0.16.0) | Compatible checkpoints, Git evidence, history and source reports | Vendored modules require explicit synchronization; avoids requiring another EXE or a Python install | MIT provenance pinned in THIRD-PARTY-NOTICES; shared-history and independent-copy tests |
| [PyInstaller](https://pyinstaller.org/en/stable/license.html) | Existing pinned Windows one-file GUI packaging | Unsigned EXE, extraction at launch; native ARM64 not validated | Existing AppTrail build pins reused, license/bootloader exception reviewed; build and launch checked for this release |

CMake, Ninja, MSVC, dotnet, and package managers are user-installed tools invoked only through recipes/probes. BuildTrail does not bundle them or claim universal support for every version. Dependency manifests are observations, not an SBOM or a lockfile resolver. No online service, account integration, or credential is needed at runtime.


0.2.0 retains the existing standard-library stack and adds sqlite3 organization
storage alongside immutable run evidence, following AppTrail's established local
catalog approach. Run locks use Windows byte-range locking (msvcrt) or flock on
POSIX. Targeted tests cover cross-process exclusion, deletion rollback/recovery,
and Windows junction refusal. No new runtime package or resource was adopted.


0.3.0 adds a standard-library log diff model and Tk window. AppTrail's vendored
source-diff policy remains unchanged: build logs have separate preview bounds.
Bounded difflib alignment, literal str.find-based Unicode search, and conservative
compiler/log diagnostic heuristics need no runtime package or network service.
Tests cover long repetitive inputs, Unicode offsets, evidence hashes, navigation,
scroll synchronization, and window/theme callback cleanup.
