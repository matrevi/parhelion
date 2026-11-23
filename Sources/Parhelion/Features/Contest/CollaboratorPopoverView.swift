import SwiftUI

struct CollaboratorPopoverView: View {
    @EnvironmentObject var store: ContestStore
    let problemCount: Int

    @UIState private var username: String = ""
    @UIState private var permission: String = "push"
    @UIState private var dryRun: Bool = false
    @UIState private var isSubmitting: Bool = false
    @UIState private var response: CollabInviteResponse?

    private var lang: AppLanguage { store.lang }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: "person.2.badge.gearshape.fill")
                    .foregroundStyle(Color.accentColor)
                Text(lang.tr("批量邀请 GitCode 协作者", "Batch Invite GitCode Collaborator"))
                    .font(.system(size: 13, weight: .bold))
                Spacer()
                Text(lang.tr("\(problemCount) 个仓库", "\(problemCount) repos"))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 8) {
                TextField(lang.tr("输入 GitCode 用户名", "GitCode username"), text: $username)
                    .textFieldStyle(.roundedBorder)
                Picker("", selection: $permission) {
                    Text("push").tag("push")
                    Text("admin").tag("admin")
                    Text("pull").tag("pull")
                }
                .frame(width: 85)
            }

            HStack {
                Toggle(lang.tr("仅预览 (Dry-run)", "Dry-run preview"), isOn: $dryRun)
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
                        Text(dryRun
                             ? lang.tr("预览全部仓库", "Preview All Repos")
                             : lang.tr("一键邀请至全部仓库", "Invite to All Repos"))
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
