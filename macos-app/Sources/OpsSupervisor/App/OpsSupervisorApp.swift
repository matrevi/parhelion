import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        setenv("TZ", "Asia/Shanghai", 1)
        tzset()
        NSTimeZone.default = Formatters.shanghaiTimeZone
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@main
struct OpsSupervisorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var store = ContestStore()

    init() {
        setenv("TZ", "Asia/Shanghai", 1)
        tzset()
        NSTimeZone.default = Formatters.shanghaiTimeZone
    }

    var body: some Scene {
        WindowGroup("Parhelion · Operator & NPU Supervisor") {
            AppRouterView()
                .environmentObject(store)
                .environment(\.timeZone, Formatters.shanghaiTimeZone)
                .frame(minWidth: 1120, minHeight: 720)
        }
        .defaultSize(width: 1340, height: 840)
        .commands {
            CommandGroup(after: .newItem) {
                Button(store.tr("立即刷新全局遥测", "Refresh All Telemetry")) {
                    Task { await store.refreshAll() }
                }
                .keyboardShortcut("r", modifiers: [.command])

                Divider()

                Picker(store.tr("界面语言 (Language)", "Language"), selection: Binding(
                    get: { store.lang },
                    set: { store.setLanguage($0) }
                )) {
                    Text("中文").tag(AppLanguage.zh)
                    Text("English").tag(AppLanguage.en)
                }
            }
        }

        Settings {
            SettingsRootView()
                .environmentObject(store)
                .frame(width: 680, height: 520)
        }
    }
}
