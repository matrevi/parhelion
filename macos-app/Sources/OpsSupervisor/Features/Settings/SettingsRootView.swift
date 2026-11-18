import SwiftUI

struct SettingsRootView: View {
    var body: some View {
        TabView {
            SSHSettingsTab()
                .tabItem { Label("NPU SSH", systemImage: "network") }

            AccountsSettingsTab()
                .tabItem { Label("Accounts", systemImage: "person.badge.key.fill") }

            ToolchainSettingsTab()
                .tabItem { Label("Agent Toolchain", systemImage: "cpu") }
        }
        .padding(14)
    }
}
