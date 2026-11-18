import SwiftUI

struct WorkspaceManagerView: View {
    @EnvironmentObject var store: ContestStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Label("Registered Contest Workspaces", systemImage: "folder.fill")
                            .font(.system(size: 15, weight: .bold))
                        Spacer()
                        Text("\(store.workspaces.count) registered")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }

                    ForEach(store.workspaces) { entry in
                        workspaceRow(entry)
                    }
                }

                Divider()

                WorkspaceWizardView()
            }
            .padding(16)
        }
    }

    private func workspaceRow(_ entry: WorkspaceEntry) -> some View {
        let isCurrent = entry.workspace == store.selectedWorkspace
        let isClean = entry.layout == "clean-agents-first"

        return SurfaceCard(highlighted: isCurrent, highlightColor: .accentColor, padding: 12) {
            HStack(spacing: 12) {
                Image(systemName: isCurrent ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 16))
                    .foregroundStyle(isCurrent ? .green : .secondary)

                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(entry.title).font(.system(size: 13.5, weight: .bold))
                        SemanticPill(text: entry.layout, compact: true)
                        if isCurrent {
                            SemanticPill(text: "ACTIVE", compact: true)
                        }
                    }
                    Text(entry.workspace)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }

                Spacer()

                if isClean {
                    Button("Regenerate AGENTS.md") {
                        Task {
                            do {
                                try await store.repository.regenerateAgentsMd(workspace: entry.workspace)
                                store.bannerMessage = "Regenerated AGENTS.md in \(entry.workspace)"
                            } catch {
                                store.errorMessage = error.localizedDescription
                            }
                        }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }

                Button {
                    SystemNavigator.revealInFinder(entry.workspace)
                } label: {
                    Label("Finder", systemImage: "folder")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                if !isCurrent {
                    Button("Activate") {
                        Task { await store.switchWorkspace(entry.workspace) }
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                }
            }
        }
    }
}
