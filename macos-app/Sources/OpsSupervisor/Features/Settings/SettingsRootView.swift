import SwiftUI

struct SettingsRootView: View {
    @EnvironmentObject var store: ContestStore

    private var lang: AppLanguage { store.lang }

    var body: some View {
        TabView {
            SSHSettingsTab()
                .tabItem { Label(lang.tr("NPU SSH 连接", "NPU SSH"), systemImage: "network") }

            AccountsSettingsTab()
                .tabItem { Label(lang.tr("平台账号认证", "Accounts"), systemImage: "person.badge.key.fill") }

            ToolchainSettingsTab()
                .tabItem { Label(lang.tr("Agent 工具链", "Agent Toolchain"), systemImage: "cpu") }
        }
        .padding(14)
    }
}
