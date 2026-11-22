import AppKit
import SwiftUI

private func activateExistingInstanceAndExitIfDuplicate() -> Bool {
    let myPid = ProcessInfo.processInfo.processIdentifier
    let myBundleId = Bundle.main.bundleIdentifier
    let others = NSWorkspace.shared.runningApplications.filter { app in
        guard app.processIdentifier != myPid, !app.isTerminated else { return false }
        if let bid = myBundleId, !bid.isEmpty, app.bundleIdentifier == bid {
            return true
        }
        return app.executableURL?.lastPathComponent == "OpsSupervisor"
    }
    if let existing = others.first {
        existing.unhide()
        existing.activate(options: [.activateAllWindows])
        return true
    }
    return false
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillFinishLaunching(_ notification: Notification) {
        if activateExistingInstanceAndExitIfDuplicate() {
            NSApp.terminate(nil)
        }
    }

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
    @StateObject private var hotStore: HotTelemetryStore
    @StateObject private var store: ContestStore

    init() {
        if activateExistingInstanceAndExitIfDuplicate() {
            exit(0)
        }
        setenv("TZ", "Asia/Shanghai", 1)
        tzset()
        NSTimeZone.default = Formatters.shanghaiTimeZone

        let hs = HotTelemetryStore()
        _hotStore = StateObject(wrappedValue: hs)
        _store = StateObject(wrappedValue: ContestStore(hotStore: hs))
    }

    var body: some Scene {
        WindowGroup("Parhelion · Operator & NPU Supervisor") {
            AppRouterView()
                .environmentObject(store)
                .environmentObject(hotStore)
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
                .environmentObject(hotStore)
                .frame(width: 680, height: 520)
        }
    }
}
