import SwiftUI

struct ToolchainSettingsTab: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var isInstalling: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SurfaceCard(padding: 12) {
                HStack(spacing: 12) {
                    Image(systemName: "doc.badge.gearshape.fill")
                        .font(.system(size: 24))
                        .foregroundStyle(Color.accentColor)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 8) {
                            Text("Primary Guidance: `AGENTS.md` + `~/.local/bin/opsb`")
                                .font(.system(size: 13.5, weight: .bold))
                            SemanticPill(text: "ACTIVE", compact: true)
                        }
                        Text("Agents execute `opsb test`, `opsb submit`, and `opsb history` directly from `AGENTS.md` with zero skill overhead.")
                            .font(.system(size: 11.5))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
            }

            SurfaceCard(padding: 12) {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Optional Fallback Skill (`kernelc-deep-reference`)")
                                .font(.system(size: 13.5, weight: .bold))
                            Text("Deep Accel soc-a alignment & PipeBarrier reference. Installed strictly to `dsh` & `codex` (never Claude Code).")
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button {
                            isInstalling = true
                            Task {
                                do {
                                    try await store.repository.installFallbackSkills()
                                    store.bannerMessage = "Installed fallback skill to ~/.dsh/skills & ~/.codex/skills"
                                    await store.refreshLocal()
                                } catch {
                                    store.errorMessage = error.localizedDescription
                                }
                                isInstalling = false
                            }
                        } label: {
                            if isInstalling { ProgressView().controlSize(.small) }
                            else { Label("Register to dsh & Codex", systemImage: "square.and.arrow.down.on.square.fill") }
                        }
                        .buttonStyle(.borderedProminent)
                    }

                    Divider()

                    HStack(spacing: 12) {
                        targetTile("dsh", "~/.dsh/skills/kernelc-deep-reference", store.snapshot?.skills.targets["dsh"]?.allInstalled ?? false)
                        targetTile("Codex", "~/.codex/skills/kernelc-deep-reference", store.snapshot?.skills.targets["codex"]?.allInstalled ?? false)
                    }
                }
            }

            Spacer()
        }
    }

    private func targetTile(_ name: String, _ path: String, _ installed: Bool) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(name).font(.system(size: 13, weight: .bold))
                    SemanticPill(text: installed ? "OK" : "MISSING", compact: true)
                }
                Text(path).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(10)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
    }
}
