import SwiftUI

struct CollaboratorPopoverView: View {
    @EnvironmentObject var store: ContestStore
    let problemCount: Int

    @UIState private var username: String = ""
    @UIState private var permission: String = "push"
    @UIState private var dryRun: Bool = false
    @UIState private var isSubmitting: Bool = false
    @UIState private var response: CollabInviteResponse?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: "person.2.badge.gearshape.fill")
                    .foregroundStyle(Color.accentColor)
                Text("Batch Invite GitCode Collaborator")
                    .font(.system(size: 13, weight: .bold))
                Spacer()
                Text("\(problemCount) repos")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 8) {
                TextField("GitCode username", text: $username)
                    .textFieldStyle(.roundedBorder)
                Picker("", selection: $permission) {
                    Text("push").tag("push")
                    Text("admin").tag("admin")
                    Text("pull").tag("pull")
                }
                .frame(width: 85)
            }

            HStack {
                Toggle("Dry-run preview", isOn: $dryRun)
                    .toggleStyle(.checkbox)
                    .font(.system(size: 11))
                Spacer()
                Button {
                    let clean = username.trimmingCharacters(in: .whitespaces)
                    guard !clean.isEmpty else { return }
                    isSubmitting = true
                    Task {
                        do {
                            response = try await store.repository.inviteCollaborator(
                                workspace: store.selectedWorkspace,
                                username: clean,
                                permission: permission,
                                dryRun: dryRun
                            )
                        } catch {
                            store.errorMessage = error.localizedDescription
                        }
                        isSubmitting = false
                    }
                } label: {
                    if isSubmitting {
                        ProgressView().controlSize(.small)
                    } else {
                        Text(dryRun ? "Preview All Repos" : "Invite to All Repos")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(username.trimmingCharacters(in: .whitespaces).isEmpty || isSubmitting)
            }

            if let res = response {
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(res.results) { item in
                            HStack(spacing: 8) {
                                SemanticPill(text: item.status.uppercased(), compact: true)
                                Text(item.repo)
                                    .font(.system(size: 11, design: .monospaced))
                                    .lineLimit(1)
                                Spacer()
                            }
                        }
                    }
                }
                .frame(maxHeight: 180)
            }
        }
        .padding(14)
        .frame(width: 390)
    }
}
