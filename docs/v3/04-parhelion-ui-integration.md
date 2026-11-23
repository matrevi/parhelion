# Track 4 Spec · Parhelion macOS SwiftUI Integration (Wave 2)

> **Branch**: `v3/macos-ui` | **Worktree**: `.worktrees/v3-macos-ui` (branched from `main` AFTER Wave 1 merge)
> **Owned Files**: `Sources/Parhelion/**`

---

## 1. Goals
Surface all v3 platform capabilities cleanly in native macOS SwiftUI (`/Applications/Parhelion.app`) while preserving the `< 1%` steady-state CPU hot/cold split architecture:
1. **Triad Knowledge Hub (`KnowledgeHubView`)**: Upgrade the Breakthroughs tab into a 3-pillar segmented hub (`Breakthroughs` | `Snippets` | `Community` | `Evolution Log`) with confidence badges, `portable` / `remap-to-accel` / `anti-pattern` semantic pills, one-click copy for `.h` snippets, and quick Win/Fail feedback recording.
2. **Driver & Harness Control Bar**:
   - In **Settings** & **Header / Overview**: show active Driver (`Antigravity` / `Codex` / `Claude Code` / `Pi` / `Gemini` / `DeepSeek`) with live `doctor` status indicator (version + auth check) and one-click `Sync Workspace`.
   - Per-operator row action button **"新开调优对话 / Launch Harness"** calling `opsb driver launch <Title> --mode gui|headless --json` (showing session confirmation banner & active sessions drawer).
3. **ELF & Cold-3 Redline Badges**:
   - Display compact `ELF` / `Cold3` status pills on the operator inspector (`ProblemCalibrationViews.swift`) calling `opsb bench elf` / `opsb bench cold3` / `opsb bench pmu` on demand (never in the 1s hot loop!).

---

## 2. Swift Architecture Changes
- **`Domain/`**:
  - `TriadKBModels.swift` (new): `TriadKBEntry`, `TriadKBQueryResponse`, `KBEvolutionEvent`
  - `DriverModels.swift` (new): `DriverDiagnosticModel`, `DriverSessionModel`, `FastFeedbackSummary` (`ElfGuardResult`, `Cold3Result`, `PmuTorchCard`)
- **`Core/OpsRepository.swift`**:
  - Add async methods calling `opsb kb list/query/feedback`, `opsb driver doctor/select/sync/launch/sessions`, `opsb bench elf/cold3/pmu` (all off-MainActor with timeouts).
- **`Features/Breakthroughs/BreakthroughLibraryView.swift`** (upgraded to Triad Knowledge Hub):
  - Segmented picker (`突破档案` / `代码积木` / `社区方案` / `进化日志`)
  - Portability filter pills (`All`, `native-accel`, `portable`, `remap-to-accel`, `anti-pattern`)
  - Detail pane with monospace C++ code viewer + Copy button for `snippets`, and Evidence history table.
- **`Features/Contest/` & `Features/Settings/`**:
  - Add Driver picker + `Doctor` refresh + `Sync Workspace` in Settings.
  - Add per-operator **"Launch Harness"** button + compact Active Sessions popover in Contest Overview.
  - Add **PMU / ELF / Cold-3** diagnostic section inside `ProblemCalibrationViews.swift`.

---

## 3. Verification
- `swift build -c release` exits `0` with zero errors.
- No `/Users/winbeau` hardcodes anywhere in `Sources/Parhelion/`.
- Preserves hot/cold store separation (`HotTelemetryStore` untouched; v3 queries are strictly on-demand / cold-path).
